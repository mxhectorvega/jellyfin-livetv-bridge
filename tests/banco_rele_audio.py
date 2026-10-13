"""El rele, contra un canal REAL de los que traen el audio aparte.

No prueba el muxer: prueba el CAMINO COMPLETO del rele —resolver el master,
detectar la rendicion de audio, bajar los dos segmentos, muxear y servir— que es
lo que ve Jellyfin.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from src.relay import RelayHLS  # noqa: E402

TMP = Path("/home/server/tmp_lvb/rele_audio")
TMP.mkdir(parents=True, exist_ok=True)
CUANTOS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

scan = json.loads(Path("/home/server/tmp_lvb/pluto_scan.json").read_text(encoding="utf-8"))
aparte = [f for f in scan if f.get("elegida_audio")][:CUANTOS]
st = {
    c["id"]: c
    for c in json.loads(
        Path("/home/server/jellyfin-livetv-bridge/data/state.json").read_text(encoding="utf-8")
    )["canales"]
}


def streams(ruta):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "csv=p=0",
            str(ruta),
        ],
        capture_output=True,
        timeout=60,
    ).stdout.decode()
    return [x for x in out.splitlines() if x.strip()]


ok = fallo = 0
for f in aparte:
    c = st.get(f["id"])
    if not c:
        continue
    nombre = str(f["name"])[:36]
    r = RelayHLS([c["source_url"]], remendar=True, nombre=f["id"])
    r.arrancar()
    salida = TMP / f"{ok + fallo}.ts"
    try:
        t0 = time.time()
        with salida.open("wb") as fh:
            datos = bytearray()
            for trozo in r.servir(espera_max_s=12.0):
                fh.write(trozo)
                datos += trozo
                if len(datos) >= 3 * 1024 * 1024 or time.time() - t0 > 40:
                    break
        stt = r.stats()
        ss = streams(salida)
        tiene_v = any("video" in s for s in ss)
        tiene_a = any("audio" in s for s in ss)
        bien = tiene_v and tiene_a and stt["muxeados"] > 0
        ok += 1 if bien else 0
        fallo += 0 if bien else 1
        print(
            f"  {'OK ' if bien else 'MAL'} {nombre:38} {len(datos) // 1024:5} KB "
            f"muxeados={stt['muxeados']:3} sin_audio={stt['sin_audio']:2} "
            f"rendicion={stt['rendicion_audio']} {ss}"
        )
    except Exception as exc:
        fallo += 1
        print(f"  ERR {nombre:38} {str(exc)[:60]}")
    finally:
        r.detener()
        salida.unlink(missing_ok=True)

print()
print(f"=== rele: {ok} con audio | {fallo} sin audio o fallidos (de {len(aparte)}) ===")
sys.exit(0 if fallo == 0 and ok > 0 else 1)
