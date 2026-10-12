"""Pruebas de los respaldos de fuente (asociacion y orden)."""

from __future__ import annotations

from src.models import Channel
from src.normalize import aplicar_respaldos, name_key, normalize, normalize_tvg_id


def test_respaldos_por_id_tvg_id_y_nombre():
    chans = [
        Channel(name="Canal Once", tvg_id="xeipntdt.mx"),
        Channel(name="Canal 22", tvg_id="canal.22.de.mxico.mx"),
        Channel(name="Sin Respaldo", tvg_id="otro.mx"),
    ]
    respaldos = {
        "xeipntdt.mx": ["https://alt/once.m3u8"],
        "Canal 22": ["https://alt/22a.m3u8", "https://alt/22b.m3u8"],
    }
    aplicar_respaldos(chans, respaldos)
    assert chans[0].backups == ["https://alt/once.m3u8"]
    # Por nombre visible, y el ORDEN se conserva.
    assert chans[1].backups == ["https://alt/22a.m3u8", "https://alt/22b.m3u8"]
    assert chans[2].backups == []


def test_respaldos_no_inventan_nada_sin_bloque():
    chans = [Channel(name="Canal 5", tvg_id="canal.5.de.mxico.xhgc.mx")]
    aplicar_respaldos(chans, None)
    assert chans[0].backups == []


def test_normalize_conserva_los_respaldos():
    from src.config import Settings

    chans = [Channel(name="Canal 22", tvg_id="Canal.22.de.México.mx", source_url="u")]
    chans = aplicar_respaldos(chans, {"canal.22.de.mxico.mx": ["https://alt/x.m3u8"]})
    chans, _ = normalize(chans, Settings())
    assert chans[0].id == "canal.22.de.mxico.mx"
    assert chans[0].backups == ["https://alt/x.m3u8"]
    # El id publicado coincide con la clave usada en sources.yml.
    assert normalize_tvg_id("Canal.22.de.México.mx") == "canal.22.de.mxico.mx"
    assert name_key("Canal 22") == "canal22"
