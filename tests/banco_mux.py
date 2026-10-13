"""Banco del muxeo, contra canales REALES de los que traen el audio aparte.

Usa el parser de playlists DEL PROPIO RELE (``RelayHLS._segmentos_del_playlist``)
para sacar la llave y el IV de cada segmento. Si el banco parseara por su cuenta,
banco y rele podrian divergir y la prueba diria "verde" sobre otro camino.
"""

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from src.relay import RelayHLS  # noqa: E402
from src.ts_mux import muxear  # noqa: E402

TMP = Path("/home/server/tmp_lvb/mux")
TMP.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (VLC/3.0.20)"
CUANTOS = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def trae(url):
    req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read()


def descifra(seg: bytes, ref) -> bytes:
    """Descifra UN segmento con la llave y el IV que le tocan."""
    if not ref.key_uri:
        return seg
    crudo = trae(ref.key_uri)
    if len(crudo) < 16:
        raise ValueError(f"llave de {len(crudo)} bytes")
    iv = ref.iv or f"{ref.seq:032x}"
    p = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-K", crudo[:16].hex(), "-iv", iv],
        input=seg,
        capture_output=True,
    )
    if p.returncode != 0:
        raise ValueError("openssl: " + p.stderr.decode().strip().splitlines()[-1][:70])
    return p.stdout


def streams(ruta: Path) -> list[str]:
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


def decodifica_limpio(ruta: Path) -> list[str]:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(ruta), "-f", "null", "-"],
        capture_output=True,
        timeout=90,
    )
    return [x for x in r.stderr.decode().splitlines() if x.strip()][:3]


scan = json.loads(Path("/home/server/tmp_lvb/pluto_scan.json").read_text(encoding="utf-8"))
aparte = [f for f in scan if f.get("elegida_audio")][:CUANTOS]
st = {
    c["id"]: c
    for c in json.loads(
        Path("/home/server/jellyfin-livetv-bridge/data/state.json").read_text(encoding="utf-8")
    )["canales"]
}

ok = fallo = saltado = 0
for f in aparte:
    c = st.get(f["id"])
    if not c:
        continue
    nombre = str(f["name"])[:38]
    try:
        master = trae(c["source_url"]).decode("utf-8", "replace")
        m = re.search(r'#EXT-X-MEDIA:TYPE=AUDIO[^\n]*URI="([^"]+)"', master)
        if not m:
            saltado += 1
            continue
        audio_pl = urllib.parse.urljoin(c["source_url"], m.group(1))

        infos, bw = [], 0
        for linea in master.splitlines():
            if linea.startswith("#EXT-X-STREAM-INF"):
                bw = int(re.search(r"BANDWIDTH=(\d+)", linea).group(1))
            elif linea and not linea.startswith("#"):
                infos.append((bw, urllib.parse.urljoin(c["source_url"], linea)))
        video_pl = max(infos, key=lambda p: p[0])[1]

        # el parser del rele, no uno propio
        refs_v = RelayHLS._segmentos_del_playlist(
            trae(video_pl).decode("utf-8", "replace"), video_pl
        )
        refs_a = RelayHLS._segmentos_del_playlist(
            trae(audio_pl).decode("utf-8", "replace"), audio_pl
        )
        if not refs_v or not refs_a:
            saltado += 1
            continue
        idx = min(1, len(refs_v) - 1, len(refs_a) - 1)
        sv = descifra(trae(refs_v[idx].url), refs_v[idx])
        sa = descifra(trae(refs_a[idx].url), refs_a[idx])

        unido, _rep = muxear(sv, sa)
        fm = TMP / f"m{ok}.ts"
        fm.write_bytes(unido)
        ss = streams(fm)
        tiene_v = any("video" in s for s in ss)
        tiene_a = any("audio" in s for s in ss)
        errores = decodifica_limpio(fm)
        veredicto = "OK " if (tiene_v and tiene_a and not errores) else "MAL"
        if veredicto == "OK ":
            ok += 1
        else:
            fallo += 1
        extra = f"errores: {errores}" if errores else ""
        print(f"  {veredicto} {nombre:40} {ss} {extra}")
        fm.unlink(missing_ok=True)
    except Exception as exc:
        fallo += 1
        print(f"  ERR {nombre:40} {str(exc)[:70]}")

print()
print(f"=== {ok} OK | {fallo} fallo | {saltado} saltados  (de {len(aparte)} con audio aparte) ===")
sys.exit(0 if fallo == 0 and ok > 0 else 1)
