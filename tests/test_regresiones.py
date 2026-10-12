"""Regresiones de la auditoria: un test por hallazgo corregido.

Cada prueba de aqui existe porque el defecto YA ocurrio, no porque "suene bien":
varios estuvieron dormidos en produccion (el de los offsets, por ejemplo, no se
notaba porque todas las fuentes cableadas publican ``+0000``). Si alguno se
rompe, se rompe una medicion, no una opinion.
"""

from __future__ import annotations

import time
from datetime import timedelta

from fastapi.testclient import TestClient

from src import probe as probe_mod
from src.api import AppState, create_app
from src.config import Settings
from src.epg import _parse_time
from src.health import HealthMonitor
from src.models import Channel, ProbeResult
from src.playlist import build_m3u, m3u_attr
from src.relay import MAX_CLAVES, MAX_VISTOS, RelayHLS, RelayPool, Segmento


# ---------------------------------------------------------------------------
# EPG: el offset del XMLTV
# ---------------------------------------------------------------------------
def test_parse_time_respeta_el_offset():
    """Antes: ``-0600`` daba -01:00 y ``+0200`` daba -00:20 (posiciones fijas)."""
    casos = {
        "20260101120000 +0000": 0,
        "20260101120000 -0600": -6 * 60,
        "20260101120000 +0200": 2 * 60,
        "20260101120000-0600": -6 * 60,
        "20260101120000+0200": 2 * 60,
        "20260101120000Z": 0,
        "20260101120000": 0,
    }
    for texto, minutos in casos.items():
        dt = _parse_time(texto)
        assert dt is not None, texto
        assert dt.utcoffset() == timedelta(minutes=minutos), texto


def test_parse_time_convierte_al_instante_correcto():
    """La hora local con offset tiene que caer en el UTC que le toca."""
    from datetime import timezone

    dt = _parse_time("20261011200000 -0600")
    assert dt is not None
    assert dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-10-12 02:00"


def test_parse_time_rechaza_basura():
    assert _parse_time("") is None
    assert _parse_time(None) is None
    assert _parse_time("no-es-fecha") is None
    assert _parse_time("20261399999999 +0000") is None


# ---------------------------------------------------------------------------
# PROBE: reintentos
# ---------------------------------------------------------------------------
def test_reintentable_trata_429_y_408_como_transitorios():
    """El atajo por prefijo ``http 4`` interceptaba los dos antes de la lista.

    Un 401/403 no se reintenta: es permiso, no carga, y reintentar la MISMA URL
    (el token se resuelve una vez por sonda) no lo arregla.
    """
    assert probe_mod._reintentable("HTTP 429 en http://x") is True
    assert probe_mod._reintentable("HTTP 408 en http://x") is True
    assert probe_mod._reintentable("timeout") is True
    assert probe_mod._reintentable("HTTP 503 en http://x") is True
    assert probe_mod._reintentable("HTTP 401 en http://x") is False
    assert probe_mod._reintentable("HTTP 403 en http://x") is False
    assert probe_mod._reintentable("HTTP 404 en http://x") is False
    assert probe_mod._reintentable("HTTP 416 en http://x") is False
    assert probe_mod._reintentable("") is False


# ---------------------------------------------------------------------------
# PLAYLIST: saneo de atributos de terceros
# ---------------------------------------------------------------------------
def test_m3u_attr_neutraliza_comillas_y_saltos():
    assert m3u_attr('Canal "Uno"') == "Canal 'Uno'"
    assert m3u_attr("Uno\n#EXTINF:-1,inyectado") == "Uno #EXTINF:-1,inyectado"
    assert m3u_attr("  con   espacios  ") == "con espacios"
    assert m3u_attr(None) == ""
    assert m3u_attr("Demo\rmac") == "Demo mac"


