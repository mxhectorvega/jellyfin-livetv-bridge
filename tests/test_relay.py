"""Pruebas del rele HLS (parseo; el transporte se mide en banco)."""

from __future__ import annotations

from src.relay import RelayHLS

PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-TARGETDURATION:3\n"
    "#EXT-X-MEDIA-SEQUENCE:3251\n"
    "#EXTINF:2.969,\n"
    "213910726_2c7b.ts\n"
    "#EXTINF:3.0,\n"
    "http://otro.host/absoluto.ts\n"
)


def test_segmentos_del_playlist_resuelve_relativas_y_guarda_duracion():
    refs = RelayHLS._segmentos_del_playlist(PLAYLIST, "http://host:8000/play/a06e/67249697.m3u8")
    assert [r.url for r in refs] == [
        "http://host:8000/play/a06e/213910726_2c7b.ts",
        "http://otro.host/absoluto.ts",
    ]
    assert [r.dur for r in refs] == [2.969, 3.0]
    # Sin EXT-X-KEY no hay llave; el numero de secuencia viene del playlist.
    assert [r.key_uri for r in refs] == ["", ""]
    assert [r.seq for r in refs] == [3251, 3252]


def test_segmentos_del_playlist_lee_la_llave_aes_por_segmento():
    """El origen manda una llave AES-128 con su IV en CADA segmento."""
    texto = (
        "#EXTM3U\n"
        "#EXT-X-MEDIA-SEQUENCE:7157\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://k.cdn/x/aes.key",IV=0x00000000000000000000000000001BF5\n'
        "#EXTINF:6.0,\n"
        "media_07157.ts\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://k.cdn/x/aes.key",IV=0x00000000000000000000000000001BF6\n'
        "#EXTINF:6.0,\n"
        "media_07158.ts\n"
    )
    refs = RelayHLS._segmentos_del_playlist(texto, "https://cdn/x/index.m3u8")
    assert [r.key_uri for r in refs] == ["https://k.cdn/x/aes.key"] * 2
    assert [r.iv for r in refs] == [
        "00000000000000000000000000001bf5",
        "00000000000000000000000000001bf6",
    ]
    assert [r.seq for r in refs] == [7157, 7158]


def test_mejor_variante_elige_la_de_mayor_bandwidth():
    """El master trae 480p, 360p y 720p: hay que quedarse con 720p."""
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1623600,RESOLUTION=854x480\n"
        "https://cdn/v/480.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=915200,RESOLUTION=640x360\n"
        "https://cdn/v/360.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2787400,RESOLUTION=1280x720\n"
        "https://cdn/v/720.m3u8\n"
    )
    assert RelayHLS._mejor_variante(master, "https://cdn/master.m3u8") == "https://cdn/v/720.m3u8"


def test_es_master_distingue_master_de_playlist_media():
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2759000\nhttp://h/v.m3u8\n"
    assert RelayHLS._es_master(master)
    assert not RelayHLS._es_master(PLAYLIST)
