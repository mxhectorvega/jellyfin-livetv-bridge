"""Pruebas del muxeo de audio (src/ts_mux.py).

La prueba del CRC usa el AUTOCONTROL de la norma: el Anexo A de H.222.0 dice
que el campo CRC_32 se elige para que procesar la seccion ENTERA, CRC incluido,
de salida cero. Asi que el test no compara contra un numero magico: comprueba
esa propiedad.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from src.ts_mux import (  # noqa: E402
    SYNC,
    TAM_PKT,
    agregar_es,
    crc32_mpeg,
    es_de,
    leer_pmt,
    leer_pmt_pid,
    seccion_a_paquetes,
)

PID_PMT = 0x1000
PID_VIDEO = 0x0100
PID_AUDIO = 0x0101
PCR_PID = 0x0100


# ---------------------------------------------------------------------------
# Constructores de PSI sinteticos, segun la Tabla 2-33
# ---------------------------------------------------------------------------
def _cierra(seccion_sin_crc: bytes) -> bytes:
    return seccion_sin_crc + struct.pack(">I", crc32_mpeg(seccion_sin_crc))


def pat(pmt_pid: int = PID_PMT, programa: int = 1) -> bytes:
    cuerpo = struct.pack(">HH", programa, 0xE000 | pmt_pid)
    cab = bytearray([0x00, 0, 0])
    cab += struct.pack(">H", 1)  # transport_stream_id
    cab += b"\xc1\x00\x00"  # version/current_next, sec, last
    cuerpo = bytes(cab) + cuerpo
    largo = len(cuerpo) + 4 - 3
    cabecera = bytearray(cuerpo[:3])
    cabecera[1] = 0xB0 | ((largo >> 8) & 0x0F)
    cabecera[2] = largo & 0xFF
    return _cierra(bytes(cabecera) + cuerpo[3:])


def pmt(es: list[tuple[int, int]], pcr: int = PCR_PID) -> bytes:
    cuerpo = bytearray([0x02, 0, 0])
    cuerpo += struct.pack(">H", 1)  # program_number
    cuerpo += b"\xc1\x00\x00"  # version/current_next, sec, last
    cuerpo += struct.pack(">H", 0xE000 | pcr)
    cuerpo += struct.pack(">H", 0xF000)  # program_info_length = 0
    for tipo, pid in es:
        cuerpo += bytes([tipo, 0xE0 | ((pid >> 8) & 0x1F), pid & 0xFF, 0xF0, 0x00])
    largo = len(cuerpo) + 4 - 3
    cabecera = bytearray(cuerpo[:3])
    cabecera[1] = 0xB0 | ((largo >> 8) & 0x0F)
    cabecera[2] = largo & 0xFF
    return _cierra(bytes(cabecera) + cuerpo[3:])


# ---------------------------------------------------------------------------
# CRC_32 — Anexo A
# ---------------------------------------------------------------------------
def test_el_crc_cierra_en_cero_con_el_campo_incluido():
    """Autocontrol del Anexo A: seccion + CRC -> salida cero."""
    for datos in (b"", b"\x00", b"123456789", bytes(range(256)) * 3):
        assert crc32_mpeg(datos + struct.pack(">I", crc32_mpeg(datos))) == 0, datos[:8]


def test_el_crc_no_es_el_de_zlib():
    """El de zlib es REFLEJADO: confundirlos deja un PMT que ffprobe marca."""
    import zlib

    datos = b"program_map_section de prueba"
    assert crc32_mpeg(datos) != zlib.crc32(datos)


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------
def test_lee_el_pid_del_pmt_desde_el_pat():
    buf = seccion_a_paquetes(pat(), 0x0000)
    assert leer_pmt_pid(buf) == PID_PMT


def test_el_pid_del_pmt_no_esta_en_los_paquetes_de_otro_pid():
    buf = seccion_a_paquetes(pmt([(0x1B, PID_VIDEO)]), PID_PMT)
    assert leer_pmt_pid(buf) is None


def test_lee_el_pmt_y_sus_es():
    buf = seccion_a_paquetes(pmt([(0x1B, PID_VIDEO)]), PID_PMT)
    p = leer_pmt(buf, PID_PMT)
    assert p is not None
    assert p.programa == 1
    assert es_de(p) == [(0x1B, PID_VIDEO)]


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------
def test_agregar_es_suma_cinco_bytes_y_el_crc_vuelve_a_cerrar():
    buf = seccion_a_paquetes(pmt([(0x1B, PID_VIDEO)]), PID_PMT)
    p = leer_pmt(buf, PID_PMT)
    assert p is not None
    antes = len(p.seccion)
    nueva = agregar_es(p, 0x0F, PID_AUDIO)
    assert len(nueva) == antes + 5, "una entrada de ES son 5 bytes"
    assert crc32_mpeg(nueva) == 0, "el CRC recalculado tiene que cerrar"
    # y la seccion nueva, metida en paquetes, se vuelve a leer con los DOS ES
    buf2 = seccion_a_paquetes(nueva, PID_PMT)
    p2 = leer_pmt(buf2, PID_PMT)
    assert p2 is not None
    assert es_de(p2) == [(0x1B, PID_VIDEO), (0x0F, PID_AUDIO)]


def test_agregar_es_actualiza_section_length():
    """§2.4.4.9: section_length cuenta lo que sigue, CRC incluido."""
    buf = seccion_a_paquetes(pmt([(0x1B, PID_VIDEO)]), PID_PMT)
    p = leer_pmt(buf, PID_PMT)
    largo_antes = ((p.seccion[1] & 0x0F) << 8) | p.seccion[2]
    nueva = agregar_es(p, 0x0F, PID_AUDIO)
    largo_despues = ((nueva[1] & 0x0F) << 8) | nueva[2]
    assert largo_despues == largo_antes + 5


def test_agregar_es_rechaza_un_pid_que_ya_esta():
    buf = seccion_a_paquetes(pmt([(0x1B, PID_VIDEO)]), PID_PMT)
    p = leer_pmt(buf, PID_PMT)
    try:
        agregar_es(p, 0x0F, PID_VIDEO)
    except ValueError as exc:
        assert "ya esta" in str(exc)
        return
    raise AssertionError("debia rechazar el PID repetido")


def test_el_pmt_no_pasa_del_tope_de_la_norma():
    """§2.4.4.9: section_length no pasa de 1021 (0x3FD)."""
    es = [(0x1B, PID_VIDEO)] + [(0x06, 0x0200 + i) for i in range(200)]
    buf = seccion_a_paquetes(pmt(es), PID_PMT)
    p = leer_pmt(buf, PID_PMT)
    assert p is not None
    try:
        agregar_es(p, 0x0F, PID_AUDIO)
    except ValueError as exc:
        assert "tope" in str(exc)
        return
    raise AssertionError("debia avisar que no cabe")


# ---------------------------------------------------------------------------
# Empaquetado
# ---------------------------------------------------------------------------
def test_la_seccion_empaquetada_mide_188_y_se_reensambla():
    original = pat()
    buf = seccion_a_paquetes(original, 0x0000)
    assert len(buf) % TAM_PKT == 0, len(buf)
    assert all(buf[i] == SYNC for i in range(0, len(buf), TAM_PKT))
    # el pointer_field del primer paquete y la seccion detras
    primero = buf[:TAM_PKT]
    off = 4 + (1 + primero[4] if (primero[3] >> 4) & 0x02 else 0)
    assert primero[off] == 0x00
    assert primero[off + 1 : off + 1 + len(original)] == original


def test_una_seccion_larga_ocupa_varios_paquetes():
    # 40 entradas de ES = 200 bytes de seccion: pasa de los 184 de un paquete
    es = [(0x1B, PID_VIDEO)] + [(0x06, 0x0300 + i) for i in range(40)]
    buf = seccion_a_paquetes(pmt(es), PID_PMT)
    assert len(buf) > TAM_PKT, "una seccion de 200 bytes no cabe en un paquete"
    assert len(buf) % TAM_PKT == 0
    p = leer_pmt(buf, PID_PMT)
    assert p is not None
    assert len(es_de(p)) == len(es)


def test_el_stream_type_del_aac_es_el_de_la_norma():
    """Tabla 2-34: 0x0F = ISO/IEC 13818-7 Audio with ADTS transport syntax."""
    from src.ts_mux import STREAM_TYPE_AAC_ADTS

    assert STREAM_TYPE_AAC_ADTS == 0x0F


# ---------------------------------------------------------------------------
# muxear(): la union de los dos flujos
# ---------------------------------------------------------------------------
def _ts(seccion_pmt: bytes, pid_pmt: int, pid_es: int, cuantos: int = 4) -> bytes:
    """Un TS minimo: su PAT, su PMT, y paquetes de carga en el PID del ES."""
    salida = seccion_a_paquetes(pat(pid_pmt), 0x0000)
    salida += seccion_a_paquetes(seccion_pmt, pid_pmt)
    for i in range(cuantos):
        pkt = bytearray(TAM_PKT)
        pkt[0] = SYNC
        pkt[1] = (pid_es >> 8) & 0x1F
        pkt[2] = pid_es & 0xFF
        pkt[3] = 0x10 | (i & 0x0F)
        pkt[4:] = b"\xaa" * (TAM_PKT - 4)
        salida += pkt
    return bytes(salida)


def test_muxear_junta_video_y_audio_en_un_solo_ts():
    from src.ts_mux import muxear, pids_usados

    video = _ts(pmt([(0x1B, PID_VIDEO)]), PID_PMT, PID_VIDEO, 8)
    audio = _ts(pmt([(0x0F, PID_AUDIO)]), 0x1001, PID_AUDIO, 4)

    unido, reporte = muxear(video, audio)
    assert reporte["muxeado"] is True
    assert reporte["stream_type_audio"] == 0x0F

    p = leer_pmt(unido, PID_PMT)
    assert p is not None
    pids = [pid for _, pid in es_de(p)]
    assert PID_VIDEO in pids, "el video tiene que seguir declarado"
    assert reporte["pid_audio"] in pids, "y el audio tambien"

    usados = pids_usados(unido)
    assert PID_VIDEO in usados
    assert reporte["pid_audio"] in usados


def test_muxear_tira_la_psi_del_audio():
    """Dos PAT/PMT en el mismo TS se contradicen: solo puede quedar uno."""
    from src.ts_mux import muxear, pids_usados

    video = _ts(pmt([(0x1B, PID_VIDEO)]), PID_PMT, PID_VIDEO, 4)
    audio = _ts(pmt([(0x0F, PID_AUDIO)]), 0x1001, PID_AUDIO, 4)
    unido, _ = muxear(video, audio)
    usados = pids_usados(unido)
    assert 0x1001 not in usados, "el PMT del audio no puede sobrevivir"


def test_muxear_emite_el_pat_que_apunta_al_pmt():
    """§2.4.4.3 (Tabla 2-30): el PAT es el punto de entrada de la PSI.

    Sin PAT el decodificador queda obligado a barrer el flujo buscando una
    tabla con table_id 0x02. ffprobe lo hace, pero un cliente estricto no tiene
    por que.
    """
    from src.ts_mux import SYNC, _pid_de, leer_pmts_pids, muxear

    video = _ts(pmt([(0x1B, PID_VIDEO)]), PID_PMT, PID_VIDEO, 4)
    audio = _ts(pmt([(0x0F, PID_AUDIO)]), 0x1001, PID_AUDIO, 4)
    unido, _ = muxear(video, audio)

    assert unido[0] == SYNC
    assert _pid_de(unido, 0) == 0x0000, "el primer paquete tiene que ser el PAT"
    assert leer_pmts_pids(unido) == [PID_PMT], "el PAT tiene que apuntar al PMT"


def test_el_pat_lleva_el_mismo_programa_que_el_pmt():
    """Si el numero de programa no empata, el decodificador no liga PAT y PMT."""
    from src.ts_mux import PID_PAT, _pid_de, _seccion_de_paquetes, pat_a_paquetes

    for programa in (1, 7, 300):
        ts = pat_a_paquetes(0x1000, programa)
        assert _pid_de(ts, 0) == 0x0000
        # Se extrae la seccion con el reensamblador del modulo: el paquete lleva
        # campo de adaptacion de relleno, asi que el inicio de la carga util NO
        # esta en un offset fijo.
        sec = _seccion_de_paquetes(ts, PID_PAT)
        assert sec is not None
        assert sec[0] == 0x00, "table_id del PAT"
        assert sec[8:10] == programa.to_bytes(2, "big"), "numero de programa"
        assert sec[10:12] == b"\xf0\x00", "PID del PMT (0x1000)"


def test_muxear_renumera_el_pid_del_audio_si_choca():
    """Medido en Pluto: el audio trae el 0x50 y el video tambien lo usa."""
    from src.ts_mux import muxear

    chocado = 0x0050
    video = _ts(pmt([(0x1B, chocado)]), PID_PMT, chocado, 4)
    audio = _ts(pmt([(0x0F, chocado)]), 0x1001, chocado, 4)
    _unido, reporte = muxear(video, audio)
    assert reporte["muxeado"] is True
    assert reporte["pid_audio"] != chocado, "no puede quedarse con el PID que ya usa el video"


def test_el_pid_del_audio_por_defecto_sale_estable():
    """El mismo PID en TODOS los segmentos del canal.

    Antes se buscaba "el primer libre" contra cada segmento y salia distinto en
    cada uno (medido sobre el rele en vivo: 0x100, 0x101, 0x102 y 0x103 en el
    mismo jale). Cada segmento trae su PAT y PMT, asi que es TS valido, pero un
    reproductor que se quede con el primer PMT pierde el audio al cambiar.
    """
    from src.ts_mux import BASE_PID_AUDIO, muxear

    audio = _ts(pmt([(0x0F, PID_AUDIO)]), 0x1001, PID_AUDIO, 4)
    # dos segmentos de video con PIDs DISTINTOS en uso
    v1 = _ts(pmt([(0x1B, 0x0110)]), PID_PMT, 0x0110, 4)
    v2 = _ts(pmt([(0x1B, 0x0111)]), PID_PMT, 0x0111, 4)

    _, r1 = muxear(v1, audio)
    _, r2 = muxear(v2, audio)
    assert r1["pid_audio"] == r2["pid_audio"], "el PID no puede bailar entre segmentos"
    assert r1["pid_audio"] >= BASE_PID_AUDIO, "se busca desde el rango alto"


def test_el_pid_del_audio_se_respeta_si_lo_da_el_rele():
    """El rele recuerda el PID del canal y lo vuelve a pedir en cada segmento."""
    from src.ts_mux import muxear

    audio = _ts(pmt([(0x0F, PID_AUDIO)]), 0x1001, PID_AUDIO, 4)
    video = _ts(pmt([(0x1B, PID_VIDEO)]), PID_PMT, PID_VIDEO, 4)
    _, r = muxear(video, audio, pid_audio=0x1E00)
    assert r["pid_audio"] == 0x1E00
    p = leer_pmt(muxear(video, audio, pid_audio=0x1E00)[0], PID_PMT)
    assert 0x1E00 in [pid for _, pid in es_de(p)]


def test_el_pid_del_audio_se_reasigna_si_el_que_dieron_choca():
    """Si el video ocupa el PID que traia el rele, se elige otro."""
    from src.ts_mux import muxear

    audio = _ts(pmt([(0x0F, PID_AUDIO)]), 0x1001, PID_AUDIO, 4)
    video = _ts(pmt([(0x1B, 0x1F00)]), PID_PMT, 0x1F00, 4)
    _, r = muxear(video, audio, pid_audio=0x1F00)
    assert r["pid_audio"] != 0x1F00


def test_muxear_avisa_si_el_video_no_trae_pmt():
    import pytest
    from src.ts_mux import muxear

    with pytest.raises(ValueError):
        muxear(b"\x00" * 188, b"\x00" * 188)
