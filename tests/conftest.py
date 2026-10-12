"""Fixtures compartidas de la suite de pruebas.

- Discussion: los tests NO tocan la red. Todo ``http_get`` y todo subprocess
  de ffmpeg/ffprobe se sustituye con ``monkeypatch``; lo que se prueba es la
  logica (parseo, dedupe, empate de ids, generacion de artefactos).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Settings  # noqa: E402
from src.models import Channel, ProbeResult  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Settings con rutas absolutas dentro de tmp_path (aisladas por test)."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        config_dir=str(config_dir),
        data_dir=str(data_dir),
        public_base="http://test.local",
        port=8581,
        probe_timeout_s=1.0,
        probe_workers=2,
        probe_mode="liveness",
    )


@pytest.fixture()
def make_channel():
    """Fabrica canales con valores por omision razonables."""

    def _make(**kwargs) -> Channel:
        base = {
            "name": "Canal Prueba",
            "source_url": "https://example.invalid/canal/master.m3u8",
            "tvg_id": "canal.prueba",
            "group": "Demo",
        }
        base.update(kwargs)
        return Channel(**base)

    return _make


@pytest.fixture()
def ok_probe() -> ProbeResult:
    """Resultado de validacion exitoso (h264 + aac)."""
    return ProbeResult(
        accessible=True,
        method="liveness",
        video_codec="h264",
        audio_codec="aac",
        width=1280,
        height=720,
        resolution="1280x720",
        video_bitrate=2_500_000,
        container="mpegts",
    )
