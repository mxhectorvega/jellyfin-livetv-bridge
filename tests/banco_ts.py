"""Banco del remendador de MPEG-TS (src/ts_splice.py).

No prueba logica: prueba el EFECTO sobre un flujo real capturado del rele.

Escenario que simula el fallo real:
  1. Se parte el corpus en A (primera mitad) y B (segunda).
  2. A B se le suma un desplazamiento arbitrario, como si fuera OTRA fuente con
     su propio reloj.
  3. SIN arreglo: A + B desplazada -> el reproductor ve un salto de PTS.
     CON arreglo: se calcula el delta y se aplica a B -> la linea continua.

La comprobacion del salto la hace **ffprobe**, no este script: es el mismo
criterio que usaria Jellyfin.

Uso:  .venv/bin/python tests/banco_ts.py [ruta_corpus]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from src.ts_splice import SYNC, TAM_PKT, Remendador, primer_pts  # noqa: E402

CORPUS = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/server/tmp_lvb/corpus.ts")
DESPLAZAMIENTO_FUENTE_B = 123_456_789   # ~22 min a 90 kHz: "otro reloj"


def cargar_corpus() -> bytearray:
    datos = bytearray(CORPUS.read_bytes())
    off = bytes(datos[:4096]).find(bytes([SYNC]))
    if off < 0:
        raise SystemExit("el corpus no tiene byte de sincronia")
    return datos[off - (off % TAM_PKT):]


def corte_en_pes(datos: bytearray, objetivo: int) -> int:
    """Indice de paquete con payload_unit_start_indicator mas cercano al objetivo.

    - Discussion: un empalme de verdad cae en frontera de segmento, que es
      frontera de PES. Cortar a la mitad de un PES probaria algo que no pasa.
    """
    n = len(datos) // TAM_PKT
    for k in range(objetivo, n):
        i = k * TAM_PKT
        if datos[i] == SYNC and (datos[i + 1] & 0x40):
            return k
    return objetivo


def pts_con_ffprobe(ruta: Path) -> list[float]:
    """ptx_time de los paquetes de video, leidos por ffprobe."""
    code = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
         "-show_entries", "packet=pts_time", "-of", "json", str(ruta)],
        capture_output=True, timeout=180,
    )
    if code.returncode != 0:
        return []
    try:
        datos = json.loads(code.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return []
    return [float(p["pts_time"]) for p in datos.get("packets", []) if p.get("pts_time")]


def escalon_mayor(tiempos: list[float]) -> tuple[float, float]:
    """Mayor salto entre paquetes consecutivos y su posicion relativa."""
    if len(tiempos) < 2:
        return 0.0, 0.0
    difs = [(abs(tiempos[i + 1] - tiempos[i]), i) for i in range(len(tiempos) - 1)]
    difs.sort(reverse=True)
    return difs[0][0], difs[0][1] / len(tiempos)


def main() -> int:
    datos = cargar_corpus()
    total = len(datos) // TAM_PKT
    corte = corte_en_pes(datos, total // 2)
    print(f"corpus   : {len(datos)} B | {total} paquetes | corte en paquete {corte}")

    A = bytearray(datos[:corte * TAM_PKT])
    B_limpia = bytearray(datos[corte * TAM_PKT:])

    # --- B con el reloj de OTRA fuente ---------------------------------
    desplazador = Remendador()
    desplazador.delta = DESPLAZAMIENTO_FUENTE_B
    desplazador.procesar(B_limpia)
    B_otra = B_limpia
    print(f"fuente B : desplazada {DESPLAZAMIENTO_FUENTE_B} ticks (simula otro reloj)")

    salida = Path("/home/server/tmp_lvb")

    # --- SIN arreglo ----------------------------------------------------
    sin = salida / "sin_arreglo.ts"
    sin.write_bytes(bytes(A) + bytes(B_otra))
    t_sin = pts_con_ffprobe(sin)

    # --- CON arreglo ----------------------------------------------------
    rem = Remendador()
    t0 = time.perf_counter()
    cA = rem.procesar(A)
    tA = time.perf_counter() - t0
    print(f"\nfuente A : {cA.paquetes} paquetes, {cA.pts} PTS, {cA.pcr} PCR, "
          f"{cA.marcados} marcados | {tA * 1000:.0f} ms")
    print(f"           ultimo PTS emitido: {rem.ultimo_pts_emitido}")

    primero_crudo = primer_pts(B_otra)
    delta = rem.sincronizar(rem.ultimo_pts_emitido, primero_crudo, paso=3003)
    t0 = time.perf_counter()
    cB = rem.procesar(B_otra)
    tB = time.perf_counter() - t0
    print(f"empalme  : primer PTS crudo de B = {primero_crudo} | delta = {delta}")
    print(f"fuente B : {cB.paquetes} paquetes, {cB.pts} PTS, {cB.pcr} PCR, "
          f"{cB.marcados} marcados | {tB * 1000:.0f} ms")

    con = salida / "con_arreglo.ts"
    con.write_bytes(bytes(A) + bytes(B_otra))
    t_con = pts_con_ffprobe(con)

    # --- veredicto ------------------------------------------------------
    print("\n" + "=" * 62)
    if not t_sin or not t_con:
        print("ffprobe no devolvio paquetes de video; no se puede juzgar")
        return 1
    s_sin, pos_sin = escalon_mayor(t_sin)
    s_con, pos_con = escalon_mayor(t_con)
    print(f"paquetes de video vistos por ffprobe: sin={len(t_sin)} con={len(t_con)}")
    print(f"SIN arreglo: escalon mayor {s_sin:.3f} s (en {pos_sin:.0%} del flujo)")
    print(f"CON arreglo: escalon mayor {s_con:.3f} s (en {pos_con:.0%} del flujo)")
    veredicto = "ARREGLA" if s_con < s_sin / 10 else "NO ARREGLA"
    print(f"VEREDICTO: {veredicto}")

    # --- rendimiento ----------------------------------------------------
    pkt_total = cA.paquetes + cB.paquetes
    seg_total = tA + tB
    print("\nRENDIMIENTO (una pasada completa sobre el corpus)")
    print(f"  paquetes      : {pkt_total}")
    print(f"  tiempo        : {seg_total * 1000:.0f} ms")
    print(f"  paquetes/s    : {pkt_total / seg_total:,.0f}")
    print(f"  MB/s          : {len(datos) / seg_total / 1e6:.1f}")
    print(f"  con sellos    : {cA.pts + cB.pts} PTS + {cA.pcr + cB.pcr} PCR "
          f"= {(cA.pts + cB.pts + cA.pcr + cB.pcr) / pkt_total:.2%} de los paquetes")
    for canales in (10, 16):
        mbps = 2.6 * canales          # medido: ~2.6 Mbps por canal
        carga = mbps / (len(datos) / seg_total * 8 / 1e6) * 100
        print(f"  {canales} canales ({mbps:.0f} Mbps) -> {carga:.1f}% de un nucleo")
    print("\nsalidas:", sin, con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