def test_build_m3u_no_deja_inyectar_una_entrada(settings: Settings):
    """Un salto de linea en un ``tvg-name`` de terceros metia un canal falso."""
    ch = Channel(
        id="uno.mx",
        name='Canal "Uno"\nmal.m3u8',
        tvg_id="uno.mx",
        tvg_name="Uno\n#EXTINF:-1,inyectado",
        logo='https://x.invalid/a"b.png',
        group='Demo"\nx',
        source_url="https://origen.invalid/u.m3u8",
    )
    texto = build_m3u([ch], settings)
    lineas = texto.splitlines()
    assert lineas[0] == "#EXTM3U"
    # Estructura intacta: UN #EXTINF y su URL. El texto inyectado queda DENTRO
    # del atributo (entre comillas), no como linea propia.
    assert len(lineas) == 3, lineas
    assert lineas[1].startswith("#EXTINF")
    assert lineas[2] == "http://test.local/stream/uno.mx"
    assert lineas[1].endswith(",Canal 'Uno' mal.m3u8")
    assert 'tvg-name="Uno #EXTINF:-1,inyectado"' in lineas[1]
    assert 'tvg-logo="https://x.invalid/a\'b.png"' in lineas[1]


# ---------------------------------------------------------------------------
# RELAY: umbral de cambio de fuente
# ---------------------------------------------------------------------------
def test_contar_fallo_cambia_al_primer_fallo_con_colchon_vacio():
    r = RelayHLS(["http://a/1.m3u8", "http://b/2.m3u8"])
    r._contar_fallo()
    assert r._cambios == 1
    assert r._indice == 1


def test_contar_fallo_cambia_al_tercero_con_colchon_lleno():
    r = RelayHLS(["http://a/1.m3u8", "http://b/2.m3u8"])
    r._buf[1] = Segmento(1, "u", b"x", 5.0)
    r._contar_fallo()
    r._contar_fallo()
    assert r._cambios == 0, "un tropiezo suelto no cambia de fuente"
    r._contar_fallo()
    assert r._cambios == 1


def test_contar_fallo_sin_respaldo_no_cambia():
    r = RelayHLS(["http://a/1.m3u8"])
    for _ in range(10):
        r._contar_fallo()
    assert r._cambios == 0
    assert r._indice == 0


# ---------------------------------------------------------------------------
# RELAY: memoria acotada
# ---------------------------------------------------------------------------
def test_vistos_es_un_lru_acotado():
    r = RelayHLS(["http://a/1.m3u8"])
    for i in range(MAX_VISTOS * 3):
        with r._lock:
            r._recordar_visto(f"http://x/{i}.ts")
    assert len(r._vistos) == MAX_VISTOS
    assert "http://x/0.ts" not in r._vistos          # lo viejo se suelta
    assert f"http://x/{MAX_VISTOS * 3 - 1}.ts" in r._vistos


def test_claves_es_un_lru_acotado():
    r = RelayHLS(["http://a/1.m3u8"])
    for i in range(MAX_CLAVES * 3):
        with r._lock:
            r._recordar_clave(f"https://k/{i}", "00" * 16)
    assert len(r._claves) == MAX_CLAVES


# ---------------------------------------------------------------------------
# RELAY: colchon y entrega
# ---------------------------------------------------------------------------
def test_servir_entrega_el_colchon_en_orden():
    r = RelayHLS(["http://a/1.m3u8"])
    for i in range(1, 4):
        r._buf[i] = Segmento(i, f"u{i}", b"\x47" + bytes([i]) * 16, 1.0)
    r._frente, r._primero = 3, 1
    gen = r.servir(espera_max_s=0.1)
    try:
        datos = [next(gen) for _ in range(3)]
    finally:
        r._stop.set()
        gen.close()
    assert [d[1] for d in datos] == [1, 2, 3]
    assert r.clientes == 0, "el cliente se desengancha al cerrar el generador"


def test_pool_barrer_apaga_lo_que_ya_no_se_publica():
    pool = RelayPool()
    r_vivo = RelayHLS(["http://a/1.m3u8"])
    r_viejo = RelayHLS(["http://b/2.m3u8"])
    r_viejo._tuvo_clientes = True
    r_viejo._ultimo_cliente = 0.0
    pool._relays = {"vivo": r_vivo, "viejo": r_viejo}
    assert pool.barrer({"vivo"}, idle_s=60) == ["viejo"]
    assert set(pool._relays) == {"vivo"}


