"""Pruebas de la GUIA (EPG): parseo, empate de ids y generacion de XMLTV."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC

from src.config import Settings
from src.epg import (
    build_name_index,
    fetch_epg,
    map_channels,
    parse_xmltv,
    render_xmltv,
)
from src.models import Channel

XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="test">
  <channel id="DemoUno.example"><display-name>Canal Uno</display-name></channel>
  <channel id="DemoDos.example"><display-name>Canal Dos</display-name></channel>
  <programme start="20260101120000 +0000" stop="20260101130000 +0000" channel="DemoUno.example">
    <title>Programa Uno</title>
  </programme>
  <programme start="20260101130000 +0000" stop="20260101140000 +0000" channel="DemoUno.example">
    <title>Programa Uno</title>
  </programme>
</tv>
"""


def test_parse_xmltv_indexa_canales_y_programas():
    bundle = parse_xmltv(XMLTV.encode())
    assert "DemoUno.example" in bundle.channels
    assert bundle.channels["DemoUno.example"] == ["Canal Uno"]
    # Los programas son de 2026-01-01: quedan fuera de la ventana actual.
    assert bundle.programmes.get("DemoUno.example", []) == []


def test_parse_xmltv_conserva_programas_vigentes():
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).strftime("%Y%m%d%H%M%S +0000")
    stop = (now + timedelta(hours=1)).strftime("%Y%m%d%H%M%S +0000")
    xml = (
        '<?xml version="1.0"?><tv>'
        '<channel id="a"><display-name>A</display-name></channel>'
        f'<programme start="{start}" stop="{stop}" channel="a"><title>Ahora</title></programme>'
        '<programme start="20200101000000 +0000" '
        'stop="20200101010000 +0000" channel="a"><title>Viejo</title></programme>'
        "</tv>"
    )
    bundle = parse_xmltv(xml.encode())
    titulos = [p.findtext("title") for p in bundle.programmes["a"]]
    assert "Ahora" in titulos
    assert "Viejo" not in titulos


def test_build_name_index_descarta_ambiguos():
    indice = build_name_index({"id1": ["Canal Común"], "id2": ["Canal Común"], "id3": ["Único"]})
    assert "canalcomun" not in indice
    assert indice["unico"] == "id3"


def test_map_channels_por_mapeo_explicito():
    bundle = parse_xmltv(XMLTV.encode())
    ch = Channel(name="Lo Que Sea", tvg_id="demo.uno.mx", source_url="u")
    reporte = map_channels([ch], bundle, {"demo.uno.mx": "DemoUno.example"})
    # epg_id guarda el id EN LA FUENTE; el id publicado sigue siendo el tvg_id.
    assert ch.epg_id == "DemoUno.example"
    assert ch.tvg_id == "demo.uno.mx"
    assert reporte["con_guia"] == 1


def test_map_channels_por_nombre_cuando_no_hay_mapeo():
    bundle = parse_xmltv(XMLTV.encode())
    ch = Channel(name="Canal Dos", tvg_id="otro.espacio", source_url="u")
    map_channels([ch], bundle, {})
    assert ch.epg_id == "DemoDos.example"


def test_map_channels_reporta_sin_guia():
    bundle = parse_xmltv(XMLTV.encode())
    ch = Channel(name="Canal Sin Guia", tvg_id="nada.mx", source_url="u")
    reporte = map_channels([ch], bundle, {})
    assert ch.epg_id == ""
    assert reporte["sin_guia"] == 1


def test_fetch_epg_fusiona_y_reporta_caidas(monkeypatch, tmp_path):
    from src import epg as epg_mod

    def fake_get(url, timeout=30, headers=None, max_bytes=0):
        if "caida" in url:
            raise RuntimeError("HTTP 404")
        return XMLTV.encode()

    monkeypatch.setattr(epg_mod, "http_get", fake_get)
    bundle = fetch_epg(
        [
            {"name": "ok", "url": "https://example.invalid/guia.xml"},
            {"name": "caida", "url": "https://example.invalid/caida.xml"},
        ]
    )
    assert bundle.sources_ok == ["ok"]
    assert bundle.sources_failed == ["caida"]


def test_render_xmltv_usa_ids_propios_y_deduplica(settings: Settings):
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).strftime("%Y%m%d%H%M%S +0000")
    stop = (now + timedelta(hours=1)).strftime("%Y%m%d%H%M%S +0000")
    xml = (
        '<tv><channel id="src1"><display-name>Canal Uno</display-name></channel>'
        f'<programme start="{start}" stop="{stop}" channel="src1">'
        "<title>Programa Uno</title></programme>"
        f'<programme start="{start}" stop="{stop}" channel="src1">'
        "<title>Programa Uno</title></programme>"
        "</tv>"
    )
    bundle = parse_xmltv(xml.encode())
    ch = Channel(name="Canal Uno", tvg_id="canal.uno", epg_id="src1", source_url="u")
    texto = render_xmltv([ch], bundle, settings)

    root = ET.fromstring(texto)
    ids = [c.get("id") for c in root.findall("channel")]
    # El id publicado es el tvg-id del M3U (lo que Jellyfin une).
    assert ids == ["canal.uno"]
    # Dos programas iguales -> uno solo tras la dedupe.
    assert len(root.findall("programme")) == 1
    assert root.find("programme").get("channel") == "canal.uno"
    assert settings.epg_file.exists()


def test_render_xmltv_publica_una_sola_parrilla_sin_traslapes(settings: Settings):
    """Una fuente con dos rejillas intercaladas no debe encimar programas.

    Medido en produccion: Canal del Congreso (iptv-epg MX) traia 27 traslapes
    sobre 83 programas, y Jellyfin los dibuja encimados.
    """
    from datetime import datetime, timedelta

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y%m%d%H%M%S +0000"

    def prog(offset_h: float, dur_h: float, titulo: str) -> str:
        ini = (now + timedelta(hours=offset_h)).strftime(fmt)
        fin = (now + timedelta(hours=offset_h + dur_h)).strftime(fmt)
        return (
            f'<programme start="{ini}" stop="{fin}" channel="src1">'
            f"<title>{titulo}</title></programme>"
        )

    xml = (
        '<tv><channel id="src1"><display-name>Congreso</display-name></channel>'
        + prog(0, 1, "Rejilla A1")
        + prog(1, 1, "Rejilla A2")
        + prog(0.5, 0.5, "Rejilla B1")
        + prog(1.5, 0.5, "Rejilla B2")
        + "</tv>"
    )
    bundle = parse_xmltv(xml.encode())
    ch = Channel(name="Congreso", tvg_id="congreso.mx", epg_id="src1", source_url="u")
    texto = render_xmltv([ch], bundle, settings)

    root = ET.fromstring(texto)
    titulos = [p.findtext("title") for p in root.findall("programme")]
    assert titulos == ["Rejilla A1", "Rejilla A2"]

    spans = [(p.get("start"), p.get("stop")) for p in root.findall("programme")]
    for (_, fin_previo), (inicio, _) in zip(spans, spans[1:], strict=False):
        assert fin_previo <= inicio


def test_render_xmltv_no_sobrescribe_si_queda_vacio(settings: Settings):
    import pytest

    bundle = parse_xmltv(XMLTV.encode())
    ch = Channel(name="Sin guia", tvg_id="x", epg_id="", source_url="u")
    with pytest.raises(ValueError):
        render_xmltv([ch], bundle, settings)
    assert not settings.epg_file.exists()
