"""Etapa 1 — INGESTA.

Lee ``/config/sources.yml`` (aportado por el usuario) y construye la lista de
canales a procesar. Soporta dos origenes:

- ``sources:``  listas M3U/HLS externas o locales (se descargan y parsean)
- ``channels:`` canales sueltos con metadatos completos

- Important: los canales declarados explicitamente en ``channels:`` tienen
  prioridad sobre los de las listas cuando comparten ``tvg_id`` o nombre.
- Note: la lista en ``/config`` es del USUARIO. El repositorio solo trae
  ``sources.example.yml`` con datos ficticios.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

try:  # PyYAML es la unica dependencia de parseo
    import yaml
except ImportError as exc:  # pragma: no cover — error de entorno, no de logica
    raise SystemExit("Falta PyYAML: pip install -r requirements.txt") from exc

from .config import Settings
from .models import Channel
from .util import get_logger, http_get

log = get_logger("ingest")

# Atributos de #EXTINF: clave="valor" (tvg-id, tvg-name, tvg-logo, group-title...)
_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"')
# Nombre visible: lo que va despues de la ultima coma del #EXTINF
_NAME_RE = re.compile(r",\s*(.+?)\s*$")


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Carga el YAML de configuracion y valida que la raiz sea un mapa.

    - Throws: ``FileNotFoundError`` si no existe; ``ValueError`` si la raiz no
      es un diccionario.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"no existe {file_path}")
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{file_path}: la raiz del YAML debe ser un mapa")
    return data


def parse_m3u(text: str, source_name: str = "", defaults: dict[str, Any] | None = None) -> list[Channel]:
    """Convierte el texto de una lista M3U en canales.

    - Discussion: el parser es tolerante: una linea ``#EXTINF`` sin atributos
      vale igual, y las lineas en blanco no importan porque cada ``#EXTINF``
      va pegado a su URL. Se empareja ``#EXTINF`` con la SIGUIENTE linea que
      empiece con ``http`` (o ``rtsp``/``rtmp``/``udp``).
    - Important: ademas de ``#EXTINF``, se respetan las directivas
      ``#EXTVLCOPT``/``#KODIPROP`` solo para no confundirlas con URLs.
    """
    defaults = defaults or {}
    channels: list[Channel] = []
    pending: dict[str, str] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = {k.lower(): v for k, v in _ATTR_RE.findall(line)}
            name_match = _NAME_RE.search(line)
            attrs["_name"] = name_match.group(1) if name_match else attrs.get("tvg-name", "")
            pending = attrs
            continue
        if line.startswith("#"):
            # Directivas de reproductor: se ignoran a proposito.
            continue
        if line.lower().startswith(("http://", "https://", "rtsp://", "rtmp://", "udp://", "rtp://")):
            name = (pending or {}).get("_name") or _name_from_url(line)
            attrs = pending or {}
            channels.append(
                Channel(
                    name=name.strip() or _name_from_url(line),
                    source_url=line,
                    tvg_id=(attrs.get("tvg-id") or "").strip(),
                    tvg_name=(attrs.get("tvg-name") or "").strip(),
                    group=(attrs.get("group-title") or defaults.get("default_group") or "General").strip(),
                    logo=(attrs.get("tvg-logo") or "").strip(),
                    country=defaults.get("country", ""),
                    language=defaults.get("language", ""),
                    source=source_name or defaults.get("name", ""),
                    catalog=defaults.get("catalog", ""),
                )
            )
            pending = None
    return channels


def _name_from_url(url: str) -> str:
    """Nombre de respaldo cuando el #EXTINF no trae uno util.

    - Discussion: los HLS cuelgan de nombres genericos (``master.m3u8``,
      ``index.m3u8``, ``playlist.m3u8``); en ese caso se sube un nivel de la
      ruta para sacar el nombre real del canal.
    """
    genericos = {"master", "index", "playlist", "live", "stream", "manifest", "chunklist", "media"}
    partes = [p for p in url.split("?")[0].rstrip("/").split("/") if p]
    if not partes:
        return "Canal sin nombre"
    ultima = re.sub(r"\.(m3u8|m3u|ts|mpd)$", "", partes[-1], flags=re.I)
    if ultima.lower() in genericos and len(partes) >= 2:
        ultima = partes[-2]
    return ultima.replace("_", " ").replace("-", " ").strip() or "Canal sin nombre"


def channels_from_config(cfg: dict[str, Any]) -> list[Channel]:
    """Extrae la lista ``channels:`` del YAML ya cargado."""
    out: list[Channel] = []
    for item in cfg.get("channels") or []:
        if not isinstance(item, dict) or not item.get("url"):
            log.warning("entrada de channels: ignorada (sin url)", extra={"detail": str(item)[:120]})
            continue
        out.append(
            Channel(
                name=str(item.get("name") or _name_from_url(str(item["url"]))),
                source_url=str(item["url"]),
                tvg_id=str(item.get("tvg_id") or item.get("tvg-id") or ""),
                tvg_name=str(item.get("tvg_name") or item.get("tvg-name") or ""),
                group=str(item.get("group") or "General"),
                logo=str(item.get("logo") or ""),
                country=str(item.get("country") or ""),
                language=str(item.get("language") or ""),
                source="config",
                catalog=str(item.get("catalog") or "manual").strip().lower(),
                profile=str(item.get("profile") or "auto"),
            )
        )
    return out


def channels_from_sources(sources: Iterable[dict[str, Any]]) -> list[Channel]:
    """Descarga y parsea cada lista M3U declarada en ``sources:``."""
    out: list[Channel] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        if not src.get("enabled", True):
            continue
        name = str(src.get("name") or "lista")
        url = str(src.get("url") or "")
        if not url:
            log.warning("fuente sin url", extra={"channel": name})
            continue
        try:
            raw = http_get(
                url,
                timeout=float(src.get("timeout_s") or 20),
                headers={str(k): str(v) for k, v in (src.get("headers") or {}).items()},
            )
        except Exception as exc:  # noqa: BLE001 — una fuente caida no debe tumbar el resto
            log.error("no se pudo leer la fuente", extra={"channel": name, "url": url, "detail": str(exc)})
            continue
        if raw[:2] == b"\x7f\x00" or raw[:1] == b"{":  # binario o JSON inesperado
            log.warning("fuente con contenido inesperado", extra={"channel": name, "detail": raw[:40].hex()})
        defaults = {
            "name": name,
            "country": src.get("country", ""),
            "language": src.get("language", ""),
            "default_group": src.get("default_group", "General"),
            # El catalogo sale de la fuente (o de su nombre): es lo que permite
            # alimentar y validar los tres y publicar solo uno a la vez.
            "catalog": str(src.get("catalog") or name).strip().lower(),
        }
        parsed = parse_m3u(raw.decode("utf-8", "replace"), source_name=name, defaults=defaults)
        log.info("fuente leida", extra={"channel": name, "count": len(parsed), "url": url})
        out.extend(parsed)
    return out


def ingest(settings: Settings, cfg: dict[str, Any] | None = None) -> list[Channel]:
    """Ejecuta la etapa de ingesta completa.

    - Discussion: lee el YAML (o usa ``cfg`` precargado, util en pruebas),
      recolecta listas y canales sueltos, y devuelve la union. La dedupe real
      vive en :mod:`src.normalize`; aqui solo se respeta que los declarados
      explicitamente vayan PRIMERO para ganar el empate.
    """
    if cfg is None:
        cfg = load_config_file(settings.sources_file)

    explicit = channels_from_config(cfg)
    listed = channels_from_sources(cfg.get("sources") or [])
    total = explicit + listed
    log.info("ingesta terminada", extra={"count": len(total)})
    return total
