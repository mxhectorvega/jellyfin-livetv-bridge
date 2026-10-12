"""Etapa 4 — GUIA (EPG).

Genera un XMLTV propio a partir de fuentes publicas configurables y mapea cada
canal a un id de guia.

- Discussion: el empate se hace por ``tvg-id`` explicito (bloque
  ``epg.sources[].mappings``) y, si no hay, por NOMBRE normalizado. Los ids de
  la guia y los del M3U casi nunca coinciden por si solos: cada fuente usa su
  propio espacio de nombres. Como este servicio escribe AMBOS artefactos, se
  reescribe el XMLTV con los ids propios del catalogo y el empate queda
  garantizado por construccion.
- Important: un XMLTV que parsea NO prueba que Jellyfin vaya a unir la guia a
  un canal. Lo unico que lo decide es que ``<channel id>`` y ``tvg-id`` sean
  el MISMO string. Sin empate, la guia se descarga, se ve sana y no aparece en
  ninguna parte.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import Settings
from .models import Channel
from .normalize import name_key
from .util import get_logger, http_get, write_atomic

log = get_logger("epg")

MAX_PROGRAMMES_PER_CHANNEL = 3000
WINDOW_PAST_HOURS = 6
WINDOW_FUTURE_DAYS = 7
# Centinela para ordenar programas sin ``start`` utilizable: van primero y el
# chequeo de traslape los deja pasar (no se puede comparar lo que no se parsea).
EPOCH_MIN = datetime(1970, 1, 1, tzinfo=timezone.utc)

# ``YYYYMMDDHHMMSS`` seguido de nada, de ``Z`` o de ``+HHMM``/``-HHMM`` (con o
# sin espacio en medio). El offset se captura en grupos SEPARADOS (signo, horas,
# minutos) para no volver a depender de posiciones fijas.
_TIME_RE = re.compile(r"^(\d{14})\s*(?:(Z)|([+-])(\d{2})(\d{2}))?$")

# Declarar ENTIDADES dentro del propio documento es la via de la "bomba XML"
# (expansion exponencial: medido, ~250 B de entrada produjeron 10,000 caracteres
# de salida en ElementTree). Un XMLTV legitimo puede traer
# ``<!DOCTYPE tv SYSTEM "xmltv.dtd">``, pero NO necesita declarar entidades, asi
# que se rechaza solo eso y no el DOCTYPE.
_ENTIDAD_RE = re.compile(rb"<!\s*ENTITY", re.I)


@dataclass
class EpgBundle:
    """Guia cruda de todas las fuentes, indexada para el empate."""

    channels: dict[str, list[str]] = field(default_factory=dict)   # id -> nombres
    programmes: dict[str, list[ET.Element]] = field(default_factory=dict)  # id -> <programme>
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)


def _parse_time(text: str | None) -> datetime | None:
    """Parsea un timestamp XMLTV (``YYYYMMDDHHMMSS [+/-HHMM]`` / ``...Z``).

    - Discussion: el offset se lee con una regex y NO por posicion fija. La
      version anterior miraba ``raw[-5]`` y tomaba ``raw[-6:]`` como offset, asi
      que en ``20260101120000 -0600`` el "offset" salia ``" -0600"`` (con el
      espacio dentro del grupo) y el resultado era **-01:00** en vez de -06:00;
      en ``20260101120000+0200`` salia -00:20. Como las fuentes cableadas hoy
      publican ``+0000`` (que por casualidad salia correcto) el fallo estaba
      dormido: la primera fuente que publique hora local desfasa la guia horas.
    - Important: sin marca de zona se asume UTC, que es lo que declara el DTD de
      XMLTV para el caso sin offset.
    - Returns: ``datetime`` con zona, o ``None`` si el texto no es parseable.
    """
    if not text:
        return None
    match = _TIME_RE.match(text.strip())
    if not match:
        return None
    try:
        base = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    if match.group(3):                       # offset explicito +HHMM / -HHMM
        delta = timedelta(hours=int(match.group(4)), minutes=int(match.group(5)))
        return base.replace(tzinfo=timezone(-delta if match.group(3) == "-" else delta))
    return base.replace(tzinfo=timezone.utc)  # ``Z`` o sin marca: UTC


def parse_xmltv(xml_bytes: bytes) -> EpgBundle:
    """Parsea un XMLTV y lo indexa por id de canal.

    - Important: los programas se agrupan por canal para poder filtrar sin
      recorrer el arbol entero otra vez, y se recorta la ventana temporal
      (de ``WINDOW_PAST_HOURS`` atras a ``WINDOW_FUTURE_DAYS`` adelante) para
      no arrastrar guias de 260 MB.
    """
    bundle = EpgBundle()
    if _ENTIDAD_RE.search(xml_bytes):
        raise ValueError(
            "XMLTV con declaracion de ENTIDADES: se rechaza (bomba XML). "
            "Ninguna guia legitima necesita entidades internas."
        )
    root = ET.fromstring(xml_bytes)
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(hours=WINDOW_PAST_HOURS)
    latest = now + timedelta(days=WINDOW_FUTURE_DAYS)

    for node in root.findall("channel"):
        cid = node.get("id") or ""
        if not cid:
            continue
        names = [n.text.strip() for n in node.findall("display-name") if n.text]
        bundle.channels.setdefault(cid, []).extend(names)

    for node in root.findall("programme"):
        cid = node.get("channel") or ""
        if not cid:
            continue
        stop = _parse_time(node.get("stop"))
        start = _parse_time(node.get("start"))
        if stop and stop < earliest:
            continue
        if start and start > latest:
            continue
        bucket = bundle.programmes.setdefault(cid, [])
        if len(bucket) < MAX_PROGRAMMES_PER_CHANNEL:
            bucket.append(node)

    return bundle


def fetch_epg(sources: list[dict]) -> EpgBundle:
    """Descarga y fusiona todas las fuentes XMLTV habilitadas.

    - Discussion: ``solo_canales`` en una fuente recorta lo que se conserva del
      bundle a esos ids. Sirve para fuentes enormes de las que solo interesa un
      canal: EPGTalk Latino pesa **109 MB descompuesto** y de ahí solo se usa
      Imagen TV. El pico de parseo se paga igual (no hay parseo por streaming),
      pero lo que queda en memoria es una fraccion.
    """
    merged = EpgBundle()
    for src in sources or []:
        if not isinstance(src, dict) or not src.get("enabled", True):
            continue
        name = str(src.get("name") or "epg")
        url = str(src.get("url") or "")
        if not url:
            continue
        try:
            raw = http_get(url, timeout=float(src.get("timeout_s") or 30))
            bundle = parse_xmltv(raw)
        except Exception as exc:  # noqa: BLE001 — una guia caida no tumba el resto
            log.error("fuente EPG inaccesible", extra={"channel": name, "url": url, "detail": str(exc)})
            merged.sources_failed.append(name)
            continue

        solo = {str(c).strip() for c in (src.get("solo_canales") or []) if str(c).strip()}
        if solo:
            bundle.channels = {cid: n for cid, n in bundle.channels.items() if cid in solo}
            bundle.programmes = {cid: p for cid, p in bundle.programmes.items() if cid in solo}

        for cid, names in bundle.channels.items():
            merged.channels.setdefault(cid, [])
            for n in names:
                if n not in merged.channels[cid]:
                    merged.channels[cid].append(n)
        for cid, programmes in bundle.programmes.items():
            merged.programmes.setdefault(cid, []).extend(programmes)
        merged.sources_ok.append(name)
        log.info(
            "fuente EPG fusionada",
            extra={"channel": name, "count": sum(len(v) for v in bundle.programmes.values())},
        )
    return merged


def build_name_index(epg_channels: dict[str, list[str]]) -> dict[str, str]:
    """Indice nombre-normalizado -> id de guia (solo nombres no ambiguos).

    - Important: si dos canales de la guia comparten nombre normalizado, ese
      nombre se descarta del indice: un empate ambiguo es peor que ninguno.
    """
    index: dict[str, str] = {}
    ambiguous: set[str] = set()
    for cid, names in epg_channels.items():
        for candidate in {name_key(n) for n in names} | {name_key(cid)}:
            if not candidate:
                continue
            if candidate in index and index[candidate] != cid:
                ambiguous.add(candidate)
            else:
                index[candidate] = cid
    for key in ambiguous:
        index.pop(key, None)
    return index


def map_channels(channels: list[Channel], bundle: EpgBundle, mappings: dict[str, str] | None = None) -> dict[str, int]:
    """Asigna ``epg_id`` a cada canal y reporta cobertura.

    Orden de precedencia:
      1. mapeo explicito en ``sources.yml`` (tvg_id nuestro -> id de la guia)
      2. coincidencia exacta de id (cuando la fuente ya usa nuestro espacio)
      3. nombre normalizado, si es unico
    """
    mappings = {normalize_map_key(k): v for k, v in (mappings or {}).items()}
    name_index = build_name_index(bundle.channels)
    report = {"con_guia": 0, "sin_guia": 0}

    for ch in channels:
        target = ""
        explicit = mappings.get(ch.tvg_id) or mappings.get(normalize_map_key(ch.name))
        if explicit and explicit in bundle.channels:
            target = explicit
        elif ch.tvg_id in bundle.channels:
            target = ch.tvg_id
        elif name_index.get(name_key(ch.name)):
            target = name_index[name_key(ch.name)]

        if target:
            ch.epg_id = target
            report["con_guia"] += 1
        else:
            ch.epg_id = ""
            report["sin_guia"] += 1
    log.info("mapeo de guia terminado", extra={"detail": str(report)})
    return report


def normalize_map_key(key: str) -> str:
    """Normaliza la clave de un mapeo explicito (tvg-id o nombre)."""
    from .normalize import normalize_tvg_id

    return normalize_tvg_id(key) or name_key(key)


def channels_without_guide(channels: list[Channel]) -> list[str]:
    """Nombres de los canales que quedaron sin guia (para el reporte)."""
    return [ch.name for ch in channels if not ch.epg_id]


def render_xmltv(channels: list[Channel], bundle: EpgBundle, settings: Settings) -> str:
    """Escribe el XMLTV final en ``/data/epg.xml`` con los ids del catalogo.

    - Discussion: se re-emiten los ``<channel>`` con NUESTROS ids (los
      mapeados) y solo los ``<programme>`` de los ids que empataron. Los
      programas se deduplican por ``(canal, inicio, titulo)`` porque la fusion
      de varias fuentes suele traer el mismo programa dos veces.
    - Important: por canal se publica UNA sola parrilla. Hay fuentes que traen
      dos rejillas intercaladas para el mismo canal (medido: Canal del
      Congreso en iptv-epg MX, 27 traslapes sobre 83 programas) y Jellyfin
      dibuja el resultado como programas encimados. Se ordena por inicio y se
      descarta todo programa que empiece antes de que termine el anterior.
    """
    root = ET.Element("tv", attrib={"generator-info-name": "jellyfin-livetv-bridge"})
    emitted: set[str] = set()
    seen_programmes: set[tuple[str, str, str]] = set()
    total_programmes = 0
    descartados_traslape = 0

    for ch in channels:
        if not ch.epg_id:
            continue
        # El id que Jellyfin une es el tvg-id del M3U: se emite ESE, y los
        # programas (que vienen con el id de la fuente) se reetiquetan.
        published = ch.tvg_id or ch.epg_id
        if published not in emitted:
            node = ET.SubElement(root, "channel", attrib={"id": published})
            ET.SubElement(node, "display-name").text = ch.tvg_name or ch.name
            if ch.logo:
                ET.SubElement(node, "icon", attrib={"src": ch.logo})
            emitted.add(published)
        programas = sorted(
            bundle.programmes.get(ch.epg_id, []),
            key=lambda p: _parse_time(p.get("start")) or EPOCH_MIN,
        )
        fin_anterior: datetime | None = None
        for programme in programas:
            inicio = _parse_time(programme.get("start"))
            fin = _parse_time(programme.get("stop"))
            if fin_anterior is not None and inicio is not None and inicio < fin_anterior:
                descartados_traslape += 1
                continue
            title = (programme.findtext("title") or "").strip()
            key = (published, programme.get("start") or "", title)
            if key in seen_programmes:
                continue
            seen_programmes.add(key)
            clone = ET.fromstring(ET.tostring(programme))
            clone.set("channel", published)
            root.append(clone)
            total_programmes += 1
            if fin is not None and (fin_anterior is None or fin > fin_anterior):
                fin_anterior = fin

    if not list(root):
        # Un XMLTV vacio no debe reemplazar a uno viejo con datos.
        raise ValueError("la guia generada quedo vacia; no se sobreescribe la anterior")

    text = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
    write_atomic(settings.epg_file, text)
    log.info(
        "guia escrita",
        extra={
            "count": total_programmes,
            "detail": f"canales={len(emitted)} traslapes_descartados={descartados_traslape}",
        },
    )
    return text


def refresh_epg(settings: Settings, cfg: dict, channels: list[Channel]) -> tuple[dict[str, int], EpgBundle]:
    """Corre la etapa EPG: descarga, mapea y devuelve el bundle.

    - Discussion: NO escribe el XMLTV. La escritura la hace
      ``playlist.write_active_outputs`` con el catalogo visible, usando el
      bundle devuelto aqui. Asi cambiar de catalogo no re-descarga la guia.
    """
    epg_cfg = cfg.get("epg") or {}
    sources = epg_cfg.get("sources") or []
    mappings: dict[str, str] = {}
    for src in sources:
        mappings.update({str(k): str(v) for k, v in (src.get("mappings") or {}).items()})

    bundle = fetch_epg(sources)
    report = map_channels(channels, bundle, mappings)
    return report, bundle
