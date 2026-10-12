"""Estructuras de datos compartidas.

- Discussion: todo el pipeline habla de :class:`Channel` y :class:`ProbeResult`.
  Son dataclasses simples, serializables a JSON, para que el estado pueda
  persistirse en ``/data/state.json`` y exponerse en ``/status``.
- Important: el ``id`` de un canal es ESTABLE (deriva del tvg-id o del nombre
  normalizado y se guarda en el canal). Jellyfin deriva el id de canal del
  ``tvg-id`` del M3U, asi que cambiarlo obliga a re-importar el tuner.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ProbeResult:
    """Resultado de validar UN stream.

    - Discussion: separa lo que reporta ffprobe (metadatos declarados, que en
      HLS pueden venir de un manifiesto de plantilla y ser falsos) de lo que
      se comprobo bajando bytes (``method`` = ``liveness``).
    - Important: ``accessible`` en modo ``ffprobe`` NO prueba que haya video.
    """

    accessible: bool = False
    method: str = ""                 # ffprobe | liveness
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    video_bitrate: int = 0
    audio_bitrate: int = 0
    format_name: str = ""
    container: str = ""
    resolution: str = ""
    encrypted: bool = False          # EXT-X-KEY / ContentProtection / senc-cenc
    drm: bool = False                # licencia declarada (Widevine/PlayReady)
    signed_token: bool = False       # URL con token firmado (jwt/exp/hdnts)
    auth_required: bool = False      # 401/403 o cabeceras exigidas
    error: str = ""
    checked_at: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Channel:
    """Un canal listo para salir en el M3U.

    - Discussion: ``source_url`` es la URL original; la URL que publica el M3U
      se construye en la capa de playlist (normalmente el proxy /stream/<id>)
      para que Jellyfin no consuma el origen crudo.
    - Important: ``epg_id`` es el ``tvg-id`` que se escribe y el MISMO string
      con el que se identifica el canal en el XMLTV. Si no empatan, la guia se
      descarga, se ve sana y no aparece en ninguna parte.
    """

    id: str = ""
    name: str = ""
    source_url: str = ""
    tvg_id: str = ""
    tvg_name: str = ""
    group: str = "General"
    logo: str = ""
    country: str = ""
    language: str = ""
    source: str = ""
    catalog: str = ""                # grupo seleccionable (nacionales|pluto|vix|...)
    profile: str = "auto"            # auto | copy | h264 | aac
    backups: list[str] = field(default_factory=list)   # fuentes alternas, en orden
    enabled: bool = True
    epg_id: str = ""                 # id final en el XMLTV (vacío = sin guía)
    probe: ProbeResult = field(default_factory=ProbeResult)
    disabled_reason: str = ""

    # ------------------------------------------------------------------
    @property
    def has_epg(self) -> bool:
        return bool(self.epg_id)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["probe"] = self.probe.as_dict()
        return data

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Channel":
        probe = ProbeResult(**{k: v for k, v in (data.get("probe") or {}).items() if k in ProbeResult.__dataclass_fields__})
        payload = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "probe"}
        return cls(probe=probe, **payload)


@dataclass
class ChannelStatus:
    """Estado de salud de un canal (persistido y expuesto en /status)."""

    channel_id: str = ""
    active: bool = True
    fails: int = 0
    oks: int = 0
    last_ok: str = ""
    last_error: str = ""
    next_check_at: float = 0.0
    backoff_s: float = 0.0
