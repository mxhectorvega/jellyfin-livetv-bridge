"""Pruebas del remendador de MPEG-TS (src/ts_splice.py).

Construyen paquetes TS sinteticos y comprueban los sellos byte a byte: aqui lo
que se prueba es la aritmetica y la posicion de los campos, no el rendimiento
(eso es tests/banco_ts.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from src.ts_splice import (  # noqa: E402
    MOD,
    TAM_PKT,
    Remendador,
    escribir_pcr,
    escribir_sello,
    leer_pcr,
    leer_sello,
    primer_pts,
)

PID_VIDEO = 0x1E1


# ---------------------------------------------------------------------------
# Constructores de paquetes sinteticos
# ---------------------------------------------------------------------------
def pkt_pcr(pid: int, base: int, ext: int = 0, disc: int = 0) -> bytearray:
    """Paquete con SOLO campo de adaptacion (afc=2) y PCR."""
    af = bytearray([0x10 | (disc << 7)])  # PCR_flag (+ discontinuity)
    seis = bytearray(6)
    escribir_pcr(seis, 0, base, ext)
    af += seis
    relleno = 183 - len(af)
    af += b"\xff" * relleno
    pkt = bytearray([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x20])
    pkt += bytes([len(af)]) + af
    assert len(pkt) == TAM_PKT, len(pkt)
    return pkt


def pkt_pes(pid: int, pts: int, dts: int | None = None, carga: bytes = b"VIDEO") -> bytearray:
    """Paquete con PES (PUSI) y PTS o PTS+DTS."""
    opc = bytearray()
    if dts is None:
        flags2, largo = 0x80, 5
        cinco = bytearray(5)
        escribir_sello(cinco, 0, pts, 0b0010)
        opc += cinco
    else:
        flags2, largo = 0xC0, 10
        cinco = bytearray(5)
        escribir_sello(cinco, 0, pts, 0b0011)
        opc += cinco
        cinco = bytearray(5)
        escribir_sello(cinco, 0, dts, 0b0001)
        opc += cinco
    cuerpo = bytes([0x00, 0x00, 0x01, 0xE0, 0x00, 0x00, 0x80, flags2, largo]) + bytes(opc) + carga
    # PUSI (0x40) tiene que ir puesto o no es un arranque de PES.
    pkt = bytearray([0x47, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10])
    pkt += cuerpo
    pkt += b"\xff" * (TAM_PKT - len(pkt))
    assert len(pkt) == TAM_PKT
    return pkt


# ---------------------------------------------------------------------------
# Aritmetica de los sellos
# ---------------------------------------------------------------------------
def test_sello_ida_y_vuelta():
    for valor in (0, 1, 90_000, MOD - 1, 123_456_789, (1 << 32) | 7):
        b = bytearray(5)
        escribir_sello(b, 0, valor, 0b0010)
        assert leer_sello(b, 0) == valor % MOD, valor
        # los tres bits marcadores de la norma van a 1
        assert b[0] & 0x01 and b[2] & 0x01 and b[4] & 0x01, valor


def test_sello_conserva_la_bandera():
    for bandera in (0b0010, 0b0011):
        b = bytearray(5)
        escribir_sello(b, 0, 42, bandera)
        assert (b[0] >> 4) == bandera


def test_pcr_ida_y_vuelta():
    for base, ext in ((0, 0), (1, 300), (MOD - 1, 299), (12_345_678, 155)):
        b = bytearray(6)
        escribir_pcr(b, 0, base, ext)
        assert leer_pcr(b, 0) == (base, ext), (base, ext)
        # los 6 bits reservados entre base y extension van a 1
        assert (b[4] & 0x7E) == 0x7E


# ---------------------------------------------------------------------------
# Desplazamiento
# ---------------------------------------------------------------------------
def test_sin_delta_no_toca_un_byte():
    """Con delta=0 ``procesar`` cuenta pero no modifica nada."""
    buf = bytearray(pkt_pcr(PID_VIDEO, 1000) + pkt_pes(PID_VIDEO, 5000))
    original = bytes(buf)
    rem = Remendador()
    cuenta = rem.procesar(buf)
    assert bytes(buf) == original
    assert cuenta.paquetes == 2 and cuenta.pts == 1 and cuenta.pcr == 1
    assert cuenta.movidos == 0, "sin delta no se reescribe ningun sello"


def test_desplaza_pcr_y_pts_una_constante():
    rem = Remendador()
    rem.sincronizar(10_000, 1_000)  # delta = 9000
    buf = bytearray(pkt_pcr(PID_VIDEO, 1_000) + pkt_pes(PID_VIDEO, 1_000))
    cuenta = rem.procesar(buf)
    assert cuenta.pcr == 1 and cuenta.pts == 1 and cuenta.movidos == 2
    assert leer_pcr(buf, 6)[0] == 1_000 + 9_000
    # el PTS va en el payload: offset 4 (cabecera) + 9 (cabecera PES)
    assert leer_sello(buf, TAM_PKT + 13) == 1_000 + 9_000


def test_el_paso_deja_un_cuadro_de_separacion():
    rem = Remendador()
    rem.sincronizar(10_000, 1_000, paso=3003)
    buf = bytearray(pkt_pes(PID_VIDEO, 1_000))
    rem.procesar(buf)
    assert leer_sello(buf, 13) == 10_000 + 3003


def test_envuelve_en_33_bits():
    casi_final = MOD - 5
    rem = Remendador()
    rem.sincronizar(casi_final, 100)  # delta = MOD - 105
    buf = bytearray(pkt_pes(PID_VIDEO, 100))
    rem.procesar(buf)
    assert leer_sello(buf, 13) == casi_final % MOD


def test_dts_se_mueve_junto_al_pts():
    rem = Remendador()
    rem.sincronizar(50_000, 1_000)
    buf = bytearray(pkt_pes(PID_VIDEO, 1_100, dts=1_000))
    cuenta = rem.procesar(buf)
    assert cuenta.pts == 1 and cuenta.dts == 1
    assert leer_sello(buf, 13) == 1_100 + 49_000
    assert leer_sello(buf, 18) == 1_000 + 49_000


def test_la_carga_util_no_se_toca():
    carga = b"ESTO ES VIDEO CRUDO \x00\x01\x02"
    rem = Remendador()
    rem.sincronizar(90_000, 1_000)
    buf = bytearray(pkt_pes(PID_VIDEO, 1_000, carga=carga))
    pos = bytes(buf).index(carga)
    rem.procesar(buf)
    assert bytes(buf)[pos : pos + len(carga)] == carga, "el video va intacto"
    assert leer_sello(buf, 13) == 1_000 + 89_000, "y el sello si se movio"


# ---------------------------------------------------------------------------
# Marca de discontinuidad
# ---------------------------------------------------------------------------
def test_marca_discontinuidad_una_vez_por_pid():
    rem = Remendador()
    rem.sincronizar(10_000, 1_000)
    buf = bytearray(pkt_pcr(PID_VIDEO, 1_000) + pkt_pcr(PID_VIDEO, 1_500) + pkt_pcr(0x100, 1_500))
    cuenta = rem.procesar(buf)
    assert cuenta.marcados == 2, "un marcado por PID, no por paquete"
    assert buf[5] & 0x80, "el primero del PID de video queda marcado"
    assert buf[TAM_PKT + 5] & 0x80 == 0, "el segundo del mismo PID no"
    assert buf[2 * TAM_PKT + 5] & 0x80, "el PID nuevo si"


def test_no_marca_si_no_hubo_empalme():
    rem = Remendador()
    buf = bytearray(pkt_pcr(PID_VIDEO, 1_000))
    rem.procesar(buf)
    assert buf[5] & 0x80 == 0


# ---------------------------------------------------------------------------
# Lectura sin tocar
# ---------------------------------------------------------------------------
def test_primer_pts_lee_sin_modificar():
    buf = bytearray(pkt_pcr(PID_VIDEO, 1) + pkt_pes(PID_VIDEO, 77_000))
    original = bytes(buf)
    assert primer_pts(buf) == 77_000
    assert bytes(buf) == original


def test_primer_pts_none_si_no_hay_pes():
    assert primer_pts(bytearray(pkt_pcr(PID_VIDEO, 1))) is None


def test_procesar_cuenta_el_sync_malo():
    rem = Remendador()
    buf = bytearray(b"\x00" * TAM_PKT)
    cuenta = rem.procesar(buf)
    assert cuenta.sync_malo == 1 and cuenta.paquetes == 0
