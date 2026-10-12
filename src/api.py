"""Etapa 8 — API HTTP (FastAPI).

Endpoints:

    GET  /health               liveness del servicio
    GET  /status               estado por canal, config y reportes
    GET  /channels             catalogo en JSON
    GET  /playlist.m3u         M3U para Jellyfin (tuner tipo m3u)
    GET  /epg.xml              XMLTV para Jellyfin (listing provider xmltv)
    GET  /stream/{channel_id}  flujo MPEG-TS continuo (copy o transcodificado)
    POST /reload               dispara el pipeline otra vez

- Discussion: el estado es un objeto compartido en memoria
  (:class:`AppState`) que el pipeline rellena y el monitor de salud actualiza.
  ``/playlist.m3u`` se genera AL VUELO desde ese estado: asi el health-check
  que desactiva un canal se refleja sin reescribir archivos.
- Important: ``/stream`` es el unico punto donde se invoca ffmpeg. El
  proceso se cierra si el cliente se desconecta.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from . import __version__
from .config import Settings
from .health import HealthMonitor
from .models import Channel
from .playlist import build_m3u, catalogs_of, select_for_output, set_active_catalog, write_active_outputs
from .relay import RelayPool
from .transcode import build_ffmpeg_cmd, can_transcode, plan_for
from .util import get_logger, now_iso

log = get_logger("api")


@dataclass
class AppState:
    """Estado compartido entre pipeline, monitor de salud y API."""

    settings: Settings
    channels: dict[str, Channel] = field(default_factory=dict)
    monitor: HealthMonitor | None = None
    cfg: dict[str, Any] = field(default_factory=dict)
    epg_bundle: Any = None          # EpgBundle en memoria: permite cambiar de
                                    # catalogo sin re-descargar la guia
    last_pipeline: str = ""
    last_report: dict[str, Any] = field(default_factory=dict)
    pipeline_running: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    relay_pool: RelayPool = field(default_factory=RelayPool)

    def channel_list(self) -> list[Channel]:
        return list(self.channels.values())

    def put_channels(self, channels: list[Channel]) -> None:
        self.channels = {ch.id: ch for ch in channels}

    def get_channel(self, channel_id: str) -> Channel | None:
        if channel_id in self.channels:
            return self.channels[channel_id]
        # Tolerancia: el id puede venir URL-encoded o con acentos.
        decoded = urllib.parse.unquote(channel_id)
        return self.channels.get(decoded)


def fuentes_de_canal(channel: Channel) -> list[str]:
    """Principal + respaldos, en orden de preferencia (sin entradas vacías)."""
    crudas = [channel.source_url] + list(channel.backups or [])
    return [u for u in crudas if u]


def resolver_de_canal(channel: Channel, settings: Settings):
    """Devuelve la URL viva de la fuente ``idx`` del canal.

    - Discussion: TV Azteca rota su ``access_token``; si el relé guardara la
      URL resuelta una sola vez, al vencer el token el canal moriría. Con este
      resolver, cada re-resolución pide token fresco. ``idx`` 0 es la principal
      y los demás son los respaldos declarados en ``sources.yml``.
    - Important: ``forzar=True`` pide token NUEVO saltando la caché; el relé lo
      usa cuando el intento anterior dio 401/403, porque el cacheado ya murió.
    """
    from .azteca import reescribir_url

    fuentes = fuentes_de_canal(channel)

    def _resolver(idx: int = 0, forzar: bool = False) -> str:
        url = fuentes[max(0, min(int(idx), len(fuentes) - 1))]
        return reescribir_url(
            url, settings.azteca_local_base, settings.azteca_enabled, forzar=forzar
        )

    return _resolver


def create_app(state: AppState) -> FastAPI:
    """Fabrica la app FastAPI enlazada a un :class:`AppState`."""
    app = FastAPI(title="jellyfin-livetv-bridge", version=__version__)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        active = sum(1 for ch in state.channel_list() if ch.enabled)
        return {
            "status": "ok",
            "version": __version__,
            "canales": len(state.channels),
            "activos": active,
            "pipeline": state.pipeline_running,
            "last_pipeline": state.last_pipeline,
        }

    @app.get("/status")
    async def status() -> dict[str, Any]:
        monitor = state.monitor.snapshot() if state.monitor else {}
        channels = []
        for ch in state.channel_list():
            channels.append(
                {
                    "id": ch.id,
                    "nombre": ch.name,
                    "grupo": ch.group,
                    "habilitado": ch.enabled,
                    "motivo": ch.disabled_reason,
                    "tvg_id": ch.tvg_id,
                    "epg_id": ch.epg_id,
                    "logo": bool(ch.logo),
                    "accesible": ch.probe.accessible,
                    "video": ch.probe.video_codec,
                    "audio": ch.probe.audio_codec,
                    "resolucion": ch.probe.resolution,
                    "bitrate": ch.probe.video_bitrate,
                    "cifrado": ch.probe.encrypted,
                    "drm": ch.probe.drm,
                    "token_firmado": ch.probe.signed_token,
                    "auth_requerida": ch.probe.auth_required,
                    "error": ch.probe.error,
                }
            )
        return {
            "version": __version__,
            "catalogo_activo": state.settings.resolve_active_catalog() or "todos",
            "catalogos": catalogs_of(state.channel_list()),
            "config": state.settings.redacted(),
            "reportes": state.last_report,
            "salud": monitor,
            "canales": channels,
        }

    @app.get("/channels")
    async def channels_json() -> list[dict[str, Any]]:
        return [ch.as_dict() for ch in state.channel_list()]

    @app.get("/catalogs")
    async def catalogs() -> dict[str, Any]:
        """Catalogos disponibles, conteo por catalogo y cual esta visible."""
        channels = state.channel_list()
        active = state.settings.resolve_active_catalog()
        counts = catalogs_of(channels)
        return {
            "activo": active or "todos",
            "catalogs": counts,
            "publicados": len(select_for_output(channels, state.settings, catalog=active)),
            "habilitados": sum(1 for ch in channels if ch.enabled),
        }

    @app.post("/catalog/{name}")
    async def set_catalog(name: str) -> dict[str, Any]:
        """Cambia el catalogo visible y reescribe M3U + XMLTV al vuelo.

        - Discussion: no re-ingesta ni re-valida: usa el estado en memoria. En
          Jellyfin hay que disparar despues "Actualizar canales" y
          "Refresh Guide" para que la parrilla tome el cambio.
        """
        target = urllib.parse.unquote(name).strip().lower()
        channels = state.channel_list()
        counts = catalogs_of(channels)
        if target not in ("", "todos", "all", "*") and target not in counts:
            raise HTTPException(
                status_code=404,
                detail=f"catalogo desconocido: {target}. disponibles: {sorted(counts)}",
            )
        set_active_catalog(state.settings, target)
        result = write_active_outputs(channels, state.epg_bundle, state.settings)
        log.info("catalogo cambiado", extra={"detail": str(result)})
        return {"ok": True, "activo": state.settings.resolve_active_catalog() or "todos", **result}

    @app.get("/playlist.m3u")
    async def playlist() -> PlainTextResponse:
        text = build_m3u(
            select_for_output(state.channel_list(), state.settings),
            state.settings,
            use_relay=state.settings.relay_enabled,
        )
        return PlainTextResponse(text, media_type="audio/x-mpegurl")

    @app.get("/epg.xml")
    async def epg_xml() -> FileResponse:
        path = state.settings.epg_file
        if not path.exists():
            raise HTTPException(status_code=404, detail="epg.xml aun no generado")
        return FileResponse(path, media_type="application/xml", filename="epg.xml")

    @app.post("/reload")
    async def reload_pipeline() -> dict[str, Any]:
        if state.pipeline_running:
            return {"status": "en_curso"}
        runner = state.cfg.get("_run_pipeline")
        if runner is None:
            raise HTTPException(status_code=503, detail="pipeline no enlazado")
        asyncio.create_task(runner(state))
        return {"status": "disparado"}

    @app.get("/relay/status")
    async def relay_status() -> dict[str, Any]:
        """Estado de los relés vivos (colchón, re-resoluciones, error)."""
        return state.relay_pool.stats()

    @app.get("/relay/{channel_id}")
    async def relay_path(channel_id: str) -> StreamingResponse:
        """Flujo continuo desde el relé con colchón propio.

        - Discussion: a diferencia de ``/stream`` (que lanza ffmpeg contra el
          origen y entrega lo que salga), aquí un hilo lee el HLS por su cuenta
          y mantiene un colchón: el hueco del origen se absorbe y una variante
          muerta se re-resuelve sola. Los segmentos ya son MPEG-TS, así que se
          concatenan tal cual — sin ffmpeg, sin re-timestamping.
        """
        channel = state.get_channel(channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="canal desconocido")
        if not channel.enabled:
            raise HTTPException(status_code=409, detail=f"canal desactivado: {channel.disabled_reason}")
        if channel.probe.drm:
            raise HTTPException(status_code=451, detail="DRM (CENC/SAMPLE-AES): no reproducible")

        relay = state.relay_pool.get(
            channel.id,
            fuentes_de_canal(channel),
            state.settings.relay_user_agent,
            resolver=resolver_de_canal(channel, state.settings),
        )
        return StreamingResponse(relay.servir(), media_type="video/mp2t")

    @app.get("/azteca/{clave}")
    async def azteca(clave: str) -> RedirectResponse:
        """Resuelve el token de TV Azteca y redirige al master autenticado.

        - Discussion: misma funcion que hacia el servicio systemd en :8582,
          ahora dentro del bridge. Un cliente (o el catalogo, si algun dia se
          reapunta) puede consumir esta ruta y seguir el 302.
        """
        from .azteca import CANALES, resolver_url

        key = urllib.parse.unquote(clave).strip().lower()
        if key not in CANALES:
            raise HTTPException(status_code=404, detail=f"canal no listado: {key}. hay: {sorted(CANALES)}")
        try:
            destino = resolver_url(key)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"sin token: {str(exc)[:150]}") from exc
        log.info("token de Azteca resuelto", extra={"channel": key})
        return RedirectResponse(url=destino, status_code=302)

    @app.get("/stream/{channel_id}")
    async def stream(channel_id: str) -> StreamingResponse:
        channel = state.get_channel(channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="canal desconocido")
        if not channel.enabled:
            raise HTTPException(status_code=409, detail=f"canal desactivado: {channel.disabled_reason}")
        if channel.probe.drm:
            raise HTTPException(status_code=451, detail="DRM (CENC/SAMPLE-AES): no reproducible")

        plan = plan_for(channel, state.settings)
        if not plan.is_copy and not can_transcode(state.settings):
            raise HTTPException(status_code=503, detail="limite de transcodificaciones alcanzado")
        # Fuente de TV Azteca integrada: si la URL del canal apunta al proveedor
        # local de token, el bridge la resuelve aqui (token fresco) y ffmpeg
        # recibe el master autenticado.
        from .azteca import reescribir_url

        origen = reescribir_url(
            channel.source_url, state.settings.azteca_local_base, state.settings.azteca_enabled
        )
        cmd = build_ffmpeg_cmd(origen, plan, state.settings)

        async def generator():
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            log.info(
                "stream abierto",
                extra={"channel": channel.name, "detail": plan.reason or "copy"},
            )
            try:
                assert proc.stdout is not None
                while True:
                    chunk = await proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
                log.info("stream cerrado", extra={"channel": channel.name, "code": proc.returncode})

        return StreamingResponse(generator(), media_type="video/mp2t")

    return app
