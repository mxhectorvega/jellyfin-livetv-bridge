"""Token fresco de Pluto TV para las URLs del stitcher.

- Discussion: las listas M3U de Pluto traen un ``jwt`` incrustado en cada URL.
  Ese jwt es un token de SESION con ~24 h de vida: cuando vence, el stitcher
  responde ``HTTP 401 JWT verification fails`` y el canal queda muerto aunque
  la lista siga publicada. El rele re-resuelve el origen tras un 401, pero el
  resolver devolvia la MISMA URL estatica con el jwt vencido: no habia forma de
  refrescarlo. Este modulo acuna un token nuevo contra ``boot.pluto.tv`` y
  reescribe el ``jwt`` de la URL, conservando host y ruta del canal.
- Important: el token es de sesion (sirve para todos los canales), no por
  canal. Se cachea y se renueva con margen antes de vencer. El mint corre bajo
  lock: dos workers de sonda no deben pedir dos tokens a la vez.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
import uuid

from .util import get_logger

log = get_logger("pluto_token")

BOOT_URL = "https://boot.pluto.tv/v4/start"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Margen de renovacion: si al token le quedan menos de 30 min, se pide otro.
MARGEN_S = 1800

_lock = threading.Lock()
# Cache POR CLAVE. El stitcher de Pluto cierra la sesion cuando el mismo jwt se
# usa en canales distintos; el rele de cada canal pide su propio token con
# cache_key=<id del canal>. La sonda del pipeline usa la clave global "".
_caches: dict[str, dict] = {}


def _mint(force: bool = False, cache_key: str = "") -> dict:
    """Devuelve un token vigente (cacheado) y sus stitcherParams."""
    with _lock:
        ahora = time.time()
        _cache = _caches.setdefault(cache_key, {"token": "", "params": "", "exp": 0.0})
        if not force and _cache["token"] and (_cache["exp"] - ahora) > MARGEN_S:
            return _cache
        params = {
            "appName": "web",
            "appVersion": "8.0.0",
            "deviceVersion": "122.0.0",
            "deviceModel": "web",
            "deviceMake": "chrome",
            "deviceType": "web",
            "clientID": str(uuid.uuid4()),
            "clientModelNumber": "1.0.0",
            "serverSideAds": "false",
            "dnt": "0",
            "country": "MX",
            "language": "es",
        }
        url = BOOT_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Origin": "https://pluto.tv",
                "Referer": "https://pluto.tv/",
            },
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
        token = str(data.get("sessionToken") or "")
        stitcher = str(data.get("stitcherParams") or "")
        if not token or not stitcher:
            raise RuntimeError("boot.pluto.tv no devolvio sessionToken/stitcherParams")
        exp = _exp_del_jwt(token)
        _cache.update({"token": token, "params": stitcher, "exp": exp})
        log.info("token de Pluto renovado", extra={"detail": f"exp={exp:.0f}"})
        return _cache


def _exp_del_jwt(token: str) -> float:
    """Lee el ``exp`` del payload del JWT sin validar la firma."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import base64

        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return float(exp) if exp else time.time() + 3600.0
    except Exception:  # noqa: BLE001
        return time.time() + 3600.0


def refresh_url(url: str, force: bool = False, cache_key: str = "") -> str:
    """Devuelve la URL con un jwt fresco si es un stitcher de Pluto.

    Cualquier otra URL se devuelve intacta. Si el mint falla, se devuelve la
    original: es preferible un 401 visible a tumbar la sonda o el rele.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if "pluto.tv" not in host or "/stitch/hls/channel/" not in parsed.path:
            return url
        c = _mint(force=force, cache_key=cache_key)
        query = (
            c["params"]
            + "&jwt="
            + urllib.parse.quote(c["token"], safe="")
            + "&masterJWTPassthrough=true"
        )
        return urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, "")
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("no se pudo refrescar el token de Pluto", extra={"detail": str(exc)[:200]})
        return url


def refresh_channels(channels) -> int:
    """Reescribe in-place el source_url de los canales de Pluto. Devuelve cuantos."""
    n = 0
    for ch in channels:
        original = getattr(ch, "source_url", "") or ""
        nuevo = refresh_url(original)
        if nuevo != original:
            ch.source_url = nuevo
            n += 1
    return n
