"""Pruebas de la etapa de VALIDACION (probe)."""

from __future__ import annotations

import json

from src import probe as probe_mod
from src.probe import (
    absolute,
    analyze_manifest,
    check_stream,
    probe_ffprobe,
    probe_liveness,
    signature,
)

MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nv/720.m3u8\n"
VARIANT = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:5.0,\nseg0.ts\n"
SEGMENT = b"\x47" + b"\x00" * 200_000


def test_signature_clasifica_contenedores():
    assert signature(SEGMENT) == "TS"
    assert signature(b"\x00\x00\x00\x18ftypmp42") == "fMP4"
    assert signature(b"#EXTM3U\n") == "m3u8"
    assert signature(b"") == "vacio"
    assert signature(b"<h1>404 Not Found</h1>") == "404"


def test_absolute_resuelve_relativas():
    assert absolute("https://h/a/b/master.m3u8", "v/720.m3u8") == "https://h/a/b/v/720.m3u8"
    assert absolute("https://h/a/master.m3u8", "https://otro/v.m3u8") == "https://otro/v.m3u8"


def test_analyze_manifest_detecta_ext_x_key():
    flags = analyze_manifest('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.bin"\n')
    assert flags["encrypted"] is True


def test_analyze_manifest_detecta_drm_y_token():
    flags = analyze_manifest("#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI=widevine\n")
    assert flags["drm"] is True
    token = analyze_manifest("https://x/master.m3u8?jwt=abc.def.ghi&masterJWTPassthrough=true")
    assert token["signed_token"] is True


def test_probe_liveness_vivo(monkeypatch):
    def fake_get(url, timeout=10, headers=None, max_bytes=0, byte_range=None):
        if url.endswith("master.m3u8"):
            return MASTER.encode()
        if url.endswith("720.m3u8"):
            return VARIANT.encode()
        return SEGMENT

    monkeypatch.setattr(probe_mod, "http_get", fake_get)
    res = probe_liveness("https://example.invalid/master.m3u8", timeout=1)
    assert res.accessible is True
    assert res.container == "mpegts"
    assert "TS" in res.detail


def test_probe_liveness_segmento_chico_muere(monkeypatch):
    monkeypatch.setattr(
        probe_mod,
        "http_get",
        lambda *a, **k: (
            VARIANT.encode()
            if a[0].endswith("720.m3u8")
            else (MASTER.encode() if a[0].endswith("master.m3u8") else b"\x47" + b"\x00" * 100)
        ),
    )
    res = probe_liveness("https://example.invalid/master.m3u8", timeout=1)
    assert res.accessible is False
    assert "piso" in res.error or "por debajo" in res.error


def test_probe_liveness_drm_no_reproducible(monkeypatch):
    drm_master = "#EXTM3U\n#EXT-X-SESSION-KEY:URI=widevine\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n"
    monkeypatch.setattr(
        probe_mod,
        "http_get",
        lambda *a, **k: (
            drm_master.encode()
            if a[0].endswith("master.m3u8")
            else (VARIANT.encode() if a[0].endswith("v.m3u8") else SEGMENT)
        ),
    )
    res = probe_liveness("https://example.invalid/master.m3u8", timeout=1)
    assert res.drm is True
    assert res.accessible is False


def test_probe_liveness_lista_directa_sin_ladder(monkeypatch):
    """Regresion: una playlist de segmentos SIN #EXT-X-STREAM-INF.

    Bug medido: la variable `base` referenciada antes de asignarse marcaba
    caido un canal vivo. Ahora el descenso resuelve la base contra la URL.
    """
    directa = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg000.ts\n#EXTINF:4.0,\nseg001.ts\n"
    monkeypatch.setattr(
        probe_mod,
        "http_get",
        lambda url, **k: directa.encode() if url.endswith("master.m3u8") else SEGMENT,
    )
    res = probe_liveness("https://example.invalid/master.m3u8", timeout=1)
    assert res.accessible is True
    assert res.container == "mpegts"


def test_probe_liveness_marca_autenticacion(monkeypatch):
    def boom(url, timeout=10, headers=None, max_bytes=0):
        raise RuntimeError(f"HTTP 401 en {url}")

    monkeypatch.setattr(probe_mod, "http_get", boom)
    res = probe_liveness("https://example.invalid/x", timeout=1)
    assert res.auth_required is True
    assert res.accessible is False


def test_probe_ffprobe_parsea_codecs(monkeypatch):
    salida = {
        "format": {"format_name": "mpegts", "bit_rate": "2500000"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
            {"codec_type": "audio", "codec_name": "aac", "bit_rate": "128000"},
        ],
    }
    monkeypatch.setattr(probe_mod, "run_cmd", lambda *a, **k: (0, json.dumps(salida).encode(), b""))
    res = probe_ffprobe("https://example.invalid/x", timeout=1)
    assert res.accessible is True
    assert res.video_codec == "h264"
    assert res.audio_codec == "aac"
    assert res.resolution == "1280x720"


