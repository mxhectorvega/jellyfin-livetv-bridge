"""Etapa 5 — SALIDA (playlist M3U).

Genera ``/data/playlist.m3u``, el artefacto que Jellyfin importa como tuner.

- Discussion: cada entrada apunta al proxy interno ``/stream/<id>`` del propio
  servicio (no a la URL del origen). Eso es lo que permite copiar el flujo,
  aplicar el perfil de transcodificacion y no exponer credenciales de la
  fuente al cliente.
- Important: ``tvg-id`` escrito en el M3U es el MISMO string que el ``id`` del
  canal en el XMLTV. Si no empatan, la guia no aparece.
"""

from __future__ import annotations

import urllib.parse

from .config import Settings
from .models import Channel
from .util import get_logger, write_atomic

log = get_logger("playlist")


def m3u_attr(valor: str) -> str:
    """Sanea un valor para un atributo ``clave="valor"`` del M3U.

    - Discussion: los nombres, logos y grupos vienen de listas de TERCEROS
      (Pluto y compania). Una comilla doble o un salto de linea en un
      ``tvg-name`` rompe el archivo entero; un salto de linea, ademas, permite
      inyectar una entrada falsa con su propia URL, porque el M3U es un formato
      de una linea por campo.
    - Important: la comilla se degrada a apostrofo y los espacios (incluidos los
      saltos) se colapsan. No se recorta el contenido util.
    """
    return " ".join(str(valor or "").replace('"', "'").split())


def channel_url(channel: Channel, settings: Settings, use_proxy: bool = True, use_relay: bool = False) -> str:
    """URL que se publica para un canal.

    - Discussion: con ``use_proxy`` la URL es ``{base}/stream/{id}``; sin el,
      se publica la URL original. Preferimos el proxy: mantiene la sesion y
      los tokens del lado del servidor, y da un punto donde aplicar el
      perfil de transcodificacion.
    - Note: con ``use_relay`` la URL es ``{base}/relay/{id}``, que es la ruta
      del rele con colchon propio (repara huecos y re-resuelve el master).
    - Important: el id va URL-encoded; los canales traen acentos y parentesis.
    """
    if not use_proxy:
        return channel.source_url
    base = settings.public_base
    if not base:
        base = f"http://127.0.0.1:{settings.port}"
    ruta = "relay" if use_relay else "stream"
    return f"{base}/{ruta}/{urllib.parse.quote(channel.id)}"


def build_m3u(channels: list[Channel], settings: Settings, use_proxy: bool = True, use_relay: bool = False) -> str:
    """Construye el texto del M3U.

    - Important: solo salen canales habilitados y con validacion accesible.
      Los protegidos (DRM/cifrado) se pueden incluir marcados si
      ``include_protected`` esta activo, pero no reproducen por copia.
    """
    lines = ["#EXTM3U"]
    emitted = 0
    for ch in channels:
        if not ch.enabled:
            continue
        lines.append(
            f'#EXTINF:-1 tvg-id="{m3u_attr(ch.tvg_id)}" '
            f'tvg-name="{m3u_attr(ch.tvg_name or ch.name)}" '
            f'tvg-logo="{m3u_attr(ch.logo)}" '
            f'group-title="{m3u_attr(ch.group)}",{m3u_attr(ch.name)}'
        )
        lines.append(channel_url(ch, settings, use_proxy=use_proxy, use_relay=use_relay))
        emitted += 1
    log.info("playlist construida", extra={"count": emitted})
    return "\n".join(lines) + "\n"


def catalogs_of(channels: list[Channel]) -> dict[str, int]:
    """Conteo de canales por catalogo (para /catalogs y el switcher)."""
    counts: dict[str, int] = {}
    for ch in channels:
        key = (ch.catalog or "sin-catalogo").strip().lower()
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def select_for_output(channels: list[Channel], settings: Settings, catalog: str | None = None) -> list[Channel]:
    """Filtra la lista que sale al M3U segun catalogo activo y salud.

    - Discussion: quedan fuera los deshabilitados por el health-check y, si
      ``include_protected`` es falso, los protegidos. Si hay catalogo activo,
      SOLO salen los canales de ese catalogo: los tres se alimentan y validan,
      pero Jellyfin ve uno.
    - Note: ``catalog=None`` lee el activo (archivo /data > .env); pasar "" para
      forzar "todos juntos".
    """
    target = settings.resolve_active_catalog() if catalog is None else catalog.strip().lower()
    out: list[Channel] = []
    for ch in channels:
        if not ch.enabled:
            continue
        if target and (ch.catalog or "").strip().lower() != target:
            continue
        protected = ch.probe.drm
        if protected and not settings.include_protected:
            continue
        out.append(ch)
    return out


def set_active_catalog(settings: Settings, catalog: str) -> str:
    """Persiste el catalogo visible (``todos`` para publicarlos juntos)."""
    value = (catalog or "").strip().lower() or "todos"
    write_atomic(settings.active_catalog_file, value + "\n")
    return value


def write_playlist(channels: list[Channel], settings: Settings, use_proxy: bool = True, use_relay: bool = False) -> int:
    """Escribe ``/data/playlist.m3u`` de forma atomica. Devuelve el conteo."""
    selected = select_for_output(channels, settings)
    text = build_m3u(selected, settings, use_proxy=use_proxy, use_relay=use_relay)
    write_atomic(settings.playlist_file, text)
    return sum(1 for line in text.splitlines() if line.startswith("#EXTINF"))


def write_active_outputs(channels: list[Channel], bundle, settings: Settings) -> dict[str, object]:
    """Escribe M3U + XMLTV SOLO del catalogo visible.

    - Discussion: es la operacion que se repite al arrancar, al terminar cada
      pipeline y al cambiar de catalogo por API. No re-descarga la guia: usa el
      ``EpgBundle`` ya cargado en memoria.
    - Important: si el catalogo activo queda vacio (todos sus canales caidos o
      protegidos), NO se sobrescribe el M3U: un artefacto viejo es mejor que uno
      vacio que Jellyfin ya adopto.
    """
    from .epg import render_xmltv  # import local: evita ciclo de importacion

    target = settings.resolve_active_catalog()
    selected = select_for_output(channels, settings, catalog=target)
    result: dict[str, object] = {
        "catalogo": target or "todos",
        "canales": len(selected),
        "con_guia": sum(1 for ch in selected if ch.epg_id),
        "m3u_escrito": False,
        "epg_escrito": False,
    }
    if not selected:
        log.warning("catalogo activo sin canales: no se sobrescribe el M3U", extra={"detail": target or "todos"})
        return result

    write_playlist(selected, settings, use_relay=settings.relay_enabled)
    result["m3u_escrito"] = True

    if bundle is not None:
        try:
            render_xmltv(selected, bundle, settings)
            result["epg_escrito"] = True
        except ValueError as exc:
            log.warning("guia no escrita", extra={"detail": str(exc)})
    return result
