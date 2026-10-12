"""Pruebas de la etapa de INGESTA."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.config import Settings
from src.ingest import (
    channels_from_config,
    channels_from_sources,
    ingest,
    load_config_file,
    parse_m3u,
)

M3U = textwrap.dedent(
    """
    #EXTM3U
    #EXTINF:-1 tvg-id="uno.mx" tvg-name="Canal Uno HD" tvg-logo="https://x.invalid/1.png" group-title="Nacionales",Canal Uno (720p)
    https://example.invalid/uno/master.m3u8
    #EXTINF:-1 group-title="Deportes",Canal Dos FHD
    https://example.invalid/dos/index.m3u8
    #EXTVLCOPT:http-user-agent=VLC
    #EXTINF:-1,Canal Tres
    https://example.invalid/tres.ts
    """
).strip()


def test_parse_m3u_lee_atributos_y_nombre():
    canales = parse_m3u(M3U, source_name="demo")
    assert len(canales) == 3
    uno = canales[0]
    assert uno.tvg_id == "uno.mx"
    assert uno.tvg_name == "Canal Uno HD"
    assert uno.group == "Nacionales"
    assert uno.logo == "https://x.invalid/1.png"
    assert uno.name == "Canal Uno (720p)"
    assert uno.source == "demo"
    # El canal sin group-title usa el grupo por defecto de la fuente.
    assert canales[1].group == "Deportes"
    assert canales[2].name == "Canal Tres"
    assert canales[2].group == "General"


def test_parse_m3u_no_confunde_directivas_con_urls():
    canales = parse_m3u(M3U)
    urls = {c.source_url for c in canales}
    assert urls == {
        "https://example.invalid/uno/master.m3u8",
        "https://example.invalid/dos/index.m3u8",
        "https://example.invalid/tres.ts",
    }


def test_parse_m3u_usa_nombre_de_respaldo_si_falta():
    canales = parse_m3u("#EXTM3U\n#EXTINF:-1,\nhttps://example.invalid/mi_canal_uno/master.m3u8")
    assert canales[0].name == "mi canal uno"


def test_channels_from_config_lee_metadatos_completos():
    cfg = {
        "channels": [
            {
                "name": "Canal Demo",
                "url": "https://example.invalid/demo.m3u8",
                "tvg_id": "demo.mx",
                "group": "Demo",
                "logo": "https://example.invalid/logo.png",
                "country": "MX",
                "language": "es",
                "profile": "h264",
            }
        ]
    }
    canales = channels_from_config(cfg)
    assert len(canales) == 1
    assert canales[0].profile == "h264"
    assert canales[0].country == "MX"
    assert canales[0].source == "config"


def test_channels_from_config_ignora_entradas_sin_url():
    assert channels_from_config({"channels": [{"name": "sin url"}]}) == []


def test_channels_from_sources_soporta_archivo_local(monkeypatch, tmp_path: Path):
    lista = tmp_path / "lista.m3u"
    lista.write_text("#EXTM3U\n#EXTINF:-1 tvg-id=\"a.mx\",Canal A\nhttps://example.invalid/a.m3u8\n", encoding="utf-8")
    canales = channels_from_sources([{"name": "local", "url": str(lista), "default_group": "Local"}])
    assert len(canales) == 1
    assert canales[0].group == "Local"
    assert canales[0].source == "local"


def test_channels_from_sources_una_fuente_caida_no_tumba_el_resto(monkeypatch, tmp_path: Path):
    buena = tmp_path / "b.m3u"
    buena.write_text("#EXTM3U\n#EXTINF:-1,Canal B\nhttps://example.invalid/b.m3u8\n", encoding="utf-8")
    canales = channels_from_sources(
        [
            {"name": "caida", "url": "https://example.invalid/no-existe.m3u"},
            {"name": "buena", "url": str(buena)},
        ]
    )
    assert [c.name for c in canales] == ["Canal B"]


def test_ingest_prioriza_canales_explicitos(settings: Settings):
    cfg = {
        "channels": [{"name": "Explicito", "url": "https://example.invalid/e.m3u8", "tvg_id": "dup.mx"}],
        "sources": [],
    }
    canales = ingest(settings, cfg)
    assert len(canales) == 1
    assert canales[0].name == "Explicito"


def test_load_config_file_rechaza_raiz_no_mapa(tmp_path: Path):
    ruta = tmp_path / "malo.yml"
    ruta.write_text("- solo\n- lista\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config_file(ruta)


def test_load_config_file_falta_el_archivo(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config_file(tmp_path / "nope.yml")


def test_sources_example_es_yaml_valido():
    ejemplo = Path(__file__).resolve().parents[1] / "config" / "sources.example.yml"
    cfg = load_config_file(ejemplo)
    assert "sources" in cfg and "channels" in cfg
    canales = channels_from_config(cfg)
    # El ejemplo trae tres canales ficticios.
    assert len(canales) == 3
    # Y un canal marcado como DRM a proposito.
    assert any("drm" in c.tvg_id for c in canales)
