"""Pruebas del orquestador (src/main.py).

El pipeline no tenia ninguna prueba: 306 lineas de orquestacion que solo se
ejercitaban en produccion. Aqui se prueban las piezas que SI tienen logica
propia y los dos invariantes que ya se rompieron una vez:

- el ORDEN del pipeline (normalizar antes de asociar respaldos), que invertido
  deshabilitaba canales con respaldo vivo (medido con Canal 5), y
- el TOPE de precalentado, que sin el abre un lector por cada canal publicado y
  satura el enlace de subida.
"""

from __future__ import annotations

import asyncio
import json
import stat

from src.api import AppState
from src.main import canales_para_salud, load_state, persist_state, run_pipeline


# --------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------
def _canal(make_channel, identificador: str = "uno.mx", respaldos=None):
    ch = make_channel(name=f"Canal {identificador}", tvg_id=identificador)
    ch.id = identificador
    ch.backups = list(respaldos or [])
    return ch


def _cablea_pipeline(monkeypatch, orden, canales, *, escrito=None):
    """Sustituye las seis etapas del pipeline por dobles que anotan el orden."""
    import src.main as main

    def falso_ingest(settings, cfg):
        orden.append("ingest")
        return list(canales)

    def falso_normalize(chs, settings, check_logos=False):
        orden.append("normalize")
        return list(chs), {"entrada": len(chs)}

    def falso_respaldos(chs, respaldo):
        orden.append("respaldos")
        return list(chs)

    def falso_probe(chs, settings):
        orden.append("probe")
        return list(chs)

    def falso_epg(settings, cfg, chs):
        orden.append("epg")
        return {}, None

    def falso_salida(chs, bundle, settings):
        orden.append("salida")
        if escrito is not None:
            escrito.append(len(chs))
        return {"canales": len(chs)}

    monkeypatch.setattr(main, "ingest", falso_ingest)
    monkeypatch.setattr(main, "normalize", falso_normalize)
    monkeypatch.setattr(main, "aplicar_respaldos", falso_respaldos)
    monkeypatch.setattr(main, "check_channels", falso_probe)
    monkeypatch.setattr(main, "refresh_epg", falso_epg)
    monkeypatch.setattr(main, "write_active_outputs", falso_salida)


# --------------------------------------------------------------------------
# canales_para_salud: no duplicar el sondeo mientras corre el pipeline
# --------------------------------------------------------------------------
def test_canales_para_salud_vacia_mientras_corre_el_pipeline(settings, make_channel):
    """Con el pipeline corriendo, el monitor no sondea: ya se validan todos.

    Duplicar la carga sobre los mismos CDN multiplica por tres las peticiones y
    la medicion del proyecto dice que eso hace que devuelvan manifiestos vacios.
    """
    state = AppState(settings=settings)
    state.put_channels([_canal(make_channel)])
    state.pipeline_running = True
    assert canales_para_salud(state) == []


def test_canales_para_salud_devuelve_los_canales_cuando_no_corre(settings, make_channel):
    state = AppState(settings=settings)
    canales = [_canal(make_channel)]
    state.put_channels(canales)
    state.pipeline_running = False
    assert [c.id for c in canales_para_salud(state)] == ["uno.mx"]


# --------------------------------------------------------------------------
# persist_state / load_state
# --------------------------------------------------------------------------
def test_persist_state_deja_el_catalogo_activo_y_los_canales(settings, make_channel):
    state = AppState(settings=settings)
    state.put_channels([_canal(make_channel), _canal(make_channel, "dos.mx")])
    persist_state(state)

    datos = json.loads(settings.state_file.read_text(encoding="utf-8"))
    assert datos["catalogo_activo"] == "todos"  # sin catalogo activo = todos
    assert len(datos["canales"]) == 2
    assert datos["canales"][0]["id"] == "uno.mx"


def test_persist_state_escribe_con_modo_600(settings, make_channel):
    """El estado lleva URLs de origen con token firmado: no es publico."""
    state = AppState(settings=settings)
    state.put_channels([_canal(make_channel)])
    persist_state(state)

    modo = stat.S_IMODE(settings.state_file.stat().st_mode)
    assert modo == 0o600, f"el estado quedo en modo {oct(modo)}"


def test_load_state_ida_y_vuelta(settings, make_channel):
    state = AppState(settings=settings)
    state.put_channels([_canal(make_channel, "uno.mx", ["https://alt.invalid/x.m3u8"])])
    persist_state(state)

    recuperados = load_state(settings.state_file)
    assert [c.id for c in recuperados] == ["uno.mx"]
    assert recuperados[0].backups == ["https://alt.invalid/x.m3u8"]


