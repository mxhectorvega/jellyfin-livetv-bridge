"""Fuente de TV Azteca integrada al bridge (token de mediastream).

- Discussion: TV Azteca publica su senal en su propia plataforma (mediastream /
  mdstrm.com). El master exige un ``access_token`` que CADUCA y ROTA en cada
  llamada al GraphQL, asi que en el catalogo del usuario la URL del canal apunta
  a un proveedor local (``http://127.0.0.1:8582/azteca7``) que responde un 302
  con un token fresco.

  Este modulo absorbe esa funcion: el bridge resuelve el token el mismo, sin
  servicio systemd aparte ni un puerto extra. La reescritura se aplica en el
  momento de USAR la URL (validacion y /stream), nunca en el M3U publicado.

- Important: la tabla de canales lleva el id de la plataforma. Si TV Azteca
  cambia un id, el canal deja de resolver: el error se ve como ``sin token``
  en /status, no como un fallo silencioso.
- Note: el token se cachea 240 s (el original pedia uno fresco por peticion; el
  token vive mas que eso y el sondeo del health-check es frecuente).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from .util import get_logger

log = get_logger("azteca")

GRAPHQL = "https://next.mediastream.co/graphql"
CLIENTE = "66f7768a8cc83114d50a126865c543525563006b6b620cd9d9"
TTL_TOKEN_S = 240.0
TIMEOUT_S = 25.0
BASE_MASTER = "https://mdstrm.com/live-stream-playlist/{id}.m3u8?access_token={tok}"

# clave publica -> (id de la plataforma, nombre)
CANALES: dict[str, tuple[str, str]] = {
    "azteca7": ("67da2962c4eed1a805b30e61", "Azteca 7"),
    "azteca-intl": ("65a1d2fd7e7f14355550d570", "Azteca Internacional"),
}

_cache: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def pedir_token(clave: str, timeout: float = TIMEOUT_S, forzar: bool = False) -> str:
    """Devuelve un ``access_token`` fresco (o cacheado) para la clave dada.

    - Throws: ``KeyError`` si la clave no esta en :data:`CANALES`;
      ``RuntimeError`` si el GraphQL no devuelve token.
    - Important: ``forzar=True`` salta la cache. Hace falta porque el token
      puede morir ANTES de los 240 s: medido, un token cacheado que mdstrm ya
      habia rotado devuelve 401 y, sin forzar, se vuelve a pedir el MISMO
      token muerto y el canal no se recupera nunca.
    """
    ident, nombre = CANALES[clave]
    ahora = time.time()
    if not forzar:
        with _lock:
            guardado = _cache.get(clave)
            if guardado and ahora - guardado[1] < TTL_TOKEN_S:
                return guardado[0]

    cuerpo = json.dumps({"query": f'query{{getLive(_id:"{ident}"){{accessToken}}}}'}).encode()
    req = urllib.request.Request(
        GRAPHQL,
        data=cuerpo,
        method="POST",
        headers={"Content-Type": "application/json", "x-client-id": CLIENTE},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GraphQL inaccesible: {exc}") from exc

    token = ((data.get("data") or {}).get("getLive") or {}).get("accessToken")
    if not token:
        raise RuntimeError(f"sin token para {nombre}: {json.dumps(data)[:160]}")
    with _lock:
        _cache[clave] = (token, ahora)
    return token


def invalidar(clave: str | None = None) -> None:
    """Tira el token cacheado (de una clave o de todas)."""
    with _lock:
        if clave is None:
            _cache.clear()
        else:
            _cache.pop(clave, None)


def resolver_url(clave: str, forzar: bool = False) -> str:
    """URL del master autenticado para una clave de :data:`CANALES`."""
    ident, _nombre = CANALES[clave]
    return BASE_MASTER.format(id=ident, tok=pedir_token(clave, forzar=forzar))


def clave_de_url(url: str, base_local: str) -> str | None:
    """Extrae la clave de canal si la URL apunta al proveedor local.

    - Discussion: en el catalogo del usuario la URL es
      ``http://127.0.0.1:8582/azteca7``. Se compara por prefijo contra
      ``base_local`` (configurable) y se devuelve la clave solo si esta en
      :data:`CANALES`; cualquier otra URL devuelve ``None`` y se deja intacta.
    """
    if not url or not base_local:
        return None
    prefijo = base_local.rstrip("/")
    if not url.startswith(prefijo):
        return None
    clave = url[len(prefijo) :].split("?")[0].strip("/").split("/")[0].lower()
    return clave if clave in CANALES else None


def reescribir_url(url: str, base_local: str, habilitado: bool = True, forzar: bool = False) -> str:
    """Devuelve la URL real del stream, resolviendo el token si hace falta.

    - Important: si el token no se puede obtener, devuelve la URL ORIGINAL y
      loguea el fallo. Asi el error aparece como "inaccesible" en la
      validacion (honesto) en vez de romper el pipeline.
    - Note: ``forzar=True`` pide token NUEVO, saltando la cache. Se usa cuando
      el intento anterior devolvio 401/403: el token cacheado ya no sirve.
    """
    if not habilitado:
        return url
    clave = clave_de_url(url, base_local)
    if not clave:
        return url
    try:
        return resolver_url(clave, forzar=forzar)
    except Exception as exc:  # noqa: BLE001 — nunca tumbar por un token
        log.error(
            "no se pudo resolver el token", extra={"channel": clave, "detail": str(exc)[:200]}
        )
        return url
