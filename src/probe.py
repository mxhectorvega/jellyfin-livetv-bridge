"""Etapa 2 — VALIDACION de streams.

Dos niveles, deliberadamente separados:

- ``ffprobe``: rapido, da codecs/resolucion/bitrate DECLARADOS.
- ``liveness``: baja bytes de verdad (master -> variante -> segmento) y
  comprueba la firma del contenedor.

- Important: ffprobe NO prueba liveness. Un master HLS de plantilla contesta
  200 con un ladder falso y ffprobe lo reporta como si el flujo existiera
  (medido: 38 de 40 "vivos" que en realidad estaban muertos). Por eso el modo
  por omision es ``liveness``; el modo ``ffprobe`` solo sirve para pre-filtrar.
- Note: los streams con cifrado (EXT-X-KEY), DRM (ContentProtection /
  Widevine / PlayReady), token firmado o autenticacion requerida NO se
  descartan: se INCLUYEN marcados en el resultado, para que el operador sepa
  por que no reproducen. La copia de flujo no puede descifrar CENC.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Settings
from .models import Channel, ProbeResult
from .util import get_logger, http_get, now_iso, run_cmd

log = get_logger("probe")

FFPROBE = "ffprobe"
MIN_SEGMENT_BYTES = 60_000     # un segmento TS real pesa 2-4 MB; 60 KB es piso
MANIFEST_MAX_BYTES = 2 * 1024 * 1024
SEGMENT_MAX_BYTES = 8 * 1024 * 1024
# Se pide solo el inicio del segmento: alcanza para la firma y el piso, y evita
# bajar 2-4 MB por canal en cada barrido.
SEGMENT_RANGE = "bytes=0-262143"
MAX_HLS_NIVELES = 3            # Pluto anida master -> playlist de chunks -> segmento
REINTENTOS_LIVENESS = 4        # el CDN de Pluto devuelve manifiestos vacios bajo carga
REINTENTO_ESPERA_S = 0.8       # (medido: 22/24 con 2 hilos vs 1/12 con 6 hilos)

# Marcadores de proteccion en manifiestos. OJO: "DRMCapabilities=" es una
# capacidad DECLARADA POR EL CLIENTE en el query string, no una proteccion del
# contenido; leerlo como DRM daba falso positivo en todo el catalogo de Pluto.
_KEY_RE = re.compile(r"#EXT-X-KEY:[^\n]*METHOD\s*=\s*([A-Z0-9-]+)", re.I)
_DRM_RE = re.compile(r"(ContentProtection|widevine|playready|clearkey|pssh|"
                     r"licenseurl|drmtoday|#EXT-X-SESSION-KEY|SAMPLE-AES)", re.I)
_DRM_CAP_RE = re.compile(r"DRMCapabilities=[^&\"'\s]*", re.I)
_TOKEN_RE = re.compile(r"(?:[?&](jwt|token|hdnts|hdnea|exp|expires|sig|signature|"
                       r"auth|apikey|api_key|key)=)", re.I)


# ---------------------------------------------------------------------------
# Firmas de contenedor
# ---------------------------------------------------------------------------
def signature(data: bytes) -> str:
    """Clasifica un trozo de flujo por su firma binaria.

    - Discussion: es la unica prueba de que hay video de verdad: TS crudo
      empieza con ``0x47``, fMP4 trae ``ftyp`` en los bytes 4-8, y una lista
      HLS empieza con ``#EXTM3U``.
    """
    if not data:
        return "vacio"
    if data[:1] == b"\x47":
        return "TS"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "fMP4"
    if data[:7].lower() == b"#extm3u":
        return "m3u8"
    if data[:5] == b"\x00\x00\x00\x18" or data[:5] == b"\x00\x00\x00\x1c":
        return "fMP4"
    if b"<h1>404" in data[:40] or b"Not Found" in data[:60]:
        return "404"
    return "otro"


def absolute(base: str, rel: str) -> str:
    """Resuelve una URL relativa de un manifiesto contra su base."""
    return rel if rel.startswith("http") else urllib.parse.urljoin(base, rel)


def analyze_manifest(text: str) -> dict[str, bool]:
    """Detecta proteccion en el texto de un manifiesto HLS/DASH.

    - Discussion: un parametro en la URL NO prueba nada sobre el DRM (el
      reproductor puede pedir ``drm_scheme=none`` y el servidor ignorarlo);
      por eso se leen los marcadores del propio manifiesto.
    - Important: ``AES-128`` es el cifrado estandar de HLS y ffmpeg lo descifra
      solo (baja la llave del URI); se marca ``encrypted`` pero NO es un
      impedimento. Lo que bloquea de verdad es DRM/CENC/SAMPLE-AES (``drm``).
    - Note: ``DRMCapabilities=widevine`` en el query es una capacidad del
      cliente y se descarta antes de buscar, o marcaria todo Pluto como DRM.
    """
    saneado = _DRM_CAP_RE.sub("", text)
    methods = {m.upper() for m in _KEY_RE.findall(saneado)}
    aes128 = bool(methods & {"AES-128", "AES128"})
    fuerte = bool(methods & {"SAMPLE-AES", "SAMPLE-AES-CTR"})
    drm = fuerte or bool(_DRM_RE.search(saneado))
    return {
        "encrypted": aes128 or bool(methods),
        "drm": drm,
        "signed_token": bool(_TOKEN_RE.search(saneado)),
    }


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------
def probe_ffprobe(url: str, timeout: float = 10.0) -> ProbeResult:
    """Valida un stream con ffprobe (metadatos declarados).

    - Discussion: se le pasan ``-analyzeduration 3000000`` y
      ``-probesize 3000000`` (3 s), las MISMAS ventanas que usa Jellyfin al
      sondear; sondear sin ellas da falsos positivos.
    """
    cmd = [
        FFPROBE, "-v", "error", "-print_format", "json",
        "-analyzeduration", "3000000", "-probesize", "3000000",
        "-show_format", "-show_streams", url,
    ]
    code, out, err = run_cmd(cmd, timeout=timeout)
    result = ProbeResult(method="ffprobe", checked_at=now_iso())
    if code != 0 or not out.strip():
        result.error = (err or b"").decode("utf-8", "replace").strip()[:300] or f"ffprobe code {code}"
        return result
    try:
        info = json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        result.error = f"salida ffprobe ilegible: {exc}"
        return result

    fmt = info.get("format") or {}
    result.format_name = str(fmt.get("format_name") or "")
    result.container = result.format_name.split(",")[0]
    try:
        result.video_bitrate = int(fmt.get("bit_rate") or 0)
    except ValueError:
        result.video_bitrate = 0

    for stream in info.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and not result.video_codec:
            result.video_codec = str(stream.get("codec_name") or "")
            result.width = int(stream.get("width") or 0)
            result.height = int(stream.get("height") or 0)
            if stream.get("bit_rate"):
                try:
                    result.video_bitrate = int(stream["bit_rate"])
                except ValueError:
                    pass
            if result.width and result.height:
                result.resolution = f"{result.width}x{result.height}"
        elif kind == "audio" and not result.audio_codec:
            result.audio_codec = str(stream.get("codec_name") or "")
            if stream.get("bit_rate"):
                try:
                    result.audio_bitrate = int(stream["bit_rate"])
                except ValueError:
                    pass

    result.accessible = bool(result.video_codec)
    if not result.accessible:
        result.error = "ffprobe no encontro pista de video"
    return result


# ---------------------------------------------------------------------------
# Liveness real
# ---------------------------------------------------------------------------
def _descender(url: str, timeout: float, profundidad: int = MAX_HLS_NIVELES) -> tuple[str, bytes, dict[str, bool], str]:
    """Baja por los niveles de HLS hasta dar con un SEGMENTO real.

    - Discussion: Pluto anida tres niveles (master de calidades -> playlist de
      chunks -> segmentos). Asumir un solo nivel marca muertos canales vivos:
      el "ultimo" hijo del master era otra playlist, y el resultado era
      "manifiesto sin segmentos" en 194 de 208 canales.
    - Note: dentro de un nivel se prueban hasta 3 candidatos, del primero al
      ultimo: el primero suele ser la mejor calidad y estar disponible, mientras
      el ultimo puede ser una pista de audio o una variante vacia.
    - Returns: ``(url_segmento, bytes, flags, error)``. Con error no vacio,
      lo demas no es utilizable.
    """
    flags = {"encrypted": False, "drm": False, "signed_token": False}
    actual = url
    for _nivel in range(max(1, profundidad)):
        data = http_get(actual, timeout=timeout, max_bytes=MANIFEST_MAX_BYTES)
        kind = signature(data)
        if kind in ("TS", "fMP4"):
            return actual, data, flags, ""
        if kind != "m3u8":
            return "", b"", flags, f"respuesta inesperada (firma: {kind})"

        text = data.decode("utf-8", "replace")
        nivel_flags = analyze_manifest(text)
        for key, value in nivel_flags.items():
            flags[key] = flags[key] or value

        candidatos = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if not candidatos:
            return "", b"", flags, "manifiesto sin lineas utiles"

        ultimo_error = ""
        descendido = False
        for candidato in candidatos[:3]:
            hijo = absolute(actual, candidato)
            try:
                trozo = http_get(
                    hijo, timeout=timeout, max_bytes=SEGMENT_MAX_BYTES, byte_range=SEGMENT_RANGE
                )
            except Exception as exc:  # noqa: BLE001 — se prueba el siguiente candidato
                ultimo_error = str(exc)[:200]
                continue
            if signature(trozo) == "m3u8":
                actual = hijo          # es otro nivel de playlist: seguir bajando
                descendido = True
                break
            return hijo, trozo, flags, ""    # esto ya es el segmento
        if not descendido:
            return "", b"", flags, f"ningun candidato accesible ({ultimo_error or 'vacio'})"
    return "", b"", flags, f"anidamiento mayor a {profundidad} niveles"


def _reintentable(error: str) -> bool:
    """Indica si un fallo de liveness vale la pena reintentar.

    - Discussion: medido en este homelab, el CDN de Pluto responde con
      manifiestos VACIOS cuando se piden muchos canales a la vez (6 de 6
      secuencialmente, 1 de 6 con seis hilos). Es transitorio y se arregla
      reintentando; un 404 o un DNS que no resuelve no se reintenta igual.
    """
    text = (error or "").lower()
    if not text:
        return False
    # 404/416 son de RUTA y 401/403 de PERMISO: reintentar la misma URL no los
    # arregla. 408 y 429 son de CARGA y si valen la pena, asi que se exceptuan
    # antes del atajo por prefijo "http 4".
    if "http 4" in text and not any(m in text for m in ("http 408", "http 429")):
        return False
    return any(
        marca in text
        for marca in (
            "manifiesto sin lineas utiles",
            "ningun candidato accesible",
            "anidamiento mayor",
            "timeout",
            "http 5",
            "http 408",
            "http 429",
            "temporal",
        )
    )


def probe_liveness(url: str, timeout: float = 10.0) -> ProbeResult:
    """Valida un stream bajando bytes y comprobando la firma.

    - Discussion: desciende los niveles HLS necesarios (ver :func:`_descender`)
      y exige ``MIN_SEGMENT_BYTES``. Reintenta los fallos transitorios: los
      CDN de FAST degradan la respuesta cuando reciben rafagas de peticiones.
    - Important: un segmento con AES-128 no tiene firma TS reconocible porque
      esta cifrado; se acepta por tamano, porque ffmpeg baja la llave y lo
      descifra. Lo que si bloquea es DRM/CENC/SAMPLE-AES.
    """
    result = ProbeResult(method="liveness", checked_at=now_iso())
    segment = b""
    flags = {"encrypted": False, "drm": False, "signed_token": False}
    error = ""
    for intento in range(max(1, REINTENTOS_LIVENESS)):
        try:
            _seg_url, segment, flags, error = _descender(url, timeout)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:300]
            if "HTTP 401" in error or "HTTP 403" in error:
                result.auth_required = True
        if not error:
            break
        if not _reintentable(error):
            break
        if intento + 1 < REINTENTOS_LIVENESS:
            time.sleep(REINTENTO_ESPERA_S * (intento + 1))

    result.encrypted = flags["encrypted"]
    result.drm = flags["drm"]
    result.signed_token = flags["signed_token"]
    if error:
        result.error = error
        return result

    seg_kind = signature(segment)
    result.container = "mpegts" if seg_kind == "TS" else ("mp4" if seg_kind == "fMP4" else seg_kind)
    result.detail = f"segmento {seg_kind} {len(segment)} B"

    if result.drm:
        result.accessible = False
        result.error = "DRM (CENC/SAMPLE-AES): no reproducible por copia"
        return result

    if seg_kind in ("TS", "fMP4"):
        result.accessible = len(segment) >= MIN_SEGMENT_BYTES
        if not result.accessible:
            result.error = f"segmento {seg_kind} {len(segment)} B (por debajo del piso)"
    elif result.encrypted and len(segment) >= MIN_SEGMENT_BYTES:
        # AES-128: bytes cifrados sin firma reconocible. ffmpeg lo descifra.
        result.accessible = True
        result.detail += " (AES-128, descifrable por ffmpeg)"
    else:
        result.accessible = False
        result.error = f"segmento {seg_kind} {len(segment)} B (firma no reconocida)"
    return result


def check_stream(url: str, timeout: float = 10.0, mode: str = "liveness", settings: Settings | None = None) -> ProbeResult:
    """Valida un stream combinando metadatos y (segun el modo) bytes reales.

    - Discussion: si se pasa ``settings``, la URL se reescribe antes de validar
      (fuente de TV Azteca integrada: el token se resuelve aqui mismo). Sin
      ``settings`` se valida la URL tal cual, que es lo que quieren los tests.
    """
    if settings is not None and settings.azteca_enabled:
        from .azteca import reescribir_url

        url = reescribir_url(url, settings.azteca_local_base, settings.azteca_enabled)

    if mode == "ffprobe":
        result = probe_ffprobe(url, timeout=timeout)
        manifest = _safe_manifest_flags(url, timeout)
        result.encrypted = manifest["encrypted"]
        result.drm = manifest["drm"]
        result.signed_token = manifest["signed_token"] or bool(_TOKEN_RE.search(url))
        if result.drm:
            result.accessible = False
            result.error = result.error or "DRM (CENC/SAMPLE-AES): no reproducible por copia"
        return result

    result = probe_liveness(url, timeout=timeout)
    result.signed_token = result.signed_token or bool(_TOKEN_RE.search(url))
    # Enriquecer con ffprobe solo si el flujo es accesible: los metadatos
    # (codec/resolucion) son los que deciden si hace falta transcodificar.
    if result.accessible:
        meta = probe_ffprobe(url, timeout=timeout)
        result.video_codec = meta.video_codec or result.video_codec
        result.audio_codec = meta.audio_codec or result.audio_codec
        result.width = meta.width
        result.height = meta.height
        result.resolution = meta.resolution or result.resolution
        result.video_bitrate = meta.video_bitrate or result.video_bitrate
        result.audio_bitrate = meta.audio_bitrate or result.audio_bitrate
        result.format_name = meta.format_name or result.format_name
    return result


def _safe_manifest_flags(url: str, timeout: float) -> dict[str, bool]:
    """Banderas de proteccion del manifiesto, sin fallar si no se puede leer."""
    try:
        data = http_get(url, timeout=timeout, max_bytes=MANIFEST_MAX_BYTES)
        return analyze_manifest(data.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"encrypted": False, "drm": False, "signed_token": False}


def check_channels(channels: list[Channel], settings: Settings) -> list[Channel]:
    """Valida en paralelo todos los canales y guarda su ``ProbeResult``.

    - Discussion: los trabajadores acotan el numero de ffprobe/descargas
      simultaneas. Una fuente caida no detiene el barrido: cada canal guarda
      su propio error.
    """
    log.info("iniciando validacion", extra={"count": len(channels), "detail": settings.probe_mode})
    workers = max(1, int(settings.probe_workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(check_stream, ch.source_url, settings.probe_timeout_s, settings.probe_mode, settings): ch
            for ch in channels
        }
        done = 0
        for future in as_completed(futures):
            ch = futures[future]
            try:
                ch.probe = future.result()
            except Exception as exc:  # noqa: BLE001 — la validacion nunca tumba el pipeline
                ch.probe = ProbeResult(method=settings.probe_mode, error=str(exc)[:300], checked_at=now_iso())
            done += 1
            if ch.probe.accessible:
                ch.disabled_reason = ""
            elif ch.probe.drm:
                ch.disabled_reason = "DRM"
            elif ch.probe.auth_required:
                ch.disabled_reason = "requiere autenticacion"
            else:
                ch.disabled_reason = ch.probe.error or "inaccesible"
    ok = sum(1 for c in channels if c.probe.accessible)
    log.info("validacion terminada", extra={"count": len(channels), "detail": f"accesibles={ok}"})
    return channels
