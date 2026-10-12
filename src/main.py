"""Etapa 9 — ORQUESTADOR.

Une las etapas y arranca el servicio:

    ingest -> probe -> normalize -> epg -> playlist -> API + health

- Discussion: el pipeline se corre una vez al arrancar y luego se repite cada
  ``epg.refresh_hours`` (configurable). El monitor de salud corre en paralelo
  y sus cambios se reflejan al vuelo en ``/playlist.m3u``.
- Important: si una corrida sale pobre (sin canales, sin guia), NO se
  sobreescriben los artefactos buenos: un archivo viejo de una semana es
  mejor que uno vacio que el consumidor ya adopto.
"""

from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

import uvicorn

from .api import AppState, create_app, fuentes_de_canal, resolver_de_canal
from .config import Settings
from .epg import refresh_epg
from .health import HealthMonitor
from .ingest import ingest
from .models import Channel
from .normalize import aplicar_respaldos, normalize
from .playlist import select_for_output, write_active_outputs
from .probe import check_channels
from .util import get_logger, now_iso, setup_logging, write_atomic

log = get_logger("main")

# Orden de operaciones del pipeline. Cada etapa es independiente salvo por el
# orden: validar ANTES de normalizar permite deduplicar conservando el canal
# que de verdad responde.
PIPELINE_STAGES = ("ingest", "probe", "normalize", "epg", "playlist")


async def run_pipeline(state: AppState) -> dict[str, Any]:
    """Corre el pipeline completo una vez y persiste el estado.

    - Important: es seguro llamarlo en paralelo: el ``lock`` del estado
      serializa las corridas.
    """
    async with state.lock:
        state.pipeline_running = True
        report: dict[str, Any] = {"inicio": now_iso()}
        try:
            cfg = state.cfg
            settings = state.settings

            channels = await asyncio.to_thread(ingest, settings, cfg)
            report["ingeridos"] = len(channels)

            channels = [ch for ch in channels if ch.source_url]
            channels = await asyncio.to_thread(check_channels, channels, settings)
            report["accesibles"] = sum(1 for ch in channels if ch.probe.accessible)

            channels, norm_report = normalize(
                channels, settings, check_logos=bool(cfg.get("check_logos"))
            )
            channels = aplicar_respaldos(channels, cfg.get("respaldo"))
            report["normalizacion"] = norm_report

            epg_report, bundle = await asyncio.to_thread(refresh_epg, settings, cfg, channels)
            state.epg_bundle = bundle
            report["epg"] = epg_report
            report["sin_guia"] = [ch.name for ch in channels if not ch.epg_id][:200]

            state.put_channels(channels)

            if channels:
                try:
                    written = write_active_outputs(channels, bundle, settings)
                    report["publicado"] = written
                except Exception as exc:  # noqa: BLE001 — no romper la corrida por la salida
                    log.error("no se pudo escribir la salida", extra={"detail": str(exc)})
                    report["error_salida"] = str(exc)
            else:
                log.warning("corrida sin canales: no se sobrescribe la playlist")

            # El rele tarda ~3.5 s en llenar su primer segmento y el sondeo de
            # Jellyfin solo espera 3 s: se precalientan los canales publicados
            # para que al pedir /relay/<id> el colchon ya este lleno.
            # TOPE: cada rele sostiene ~330 KB/s de bajada continua; sin tope,
            # cambiar al catalogo de Pluto (198 canales) abre 198 lectores y
            # satura el enlace. El resto arranca on-demand.
            if channels and settings.relay_enabled and settings.relay_precalentar:
                try:
                    publicados = select_for_output(channels, settings)
                    objetivo = publicados[: max(0, settings.relay_precalentar_max)]
                    for ch in objetivo:
                        state.relay_pool.get(
                            ch.id,
                            fuentes_de_canal(ch),
                            settings.relay_user_agent,
                            resolver=resolver_de_canal(ch, settings),
                            remendar=settings.relay_remendar,
                            paso_cuadro=settings.relay_paso_cuadro,
                        )
                    report["reles_precalentados"] = len(objetivo)
                    report["reles_omitidos"] = max(0, len(publicados) - len(objetivo))
                    report["reles_con_respaldo"] = sum(1 for ch in objetivo if ch.backups)
                except Exception as exc:  # noqa: BLE001 — no tumbar la corrida
                    log.warning(
                        "no se pudieron precalentar los reles", extra={"detail": str(exc)[:200]}
                    )

            # Suelta los reles que ya no sirven al catalogo publicado (o que
            # llevan rato sin clientes). Sin esto, cambiar de catalogo dejaba los
            # del anterior bajando para siempre.
            if settings.relay_enabled:
                try:
                    vigentes = {ch.id for ch in select_for_output(channels, settings)}
                    apagados = state.relay_pool.barrer(vigentes, settings.relay_idle_s)
                    if apagados:
                        report["reles_apagados"] = len(apagados)
                except Exception as exc:  # noqa: BLE001
                    log.warning("barrido de reles fallido", extra={"detail": str(exc)[:200]})

            persist_state(state)
        except Exception as exc:  # noqa: BLE001 — el pipeline no debe tumbar el servicio
            log.error("pipeline fallido", extra={"detail": str(exc)})
            report["error"] = str(exc)
        finally:
            # El cierre del reporte va en el finally, pero el `return` NO:
            # un `return` dentro de `finally` se come cualquier excepcion que
            # este propagandose y enmascara el fallo real.
            report["fin"] = now_iso()
            state.last_pipeline = now_iso()
            state.last_report = report
            state.pipeline_running = False
        return report