def test_pool_barrer_no_mata_el_precalentado():
    """Los reles que nunca sirvieron a nadie son justo el precalentado."""
    pool = RelayPool()
    pool._relays = {"uno": RelayHLS(["http://a/1.m3u8"])}
    assert pool.barrer({"uno"}, idle_s=0) == []
    assert set(pool._relays) == {"uno"}


def test_pool_barrer_apaga_el_ocioso_que_ya_se_uso():
    pool = RelayPool()
    r = RelayHLS(["http://a/1.m3u8"])
    r._tuvo_clientes = True
    r._ultimo_cliente = time.monotonic() - 10_000
    pool._relays = {"uno": r}
    assert pool.barrer({"uno"}, idle_s=60) == ["uno"]
    assert pool._relays == {}


# ---------------------------------------------------------------------------
# CONFIG: bind y token
# ---------------------------------------------------------------------------
def test_bind_ip_y_api_token_desde_entorno(monkeypatch):
    monkeypatch.setenv("LVB_BIND_IP", "10.0.0.21")
    monkeypatch.setenv("LVB_API_TOKEN", "secreto-de-prueba")
    s = Settings.from_env()
    assert s.bind_ip == "10.0.0.21"
    assert s.api_token == "secreto-de-prueba"
    # El token NO puede salir en la config que publica /status.
    assert "api_token" not in s.redacted()
    assert "bind_ip" in s.redacted()


def test_bind_ip_vacio_por_defecto(monkeypatch):
    monkeypatch.delenv("LVB_BIND_IP", raising=False)
    assert Settings.from_env().bind_ip == ""


def test_sources_yml_pisa_al_entorno(monkeypatch):
    monkeypatch.setenv("LVB_BIND_IP", "1.1.1.1")
    assert Settings.from_env({"bind_ip": "10.0.0.5"}).bind_ip == "10.0.0.5"


def test_valores_de_rele_por_defecto(monkeypatch):
    for var in ("LVB_RELAY_PRECALENTAR_MAX", "LVB_RELAY_IDLE_S", "LVB_RELAY_PRECALENTAR"):
        monkeypatch.delenv(var, raising=False)
    s = Settings.from_env()
    assert s.relay_precalentar is True
    assert s.relay_precalentar_max == 16
    assert s.relay_idle_s == 900.0


def test_valor_invalido_avisa_y_usa_el_default(monkeypatch):
    monkeypatch.setenv("LVB_RELAY_PRECALENTAR_MAX", "no-es-numero")
    assert Settings.from_env().relay_precalentar_max == 16


# ---------------------------------------------------------------------------
# API: token en los POST, GET siempre abiertos
# ---------------------------------------------------------------------------
def _state(settings: Settings) -> AppState:
    state = AppState(settings=settings)
    state.monitor = HealthMonitor(settings)
    state.put_channels([
        Channel(
            id="uno.mx",
            name="Canal Uno",
            tvg_id="uno.mx",
            group="Demo",
            source_url="https://example.invalid/u.m3u8",
            probe=ProbeResult(accessible=True, video_codec="h264", audio_codec="aac"),
        )
    ])
    return state


def test_post_protegido_cuando_hay_token(settings: Settings):
    settings.api_token = "abc123"
    client = TestClient(create_app(_state(settings)))
    assert client.post("/catalog/todos").status_code == 401
    assert client.post("/reload").status_code == 401
    assert client.post("/catalog/todos", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/catalog/todos", headers={"Authorization": "Bearer abc123"}).status_code == 200
    assert client.post("/catalog/todos", params={"token": "abc123"}).status_code == 200


def test_get_sigue_abierto_con_token(settings: Settings):
    """Cerrar los GET romperia el tuner de Jellyfin, que no manda cabeceras."""
    settings.api_token = "abc123"
    client = TestClient(create_app(_state(settings)))
    assert client.get("/health").status_code == 200
    assert client.get("/playlist.m3u").status_code == 200
    assert client.get("/status").status_code == 200


def test_sin_token_configurado_todo_pasa(settings: Settings):
    settings.api_token = ""
    client = TestClient(create_app(_state(settings)))
    assert client.post("/reload").status_code == 503   # sin pipeline enlazado, no 401