def test_probe_ffprobe_error_no_lanza(monkeypatch):
    monkeypatch.setattr(probe_mod, "run_cmd", lambda *a, **k: (1, b"", b"connection refused"))
    res = probe_ffprobe("https://example.invalid/x", timeout=1)
    assert res.accessible is False
    assert "connection refused" in res.error


def test_check_stream_modo_ffprobe_marca_protegido(monkeypatch):
    salida = {
        "format": {"format_name": "hls"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1, "height": 1}],
    }
    monkeypatch.setattr(probe_mod, "run_cmd", lambda *a, **k: (0, json.dumps(salida).encode(), b""))
    monkeypatch.setattr(
        probe_mod,
        "http_get",
        lambda *a, **k: b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:1,\ns.ts\n',
    )
    res = check_stream("https://example.invalid/x", timeout=1, mode="ffprobe")
    assert res.encrypted is True
    assert res.drm is False
    # AES-128 es cifrado estandar de HLS: se marca, pero NO bloquea.
    assert res.accessible is True


def test_analyze_manifest_drmcapabilities_no_es_drm():
    """Regresion: el query del reproductor declara capacidades DRM.

    Leerlo como DRM marcaba los 198 canales de Pluto como protegidos.
    """
    texto = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
        "v.m3u8?appName=web&DRMCapabilities=widevine&deviceModel=web\n"
    )
    flags = analyze_manifest(texto)
    assert flags["drm"] is False


def test_analyze_manifest_sample_aes_si_bloquea():
    flags = analyze_manifest('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"\n')
    assert flags["drm"] is True


def test_probe_liveness_anidamiento_tres_niveles(monkeypatch):
    """Pluto: master -> playlist de chunks -> segmento.

    Regresion: con un solo nivel de descenso se reportaba
    'manifiesto sin segmentos' en 194 de 208 canales vivos.
    """
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nchunks/720.m3u8\n"
    chunks = "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5.0,\nseg0.ts\n"

    def fake_get(url, timeout=10, headers=None, max_bytes=0, byte_range=None):
        if url.endswith("master.m3u8"):
            return master.encode()
        if url.endswith("720.m3u8"):
            return chunks.encode()
        return b"\x47" + b"\x00" * 300_000

    monkeypatch.setattr(probe_mod, "http_get", fake_get)
    res = probe_liveness("https://example.invalid/master.m3u8", timeout=1)
    assert res.accessible is True
    assert res.container == "mpegts"


def test_probe_liveness_segmento_cifrado_sin_firma(monkeypatch):
    """AES-128: los bytes no tienen firma TS pero el canal NO esta muerto."""
    playlist = (
        '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://k/key"\n'
        "#EXT-X-TARGETDURATION:5\n#EXTINF:5.0,\nseg0.ts\n"
    )
    cifrado = b"\xd5" + b"\x00" * 300_000
    monkeypatch.setattr(
        probe_mod,
        "http_get",
        lambda url, **k: playlist.encode() if url.endswith("master.m3u8") else cifrado,
    )
    res = probe_liveness("https://example.invalid/master.m3u8", timeout=1)
    assert res.accessible is True
    assert res.encrypted is True


def test_check_stream_marca_token_en_la_url(monkeypatch):
    monkeypatch.setattr(probe_mod, "http_get", lambda *a, **k: SEGMENT)
    # Sin red: la sonda de metadatos se sustituye por una vacia.
    monkeypatch.setattr(
        probe_mod, "probe_ffprobe", lambda *a, **k: probe_mod.ProbeResult(method="ffprobe")
    )
    res = check_stream("https://example.invalid/x.ts?jwt=aaa.bbb.ccc", timeout=1, mode="liveness")
    assert res.signed_token is True
    assert res.accessible is True


def test_probe_liveness_reintenta_manifiesto_vacio(monkeypatch):
    """El CDN de Pluto devuelve manifiestos vacios bajo carga.

    Medido: 6/6 secuencial contra 1/6 con seis hilos simultaneos. El reintento
    es lo que convierte ese fallo transitorio en un veredicto correcto.
    """
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nv.m3u8\n"
    variante = "#EXTM3U\n#EXTINF:5.0,\nseg0.ts\n"
    vacio = b"#EXTM3U\n"
    estado = {"n": 0}

    def fake_get(url, timeout=10, headers=None, max_bytes=0, byte_range=None):
        if url.endswith("master.m3u8"):
            estado["n"] += 1
            return vacio if estado["n"] == 1 else master.encode()
        if url.endswith("v.m3u8"):
            return variante.encode()
        return b"\x47" + b"\x00" * 300_000

    monkeypatch.setattr(probe_mod, "http_get", fake_get)
    monkeypatch.setattr(probe_mod, "REINTENTO_ESPERA_S", 0.0)
    res = probe_liveness("https://example.invalid/master.m3u8", timeout=1)
    assert estado["n"] >= 2
    assert res.accessible is True
