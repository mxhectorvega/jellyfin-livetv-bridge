"""Pruebas de la GENERACION de la playlist M3U."""

from __future__ import annotations

import urllib.parse

from src.config import Settings
from src.models import Channel
from src.playlist import build_m3u, channel_url, select_for_output, write_playlist


def _canal(**kw) -> Channel:
    base = dict(
        id="uno.mx",
        name="Canal Uno",
        source_url="https://origen.invalid/u.m3u8",
        tvg_id="uno.mx",
        group="Demo",
    )
    base.update(kw)
    return Channel(**base)


def test_channel_url_apunta_al_proxy(settings: Settings):
    ch = _canal()
    assert channel_url(ch, settings) == "http://test.local/stream/uno.mx"


def test_channel_url_urlencondea_el_id(settings: Settings):
    ch = _canal(id="canal 5 (mx)")
    assert channel_url(ch, settings) == "http://test.local/stream/" + urllib.parse.quote(
        "canal 5 (mx)"
    )


def test_build_m3u_estructura_y_atributos(settings: Settings):
    texto = build_m3u([_canal(logo="https://x.invalid/l.png", tvg_name="Uno")], settings)
    lineas = texto.splitlines()
    assert lineas[0] == "#EXTM3U"
    assert 'tvg-id="uno.mx"' in lineas[1]
    assert 'group-title="Demo"' in lineas[1]
    assert ",Canal Uno" in lineas[1]
    assert lineas[2] == "http://test.local/stream/uno.mx"
    assert texto.endswith("\n")


def test_build_m3u_excluye_deshabilitados(settings: Settings):
    canales = [_canal(), _canal(id="dos.mx", name="Dos", enabled=False)]
    texto = build_m3u(select_for_output(canales, settings), settings)
    assert "Dos" not in texto
    assert texto.count("#EXTINF") == 1


def test_select_for_output_excluye_protegidos_si_no_se_incluyen(settings: Settings):
    settings.include_protected = False
    ch = _canal()
    ch.probe.drm = True
    assert select_for_output([ch], settings) == []
    settings.include_protected = True
    assert len(select_for_output([ch], settings)) == 1


def test_write_playlist_escribe_atomico(settings: Settings):
    n = write_playlist([_canal()], settings)
    assert n == 1
    assert settings.playlist_file.exists()
    contenido = settings.playlist_file.read_text(encoding="utf-8")
    assert contenido.startswith("#EXTM3U")
    # No debe quedar el temporal.
    assert not settings.playlist_file.with_name(settings.playlist_file.name + ".tmp").exists()
