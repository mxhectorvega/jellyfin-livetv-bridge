"""Utilidades compartidas: HTTP, subprocess, escritura atomica y logs JSON.

- Discussion: el proyecto no usa `requests` ni `httpx` en runtime. Todo el
  HTTP sale de la stdlib (`urllib`) para mantener las dependencias al minimo.
- Important: toda escritura de artefactos (`playlist.m3u`, `epg.xml`,
  `state.json`) es ATOMICA (temporal + ``os.replace``): el consumidor puede
  estar leyendo justo cuando el pipeline escribe, y con reemplazo atomico
  nunca ve un archivo a medias.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

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
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    req = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} en {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"fallo de red en {url}: {exc.reason}") from exc
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
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
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
def write_atomic(path: str | Path, data: str | bytes) -> Path:
    """Escribe un archivo de forma atomica (temporal + ``os.replace``)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = data.encode("utf-8") if isinstance(data, str) else data
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
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