def persist_state(state: AppState) -> None:
    """Guarda canales y salud en ``/data/state.json`` (escritura atomica)."""
    payload = {
        "generated_at": now_iso(),
        "catalogo_activo": state.settings.resolve_active_catalog() or "todos",
        "canales": [ch.as_dict() for ch in state.channel_list()],
        "salud": state.monitor.snapshot() if state.monitor else {},
    }
    try:
        write_atomic(
            state.settings.state_file,
            json.dumps(payload, ensure_ascii=False, indent=1),
            modo=0o600,
        )
    except OSError as exc:
        log.warning("no se pudo persistir el estado", extra={"detail": str(exc)})


def load_state(path) -> list[Channel]:
    """Carga canales persistidos (arranque rapido si el pipeline aun no corre)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [Channel.from_dict(item) for item in data.get("canales", [])]


async def pipeline_forever(state: AppState, interval_s: float) -> None:
    """Repite el pipeline cada ``interval_s`` segundos."""
    while True:
        await run_pipeline(state)
        await asyncio.sleep(max(300.0, interval_s))


async def barrer_forever(state: AppState, interval_s: float = 300.0) -> None:
    """Apaga relés ociosos sin esperar a la próxima corrida del pipeline.

    - Discussion: el pipeline corre cada 12 h, así que un canal que el
      health-check desactiva seguiría con su relé bajando durante horas. Este
      barrido corre al ritmo del health-check (300 s) y aplica la misma regla.
    """
    while True:
        await asyncio.sleep(max(60.0, interval_s))
        try:
            settings = state.settings
            vigentes = {ch.id for ch in select_for_output(state.channel_list(), settings)}
            state.relay_pool.barrer(vigentes, settings.relay_idle_s)
        except Exception as exc:  # noqa: BLE001 — el barrido nunca tumba nada
            log.warning("barrido de reles fallido", extra={"detail": str(exc)[:200]})


def canales_para_salud(state: AppState) -> list[Channel]:
    """Canales que vigila el health-check en esta vuelta.

    - Discussion: mientras el pipeline corre (una corrida completa cada 12 h,
      ~11 min sobre 208 canales) ya se validan TODOS los canales. Dejar que el
      monitor los sondee a la vez triplica la carga sobre los mismos CDN, y la
      medicion del propio proyecto dice que la concurrencia alta hace que
      devuelvan manifiestos vacios (1/12 accesibles con 6 hilos contra 22/24 con
      2). Con la lista vacia, ``check_all`` no hace nada y el monitor espera a la
      vuelta siguiente.
    - Note: el pipeline aplica la misma sonda y el mismo backoff, asi que no se
      pierde vigilancia: solo se deja de duplicarla.
    """
    return [] if state.pipeline_running else state.channel_list()


async def _serve(state: AppState) -> None:
    app = create_app(state)
    # bind_ip manda sobre host: LVB_BIND_IP=10.0.0.21 publica SOLO en la LAN.
    # Vacio => LVB_HOST (0.0.0.0 por omision).
    host = state.settings.bind_ip or state.settings.host
    config = uvicorn.Config(
        app,
        host=host,
        port=state.settings.port,
        log_level=state.settings.log_level.lower(),
        access_log=False,
    )
    log.info("api escuchando", extra={"detail": f"{host}:{state.settings.port}"})
    server = uvicorn.Server(config)
    await server.serve()


def build_state() -> AppState:
    """Arma el estado inicial: settings, config y monitor (sin red)."""
    from .ingest import load_config_file

    pre = Settings.from_env()
    try:
        cfg = load_config_file(pre.sources_file)
    except FileNotFoundError:
        log.warning(
            "no hay sources.yml; se usara /config/sources.example.yml como ejemplo",
            extra={"detail": str(pre.sources_file)},
        )
        cfg = {}
    settings = Settings.from_env(cfg.get("settings") or {})
    settings.ensure_dirs()
    state = AppState(settings=settings, cfg=cfg)
    state.monitor = HealthMonitor(settings)
    return state


async def main_async() -> None:
    """Arranque completo del servicio."""
    state = build_state()
    setup_logging(state.settings.log_level, state.settings.log_format)
    log.info(
        "jellyfin-livetv-bridge arrancando",
        extra={"detail": f"config={state.settings.config_dir} data={state.settings.data_dir}"},
    )

    # Estado previo: sirve canales mientras corre el primer pipeline.
    state.put_channels(load_state(state.settings.state_file))

    # El pipeline se enlaza al estado para que /reload pueda dispararlo.
    state.cfg["_run_pipeline"] = run_pipeline

    refresh_hours = float((state.cfg.get("epg") or {}).get("refresh_hours") or 12)
    interval_s = max(3600.0, refresh_hours * 3600.0)

    tasks = [
        asyncio.create_task(pipeline_forever(state, interval_s)),
        asyncio.create_task(state.monitor.run_forever(lambda: canales_para_salud(state))),
        asyncio.create_task(barrer_forever(state, state.settings.health_interval_s)),
        asyncio.create_task(_serve(state)),
    ]

    stop = asyncio.Event()

    def _signal(*_args: Any) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except NotImplementedError:  # pragma: no cover — solo en plataformas sin señales
            pass

    await stop.wait()
    log.info("apagando servicio")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # Los hilos del rele son daemon, pero cerrarlos aqui apaga la bajada de cada
    # canal en el apagado ordenado (y deja el log limpio).
    state.relay_pool.detener_todos()


def main() -> None:
    """Entrada del servicio (``python -m src.main``)."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
