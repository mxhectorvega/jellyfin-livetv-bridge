"""Etapa 6 — TRANSCODIFICACION BAJO DEMANDA.

Por defecto NO se recodifica: se copia el flujo (``-c copy``). Solo se
transcodifica cuando el perfil del canal lo requiere:

- video H.265/HEVC -> H.264 (Jellyfin y los clientes web lo piden)
- audio no-AAC (AC-3/E-AC-3/MP2) -> AAC
- perfil explicito del canal (``copy`` | ``h264`` | ``aac`` | ``auto``)

- Discussion: se intenta VAAPI/QSV primero y se cae a CPU. En este homelab el
  host no tiene GPU dedicada (Intel HD 4000 con QSV limitado, driver i965);
  el limite ``max_transcodes`` evita que varias recodificaciones simultaneas
  tumben el servicio.
- Important: ``-c copy`` NO descifra contenido CENC/DRM. Un canal protegido se
  marca y no se sirve, no se intenta "arreglar" con banderas.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass

from .config import Settings
from .models import Channel
from .util import get_logger, run_cmd

log = get_logger("transcode")

COPY_VIDEO = {"h264", "avc1"}
COPY_AUDIO = {"aac", "mp4a"}
# Antes habia dos listas NEGRAS (TRANSCODE_VIDEO / TRANSCODE_AUDIO) y el plan
# copiaba todo lo que NO estuviera en ellas: un codec no previsto (vc1, theora,
# vp8) acababa en `-c copy` dentro de `-f mpegts`. Ahora se copia SOLO lo que el
# muxer MPEG-TS lleva sin reencodear, que es lo que estas dos listas declaran.


@dataclass
class TranscodePlan:
    """Decision de procesamiento para un canal."""

    copy_video: bool = True
    copy_audio: bool = True
    hwaccel: str = ""          # "" | vaapi | qsv | cpu
    reason: str = ""

    @property
    def is_copy(self) -> bool:
        return self.copy_video and self.copy_audio


@functools.lru_cache(maxsize=8)
def detect_hwaccel(preference: str = "auto") -> str:
    """Detecta el acelerador disponible: ``vaapi``/``qsv``/``cpu``.

    - Discussion: se comprueba con ``ffmpeg -hwaccels`` Y con la existencia de
      ``/dev/dri``. Un ffmpeg que declara vaapi sin dispositivos DRM accesibles
      falla al primer frame; mejor decidir antes.
    - Important: el resultado se cachea: la deteccion cuesta un subprocess.
    """
    choice = (preference or "auto").lower()
    if choice in ("cpu", "off", "none"):
        return "cpu"

    code, out, _ = run_cmd(["ffmpeg", "-hide_banner", "-hwaccels"], timeout=10)
    available = {line.strip().lower() for line in (out or b"").decode("utf-8", "replace").splitlines()}
    has_dri = os.path.isdir("/dev/dri")

    if choice == "vaapi":
        return "vaapi" if ("vaapi" in available and has_dri) else "cpu"
    if choice == "qsv":
        return "qsv" if ("qsv" in available and has_dri) else "cpu"
    if "vaapi" in available and has_dri:
        return "vaapi"
    if "qsv" in available and has_dri:
        return "qsv"
    return "cpu"


def plan_for(channel: Channel, settings: Settings) -> TranscodePlan:
    """Decide si un canal se copia o se transcodifica.

    - Discussion: sin metadatos utiles (ffprobe no pudo leer) se opta por
      copiar: recodificar a ciegas es exactamente lo que hace Jellyfin cuando
      su sonda falla, y es lo que hay que evitar.
    """
    profile = (channel.profile or "auto").lower()
    probe = channel.probe

    if settings.hwaccel == "off" or profile == "copy":
        return TranscodePlan(True, True, "", "perfil copy / hwaccel off")

    # LISTA BLANCA, no lista negra: solo se copia lo que el muxer MPEG-TS
    # soporta tal cual. Sin metadatos (codec vacio) se copia: recodificar a
    # ciegas es justo lo que hace Jellyfin cuando su sonda falla.
    copy_video = probe.video_codec.lower() in COPY_VIDEO if probe.video_codec else True
    copy_audio = probe.audio_codec.lower() in COPY_AUDIO if probe.audio_codec else True

    if profile == "h264":
        copy_video = False
    elif profile == "aac":
        copy_audio = False

    if copy_video and copy_audio:
        return TranscodePlan(True, True, "", "codecs compatibles: copia directa")

    hw = detect_hwaccel(settings.hwaccel) if not copy_video else ""
    reason_parts = []
    if not copy_video:
        reason_parts.append(f"video {probe.video_codec or '?'} -> h264")
    if not copy_audio:
        reason_parts.append(f"audio {probe.audio_codec or '?'} -> aac")
    return TranscodePlan(copy_video, copy_audio, hw, ", ".join(reason_parts))


def build_ffmpeg_cmd(
    url: str,
    plan: TranscodePlan,
    settings: Settings,
    output: str = "pipe:1",
    duration: float | None = None,
) -> list[str]:
    """Construye la linea de comandos de ffmpeg para un canal.

    - Discussion: la salida por omision es MPEG-TS por tuberia (``pipe:1``),
      que es lo que Jellyfin consume de un tuner. Se fuerzan timestamps
      crecientes para que el cliente no vea retrocesos al reconectar.
    - Important: los comandos se pasan siempre como LISTA, nunca por shell.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-fflags", "+genpts+igndts",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
    ]
    if plan.hwaccel == "vaapi":
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"]
    elif plan.hwaccel == "qsv":
        cmd += ["-hwaccel", "qsv"]
    cmd += ["-i", url]

    if plan.copy_video:
        cmd += ["-c:v", "copy"]
    elif plan.hwaccel == "vaapi":
        cmd += ["-c:v", "h264_vaapi", "-qp", "23"]
    elif plan.hwaccel == "qsv":
        cmd += ["-c:v", "h264_qsv", "-global_quality", "23"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-crf", "23"]

    if plan.copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]

    cmd += ["-f", "mpegts", "-mpegts_flags", "+resend_headers"]
    if duration:
        cmd += ["-t", str(duration)]
    cmd.append(output)
    return cmd


def count_active_transcodes() -> int:
    """Cuenta procesos ffmpeg de transcodificacion activos en el host.

    - Discussion: sin GPU dedicada, mas de ``max_transcodes`` recodificaciones
      simultaneas saturan la CPU. El contador se lee del sistema de archivos
      de procesos (``/proc``), que existe tanto en el contenedor como en el host.
    """
    total = 0
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                    cmdline = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            if "ffmpeg" in cmdline and "-c:v" in cmdline and "copy" not in cmdline.split("-c:v", 1)[1][:12]:
                total += 1
    except OSError:
        return 0
    return total


def can_transcode(settings: Settings) -> bool:
    """Permite o niega una transcodificacion nueva segun el limite configurado."""
    return count_active_transcodes() < max(0, settings.max_transcodes)
