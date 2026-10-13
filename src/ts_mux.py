"""Muxeo del audio que el origen publica en una rendicion aparte.

El problema
-----------
Algunos canales (medido: 66 de 197 en el catalogo de Pluto) publican el master
asi::

    #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio/.../audio.m3u8"
    #EXT-X-STREAM-INF:...AUDIO="audio"
    .../3321280/playlist.m3u8

La variante de video NO trae audio: vive en una playlist aparte y el master la
referencia con ``AUDIO="audio"``. El rele baja solo la variante de video, asi
que sirve imagen sin sonido. ffprobe si ve el audio (resuelve el master
completo), y por eso ``/status`` decia ``audio: aac`` en canales que se oian
mudos.

Que hace este modulo
--------------------
Junta los dos flujos en UN solo MPEG-TS. Los segmentos de audio y de video del
mismo canal estan alineados (medido: mismo numero de segmentos, duraciones con
13 ms de diferencia), asi que se empareja segmento i con segmento i.

Lo delicado no es copiar bytes: es que un TS **no lleva los canales porque si**.
El reproductor los descubre leyendo el PMT, una tabla que lista los PIDs. El TS
de video anuncia solo el PID de video; si se le pegan paquetes de audio sin mas,
el decodificador no sabe que existen y los ignora. Hay que:

1. leer el PAT para hallar el PID del PMT,
2. leer el PMT y su lista de ES,
3. agregarle la entrada del audio,
4. rehacer ``section_length`` y el ``CRC_32``,
5. y recien entonces intercalar.

Norma (leida del texto, no de memoria)
--------------------------------------
- **Tabla 2-33** de H.222.0: sintaxis de ``TS_program_map_section()``. El
  ``table_id`` vale ``0x02`` y ``section_syntax_indicator`` es ``'1'``.
- **§2.4.4.9**: ``section_length`` son 12 bits, los dos primeros ``'00'``, y los
  10 restantes cuentan los bytes que siguen al campo **incluido el CRC**; no
  pasa de 1021 (``0x3FD``).
- **Tabla 2-34**: ``stream_type`` = ``0x0F`` es "ISO/IEC 13818-7 Audio with ADTS
  transport syntax", que es el AAC de estos canales.
- **Anexo A** ("CRC decoder model", parte integrante): polinomio
  ``x^32 + x^26 + x^23 + x^22 + x^16 + x^12 + x^11 + x^10 + x^8 + x^7 + x^5 +
  x^4 + x^2 + x + 1``, con los 32 biestables arrancando en ``'1'`` y los bytes
  entrando con el bit mas significativo primero. El CRC del campo se elige para
  que procesar la seccion **entera, CRC incluido**, de salida cero.
  Nota: el texto de §2.4.4.9 remite al "Anexo B", pero en la edicion 06/2012 el
  modelo del CRC es el **Anexo A** (el B es DSM-CC). Se implementa el Anexo A.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

TAM_PKT = 188
SYNC = 0x47
PID_PAT = 0x0000
TABLE_ID_PMT = 0x02
STREAM_TYPE_AAC_ADTS = 0x0F
MAX_SECTION_LENGTH = 1021  # §2.4.4.9: no pasa de 1021 (0x3FD)


# ---------------------------------------------------------------------------
# CRC_32 — Anexo A de H.222.0
# ---------------------------------------------------------------------------
def crc32_mpeg(datos: bytes) -> int:
    """CRC_32 del Anexo A, no el de zlib.

    - Discussion: es un CRC-32 con el polinomio ``0x04C11DB7``, SIN reflejar,
    con los registros arrancando en 1 (``0xFFFFFFFF``) y sin xor final. El
    ``zlib.crc32`` de Python es el REFLEJADO y da otro numero: confundirlos
    produce un PMT que ffprobe marca como corrupto.
    """
    crc = 0xFFFFFFFF
    for byte in datos:
        crc ^= byte << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


@dataclass
class Pmts:
    """Lo que se necesita saber de un PMT para agregarle un ES."""

    pid: int
    programa: int
    version: int
    seccion: bytes  # la seccion completa, del table_id al CRC
    inicio_es: int  # offset del primer stream_type dentro de la seccion
    fin_es: int  # offset donde empieza el CRC


# ---------------------------------------------------------------------------
# Lectura de PSI
# ---------------------------------------------------------------------------
def _seccion_de_paquetes(buf: bytes | bytearray, pid: int) -> bytes | None:
    """Reensambla la seccion PSI que viaja en los paquetes de un PID.

    - Discussion: una seccion puede cruzar varios paquetes TS. El primero lleva
      ``payload_unit_start_indicator`` y un ``pointer_field`` que dice donde
      empieza la seccion dentro de la carga.
    """
    partes: list[bytes] = []
    esperando = 0
    for i in range(0, len(buf) - TAM_PKT + 1, TAM_PKT):
        if buf[i] != SYNC:
            continue
        p = ((buf[i + 1] & 0x1F) << 8) | buf[i + 2]
        if p != pid:
            continue
        afc = (buf[i + 3] >> 4) & 0x03
        off = i + 4
        if afc & 0x02:
            off += 1 + buf[off]
        if not (buf[i + 1] & 0x40):  # sin PUSI: continuacion
            if esperando:
                partes.append(bytes(buf[off : i + TAM_PKT]))
            continue
        puntero = buf[off]
        partes = [bytes(buf[off + 1 + puntero : i + TAM_PKT])]
        if len(partes[0]) >= 3:
            esperando = 3 + (((partes[0][1] & 0x0F) << 8) | partes[0][2])
        else:
            esperando = 1
        if sum(len(x) for x in partes) >= esperando:
            return b"".join(partes)[:esperando]
    if partes and esperando:
        return b"".join(partes)[:esperando]
    return None


def leer_pmts_pids(buf: bytes | bytearray) -> list[int]:
    """TODOS los PID de PMT que declara el PAT.

    - Discussion: un mismo TS puede traer mas de un programa (medido en Pluto:
      FailArmy trae dos, cada uno con su video y su timed_id3). Reescribir solo
      el primero y dejar los otros PMT en el flujo deja DOS tablas que se
      contradicen, y el decodificador lee basura: ffprobe lo reporto como un
      stream ``mp3`` que no existe, con "Header missing".
    """
    seccion = _seccion_de_paquetes(buf, PID_PAT)
    if not seccion or len(seccion) < 12 or seccion[0] != 0x00:
        return []
    fin = 3 + (((seccion[1] & 0x0F) << 8) | seccion[2]) - 4
    pids: list[int] = []
    i = 8
    while i + 4 <= fin:
        programa = (seccion[i] << 8) | seccion[i + 1]
        pid = ((seccion[i + 2] & 0x1F) << 8) | seccion[i + 3]
        if programa != 0 and pid not in pids:
            pids.append(pid)
        i += 4
    return pids


def leer_pmt_pid(buf: bytes | bytearray) -> int | None:
    """PID del PMT del primer programa, segun el PAT."""
    pids = leer_pmts_pids(buf)
    return pids[0] if pids else None


def leer_pmt(buf: bytes | bytearray, pid: int) -> Pmts | None:
    """Lee el PMT de un PID y localiza su lista de ES."""
    seccion = _seccion_de_paquetes(buf, pid)
    if not seccion or len(seccion) < 16 or seccion[0] != TABLE_ID_PMT:
        return None
    largo = ((seccion[1] & 0x0F) << 8) | seccion[2]
    programa = (seccion[3] << 8) | seccion[4]
    version = (seccion[5] >> 1) & 0x1F
    info_len = ((seccion[10] & 0x0F) << 8) | seccion[11]
    inicio_es = 12 + info_len
    fin_es = 3 + largo - 4  # el CRC ocupa los ultimos 4
    return Pmts(
        pid=pid,
        programa=programa,
        version=version,
        seccion=seccion,
        inicio_es=inicio_es,
        fin_es=fin_es,
    )


def es_de(pmt: Pmts) -> list[tuple[int, int]]:
    """Lista ``(stream_type, pid)`` de un PMT."""
    salida = []
    i = pmt.inicio_es
    while i + 5 <= pmt.fin_es:
        tipo = pmt.seccion[i]
        pid = ((pmt.seccion[i + 1] & 0x1F) << 8) | pmt.seccion[i + 2]
        es_len = ((pmt.seccion[i + 3] & 0x0F) << 8) | pmt.seccion[i + 4]
        salida.append((tipo, pid))
        i += 5 + es_len
    return salida


def agregar_es(pmt: Pmts, stream_type: int, pid: int) -> bytes:
    """Devuelve la seccion del PMT con un ES mas y el CRC recalculado.

    - Throws: ``ValueError`` si el PID ya estaba, o si el PMT no cabe en el
      tope de ``section_length`` (1021, §2.4.4.9).
    """
    if any(p == pid for _, p in es_de(pmt)):
        raise ValueError(f"el PID {pid:#x} ya esta en el PMT")
    nuevo = bytes([stream_type, 0xE0 | ((pid >> 8) & 0x1F), pid & 0xFF, 0xF0, 0x00])
    cuerpo = bytes(pmt.seccion[: pmt.fin_es]) + nuevo
    nuevo_largo = len(cuerpo) + 4 - 3  # lo que sigue al section_length, con CRC
    if nuevo_largo > MAX_SECTION_LENGTH:
        raise ValueError(f"el PMT se pasa del tope: {nuevo_largo} > {MAX_SECTION_LENGTH}")
    cabecera = bytearray(cuerpo[:3])
    cabecera[1] = (cabecera[1] & 0xF0) | ((nuevo_largo >> 8) & 0x0F)
    cabecera[2] = nuevo_largo & 0xFF
    cuerpo = bytes(cabecera) + cuerpo[3:]
    return cuerpo + struct.pack(">I", crc32_mpeg(cuerpo))


def seccion_a_paquetes(seccion: bytes, pid: int) -> bytes:
    """Empaqueta una seccion PSI en paquetes TS.

    - Discussion: el primero lleva el ``pointer_field`` en 0 y
      ``payload_unit_start_indicator``. El relleno de un paquete se hace con un
      campo de adaptacion, que es lo que permite que el CRC quede intacto.
    """
    salida = bytearray()
    resto = b"\x00" + seccion  # pointer_field = 0
    cc = 0
    primero = True
    while resto:
        capacidad = TAM_PKT - 4  # 184
        trozo, resto = resto[:capacidad], resto[capacidad:]
        cab = bytearray([SYNC, 0, 0, 0])
        cab[1] = (0x40 if primero else 0x00) | ((pid >> 8) & 0x1F)
        cab[2] = pid & 0xFF
        if len(trozo) == capacidad:
            cab[3] = 0x10 | (cc & 0x0F)  # solo carga util
            cab += trozo
        else:
            # Campo de adaptacion para rellenar: 4 de cabecera + 1 de longitud
            # + af + len(trozo) = 188
            af = capacidad - len(trozo) - 1
            cab[3] = 0x30 | (cc & 0x0F)
            cab.append(af)
            if af > 0:
                cab.append(0x00)  # flags: ninguno
                cab += b"\xff" * (af - 1)  # relleno
            cab += trozo
        salida += cab
        cc = (cc + 1) & 0x0F
        primero = False
    return bytes(salida)


def pat_a_paquetes(pmt_pid: int, programa: int, transport_stream_id: int = 0x0001) -> bytes:
    """PAT con UN programa, apuntando al PID del PMT.

    - Discussion: al muxear se descarta la PSI original, PAT incluido, y sin PAT
      el decodificador no tiene como LOCALIZAR el PMT: queda obligado a barrer
      el flujo buscando una tabla con ``table_id`` 0x02. ffprobe lo hace y por
      eso el banco daba verde, pero es una violacion de ISO/IEC 13818-1
      §2.4.4.3 (Tabla 2-30): el PAT es el punto de entrada de la PSI y es
      obligatorio, no un adorno.
    - Discussion: ``programa`` tiene que ser el MISMO numero que declara el PMT,
      o el decodificador no empata la entrada del PAT con la tabla.
    """
    cuerpo = bytearray()
    cuerpo += struct.pack(">H", transport_stream_id)
    cuerpo.append(0xC1)  # reserved '11' + version_number 0 + current_next '1'
    cuerpo.append(0x00)  # section_number
    cuerpo.append(0x00)  # last_section_number
    cuerpo += struct.pack(">H", programa)
    cuerpo.append(0xE0 | ((pmt_pid >> 8) & 0x1F))
    cuerpo.append(pmt_pid & 0xFF)
    largo = len(cuerpo) + 4  # lo que sigue al section_length, CRC incluido
    seccion = bytearray([0x00, 0xB0 | ((largo >> 8) & 0x0F), largo & 0xFF]) + cuerpo
    seccion = bytes(seccion) + struct.pack(">I", crc32_mpeg(bytes(seccion)))
    return seccion_a_paquetes(seccion, PID_PAT)


# ---------------------------------------------------------------------------
# Union de un segmento de video con su gemelo de audio
# ---------------------------------------------------------------------------
# PIDs de PSI/SI que NO deben sobrevivir al muxeo: si se cuelan, el TS lleva
# dos tablas que se contradicen y el decodificador lee la que no describe lo
# que viene.
PSI_A_DESCARTAR = frozenset({0x0000, 0x0010, 0x0011, 0x0012, 0x0014})


def _pid_de(buf: bytes, i: int) -> int:
    return ((buf[i + 1] & 0x1F) << 8) | buf[i + 2]


def pids_usados(buf: bytes) -> set[int]:
    """Todos los PIDs que aparecen en un TS."""
    return {_pid_de(buf, i) for i in range(0, len(buf) - TAM_PKT + 1, TAM_PKT) if buf[i] == SYNC}


def pid_libre(usados: set[int], desde: int = 0x0100, hasta: int = 0x1FFE) -> int:
    """Primer PID que no usa nadie.

    - Discussion: hace falta porque las dos rendiciones numeran sus ES por su
      cuenta y PUEDEN COINCIDIR. Medido en Pluto: el audio vive en el PID 0x50
      y el video tambien usa 0x50. Pegar los paquetes de audio sin remapear el
      PID mezcla dos flujos distintos bajo la misma etiqueta.
    """
    for p in range(desde, hasta):
        if p not in usados:
            return p
    raise ValueError("no queda PID libre en el rango")


def _remapear(pkts: list[bytes], pid_viejo: int, pid_nuevo: int) -> list[bytes]:
    salida = []
    for pkt in pkts:
        b = bytearray(pkt)
        b[1] = (b[1] & 0xE0) | ((pid_nuevo >> 8) & 0x1F)
        b[2] = pid_nuevo & 0xFF
        salida.append(bytes(b))
    return salida


def _paquetes(
    buf: bytes, conservar: set[int] | None = None, descartar: set[int] | None = None
) -> list[bytes]:
    """Trocea en paquetes de 188 bytes, filtrando por PID."""
    salida = []
    for i in range(0, len(buf) - TAM_PKT + 1, TAM_PKT):
        if buf[i] != SYNC:
            continue
        p = _pid_de(buf, i)
        if conservar is not None and p not in conservar:
            continue
        if descartar is not None and p in descartar:
            continue
        salida.append(bytes(buf[i : i + TAM_PKT]))
    return salida


def muxear(video: bytes, audio: bytes) -> tuple[bytes, dict]:
    """Junta un segmento de video con su gemelo de audio en UN solo MPEG-TS.

    - Discussion: el PMT del AUDIO es el que dice cual es su PID y su
      ``stream_type``; asi no hay que adivinar ninguno de los dos. Esos valores
      son los que se declaran en el PMT del video.
    - Throws: ``ValueError`` si alguno no trae PMT legible, si el audio no
      declara ES, o si el audio no trae paquetes de su PID.
    - Returns: ``(ts, reporte)``. Si el video ya traia ese PID, devuelve el
      video intacto con ``muxeado: False``: no hay nada que hacer.
    """
    pid_pmt_v = leer_pmt_pid(video)
    pmt_v = leer_pmt(video, pid_pmt_v) if pid_pmt_v is not None else None
    if pmt_v is None:
        raise ValueError("el segmento de video no trae PMT legible")

    pid_pmt_a = leer_pmt_pid(audio)
    pmt_a = leer_pmt(audio, pid_pmt_a) if pid_pmt_a is not None else None
    if pmt_a is None:
        raise ValueError("el segmento de audio no trae PMT legible")
    entradas = es_de(pmt_a)
    if not entradas:
        raise ValueError("el segmento de audio no declara ningun ES")
    tipo_a, pid_a = entradas[0]

    # El PID del audio se renumera contra TODOS los PIDs que trae el video, no
    # solo contra los que declara su PMT. Medido con FailArmy: el video lleva
    # paquetes en el PID 0x50 que su PMT no declara (timed_id3), y al declarar
    # ese PID como AAC el decodificador leia basura y fallaba con "Header
    # missing". Y al reves tambien muerde: un PID que el PMT declara pero que no
    # trae paquetes en ESTE segmento no aparece en los paquetes, y elegirlo
    # revienta al agregarlo. Se suman las dos vistas.
    usados = pids_usados(video) | pids_usados(audio) | {p for _, p in es_de(pmt_v)}
    if pid_a in usados:
        pid_a = pid_libre(usados)

    pmt_nuevo = seccion_a_paquetes(agregar_es(pmt_v, tipo_a, pid_a), pmt_v.pid)
    # Se descartan TODOS los PMT que declare el PAT, no solo el que se reescribe.
    pmts = set(leer_pmts_pids(video)) | {pmt_v.pid}
    v_pkts = _paquetes(video, descartar=PSI_A_DESCARTAR | pmts)
    a_pkts = _remapear(_paquetes(audio, conservar={entradas[0][1]}), entradas[0][1], pid_a)
    if not a_pkts:
        raise ValueError(f"el audio no trae paquetes del PID {entradas[0][1]:#x}")

    # Intercalado proporcional: un paquete de audio cada ``paso`` de video. Asi
    # el sonido llega repartido y no a rachas de un segundo.
    # El PAT va PRIMERO: es el punto de entrada de la PSI (§2.4.4.3).
    salida = bytearray(pat_a_paquetes(pmt_v.pid, pmt_v.programa))
    salida += pmt_nuevo
    paso = max(1, len(v_pkts) // max(1, len(a_pkts)))
    ia = 0
    for n, pkt in enumerate(v_pkts, start=1):
        salida += pkt
        if n % paso == 0 and ia < len(a_pkts):
            salida += a_pkts[ia]
            ia += 1
    salida += b"".join(a_pkts[ia:])
    return bytes(salida), {
        "muxeado": True,
        "pid_audio": pid_a,
        "stream_type_audio": tipo_a,
        "paquetes_video": len(v_pkts),
        "paquetes_audio": len(a_pkts),
        "paso": paso,
    }
