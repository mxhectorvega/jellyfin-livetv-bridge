"""Pruebas de la API HTTP (FastAPI TestClient) y del plan de transcodificacion."""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from src.api import AppState, create_app
from src.config import Settings
from src.health import HealthMonitor
from src.models import Channel, ProbeResult
from src.transcode import build_ffmpeg_cmd, plan_for


def _state(settings: Settings) -> AppState:
    state = AppState(settings=settings)
    state.monitor = HealthMonitor(settings)
    canal = Channel(
        id="uno.mx",
        name="Canal Uno",
        tvg_id="uno.mx",
        group="Demo",
        source_url="https://example.invalid/u.m3u8",
        probe=ProbeResult(accessible=True, video_codec="h264", audio_codec="aac", resolution="1280x720"),
    )
    state.put_channels([canal])
    return state


def test_health(settings: Settings):
    client = TestClient(create_app(_state(settings)))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["canales"] == 1


def test_status_expone_canales(settings: Settings):
    client = TestClient(create_app(_state(settings)))
    data = client.get("/status").json()
    assert data["canales"][0]["id"] == "uno.mx"
    assert data["canales"][0]["video"] == "h264"
    assert "config" in data


def test_playlist_endpoint(settings: Settings):
    client = TestClient(create_app(_state(settings)))
    r = client.get("/playlist.m3u")
    assert r.status_code == 200
    assert r.text.startswith("#EXTM3U")
    assert "http://test.local/stream/uno.mx" in r.text


def test_epg_404_si_no_generado(settings: Settings):
    client = TestClient(create_app(_state(settings)))
    assert client.get("/epg.xml").status_code == 404


def test_stream_404_canal_desconocido(settings: Settings):
    client = TestClient(create_app(_state(settings)))
    assert client.get("/stream/no-existe").status_code == 404


def test_stream_451_si_protegido(settings: Settings):
    state = _state(settings)
    state.channels["uno.mx"].probe.drm = True
    client = TestClient(create_app(state))
    assert client.get("/stream/uno.mx").status_code == 451


def test_reload_sin_pipeline_enlazado(settings: Settings):
    client = TestClient(create_app(_state(settings)))
    assert client.post("/reload").status_code == 503


def test_plan_copy_cuando_codecs_compatibles(settings: Settings):
    ch = Channel(name="A", source_url="u", probe=ProbeResult(accessible=True, video_codec="h264", audio_codec="aac"))
    plan = plan_for(ch, settings)
    assert plan.is_copy is True
    cmd = build_ffmpeg_cmd(ch.source_url, plan, settings)
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "copy"


def test_plan_transcodifica_hevc_y_ac3(settings: Settings):
    settings.hwaccel = "off"  # fuerza decision sin GPU
    ch = Channel(name="A", source_url="u", probe=ProbeResult(accessible=True, video_codec="hevc", audio_codec="ac3"))
    plan = plan_for(ch, settings)
    # Con hwaccel=off el plan siempre es copia (por diseno).
    assert plan.is_copy is True


def test_plan_perfil_h264_forzado(settings: Settings):
    settings.hwaccel = "cpu"
    ch = Channel(name="A", source_url="u", profile="h264", probe=ProbeResult(accessible=True, video_codec="h264", audio_codec="aac"))
    plan = plan_for(ch, settings)
    assert plan.copy_video is False
    assert plan.copy_audio is True
    cmd = build_ffmpeg_cmd(ch.source_url, plan, settings)
    assert "libx264" in cmd


def test_plan_sin_metadatos_copia(settings: Settings):
    ch = Channel(name="A", source_url="u", probe=ProbeResult(accessible=True))
    plan = plan_for(ch, settings)
    assert plan.is_copy is True
