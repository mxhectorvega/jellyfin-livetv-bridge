"""Pruebas de la fuente de TV Azteca integrada (token de mediastream)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src import azteca
from src.api import AppState, create_app
from src.config import Settings
from src.health import HealthMonitor
from src.models import Channel, ProbeResult


@pytest.fixture(autouse=True)
def limpiar_cache():
    azteca._cache.clear()
    yield
    azteca._cache.clear()


class _RespuestaFalsa:
    """Respuesta minima de urllib (context manager + read)."""

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._payload


def test_clave_de_url_reconoce_el_proveedor_local():
    assert azteca.clave_de_url("http://127.0.0.1:8582/azteca7", "http://127.0.0.1:8582") == "azteca7"
    assert azteca.clave_de_url("http://127.0.0.1:8582/azteca-intl", "http://127.0.0.1:8582") == "azteca-intl"
    assert azteca.clave_de_url("https://otro.invalid/x.m3u8", "http://127.0.0.1:8582") is None
    assert azteca.clave_de_url("http://127.0.0.1:8582/desconocido", "http://127.0.0.1:8582") is None
    assert azteca.clave_de_url("", "http://127.0.0.1:8582") is None


def test_resolver_url_pide_token_y_arma_el_master(monkeypatch):
    llamadas = {"n": 0}

    def fake_urlopen(req, timeout=None):
        llamadas["n"] += 1
        assert b"getLive" in req.data
        assert req.headers.get("X-client-id") == azteca.CLIENTE
        return _RespuestaFalsa({"data": {"getLive": {"accessToken": "TOKEN123"}}})

    monkeypatch.setattr(azteca.urllib.request, "urlopen", fake_urlopen)
    url = azteca.resolver_url("azteca7")
    assert url.endswith("access_token=TOKEN123")
    assert "67da2962c4eed1a805b30e61" in url
    # Segunda llamada: la cache evita el segundo golpe a la API.
    assert azteca.resolver_url("azteca7").endswith("TOKEN123")
    assert llamadas["n"] == 1


def test_reescribir_url_sin_clave_no_toca(monkeypatch):
    monkeypatch.setattr(azteca.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no debe pedir token"))
    original = "https://ejemplo.invalid/canal/master.m3u8"
    assert azteca.reescribir_url(original, "http://127.0.0.1:8582") == original


def test_reescribir_url_deshabilitado_no_resuelve(monkeypatch):
    monkeypatch.setattr(azteca.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no debe pedir token"))
    original = "http://127.0.0.1:8582/azteca7"
    assert azteca.reescribir_url(original, "http://127.0.0.1:8582", habilitado=False) == original


def test_reescribir_url_si_no_hay_token_devuelve_la_original(monkeypatch):
    def boom(*a, **k):
        raise OSError("sin red")

    monkeypatch.setattr(azteca.urllib.request, "urlopen", boom)
    original = "http://127.0.0.1:8582/azteca7"
    # No revienta: el fallo se vera como "inaccesible" en la validacion.
    assert azteca.reescribir_url(original, "http://127.0.0.1:8582") == original


def test_rama_salud_y_302_del_endpoint(monkeypatch, settings: Settings):
    monkeypatch.setattr(
        azteca.urllib.request,
        "urlopen",
        lambda *a, **k: _RespuestaFalsa({"data": {"getLive": {"accessToken": "ABC"}}}),
    )
    state = AppState(settings=settings)
    state.monitor = HealthMonitor(settings)
    client = TestClient(create_app(state))

    r = client.get("/azteca/azteca7", follow_redirects=False)
    assert r.status_code == 302
    assert "access_token=ABC" in r.headers["location"]

    r = client.get("/azteca/otro", follow_redirects=False)
    assert r.status_code == 404


def test_check_stream_resuelve_la_url_del_canal(monkeypatch, settings: Settings):
    """Regresion de la integracion: la validacion usa el master autenticado."""
    visto = {}

    def fake_descender(url, timeout, profundidad=3):
        visto["url"] = url
        return "", b"", {"encrypted": False, "drm": False, "signed_token": False}, "fallo simulado"

    monkeypatch.setattr(
        azteca.urllib.request,
        "urlopen",
        lambda *a, **k: _RespuestaFalsa({"data": {"getLive": {"accessToken": "XYZ"}}}),
    )
    from src import probe as probe_mod

    monkeypatch.setattr(probe_mod, "_descender", fake_descender)
    probe_mod.check_stream("http://127.0.0.1:8582/azteca7", timeout=1, mode="liveness", settings=settings)
    assert "access_token=XYZ" in visto["url"]


def test_health_monitor_del_bridge_expone_canales_azteca(settings: Settings):
    """El canal de Azteca viaja como uno mas del catalogo."""
    ch = Channel(
        id="azteca.7.xhlat.nuevo.laredo.mx.mx",
        name="Azteca 7",
        tvg_id="azteca.7.xhlat.nuevo.laredo.mx.mx",
        catalog="nacionales",
        source_url="http://127.0.0.1:8582/azteca7",
        probe=ProbeResult(accessible=True, video_codec="h264", audio_codec="aac"),
    )
    state = AppState(settings=settings)
    state.monitor = HealthMonitor(settings)
    state.put_channels([ch])
    client = TestClient(create_app(state))
    data = client.get("/status").json()
    assert data["canales"][0]["id"] == "azteca.7.xhlat.nuevo.laredo.mx.mx"
    assert "azteca" in json.dumps(data).lower()