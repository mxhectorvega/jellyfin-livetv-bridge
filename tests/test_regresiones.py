"""Regresiones de la auditoria: un test por hallazgo corregido.

Cada prueba de aqui existe porque el defecto YA ocurrio, no porque "suene bien":
varios estuvieron dormidos en produccion (el de los offsets, por ejemplo, no se
notaba porque todas las fuentes cableadas publican ``+0000``). Si alguno se
rompe, se rompe una medicion, no una opinion.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, timedelta

import pytest
from fastapi.testclient import TestClient
from src import probe as probe_mod
from src.api import AppState, create_app
from src.config import Settings
from src.epg import _parse_time, parse_xmltv
from src.health import HealthMonitor
from src.ingest import parse_m3u, title_of_extinf
from src.models import Channel, ProbeResult
from src.normalize import clean_name
from src.playlist import build_m3u, m3u_attr
from src.relay import MAX_CLAVES, MAX_VISTOS, RelayHLS, RelayPool, Segmento
from src.transcode import plan_for
from src.ts_splice import MOD, escribir_sello, primer_pts


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

    dt = _parse_time("20261011200000 -0600")
    assert dt is not None
    assert dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M") == "2026-10-12 02:00"


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
    assert "http://x/0.ts" not in r._vistos  # lo viejo se suelta
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
    monkeypatch.setenv("LVB_BIND_IP", "192.168.1.50")
    monkeypatch.setenv("LVB_API_TOKEN", "secreto-de-prueba")
    s = Settings.from_env()
    assert s.bind_ip == "192.168.1.50"
    assert s.api_token == "secreto-de-prueba"
    # El token NO puede salir en la config que publica /status.
    assert "api_token" not in s.redacted()
    assert "bind_ip" in s.redacted()


def test_bind_ip_vacio_por_defecto(monkeypatch):
    monkeypatch.delenv("LVB_BIND_IP", raising=False)
    assert Settings.from_env().bind_ip == ""


def test_sources_yml_pisa_al_entorno(monkeypatch):
    monkeypatch.setenv("LVB_BIND_IP", "1.1.1.1")
    assert Settings.from_env({"bind_ip": "192.168.1.60"}).bind_ip == "192.168.1.60"


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
# RELAY: trazabilidad en el log
# ---------------------------------------------------------------------------
def test_el_pool_etiqueta_el_rele_con_el_canal():
    """Sin el nombre, un "re-resolviendo origen 404" repetido no dice de quién."""
    pool = RelayPool()
    try:
        relay = pool.get("canal.prueba.mx", "http://127.0.0.1:9/x.m3u8")
        assert relay.nombre == "canal.prueba.mx"
    finally:
        pool.detener_todos()


def test_el_aviso_de_cambio_de_fuente_lleva_el_canal(caplog):
    import logging

    r = RelayHLS(["http://127.0.0.1:9/a.m3u8", "http://127.0.0.1:9/b.m3u8"], nombre="canal.x")
    with caplog.at_level(logging.WARNING):
        r._contar_fallo()
    registro = next(reg for reg in caplog.records if reg.message == "relay: cambiando de fuente")
    assert getattr(registro, "channel", "") == "canal.x"


# ---------------------------------------------------------------------------
# MAIN: el health-check no duplica el barrido del pipeline
# ---------------------------------------------------------------------------
def test_canales_para_salud_no_sondea_durante_el_pipeline(settings: Settings):
    """Pipeline (2 obreros) + monitor a la vez = 3 sondas sobre el mismo CDN.

    La medicion del propio proyecto dice que ahi el CDN empieza a devolver
    manifiestos vacios. El monitor espera a que el pipeline termine.
    """
    from src.main import canales_para_salud

    state = _state(settings)
    state.pipeline_running = False
    assert len(canales_para_salud(state)) == 1
    state.pipeline_running = True
    assert canales_para_salud(state) == []


def test_check_all_con_lista_vacia_no_hace_nada(settings: Settings):
    import asyncio

    monitor = HealthMonitor(settings)
    report = asyncio.run(monitor.check_all([]))
    assert report.checked == 0
    assert monitor.snapshot()["canales"] == {}


# ---------------------------------------------------------------------------
# API: token en los POST, GET siempre abiertos
# ---------------------------------------------------------------------------
def _state(settings: Settings) -> AppState:
    state = AppState(settings=settings)
    state.monitor = HealthMonitor(settings)
    state.put_channels(
        [
            Channel(
                id="uno.mx",
                name="Canal Uno",
                tvg_id="uno.mx",
                group="Demo",
                source_url="https://example.invalid/u.m3u8",
                probe=ProbeResult(accessible=True, video_codec="h264", audio_codec="aac"),
            )
        ]
    )
    return state


def test_post_protegido_cuando_hay_token(settings: Settings):
    settings.api_token = "abc123"
    client = TestClient(create_app(_state(settings)))
    assert client.post("/catalog/todos").status_code == 401
    assert client.post("/reload").status_code == 401
    assert (
        client.post("/catalog/todos", headers={"Authorization": "Bearer nope"}).status_code == 401
    )
    assert (
        client.post("/catalog/todos", headers={"Authorization": "Bearer abc123"}).status_code == 200
    )
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
    assert client.post("/reload").status_code == 503  # sin pipeline enlazado, no 401


# ---------------------------------------------------------------------------
# INGESTA: la coma del #EXTINF (auditoria 2026-10-12)
# ---------------------------------------------------------------------------
def test_title_of_extinf_corta_en_la_ultima_coma_fuera_de_comillas():
    """Antes: ``re.search(r",\\s*(.+?)\\s*$")`` casaba en la PRIMERA coma.

    Medido con ``tvg-name="Canal, Uno"``: el nombre salia
    ``'Uno" group-title="G",Canal Uno'``.
    """
    linea = '#EXTINF:-1 tvg-id="a.mx" tvg-name="Canal, Uno" group-title="News, Weather",Canal Uno'
    assert title_of_extinf(linea) == "Canal Uno"
    canales = parse_m3u("#EXTM3U\n" + linea + "\nhttp://x/1.m3u8\n")
    assert canales[0].name == "Canal Uno"
    assert canales[0].tvg_name == "Canal, Uno"
    assert canales[0].group == "News, Weather"


def test_title_of_extinf_sin_coma_devuelve_vacio():
    assert title_of_extinf('#EXTINF:-1 tvg-id="a.mx"') == ""


def test_parse_m3u_sin_titulo_cae_al_tvg_name():
    linea = '#EXTINF:-1 tvg-id="a.mx" tvg-name="Respaldo"'
    assert parse_m3u("#EXTM3U\n" + linea + "\nhttp://x/1.m3u8\n")[0].name == "Respaldo"


# ---------------------------------------------------------------------------
# NORMALIZE: nombre compuesto solo de etiquetas
# ---------------------------------------------------------------------------
def test_clean_name_avisa_cuando_solo_hay_etiqueta(caplog):
    """El fallback al original es deliberado; lo que faltaba era el rastro."""
    with caplog.at_level(logging.WARNING):
        assert clean_name("(720p)") == "(720p)"
    assert any("etiquetas de calidad" in r.message for r in caplog.records)


def test_clean_name_normal_sigue_limpiando():
    assert clean_name("Canal 5 (1080p)") == "Canal 5"


# ---------------------------------------------------------------------------
# TRANSCODE: lista BLANCA (auditoria 2026-10-12)
# ---------------------------------------------------------------------------
def test_plan_codecs_desconocidos_no_se_copian(settings: Settings):
    """Antes, lista negra: vc1/theora/vp8 salian por ``-c copy`` a mpegts."""
    settings.hwaccel = "cpu"
    for codec in ("vc1", "theora", "vp8", "hevc", "mpeg2video"):
        ch = Channel(
            name="A",
            source_url="u",
            probe=ProbeResult(accessible=True, video_codec=codec, audio_codec="aac"),
        )
        assert plan_for(ch, settings).copy_video is False, codec


def test_plan_h264_y_avc1_siguen_copiando(settings: Settings):
    settings.hwaccel = "cpu"
    for codec in ("h264", "avc1"):
        ch = Channel(
            name="A",
            source_url="u",
            probe=ProbeResult(accessible=True, video_codec=codec, audio_codec="aac"),
        )
        assert plan_for(ch, settings).copy_video is True, codec


def test_plan_audio_desconocido_no_se_copia(settings: Settings):
    settings.hwaccel = "cpu"
    ch = Channel(
        name="A",
        source_url="u",
        probe=ProbeResult(accessible=True, video_codec="h264", audio_codec="opus"),
    )
    plan = plan_for(ch, settings)
    assert plan.copy_video is True and plan.copy_audio is False


# ---------------------------------------------------------------------------
# CONFIG: rangos (auditoria 2026-10-12)
# ---------------------------------------------------------------------------
def test_puerto_fuera_de_rango_usa_el_default(monkeypatch):
    """Antes: ``LVB_PORT=99999`` pasaba y el bind reventaba con OverflowError."""
    monkeypatch.setenv("LVB_PORT", "99999")
    assert Settings.from_env().port == 8581


def test_valores_negativos_o_cero_usan_el_default(monkeypatch):
    monkeypatch.setenv("LVB_RELAY_IDLE_S", "-5")
    monkeypatch.setenv("LVB_MAX_TRANSCODES", "-1")
    monkeypatch.setenv("LVB_PROBE_WORKERS", "0")
    s = Settings.from_env()
    assert s.relay_idle_s == 900.0
    assert s.max_transcodes == 2
    assert s.probe_workers == 2


def test_valor_valido_en_rango_no_se_toca(monkeypatch):
    monkeypatch.setenv("LVB_PROBE_WORKERS", "3")
    monkeypatch.setenv("LVB_PORT", "9000")
    s = Settings.from_env()
    assert s.probe_workers == 3 and s.port == 9000


# ---------------------------------------------------------------------------
# EPG: bomba de entidades (auditoria 2026-10-12)
# ---------------------------------------------------------------------------
def test_parse_xmltv_rechaza_entidades_internas():
    """Medido: ~250 B de entrada expandian a 10,000 caracteres."""
    bomba = (
        '<?xml version="1.0"?><!DOCTYPE tv [<!ENTITY a "AAAAAAAAAA">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<tv><channel id="x"><display-name>&b;</display-name></channel></tv>'
    )
    with pytest.raises(ValueError):
        parse_xmltv(bomba.encode())


def test_parse_xmltv_acepta_el_doctype_normal():
    """El DOCTYPE que apunta al DTD es legitimo y NO se bloquea."""
    from datetime import datetime, timedelta

    ahora = datetime.now(UTC) + timedelta(hours=1)
    sello = ahora.strftime("%Y%m%d%H%M%S")
    documento = (
        '<?xml version="1.0"?><!DOCTYPE tv SYSTEM "xmltv.dtd">'
        '<tv><channel id="uno.mx"><display-name>Uno</display-name></channel>'
        f'<programme start="{sello} +0000" stop="{sello} +0000" channel="uno.mx">'
        "<title>Prueba</title></programme></tv>"
    )
    bundle = parse_xmltv(documento.encode())
    assert "uno.mx" in bundle.channels
    assert bundle.programmes.get("uno.mx")


# ---------------------------------------------------------------------------
# HTTP: tope de descompresion (auditoria 2026-10-12)
# ---------------------------------------------------------------------------
def test_gunzip_acotado_corta_la_expansion():
    """Antes el tope se aplicaba al comprimido: 203,860 B -> 200 MB (1029x)."""
    import gzip as _gzip

    from src import util

    carga = b"\x00" * (2 * 1024 * 1024)
    comprimido = _gzip.compress(carga)
    assert len(comprimido) < 4096
    with pytest.raises(RuntimeError):
        util._gunzip_acotado(comprimido, 1024, "http://x/g.gz")
    assert util._gunzip_acotado(comprimido, len(carga) + 1, "http://x/g.gz") == carga


# ---------------------------------------------------------------------------
# API: /channels no publica la URL de origen (auditoria 2026-10-12)
# ---------------------------------------------------------------------------
def test_channels_no_expone_source_url(settings: Settings):
    ch = Channel(
        id="uno.mx",
        name="Canal Uno",
        tvg_id="uno.mx",
        source_url="http://origen.invalid/con-token-secreto.m3u8",
        probe=ProbeResult(accessible=True, video_codec="h264", audio_codec="aac"),
    )
    state = AppState(settings=settings)
    state.monitor = HealthMonitor(settings)
    state.put_channels([ch])
    client = TestClient(create_app(state))
    datos = client.get("/channels").json()
    assert datos and "source_url" not in datos[0]
    assert datos[0]["id"] == "uno.mx"
    assert "con-token-secreto" not in client.get("/channels").text
    assert "con-token-secreto" not in client.get("/status").text


# ---------------------------------------------------------------------------
# RELE: empalme de la linea de tiempo al cambiar de fuente (2026-10-12)
# ---------------------------------------------------------------------------
def _segmento_ts(pts_inicial: int, cuadros: int = 3, paso: int = 3003, pid: int = 0x1E1) -> bytes:
    """Segmento sintetico: N paquetes PES con PTS creciente."""
    salida = bytearray()
    for k in range(cuadros):
        opc = bytearray(5)
        escribir_sello(opc, 0, pts_inicial + k * paso, 0b0010)
        cuerpo = (
            bytes([0x00, 0x00, 0x01, 0xE0, 0x00, 0x00, 0x80, 0x80, 0x05])
            + bytes(opc)
            + b"VIDEO" * 4
        )
        pkt = bytearray([0x47, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10]) + cuerpo
        pkt += b"\xff" * (188 - len(pkt))
        salida += pkt
    return bytes(salida)


def test_rele_sin_cambio_de_fuente_no_toca_los_bytes():
    """Con una sola fuente el remiendo es una pasada de LECTURA."""
    r = RelayHLS(["http://a/1.m3u8", "http://b/2.m3u8"], nombre="x")
    seg = _segmento_ts(100_000)
    assert r._remendar(seg) == seg
    assert r._remiendos == 0


def test_rele_empalma_la_linea_al_cambiar_de_fuente():
    """Al saltar de fuente, el primer PTS nuevo cae justo despues del ultimo."""
    r = RelayHLS(["http://a/1.m3u8", "http://b/2.m3u8"], nombre="x")
    r._remendar(_segmento_ts(100_000))
    ultimo_a = r._rem.ultimo_pts_emitido
    assert ultimo_a is not None
    r._indice = 1  # el rele salto de fuente
    salida = r._remendar(_segmento_ts(9_000_000))
    assert r._remiendos == 1
    assert primer_pts(salida) == (ultimo_a + r.paso_cuadro) % MOD
    # la separacion entre cuadros se conserva
    assert primer_pts(salida[188:]) == (ultimo_a + 2 * r.paso_cuadro) % MOD


def test_rele_remiendo_apagado_no_toca_nada():
    r = RelayHLS(["http://a/1.m3u8", "http://b/2.m3u8"], nombre="x", remendar=False)
    r._remendar(_segmento_ts(100_000))
    r._indice = 1
    seg = _segmento_ts(9_000_000)
    assert r._remendar(seg) == seg
    assert r._remiendos == 0


def test_rele_el_lector_no_se_adelanta_al_tope_con_clientes():
    """Con alguien viendo, el lector espera a que el colchon baje del tope.

    Sin esto se sueltan por la izquierda segmentos que el cliente no consumio:
    medido, 24.1 s de contenido perdidos en un cambio de fuente.
    """
    r = RelayHLS(["http://a/1.m3u8"], nombre="x")
    for i in range(1, 20):
        r._buf[i] = Segmento(i, f"u{i}", b"\x47" * 16, 8.0)
    r._frente, r._primero = 19, 5
    r._clientes[1] = (17, time.monotonic())
    inicio = time.monotonic()
    assert r._esperar_hueco(max_s=0.5) is None
    assert time.monotonic() - inicio >= 0.5, "debio esperar"
    # el cliente consume y el colchon baja del tope: el lector sigue
    for i in range(1, 6):
        del r._buf[i]
    inicio = time.monotonic()
    r._esperar_hueco(max_s=1.0)
    assert time.monotonic() - inicio < 0.1, "con hueco no debe esperar"


def test_rele_el_lector_no_espera_sin_clientes():
    """El precalentado tiene que seguir avanzando: no se frena nunca."""
    r = RelayHLS(["http://a/1.m3u8"], nombre="x")
    for i in range(1, 20):
        r._buf[i] = Segmento(i, f"u{i}", b"\x47" * 16, 8.0)
    inicio = time.monotonic()
    r._esperar_hueco(max_s=5.0)
    assert time.monotonic() - inicio < 0.05, "no debio esperar sin clientes"


def test_validar_xmltv_rechaza_programa_sin_titulo():
    """El DTD pide title+ y start REQUIRED: sin eso el archivo no valida."""
    import xml.etree.ElementTree as ET

    from src.epg import validar_xmltv

    root = ET.fromstring(
        '<tv><channel id="a"><display-name>A</display-name></channel>'
        '<programme start="20260101120000 +0000" channel="a"/></tv>'
    )
    with pytest.raises(ValueError):
        validar_xmltv(root)


def test_validar_xmltv_acepta_lo_que_el_dtd_permite():
    """`stop` es #IMPLIED (opcional) y no debe contar como falta."""
    import xml.etree.ElementTree as ET

    from src.epg import validar_xmltv

    root = ET.fromstring(
        '<tv><channel id="a"><display-name>A</display-name></channel>'
        '<programme start="20260101120000 +0000" channel="a">'
        "<title>Algo</title></programme></tv>"
    )
    conteo = validar_xmltv(root)
    assert conteo == {
        "canales": 1,
        "programas": 1,
        "programas_invalidos": 0,
        "programas_sin_canal": 0,
    }


