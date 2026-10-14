"""Pruebas de la etapa de NORMALIZACION."""

from __future__ import annotations

from src.config import Settings
from src.models import Channel
from src.normalize import (
    apply_group_filters,
    clean_name,
    dedupe,
    name_key,
    normalize,
    normalize_tvg_id,
)


def test_clean_name_recorta_calidad():
    assert clean_name("Canal Uno (720p)") == "Canal Uno"
    assert clean_name("Canal Dos [HD]") == "Canal Dos"
    assert clean_name("Canal Tres FHD") == "Canal Tres"
    assert clean_name("Canal  Cuatro") == "Canal Cuatro"


def test_normalize_tvg_id_quita_sufijos_y_ruido():
    assert normalize_tvg_id("Canal5.mx@SD") == "canal5.mx"
    assert normalize_tvg_id("MX: Canal 5 HD") == "mxcanal5hd"
    assert normalize_tvg_id("") == ""


def test_name_key_ignora_acentos_y_puntuacion():
    assert name_key("México TV") == name_key("Mexico-TV") == "mexicotv"


def test_dedupe_por_tvg_id_conserva_el_primero():
    a = Channel(name="A", tvg_id="dup.mx", source_url="u1")
    b = Channel(name="B", tvg_id="dup.mx", source_url="u2")
    unicos, removed = dedupe([a, b])
    assert removed == 1
    assert unicos[0].source_url == "u1"


def test_dedupe_por_nombre_cuando_no_hay_tvg_id():
    a = Channel(name="Canal Ñ", source_url="u1")
    b = Channel(name="canal n", source_url="u2")
    unicos, removed = dedupe([a, b])
    assert removed == 1


def test_dedupe_no_fusiona_canales_distintos():
    a = Channel(name="Canal Uno", tvg_id="uno.mx", source_url="u1")
    b = Channel(name="Canal Dos", tvg_id="dos.mx", source_url="u2")
    unicos, removed = dedupe([a, b])
    assert (len(unicos), removed) == (2, 0)


def test_apply_group_filters_es_regex_completo():
    canales = [
        Channel(name="A", group="Adultos", source_url="u"),
        Channel(name="B", group="Deportes", source_url="u"),
    ]
    kept, dropped = apply_group_filters(canales, [r"^(Adultos?|XXX)$"])
    assert dropped == 1
    assert [c.name for c in kept] == ["B"]


def test_apply_group_filters_no_arrastra_subcadenas():
    canales = [Channel(name="A", group="Deportes MX", source_url="u")]
    kept, dropped = apply_group_filters(canales, [r"^(Deportes)$"])
    assert dropped == 0 and len(kept) == 1


def test_normalize_asigna_id_estable_y_epg_id(settings: Settings):
    canales = [
        Channel(name="Canal Uno (720p)", tvg_id="uno.mx@SD", group="Deportes", source_url="u1"),
        Channel(name="Canal Dos", group="Deportes", source_url="u2"),
    ]
    salida, reporte = normalize(canales, settings)
    assert salida[0].id == "uno.mx"
    assert salida[0].tvg_id == "uno.mx"
    assert salida[0].name == "Canal Uno"
    # El epg_id queda vacio hasta que la etapa EPG empata con una fuente.
    assert salida[0].epg_id == ""
    assert salida[1].id == "canaldos"
    assert reporte["entrada"] == 2


def test_normalize_ids_unicos_ante_colision(settings: Settings):
    canales = [
        Channel(name="Canal 5", tvg_id="canal5", source_url="u1"),
        Channel(name="Canal5", tvg_id="canal5-", source_url="u2"),
    ]
    salida, _ = normalize(canales, settings)
    ids = [c.id for c in salida]
    assert len(set(ids)) == len(ids)
