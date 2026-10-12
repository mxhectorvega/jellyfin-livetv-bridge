"""Etapa 3 — NORMALIZACION.

Limpia nombres, deduplica y asigna grupos/logos.

- Discussion: la dedupe es conservadora. Clave primaria: ``tvg-id``
  normalizado (minusculas, sin ``@SD``/``@HD``, solo alfanumerico). Si no hay
  tvg-id, se usa el nombre normalizado. Nada de fuzzy matching: un falso
  positivo aqui borra un canal bueno.
- Important: el ``id`` del canal y su ``tvg_id`` final son ESTABLES entre
  corridas. Jellyfin deriva el id de canal del ``tvg-id`` del M3U: si cambia,
  el tuner tiene que re-importarse y los favoritos/guia se pierden.
"""

from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from .config import Settings
from .models import Channel
from .util import get_logger, http_head_status

log = get_logger("normalize")

DEFAULT_STRIP_PATTERNS = [
    r"\s*\(\d{3,4}p\)",
    r"\s*\[(HD|FHD|SD|UHD|4K)\]",
    r"\s*\b(FHD|UHD|4K)\b",
]

_WS_RE = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    """Quita diacriticos preservando la letra base (NFKD)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def clean_name(name: str, patterns: list[str] | None = None) -> str:
    """Limpia el nombre visible de un canal.

    - Discussion: recorta marcas de calidad y ruido tipico de las listas
      (``(720p)``, ``[HD]``, ``FHD``) y normaliza espacios. Nunca toca el
      nombre mas alla de los patrones configurados.
    """
    text = _WS_RE.sub(" ", (name or "").strip())
    for pattern in (patterns or DEFAULT_STRIP_PATTERNS):
        try:
            text = re.sub(pattern, "", text, flags=re.I)
        except re.error:
            log.warning("patron de limpieza invalido", extra={"detail": pattern})
    limpio = _WS_RE.sub(" ", text).strip(" -|")
    if not limpio:
        # El fallback al original es DELIBERADO (un canal sin nombre visible es
        # peor que uno con la etiqueta pegada), pero deja rastro: el caso real
        # es un nombre compuesto SOLO por una etiqueta de calidad ("(720p)").
        log.warning("nombre compuesto solo de etiquetas de calidad", extra={"channel": name})
        return (name or "").strip()
    return limpio


def normalize_tvg_id(tvg_id: str) -> str:
    """Normaliza un ``tvg-id`` para comparar y para publicar.

    - Discussion: quita sufijos de calidad (``@SD``/``@HD``), pasa a
      minusculas y deja solo alfanumericos y punto/guion. Sin esta
      normalizacion el empate con una guia externa es 0 de N.
    """
    text = (tvg_id or "").strip().lower()
    text = re.sub(r"@(sd|hd|fhd|uhd|4k)$", "", text)
    text = re.sub(r"[^a-z0-9.\-]+", "", text)
    return text.strip(".-")


def name_key(name: str) -> str:
    """Clave de comparacion por nombre: sin acentos, minusculas, alfanumerico."""
    text = strip_accents((name or "").lower())
    return re.sub(r"[^a-z0-9]+", "", text)


def make_id(channel: Channel, used: set[str]) -> str:
    """Genera un id estable y unico para el canal.

    - Discussion: preferimos el ``tvg_id`` normalizado; si no hay, el nombre
      normalizado. Ante colision se agrega un sufijo numerico, que es
      determinista mientras el orden de entrada lo sea.
    """
    base = normalize_tvg_id(channel.tvg_id) or name_key(channel.name) or "canal"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}-{index}"
        index += 1
    used.add(candidate)
    return candidate


def dedupe(channels: list[Channel]) -> tuple[list[Channel], int]:
    """Deduplica por ``tvg-id``/nombre conservando el PRIMERO.

    - Discussion: el orden importa: la ingesta pone primero los canales
      declarados explicitamente en ``channels:``, asi que esos ganan el
      empate frente a los de una lista externa.
    - Returns: ``(canales_unicos, eliminados)``.
    """
    seen: dict[str, Channel] = {}
    out: list[Channel] = []
    removed = 0
    for ch in channels:
        key = normalize_tvg_id(ch.tvg_id) or name_key(ch.name)
        if not key:
            out.append(ch)
            continue
        if key in seen:
            removed += 1
            continue
        seen[key] = ch
        out.append(ch)
    return out, removed


def apply_group_filters(channels: list[Channel], patterns: list[str]) -> tuple[list[Channel], int]:
    """Descarta canales cuyo grupo case con algun patron (regex completo).

    - Important: el patron se evalua contra el nombre COMPLETO del grupo. Un
      filtro de subcadena arrastra grupos de paises que solo comparten letras.
    """
    if not patterns:
        return channels, 0
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.I))
        except re.error:
            log.warning("patron de grupo invalido", extra={"detail": pattern})
    kept: list[Channel] = []
    dropped = 0
    for ch in channels:
        if any(rx.match(ch.group or "") for rx in compiled):
            dropped += 1
            continue
        kept.append(ch)
    return kept, dropped


def validate_logos(channels: list[Channel], timeout: float = 6.0, workers: int = 8) -> int:
    """Comprueba los logos con HEAD y vacia los que no responden 2xx/3xx.

    - Discussion: un logo roto en Jellyfin se ve como placeholder. Igual que
      con ffprobe, es preferible medir a confiar en la lista. No bloquea el
      pipeline: si un logo no responde, se deja vacio y se sigue.
    """

    def check(ch: Channel) -> Channel:
        if not ch.logo or not ch.logo.lower().startswith("http"):
            if ch.logo:
                ch.logo = ""
            return ch
        status = http_head_status(ch.logo, timeout=timeout)
        if status == 0 or status >= 400:
            log.info("logo descartado", extra={"channel": ch.name, "code": status})
            ch.logo = ""
        return ch

    invalid = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for ch in pool.map(check, channels):
            if not ch.logo:
                invalid += 1
    return invalid


def aplicar_respaldos(channels: list[Channel], respaldos: dict | None) -> list[Channel]:
    """Asocia fuentes alternas a cada canal.

    - Discussion: en ``sources.yml`` el bloque ``respaldo:`` va de clave a
      lista de URLs. La clave se compara contra el ``id`` del canal (que ya es
      el tvg-id normalizado) y, si no, contra el nombre normalizado — así se
      puede escribir con el nombre visible o con el id.
    - Important: el orden importa: el relé usa la primera como principal y
      salta a la siguiente tras ``FALLOS_PARA_CAMBIAR`` fallos seguidos.
    """
    indice: dict[str, list[str]] = {}
    for clave, urls in (respaldos or {}).items():
        if not urls:
            continue
        llave = normalize_tvg_id(str(clave)) or name_key(str(clave))
        if llave:
            indice[llave] = [str(u) for u in urls if u]
    if not indice:
        return channels
    puestos = 0
    for ch in channels:
        urls = indice.get(ch.id) or indice.get(normalize_tvg_id(ch.tvg_id)) or indice.get(name_key(ch.name))
        if urls:
            ch.backups = urls
            puestos += 1
    log.info("respaldos asociados", extra={"detail": f"{puestos} canales con fuente alterna"})
    return channels


def normalize(channels: list[Channel], settings: Settings, check_logos: bool = False) -> tuple[list[Channel], dict[str, int]]:
    """Etapa de normalizacion completa.

    - Discussion: limpieza de nombres -> filtro de grupos -> dedupe -> ids
      estables -> logos. Devuelve tambien el reporte de cuantas entradas se
      eliminaron en cada paso, que se expone en ``/status``.
    """
    report = {"entrada": len(channels)}

    for ch in channels:
        original = ch.name
        ch.name = clean_name(ch.name, settings.strip_patterns or None)
        ch.tvg_name = clean_name(ch.tvg_name or ch.name, settings.strip_patterns or None)
        if ch.name != original:
            report["nombres_limpiados"] = report.get("nombres_limpiados", 0) + 1
        ch.tvg_id = normalize_tvg_id(ch.tvg_id)
        ch.group = (ch.group or "General").strip() or "General"

    channels, dropped_groups = apply_group_filters(channels, settings.group_exclude)
    report["grupos_descartados"] = dropped_groups

    channels, removed = dedupe(channels)
    report["duplicados"] = removed

    used: set[str] = set()
    for ch in channels:
        ch.id = make_id(ch, used)
        ch.tvg_id = ch.tvg_id or ch.id          # tvg-id publicado = id estable
        ch.epg_id = ""                          # lo asigna la etapa EPG al empatar

    if check_logos:
        report["logos_invalidos"] = validate_logos(channels, workers=settings.probe_workers)

    report["salida"] = len(channels)
    log.info("normalizacion terminada", extra={"count": len(channels), "detail": str(report)})
    return channels, report