def test_la_sonda_prueba_los_respaldos_si_la_principal_esta_caida(monkeypatch, settings: Settings):
    """Antes solo miraba la principal: un respaldo vivo no servia de nada.

    Medido el 2026-10-12 con Canal 5: principal 404, canal deshabilitado, y el
    respaldo nunca llego a ejecutarse porque el rele no arranca para un canal
    que ya salio del M3U.
    """
    from src import probe as probe_mod

    vistas: list[str] = []

    def falso(url, timeout=10.0, mode="liveness", _settings=None):
        vistas.append(url)
        if "muerta" in url:
            return ProbeResult(method=mode, accessible=False, error="HTTP 404 en " + url)
        return ProbeResult(method=mode, accessible=True, video_codec="h264", detail="ok")

    monkeypatch.setattr(probe_mod, "check_stream", falso)
    canal = Channel(id="x", name="X", source_url="http://muerta/1.m3u8")
    canal.backups = ["http://viva/2.m3u8"]
    resultado = probe_mod.check_channel(canal, settings)
    assert resultado.accessible is True
    assert resultado.respaldo_idx == 0, "debe decir que respondio el primer respaldo"
    assert vistas == ["http://muerta/1.m3u8", "http://viva/2.m3u8"]


def test_la_sonda_no_toca_el_respaldo_si_la_principal_responde(monkeypatch, settings: Settings):
    from src import probe as probe_mod

    vistas: list[str] = []

    def falso(url, timeout=10.0, mode="liveness", _settings=None):
        vistas.append(url)
        return ProbeResult(method=mode, accessible=True)

    monkeypatch.setattr(probe_mod, "check_stream", falso)
    canal = Channel(id="x", name="X", source_url="http://viva/1.m3u8")
    canal.backups = ["http://otra/2.m3u8"]
    resultado = probe_mod.check_channel(canal, settings)
    assert resultado.respaldo_idx == -1
    assert vistas == ["http://viva/1.m3u8"], "no debe probar el respaldo de gratis"


