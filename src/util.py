"""Utilidades compartidas: HTTP, subprocess, escritura atomica y logs JSON.

- Discussion: el proyecto no usa `requests` ni `httpx` en runtime. Todo el
  HTTP sale de la stdlib (`http.client` sobre un pool de conexiones REUSABLES;
  antes `urllib.request`, que abre una conexion por peticion) para mantener las
  dependencias al minimo.
- Important: toda escritura de artefactos (`playlist.m3u`, `epg.xml`,
  `state.json`) es ATOMICA (temporal + ``os.replace``): el consumidor puede
  estar leyendo justo cuando el pipeline escribe, y con reemplazo atomico
  nunca ve un archivo a medias.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import zlib
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_USER_AGENT = "jellyfin-livetv-bridge/0.1 (+https://example.invalid)"
DEFAULT_TIMEOUT = 20.0
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024  # 64 MB: guias grandes pero no infinitas
# Tope de la SALIDA ya descomprimida, mayor que el de entrada a proposito: la
# guia de EPGTalk Latino pesa ~10.5 MB comprimida y 109 MB descomprimida, y
# recortarla a 64 MB romperia la guia de Imagen TV. Lo que se corta es la
# expansion desmedida (medido: 203,860 B de gzip -> 200 MB, 1029x).
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """Formateador de logs en JSON, una linea por evento.

    - Discussion: los logs del servicio son JSON para poder filtrarlos con
      ``jq`` y para que el health-check deje rastro estructurado.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("channel", "event", "url", "code", "detail", "count"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configura el logger raiz una sola vez (idempotente)."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Tiempo
# ---------------------------------------------------------------------------
def now_iso() -> str:
    """Timestamp ISO-8601 en UTC, segundos de precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def monotonic() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _gunzip_acotado(data: bytes, max_bytes: int, url: str) -> bytes:
    """Descomprime gzip con TOPE sobre la SALIDA, no solo sobre la entrada.

    - Discussion: el tope de ``max_bytes`` se aplicaba al comprimido, asi que el
      pico residente quedaba sin limite: medido, un gzip de 203,860 B expande a
      200 MB (1029x). Se descomprime con ``zlib`` pidiendo como maximo
      ``max_bytes + 1`` bytes y se exige que no quede cola.
    - Throws: ``RuntimeError`` si la salida excede ``max_bytes``.
    """
    obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
    plano = obj.decompress(data, max_bytes + 1)
    if len(plano) > max_bytes or obj.unconsumed_tail:
        raise RuntimeError(f"gzip expande por encima de {max_bytes} bytes en {url}")
    return plano


# ---------------------------------------------------------------------------
# Conexiones HTTP(S) REUSABLES
# ---------------------------------------------------------------------------
# - Discussion: `urllib.request.urlopen` abre (y cierra) UNA conexion por
#   peticion, y como Python no cachea `getaddrinfo`, cada una paga tambien una
#   resolucion DNS. Medido 2026-11-24 desde CT201: el sondeo del rele y sobre
#   todo el health-check (203 canales cada 5 min, ~4 peticiones por canal)
#   generaban ~14 consultas DNS/s; detras de un firewall que registra el trafico
#   de salida, ~2.9 M lineas/dia (~1.3 GB de log) y el rootfs de 20 GB lleno.
#   Con la conexion viva, una peticion vuelve a costar lo que una peticion.
# - Note: el cache es `threading.local` — una conexion por (hilo, esquema, host,
#   puerto). El lector de cada rele y los hilos del executor de probe/health se
#   reusan, asi que no hace falta lock y no hay contencion. Al morir el hilo sus
#   sockets se liberan con el objeto local.
# - Important: para que la conexion se pueda reusar hay que LEER el cuerpo
#   entero (o descartarla). Una respuesta a medio leer deja el socket inservible
#   y el siguiente `request()` falla: por eso `peticion` suelta la conexion
#   cuando quedo cola.
_local = threading.local()
# Cuantas redirecciones se siguen antes de rendirse. `urlopen` las seguia solo;
# con http.client hay que hacerlo a mano o un 302 se convierte en error.
REDIRECT_MAX = 5
_REDIRECT = (301, 302, 303, 307, 308)


def conexiones() -> dict:
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = {}
        _local.conns = cache
    return cache


def partir_url(url: str) -> tuple[str, str, int, str]:
    """(esquema, host, puerto, ruta?query) de una URL."""
    partes = urllib.parse.urlsplit(url)
    esquema = (partes.scheme or "http").lower()
    host = partes.hostname or ""
    puerto = partes.port or (443 if esquema == "https" else 80)
    ruta = partes.path or "/"
    if partes.query:
        ruta += "?" + partes.query
    return esquema, host, puerto, ruta


def conexion(scheme: str, host: str, port: int, timeout: float):
    """La conexion viva de ese destino, creandola la primera vez."""
    cache = conexiones()
    clave = (scheme, host, port)
    conn = cache.get(clave)
    if conn is None:
        clase = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = clase(host, port, timeout=timeout)
        cache[clave] = conn
    else:
        conn.timeout = timeout
    return conn


def soltar_conexiones(scheme: str = "", host: str = "", port: int = 0) -> None:
    """Cierra (y olvida) la conexion de ese destino; sin destino, todas."""
    cache = conexiones()
    claves = list(cache) if not host else [(scheme, host, port)]
    for clave in claves:
        conn = cache.pop(clave, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — cerrar nunca debe tumbar al llamador
                pass


def error_http(url: str, estado: int, cabeceras: dict | None = None) -> urllib.error.HTTPError:
    """Un `HTTPError` con `code`/`headers`, como los que lanzaba `urlopen`.

    - Discussion: al dejar de usar urllib hay que levantar el MISMO tipo de
      excepcion o quien decida con `exc.code in (401, 403, 404, 410)` pierde la
      deteccion en silencio.
    """
    return urllib.error.HTTPError(url, estado, f"HTTP {estado}", cabeceras or {}, None)


def peticion(
    url: str,
    metodo: str = "GET",
    cabeceras: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> tuple[int, dict, bytes]:
    """Peticion HTTP(S) REUSANDO la conexion. Devuelve (estado, cabeceras, cuerpo).

    - Discussion: sustituye a `urlopen` en todo el proyecto. Sigue redirecciones
      hasta ``REDIRECT_MAX`` y NO levanta por 4xx/5xx: devuelve el estado y cada
      llamador decide que hacer (unos quieren `HTTPError`, otros un
      ``RuntimeError`` con el texto de siempre).
    - Note: si el cuerpo trae mas de ``max_bytes`` la conexion se DESCARTA —no
      se puede reusar una respuesta a medio leer— y se devuelve lo leido.
    - Throws: ``RuntimeError`` si una redireccion no trae ``Location`` (respuesta
      malformada) o si se encadenan mas de ``REDIRECT_MAX`` saltos.
    """
    cabeceras = dict(cabeceras or {})
    cabeceras["Connection"] = "keep-alive"
    destino = url
    for _ in range(REDIRECT_MAX + 1):
        scheme, host, port, ruta = partir_url(destino)
        resp = None
        for intento in (0, 1):
            conn = conexion(scheme, host, port, timeout)
            try:
                conn.request(metodo, ruta, headers=cabeceras)
                resp = conn.getresponse()
                break
            except Exception:
                # Conexion muerta, o a medio usar porque el servidor la cerro:
                # se descarta y se reintenta UNA vez con una nueva.
                soltar_conexiones(scheme, host, port)
                if intento:
                    raise
        estado = int(resp.status)
        if estado in _REDIRECT:
            salto = resp.getheader("Location")
            resp.read(max_bytes)
            if not salto:
                soltar_conexiones(scheme, host, port)
                raise RuntimeError(f"redireccion sin Location ({estado}): {destino}")
            nuevo = urllib.parse.urljoin(destino, salto)
            if partir_url(nuevo)[:3] != (scheme, host, port):
                soltar_conexiones(scheme, host, port)
            destino = nuevo
            continue
        cuerpo = resp.read(max_bytes)
        if resp.read(1):
            soltar_conexiones(scheme, host, port)  # habia mas cuerpo: no se reusa
        return estado, dict(resp.headers), cuerpo
    raise RuntimeError(f"demasiadas redirecciones ({REDIRECT_MAX}): {url}")


def http_get(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    byte_range: str | None = None,
    max_decompressed: int = MAX_DECOMPRESSED_BYTES,
) -> bytes:
    """Descarga una URL y devuelve bytes (descomprime gzip si aplica).

    - Discussion: acepta rutas absolutas de archivo ademas de http(s). Las
      guias XMLTV suelen venir como ``.xml.gz``; se detecta por el magic
      number (``\\x1f\\x8b``) y no por la extension, porque hay servidores que
      sirven gzip sin decirlo.
    - Important: ``byte_range`` (p.ej. ``"bytes=0-262143"``) permite pedir solo
      el inicio de un SEGMENTO en vez de bajarlo entero. Un segmento real pesa
      2-4 MB y con un limite de 2 MB se reportaba como inaccesible: falso
      negativo que marcaba de muerto medio catalogo.
    - Throws: ``RuntimeError`` si la respuesta es HTTP >= 400 o si excede
      ``max_bytes``.
    """
    if url.startswith("/") and not url.startswith("//"):
        with open(url, "rb") as fh:
            return fh.read(max_bytes)

    request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    request_headers.update(headers or {})
    if byte_range:
        request_headers["Range"] = byte_range
    try:
        estado, _, data = peticion(url, "GET", request_headers, timeout, max_bytes + 1)
    except urllib.error.HTTPError as exc:  # no deberia: peticion no levanta por 4xx
        raise RuntimeError(f"HTTP {exc.code} en {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"fallo de red en {url}: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001 — se conserva el texto de siempre
        raise RuntimeError(f"fallo de red en {url}: {exc}") from exc
    if estado >= 400:
        raise RuntimeError(f"HTTP {estado} en {url}")
    if len(data) > max_bytes:
        raise RuntimeError(f"respuesta mayor a {max_bytes} bytes en {url}")
    if data[:2] == b"\x1f\x8b":
        data = _gunzip_acotado(data, max_decompressed, url)
    return data


def http_head_status(url: str, timeout: float = DEFAULT_TIMEOUT) -> int:
    """Devuelve el codigo HTTP de un HEAD (0 si no se pudo conectar).

    - Important: un ``0`` es fallo de CONEXION, no de ruta; no se lee como
      "el recurso no existe".
    """
    try:
        estado, _, _ = peticion(
            url, "HEAD", {"User-Agent": DEFAULT_USER_AGENT}, timeout, MAX_DOWNLOAD_BYTES
        )
        return int(estado)
    except Exception:  # noqa: BLE001 — cualquier fallo de red es "0"
        return 0


# ---------------------------------------------------------------------------
# Subprocess
# ---------------------------------------------------------------------------
def run_cmd(
    cmd: Sequence[str],
    timeout: float = DEFAULT_TIMEOUT,
    stdin_data: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Ejecuta un comando y devuelve ``(returncode, stdout, stderr)``.

    - Discussion: nunca lanza por timeout; devuelve code ``-9`` y el stderr
      con el motivo, para que el llamador decida. Los comandos se pasan
      siempre como lista (nunca ``shell=True``): las URLs de estos canales
      traen caracteres que el shell interpretaria.
    """
    try:
        proc = subprocess.run(
            list(cmd),
            input=stdin_data,
            capture_output=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return -9, exc.stdout or b"", (exc.stderr or b"") + b"\n[timeout]"
    except FileNotFoundError as exc:
        return -2, b"", str(exc).encode()


# ---------------------------------------------------------------------------
# Archivos
# ---------------------------------------------------------------------------
def write_atomic(path: str | Path, data: str | bytes, modo: int | None = None) -> Path:
    """Escribe un archivo de forma atomica (temporal + ``os.replace``).

    - Note: ``modo`` fija los permisos del destino (p. ej. ``0o600`` para
      ``state.json``, que trae las URLs de origen). Sin el, manda el ``umask``
      del proceso, que en el contenedor deja 644.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = data.encode("utf-8") if isinstance(data, str) else data
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    if modo is not None:
        os.chmod(tmp, modo)
    os.replace(tmp, target)
    return target


def read_text(path: str | Path, limit: int | None = None) -> str:
    """Lee un archivo de texto (UTF-8 tolerante)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return text if limit is None else text[:limit]


def chunked(seq: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """Trocea un iterable en listas de a lo sumo ``size`` elementos."""
    batch: list[Any] = []
    for item in seq:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
