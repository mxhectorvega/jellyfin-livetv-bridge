"""Remiendo de la linea de tiempo de MPEG-TS (PCR, PTS y DTS).

El problema
-----------
El rele entrega los segmentos del origen tal cual. Mientras no cambie de fuente
la linea de tiempo cuadra, pero al saltar al respaldo los sellos del origen B no
empalman con los del A: el reproductor recibe un PTS que salta y lo dibuja como
tiron (o como fin de canal si el salto es grande).

Que hace
--------
1. **Desplaza** todos los sellos de tiempo (PCR, PTS y DTS) de la fuente nueva
   por una constante calculada para que su primer sello continue al ultimo que
   ya salio al cliente. Los sellos son de 33 bits, o sea modulo ``2**33``; el
   desplazamiento se calcula en la misma aritmetica, asi que no se rompe en el
   envolvimiento.
2. **Marca** ``discontinuity_indicator`` en el primer paquete con campo de
   adaptacion de cada PID despues del empalme. Eso es lo que corresponde cuando
   el ``continuity_counter`` puede saltar: el decodificador resincroniza el
   contador y NO el reloj, que es justo lo que acabamos de arreglar. Marcar solo
   el primero es barato: un paquete por PID por empalme.

Lo que NO hace
--------------
- No recodifica, no reordena, no re-multiplexa: la carga util (video y audio)
  sale byte por byte igual.
- No arregla un empalme a media imagen: si la fuente nueva arranca sin keyframe,
  el cuadro roto se ve. Eso es del codificador, no de la linea de tiempo.
- No reescribe el ``continuity_counter`` de todos los paquetes. Seria lo unico
  que falta para un empalme del todo invisible, y obliga a recorrer el 100% de
  los paquetes en vez del ~5% que lleva sellos. Se mide antes de decidir.

Norma
-----
ISO/IEC 13818-1 | ITU-T H.222.0 (06/2012): **§2.4.3** Transport Stream (el
``transport_packet()`` y su ``adaptation_field``) y **§2.4.3.6** PES packets,
donde viven PTS y DTS. Ambas clausulas se confirmaron contra el texto de la
recomendacion. La clausula exacta del ``adaptation_field`` queda pendiente de
fijar contra el texto completo; los NOMBRES de campo usados aqui
(``program_clock_reference_base``, ``program_clock_reference_extension``,
``discontinuity_indicator``, ``continuity_counter``) si son los de la norma.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TAM_PKT = 188
SYNC = 0x47
# Los sellos de tiempo son de 33 bits: la aritmetica es modulo 2**33.
MOD = 1 << 33
# Los 6 bits reservados entre la base del PCR y su extension van a 1.
RESERVADO_PCR = 0x7E


# ---------------------------------------------------------------------------
# Sellos de 33 bits (PTS/DTS): 5 bytes con bits marcadores
# ---------------------------------------------------------------------------
def leer_sello(b: bytes | bytearray, off: int) -> int:
    """Decodifica un PTS/DTS de 5 bytes."""
    return (
        (((b[off] >> 1) & 0x07) << 30)
        | (b[off + 1] << 22)
        | (((b[off + 2] >> 1) & 0x7F) << 15)
        | (b[off + 3] << 7)
        | ((b[off + 4] >> 1) & 0x7F)
    )


def escribir_sello(dest: bytearray, off: int, valor: int, bandera: int) -> None:
    """Escribe un PTS/DTS de 5 bytes con sus marcadores.

    - ``bandera`` es el nibble que la norma pone en los 4 bits altos: ``0b0010``
      cuando solo hay PTS, ``0b0011`` para un PTS seguido de DTS.
    """
    dest[off] = (bandera << 4) | (((valor >> 30) & 0x07) << 1) | 0x01
    dest[off + 1] = (valor >> 22) & 0xFF
    dest[off + 2] = (((valor >> 15) & 0x7F) << 1) | 0x01
    dest[off + 3] = (valor >> 7) & 0xFF
    dest[off + 4] = ((valor & 0x7F) << 1) | 0x01


# ---------------------------------------------------------------------------
# PCR: 33 bits a 90 kHz + 9 bits a 27 MHz, en 6 bytes
# ---------------------------------------------------------------------------
def leer_pcr(b: bytes | bytearray, off: int) -> tuple[int, int]:
    """Devuelve ``(base, extension)`` del PCR.

    - Discussion: el PCR vale ``base * 300 + extension`` en unidades de 27 MHz,
      pero solo la BASE se desplaza: el desplazamiento se mide en ticks de
      90 kHz, que son 300 ticks de 27 MHz, asi que la extension no se toca.
    """
    base = (
        (b[off] << 25)
        | (b[off + 1] << 17)
        | (b[off + 2] << 9)
        | (b[off + 3] << 1)
        | (b[off + 4] >> 7)
    )
    ext = ((b[off + 4] & 0x01) << 8) | b[off + 5]
    return base & (MOD - 1), ext


def escribir_pcr(dest: bytearray, off: int, base: int, ext: int) -> None:
    """Escribe los 6 bytes del PCR conservando los bits reservados."""
    dest[off] = (base >> 25) & 0xFF
    dest[off + 1] = (base >> 17) & 0xFF
    dest[off + 2] = (base >> 9) & 0xFF
    dest[off + 3] = (base >> 1) & 0xFF
    dest[off + 4] = ((base & 0x01) << 7) | RESERVADO_PCR | ((ext >> 8) & 0x01)
    dest[off + 5] = ext & 0xFF


@dataclass
class Cuenta:
    """Lo que hizo una pasada, para el log y el banco.

    - Note: ``pcr``/``pts``/``dts`` cuentan los sellos VISTOS; ``movidos`` cuenta
      los campos que de verdad se reescribieron. Con la primera fuente
      (``delta == 0``) lo visto es > 0 y lo movido es 0. Y ``max_pts`` es el
      dato que interesa para empalmar: el cliente ya presento todo lo que este
      por debajo, asi que el sello siguiente tiene que caer por encima.
    """

    paquetes: int = 0
    movidos: int = 0
    pcr: int = 0
    pts: int = 0
    dts: int = 0
    marcados: int = 0
    sync_malo: int = 0
    primer_pts: int | None = None
    max_pts: int | None = None
    pids_sello: set[int] = field(default_factory=set)


class Remendador:
    """Aplica un desplazamiento constante a los sellos de tiempo de un TS.

    - Discussion: el rele lo usa asi al cambiar de fuente::

        rem = Remendador()
        # ... la fuente A ya salio pasando por rem.procesar() ...
        delta = rem.sincronizar(rem.ultimo_pts_emitido, primer_pts(segmento_nuevo))
        # de aqui en adelante, cada segmento de la fuente B pasa por procesar()

    - Important: ``sincronizar`` no desplaza nada por si solo; deja listo el
      ``delta`` y la marca de discontinuidad. Con ``delta == 0`` (primera
      fuente) ``procesar`` recorre y cuenta, pero no toca un solo byte.
    """

    def __init__(self) -> None:
        self.delta = 0
        self.marcar_pendiente = False
        self.pids_marcados: set[int] = set()
        self.ultimo_pts_emitido: int | None = None

    # ------------------------------------------------------------------
    def sincronizar(self, ultimo_pts_emitido: int | None, primer_pts_crudo: int | None,
                    paso: int = 0) -> int:
        """Calcula el desplazamiento para que la fuente nueva continue a la vieja.

        - Returns: el ``delta`` aplicado (modulo ``2**33``), o ``0`` si falta
          alguno de los dos sellos, en cuyo caso no se toca nada.
        - Discussion: el delta se calcula FRESCO cada vez, no se acumula:
          ``ultimo_pts_emitido`` ya viene desplazado de los empalmes anteriores,
          asi que el resultado encadena solo. Un tercer empalme no vuelve a la
          linea de tiempo original.
        - Note: ``paso`` es la duracion de un cuadro en ticks de 90 kHz (3003 a
          29.97 fps, 3600 a 25). Con ``0`` el primer sello de la fuente nueva
          cae EXACTAMENTE encima del ultimo de la vieja (dos cuadros con el
          mismo tiempo); con el paso puesto, cae un cuadro despues, que es lo
          que se quiere. Un cuadro de mas o de menos no se ve; un salto de
          segundos si.
        """
        if ultimo_pts_emitido is None or primer_pts_crudo is None:
            return 0
        objetivo = (ultimo_pts_emitido + paso) % MOD
        self.delta = (objetivo - primer_pts_crudo) % MOD
        self.marcar_pendiente = True
        self.pids_marcados.clear()
        return self.delta

    # ------------------------------------------------------------------
    def procesar(self, buf: bytearray, tam_pkt: int = TAM_PKT) -> Cuenta:
        """Recorre el buffer y desplaza los sellos EN SITIO. Devuelve la cuenta.

        - Discussion: el recorrido es inevitable (hay que ver que paquete lleva
          sello); lo que se evita es tocar los bytes de los que no llevan.
        - Note: el buffer tiene que empezar alineado al byte de sincronia.
        """
        cuenta = Cuenta()
        delta = self.delta
        desplazar = delta != 0
        marcar = self.marcar_pendiente
        n = len(buf) // tam_pkt
        for i in range(0, n * tam_pkt, tam_pkt):
            if buf[i] != SYNC:
                cuenta.sync_malo += 1
                continue
            cuenta.paquetes += 1
            pid = ((buf[i + 1] & 0x1F) << 8) | buf[i + 2]
            afc = (buf[i + 3] >> 4) & 0x03
            off = i + 4
            if afc & 0x02:                        # hay campo de adaptacion
                af_len = buf[off]
                if af_len > 0:
                    if marcar and pid not in self.pids_marcados:
                        buf[off + 1] |= 0x80      # discontinuity_indicator
                        self.pids_marcados.add(pid)
                        cuenta.marcados += 1
                    if desplazar and (buf[off + 1] & 0x10):     # PCR_flag
                        base, ext = leer_pcr(buf, off + 2)
                        escribir_pcr(buf, off + 2, (base + delta) % MOD, ext)
                        cuenta.movidos += 1
                    if buf[off + 1] & 0x10:
                        cuenta.pcr += 1
                off += 1 + af_len

            if not (buf[i + 1] & 0x40):           # payload_unit_start_indicator
                continue
            if buf[off:off + 3] != b"\x00\x00\x01":
                continue
            tipo = (buf[off + 7] >> 6) & 0x03
            if tipo not in (2, 3):
                continue
            cuenta.pids_sello.add(pid)
            pts = leer_sello(buf, off + 9)
            if cuenta.primer_pts is None:
                cuenta.primer_pts = pts
            if cuenta.max_pts is None or pts > cuenta.max_pts:
                cuenta.max_pts = pts
            if desplazar:
                escribir_sello(buf, off + 9, (pts + delta) % MOD, tipo)
                cuenta.movidos += 1
                if tipo == 3:
                    dts = leer_sello(buf, off + 14)
                    escribir_sello(buf, off + 14, (dts + delta) % MOD, 1)
                    cuenta.dts += 1
                    cuenta.movidos += 1
            cuenta.pts += 1

        if cuenta.max_pts is not None:
            self.ultimo_pts_emitido = (cuenta.max_pts + delta) % MOD
        if cuenta.marcados:
            self.marcar_pendiente = False
        return cuenta


def primer_pts(buf: bytes | bytearray) -> int | None:
    """Primer PTS del buffer, SIN desplazarlo.

    - Discussion: el rele lo necesita al cambiar de fuente, porque el
      desplazamiento se calcula contra el primer sello CRUDO de la fuente nueva,
      antes de que ``procesar`` le haya sumado nada.
    """
    n = len(buf) // TAM_PKT
    for i in range(0, n * TAM_PKT, TAM_PKT):
        if buf[i] != SYNC or not (buf[i + 1] & 0x40):
            continue
        afc = (buf[i + 3] >> 4) & 0x03
        off = i + 4
        if afc & 0x02:
            off += 1 + buf[i + 4]
        if buf[off:off + 3] != b"\x00\x00\x01":
            continue
        if ((buf[off + 7] >> 6) & 0x03) in (2, 3):
            return leer_sello(buf, off + 9)
    return None


def alinear(buf: bytes | bytearray) -> int:
    """Indice del primer byte de sincronia, o ``-1`` si no hay en el arranque."""
    encontrado = bytes(buf[:4096]).find(bytes([SYNC]))
    return encontrado if encontrado >= 0 else -1
