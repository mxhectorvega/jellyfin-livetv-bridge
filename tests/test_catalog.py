"""Pruebas del selector de catalogo (varios alimentados, uno visible)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api import AppState, create_app
from src.config import Settings
from src.epg import EpgBundle
from src.health import HealthMonitor
from src.models import Channel, ProbeResult
from src.playlist import (
    catalogs_of,
    select_for_output,
    set_active_catalog,
    write_active_outputs,
)


def _ch(cid: str, catalog: str, **kw) -> Channel:
    base = dict(
        id=cid,
        name=cid,
        tvg_id=cid,
        tvg_name=cid,
        source_url=f"https://example.invalid/{cid}.m3u8",
        catalog=catalog,
        probe=ProbeResult(accessible=True, video_codec="h264", audio_codec="aac"),
    )
    base.update(kw)
    return Channel(**base)


def _canales() -> list[Channel]:
    return [_ch(f"nac{i}", "nacionales") for i in range(3)] + [
        _ch(f"plu{i}", "pluto") for i in range(5)
    ]


def test_catalogs_of_cuenta_por_catalogo():
    assert catalogs_of(_canales()) == {"nacionales": 3, "pluto": 5}


def test_select_for_output_respeta_el_catalogo_activo(settings: Settings):
    canales = _canales()
    assert len(select_for_output(canales, settings, catalog="nacionales")) == 3
    assert len(select_for_output(canales, settings, catalog="pluto")) == 5
    assert len(select_for_output(canales, settings, catalog="")) == 8


def test_active_catalog_archivo_gana_al_entorno(settings: Settings):
    settings.active_catalog = "nacionales"
    assert settings.resolve_active_catalog() == "nacionales"
    set_active_catalog(settings, "pluto")
    assert settings.resolve_active_catalog() == "pluto"
    # "todos" desactiva el filtro aunque el .env traiga un catalogo
    set_active_catalog(settings, "todos")
    assert settings.resolve_active_catalog() == ""


def test_write_active_outputs_escribe_solo_el_activo(settings: Settings):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).strftime("%Y%m%d%H%M%S +0000")
    stop = (now + timedelta(hours=1)).strftime("%Y%m%d%H%M%S +0000")
    programa = f'<programme start="{start}" stop="{stop}" channel="src"><title>P</title></programme>'
    bundle = EpgBundle()
    canales = _canales()
    for ch in canales:
        ch.epg_id = "src"
    bundle.channels["src"] = ["x"]
    import xml.etree.ElementTree as ET

    bundle.programmes["src"] = [ET.fromstring(programa)]

    set_active_catalog(settings, "pluto")
    result = write_active_outputs(canales, bundle, settings)
    assert result["catalogo"] == "pluto"
    assert result["canales"] == 5
    m3u = settings.playlist_file.read_text(encoding="utf-8")
    assert "plu0" in m3u and "nac0" not in m3u

    set_active_catalog(settings, "nacionales")
    result = write_active_outputs(canales, bundle, settings)
    assert result["canales"] == 3
    m3u = settings.playlist_file.read_text(encoding="utf-8")
    assert "nac0" in m3u and "plu0" not in m3u
    # El XMLTV se reescribe con el mismo bundle (sin re-descargar la guia).
    assert settings.epg_file.exists()


def test_write_active_outputs_no_sobrescribe_si_no_hay_canales(settings: Settings):
    canales = [_ch("plu0", "pluto", enabled=False)]
    settings.playlist_file.write_text("#EXTM3U\n#EXTINF:-1,Viejo\nhttp://x\n", encoding="utf-8")
    set_active_catalog(settings, "pluto")
    result = write_active_outputs(canales, EpgBundle(), settings)
    assert result["m3u_escrito"] is False
    assert "Viejo" in settings.playlist_file.read_text(encoding="utf-8")


def _state(settings: Settings) -> AppState:
    state = AppState(settings=settings)
    state.monitor = HealthMonitor(settings)
    state.put_channels(_canales())
    return state


def test_api_catalogs(settings: Settings):
    client = TestClient(create_app(_state(settings)))
    data = client.get("/catalogs").json()
    assert data["catalogs"] == {"nacionales": 3, "pluto": 5}
    assert data["activo"] == "todos"
    assert data["publicados"] == 8


def test_api_cambia_catalogo_y_republica(settings: Settings):
    state = _state(settings)
    client = TestClient(create_app(state))
    r = client.post("/catalog/pluto")
    assert r.status_code == 200
    assert r.json()["canales"] == 5
    assert client.get("/catalogs").json()["activo"] == "pluto"
    assert "plu0" in client.get("/playlist.m3u").text
    assert "nac0" not in client.get("/playlist.m3u").text
    # Volver a todos
    r = client.post("/catalog/todos")
    assert r.status_code == 200
    assert "nac0" in client.get("/playlist.m3u").text


def test_api_catalogo_desconocido_404(settings: Settings):
    client = TestClient(create_app(_state(settings)))
    r = client.post("/catalog/inexistente")
    assert r.status_code == 404
    assert "catalogo desconocido" in r.json()["detail"]