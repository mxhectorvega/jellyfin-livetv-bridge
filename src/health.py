"""Etapa 7 — RESILIENCIA (health-check, backoff, activar/desactivar).

- Discussion: el monitor revisa cada canal en su intervalo; tras
  ``health_fails_to_disable`` fallos CONSECUTIVOS lo desactiva (sale del M3U
  en la siguiente regeneracion) y aplica backoff exponencial. Tras
  ``health_oks_to_enable`` exitos consecutivos lo reactiva. Nada de
  desactivar por un fallo suelto: estos origenes callan varios segundos de
  forma normal y un umbral nervioso da de baja canales sanos.
- Important: el health-check mide el flujo, no el puerto. Comprueba bytes
  (firma del contenedor), igual que la validacion inicial.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .config import Settings
from .models import Channel, ChannelStatus
from .probe import check_channel
from .util import get_logger, monotonic, now_iso

log = get_logger("health")


@dataclass
class HealthReport:
    """Resultado de una pasada de health-check."""

    checked: int = 0
    ok: int = 0
    failed: int = 0
    disabled: int = 0
    reenabled: int = 0
    details: list[dict] = field(default_factory=list)


class HealthMonitor:
    """Vigila los canales y mantiene su estado de salud.

    - Discussion: el estado vive en :class:`ChannelStatus`, indexado por id de
      canal, y se puede persistir para sobrevivir reinicios.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.status: dict[str, ChannelStatus] = {}
        self.last_run: str = ""

    # ------------------------------------------------------------------
    def ensure(self, channel: Channel) -> ChannelStatus:
        status = self.status.get(channel.id)
        if status is None:
            status = ChannelStatus(channel_id=channel.id, active=channel.enabled)
            self.status[channel.id] = status
        return status

    def due(self, channel: Channel, now: float | None = None) -> bool:
        """Indica si a un canal le toca revision (respeta el backoff)."""
        status = self.ensure(channel)
        return (now if now is not None else monotonic()) >= status.next_check_at

    # ------------------------------------------------------------------
    async def check_all(self, channels: list[Channel], force: bool = False) -> HealthReport:
        """Revisa los canales que ya venzan y actualiza su estado."""
        report = HealthReport()
        now = monotonic()
        for channel in channels:
            status = self.ensure(channel)
            if not force and not self.due(channel, now):
                continue
            probe = await asyncio.to_thread(check_channel, channel, self.settings)
            channel.probe = probe
            report.checked += 1

            if probe.accessible:
                status.fails = 0
                status.oks += 1
                status.last_ok = now_iso()
                status.last_error = ""
                status.backoff_s = 0.0
                if not channel.enabled and status.oks >= self.settings.health_oks_to_enable:
                    channel.enabled = True
                    channel.disabled_reason = ""
                    status.active = True
                    report.reenabled += 1
                    log.info("canal reactivado", extra={"channel": channel.name})
            else:
                status.oks = 0
                status.fails += 1
                status.last_error = probe.error or channel.disabled_reason or "inaccesible"
                status.backoff_s = min(
                    self.settings.backoff_max_s,
                    max(
                        self.settings.backoff_base_s,
                        self.settings.backoff_base_s * (2 ** (status.fails - 1)),
                    ),
                )
                if channel.enabled and status.fails >= self.settings.health_fails_to_disable:
                    channel.enabled = False
                    channel.disabled_reason = status.last_error
                    status.active = False
                    report.disabled += 1
                    log.warning(
                        "canal desactivado",
                        extra={"channel": channel.name, "detail": status.last_error},
                    )

            status.next_check_at = monotonic() + (
                status.backoff_s or self.settings.health_interval_s
            )
            report.details.append(
                {
                    "id": channel.id,
                    "name": channel.name,
                    "enabled": channel.enabled,
                    "accessible": probe.accessible,
                    "fails": status.fails,
                    "oks": status.oks,
                    "error": status.last_error,
                }
            )

        report.ok = sum(1 for d in report.details if d["accessible"])
        report.failed = report.checked - report.ok
        self.last_run = now_iso()
        if report.checked:
            log.info(
                "health-check",
                extra={"count": report.checked, "detail": f"ok={report.ok} fail={report.failed}"},
            )
        return report

    async def run_forever(self, channels_provider) -> None:
        """Bucle de vigilancia. ``channels_provider`` devuelve la lista viva."""
        log.info(
            "monitor de salud iniciado",
            extra={"detail": f"intervalo={self.settings.health_interval_s}s"},
        )
        while True:
            try:
                channels = channels_provider()
                await self.check_all(channels)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — el vigilante nunca muere
                log.error("error en el monitor de salud", extra={"detail": str(exc)})
            await asyncio.sleep(max(5.0, self.settings.health_interval_s))

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Estado de salud serializable para ``/status``."""
        return {
            "last_run": self.last_run,
            "canales": {
                cid: {
                    "active": st.active,
                    "fails": st.fails,
                    "oks": st.oks,
                    "last_ok": st.last_ok,
                    "last_error": st.last_error,
                    "backoff_s": st.backoff_s,
                }
                for cid, st in self.status.items()
            },
        }
