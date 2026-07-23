from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Mapping, Protocol

from .base import ControlCommand, SensorSnapshot
from .read_only_monitor import ReadOnlyHardwareMonitorBackend


class MassAcquisition(Protocol):
    mass_stimuli: dict[str, float]

    @property
    def active(self) -> bool: ...

    def start(self) -> str: ...

    def read_masses(self) -> Mapping[str, float]: ...

    def safe_shutdown(self) -> None: ...


class HardwareShadowBackend:
    """Run control calculations over live inputs without dispatching outputs."""

    output_writes_enabled = False

    def __init__(self, reader: ReadOnlyHardwareMonitorBackend) -> None:
        self.reader = reader
        self.channel_names = reader.channel_names
        self.flow_channel_names = reader.flow_channel_names
        self.monitored_devices = reader.monitored_devices
        self.poll_interval_ms = reader.poll_interval_ms
        self.last_calculated_command = ControlCommand.safe(
            flow_count=len(self.flow_channel_names),
            flow_write_enabled=(False,) * len(self.flow_channel_names),
        )
        self.last_flow_writes = (False,) * len(self.flow_channel_names)
        self.calculated_command_count = 0
        self.output_commands_sent = 0

    def read(self, timestamp: float) -> SensorSnapshot:
        return self.reader.read(timestamp)

    def start_acquisition(self) -> None:
        """No-op for the read-only shadow mode without a mass spectrometer."""

    def apply(self, command: ControlCommand, dt_seconds: float) -> None:
        if dt_seconds <= 0:
            raise ValueError("Shadow timestep must be positive")
        flow_count = len(command.flow_setpoints)
        self.last_flow_writes = (False,) * flow_count
        self.last_calculated_command = replace(
            command,
            flow_write_enabled=(False,) * flow_count,
            flow_write_performed=(False,) * flow_count,
        )
        self.calculated_command_count += 1
        # Intentionally no call to reader.apply and no actuator client exists here.

    def safe_shutdown(self) -> None:
        self.reader.safe_shutdown()


class HidenHardwareShadowBackend(HardwareShadowBackend):
    """Read ADAM/Brooks live and execute only the explicitly enabled Hiden scan."""

    hiden_commands_enabled = True

    def __init__(
        self,
        reader: ReadOnlyHardwareMonitorBackend,
        mass_acquisition: MassAcquisition,
        *,
        program_directory: str | Path | None = None,
        scan_plan: object | None = None,
    ) -> None:
        super().__init__(reader)
        self.mass_acquisition = mass_acquisition
        self.mass_stimuli = dict(mass_acquisition.mass_stimuli)
        self.program_directory = (
            Path(program_directory).resolve()
            if program_directory is not None
            else None
        )
        self.scan_plan = scan_plan
        self.monitored_devices = (*reader.monitored_devices, "MSDevTh")

    def start_acquisition(self) -> None:
        self.mass_acquisition.start()

    def read(self, timestamp: float) -> SensorSnapshot:
        snapshot = super().read(timestamp)
        masses = self.mass_acquisition.read_masses()
        return replace(snapshot, masses=dict(masses))

    def safe_shutdown(self) -> None:
        try:
            self.mass_acquisition.safe_shutdown()
        finally:
            super().safe_shutdown()
