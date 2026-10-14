"""Pruebas del vigilante de estancamiento del rele.

Medido el 2026-10-14 con el CDN de Pluto: el playlist variante se VACIA en la
segunda lectura seguida —devuelve 200 con cero segmentos, que no es una
excepcion— y basta con volver a pedir el master para que lo rearme. El rele
pedia el master UNA sola vez y luego sondeaba la variante cada 0.5 s, asi que
bajaba los primeros segmentos y el canal se quedaba CONGELADO para siempre, sin
un solo error en el log. Eso es lo que el usuario veia como "canal pausado".
"""

from __future__ import annotations

import time

from src.relay import ESTANCADO_S, RelayHLS

MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nv.m3u8\n"
VACIO = "#EXTM3U\n"


def _rele_con_manifiesto(manifiesto):
    """Rele con el manifiesto sustituido: sin red y con el CDN simulado."""
    r = RelayHLS(["https://origen.invalid/master.m3u8"], nombre="prueba")
    r._leer_manifiesto = lambda url, *a, **k: manifiesto(url)
    return r


def _espera(condicion, limite_s: float = 6.0) -> bool:
    fin = time.monotonic() + limite_s
    while time.monotonic() < fin:
        if condicion():
            return True
        time.sleep(0.1)
    return False


def test_el_vigilante_re_resuelve_cuando_el_frente_se_estanca():
    """Con clientes esperando y el playlist vacio, tiene que curarse solo."""
    r = _rele_con_manifiesto(lambda url: MASTER if url.endswith("master.m3u8") else VACIO)
    r.arrancar()
    try:
        # El cliente se engancha y el frente ya lleva rato sin moverse: asi se ve
        # un canal que se quedo pausado.
        with r._lock:
            r._clientes[1] = (0, time.monotonic())
            r._ultimo_avance = time.monotonic() - (ESTANCADO_S + 5)

        assert _espera(lambda: r._estancamientos >= 1), "no detecto el estancamiento"
        assert r._refrescar_token is True, "no pidio re-resolver el master"
        assert r._variante == "", "no limpio la variante para forzar la re-resolucion"
    finally:
        r.detener()
    assert r._estancamientos >= 1


def test_el_vigilante_no_dispara_sin_clientes():
    """Un rele precalentado y sin nadie viendo no tiene por que re-resolver.

    Sin esta cota, el precalentado de 16 canales se pasaria la vida pidiendo
    masters y golpearia el CDN sin motivo.
    """
    r = _rele_con_manifiesto(lambda url: MASTER if url.endswith("master.m3u8") else VACIO)
    r.arrancar()
    try:
        with r._lock:
            r._ultimo_avance = time.monotonic() - (ESTANCADO_S + 5)
            r._clientes.clear()
        time.sleep(1.5)
    finally:
        r.detener()
    assert r._estancamientos == 0, "re-resolvio sin tener a quien servir"


def test_el_contador_de_estancamientos_sale_en_stats():
    r = _rele_con_manifiesto(lambda url: MASTER if url.endswith("master.m3u8") else VACIO)
    r.arrancar()
    try:
        with r._lock:
            r._clientes[1] = (0, time.monotonic())
            r._ultimo_avance = time.monotonic() - (ESTANCADO_S + 5)
        assert _espera(lambda: r._estancamientos >= 1)
        assert r.stats()["estancamientos"] >= 1
    finally:
        r.detener()