def test_la_sonda_reporta_los_errores_de_todos(monkeypatch, settings: Settings):
    from src import probe as probe_mod

    def falso(url, timeout=10.0, mode="liveness", _settings=None):
        return ProbeResult(method=mode, accessible=False, error="HTTP 404 en " + url)

    monkeypatch.setattr(probe_mod, "check_stream", falso)
    canal = Channel(id="x", name="X", source_url="http://a/1.m3u8")
    canal.backups = ["http://b/2.m3u8"]
    resultado = probe_mod.check_channel(canal, settings)
    assert resultado.accessible is False
    assert resultado.respaldo_idx == -1
    assert "[0]" in resultado.error and "[1]" in resultado.error


def test_el_rele_detecta_la_rendicion_de_audio_aparte():
    """66 de 197 canales de Pluto publican el audio en su propia playlist.

    El master lo declara con EXT-X-MEDIA:TYPE=AUDIO y la variante lo referencia
    con AUDIO="..."; el video de esa variante NO trae audio.
    """
    master = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",DEFAULT=YES,'
        'URI="audio/audio/English/audio.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=3321280,AUDIO="audio"\n'
        "3321280/playlist.m3u8\n"
    )
    url = RelayHLS._rendicion_audio(master, "https://cdn.tv/v2/stitch/hls/channel/abc/master.m3u8")
    assert url == "https://cdn.tv/v2/stitch/hls/channel/abc/audio/audio/English/audio.m3u8"


def test_el_rele_no_inventa_rendicion_cuando_no_la_hay():
    """El otro 66%: la variante trae el audio muxed y no hay nada que juntar."""
    master = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",URI="subs/es.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=3321280,SUBTITLES="subs"\n'
        "3321280/playlist.m3u8\n"
    )
    assert RelayHLS._rendicion_audio(master, "https://cdn.tv/x/master.m3u8") == ""


def test_rele_el_empalme_se_encadena():
    """Un tercer salto no vuelve a la linea de tiempo original."""
    r = RelayHLS(["http://a/1.m3u8", "http://b/2.m3u8"], nombre="x")
    r._remendar(_segmento_ts(1_000))
    r._indice = 1
    r._remendar(_segmento_ts(500_000))
    tras_uno = r._rem.ultimo_pts_emitido
    r._indice = 0
    salida = r._remendar(_segmento_ts(1_000))
    assert r._remiendos == 2
    assert primer_pts(salida) == (tras_uno + r.paso_cuadro) % MOD
    assert primer_pts(salida) > 1_000, "no volvio a la linea original"
