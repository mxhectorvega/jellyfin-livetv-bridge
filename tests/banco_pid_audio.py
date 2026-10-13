"""Cuantos PIDs de audio distintos sirve el rele, medidos a lo largo del flujo.

Antes: el PID se elegia por segmento, asi que bailaba. Despues: uno solo por
canal. Un reproductor que se quede con el primer PMT nota la diferencia.
"""

import json
import sys
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from src.ts_mux import es_de, leer_pmt, pids_usados  # noqa: E402

B = "http://10.0.0.21:8581"
TMP = Path("/tmp/audiopid")
TMP.mkdir(exist_ok=True)


def trae(ruta):
    return json.loads(urllib.request.urlopen(B + ruta, timeout=20).read())


def pids_de_audio(datos: bytes) -> set[int]:
    """PIDs declarados como audio (stream_type 0x0F) en cualquier PMT del jale."""
    salida = set()
    for pid in pids_usados(datos):
        if pid == 0x0000:  # el PAT no declara ES
            continue
        pmt = leer_pmt(datos, pid)
        if pmt is None:
            continue
        for tipo, epid in es_de(pmt):
            if tipo == 0x0F:
                salida.add(epid)
    return salida


reles = trae("/relay/status")
# solo los que muxean
objetivo = [cid for cid, v in reles.items() if v.get("rendicion_audio")]
print(f"reles que muxean: {len(objetivo)}")

cuantos = int(sys.argv[1]) if len(sys.argv) > 1 else 3
for cid in objetivo[:cuantos]:
    f = TMP / f"{cid[:12]}.ts"
    try:
        with urllib.request.urlopen(f"{B}/relay/{cid}", timeout=30) as r, f.open("wb") as fh:
            import time

            t0 = time.time()
            while time.time() - t0 < 25:
                trozo = r.read(65536)
                if not trozo:
                    break
                fh.write(trozo)
    except Exception as exc:  # noqa: BLE001
        print(f"  {cid[:14]} no jalo: {str(exc)[:50]}")
        continue
    datos = f.read_bytes()
    pids = pids_de_audio(datos)
    st = reles[cid]
    print(
        f"  {cid[:14]} {len(datos) // 1024:5} KB | PIDs de audio vistos: "
        f"{sorted(hex(p) for p in pids)} | pid_audio del rele: {hex(st.get('pid_audio') or 0)} "
        f"| muxeados={st.get('muxeados')}"
    )
    f.unlink(missing_ok=True)
