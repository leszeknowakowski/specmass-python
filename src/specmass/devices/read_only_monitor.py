from __future__ import annotations

from typing import Sequence

from .adam4118 import Adam4118MonitorBackend
from .base import ControlCommand, SensorSnapshot
from .brooks0254 import Brooks0254ReadOnlyClient
from .serial_transport import HardwareDisabledError


class ReadOnlyHardwareMonitorBackend:
    """Combine temperature and flow inputs without exposing actuator operations."""

    def __init__(
        self,
        temperature_monitor: Adam4118MonitorBackend,
        flow_client: Brooks0254ReadOnlyClient,
        *,
        flow_channel_names: Sequence[str] = ("Ch0", "Ch1", "Ch2", "Ch3"),
    ) -> None:
        names = tuple(str(name) for name in flow_channel_names)
        if len(names) != flow_client.channel_count:
            raise ValueError("Brooks channel-name count must match the reader channel count")
        self.temperature_monitor = temperature_monitor
        self.flow_client = flow_client
        self.channel_names = temperature_monitor.channel_names
        self.flow_channel_names = names
        self.monitored_devices = ("ADAM4118", "Brooks1")
        self.poll_interval_ms = 1000

    def read(self, timestamp: float) -> SensorSnapshot:
        temperatures = self.temperature_monitor.read(timestamp)
        flows = self.flow_client.read_all()
        return SensorSnapshot(
            timestamp=float(timestamp),
            temperature=temperatures.temperature,
            temperatures=temperatures.temperatures,
            flows=flows,
            masses={},
        )

    def apply(self, command: ControlCommand, dt_seconds: float) -> None:
        del command, dt_seconds
        raise HardwareDisabledError(
            "Combined hardware monitor is read-only and rejects all control commands"
        )

    def safe_shutdown(self) -> None:
        try:
            self.temperature_monitor.safe_shutdown()
        finally:
            self.flow_client.close()