def test_load_state_tolera_archivo_ausente(tmp_path):
    """Sin estado previo el servicio arranca igual: no es un error."""
    assert load_state(tmp_path / "no-existe.json") == []


def test_load_state_tolera_json_corrupto(tmp_path):
    roto = tmp_path / "state.json"
    roto.write_text("{ esto no es json", encoding="utf-8")
    assert load_state(roto) == []


# --------------------------------------------------------------------------
# run_pipeline: el orden y el tope
# --------------------------------------------------------------------------
def test_run_pipeline_normaliza_antes_de_asociar_respaldos(settings, make_channel, monkeypatch):
    """El ORDEN es un invariante, no un detalle.

    Normalizar primero fija los ids estables; recien entonces se pueden asociar
    los respaldos por id. Con la sonda antes de los respaldos, un canal con la
    principal caida se deshabilitaba a los 3 fallos y su alterna nunca llegaba a
    ejecutarse (medido con Canal 5 el 2026-10-12).
    """
    state = AppState(settings=settings)
    orden: list[str] = []
    _cablea_pipeline(monkeypatch, orden, [_canal(make_channel)])

    asyncio.run(run_pipeline(state))

    assert orden == ["ingest", "normalize", "respaldos", "probe", "epg", "salida"], orden


def test_run_pipeline_respeta_el_tope_de_precalentado(settings, make_channel, monkeypatch):
    """El tope existe porque cada rele sostiene ~330 KB/s de bajada continua.

    Sin tope, publicar 198 canales abre 198 lectores y satura el enlace.
    """
    state = AppState(settings=settings)
    settings.relay_enabled = True
    settings.relay_precalentar = True
    settings.relay_precalentar_max = 2

    canales = [_canal(make_channel, f"c{i}.mx") for i in range(7)]
    orden: list[str] = []
    _cablea_pipeline(monkeypatch, orden, canales)

    import src.main as main

    monkeypatch.setattr(main, "select_for_output", lambda chs, s: list(chs))
    arrancados: list[str] = []
    monkeypatch.setattr(state.relay_pool, "get", lambda cid, *a, **kw: arrancados.append(cid))
    monkeypatch.setattr(state.relay_pool, "barrer", lambda vigentes, idle: [])

    report = asyncio.run(run_pipeline(state))

    assert len(arrancados) == 2, f"precalento {len(arrancados)}, el tope era 2"
    assert report["reles_precalentados"] == 2
    assert report["reles_omitidos"] == 5


def test_run_pipeline_no_escribe_la_salida_si_no_hay_canales(settings, monkeypatch):
    """Una corrida vacia NO debe sobrescribir la playlist que ya sirve."""
    state = AppState(settings=settings)
    orden: list[str] = []
    escrito: list[int] = []
    _cablea_pipeline(monkeypatch, orden, [], escrito=escrito)

    report = asyncio.run(run_pipeline(state))

    assert escrito == [], "escribio la salida con cero canales"
    assert "publicado" not in report
    assert state.channel_list() == []


def test_run_pipeline_suelta_el_flag_y_cierra_el_reporte_aunque_falle(
    settings, make_channel, monkeypatch
):
    """Un fallo no puede dejar el pipeline marcado como corriendo para siempre.

    Si el flag se quedara en True, el health-check no volveria a sondear nunca
    (canales_para_salud devuelve vacio mientras corre).
    """
    state = AppState(settings=settings)

    import src.main as main

    def explota(settings_, cfg):
        raise RuntimeError("la fuente se cayo")

    monkeypatch.setattr(main, "ingest", explota)

    report = asyncio.run(run_pipeline(state))

    assert state.pipeline_running is False
    assert "error" in report and "se cayo" in report["error"]
    assert report["fin"]  # el reporte se cierra en el finally
    assert state.last_pipeline


def test_run_pipeline_publica_la_lista_y_guarda_el_estado(settings, make_channel, monkeypatch):
    state = AppState(settings=settings)
    orden: list[str] = []
    escrito: list[int] = []
    _cablea_pipeline(
        monkeypatch, orden, [_canal(make_channel), _canal(make_channel, "dos.mx")], escrito=escrito
    )

    report = asyncio.run(run_pipeline(state))

    assert escrito == [2]
    assert report["publicado"] == {"canales": 2}
    assert len(state.channel_list()) == 2
    assert settings.state_file.exists()
