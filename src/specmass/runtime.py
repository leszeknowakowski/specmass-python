from __future__ import annotations

from dataclasses import dataclass, replace

from .devices.base import ControlCommand, DeviceBackend, SensorSnapshot
from .pid import PIDController
from .safety import SafetyPolicy
from .state_machine import ControllerStatus, ProcessController
from .telemetry import TelemetryWriter


@dataclass(frozen=True, slots=True)
class RuntimeFrame:
    snapshot: SensorSnapshot
    command: ControlCommand
    status: ControllerStatus


class SpecMassRuntime:
    """Coordinates one safe read/decide/write cycle."""

    def __init__(
        self,
        *,
        backend: DeviceBackend,
        controller: ProcessController,
        pid: PIDController,
        safety: SafetyPolicy | None = None,
        telemetry: TelemetryWriter | None = None,
    ) -> None:
        self.backend = backend
        self.controller = controller
        self.pid = pid
        self.safety = safety or SafetyPolicy()
        self.telemetry = telemetry

    def step(self, timestamp: float, dt_seconds: float) -> RuntimeFrame:
        if dt_seconds <= 0:
            raise ValueError("Runtime timestep must be positive")
        try:
            snapshot = self.backend.read(timestamp)
            self.safety.validate_snapshot(snapshot, now=timestamp)
            command = self.controller.tick(snapshot)
            heater = 0.0
            if command.temperature_setpoint is not None:
                heater = self.pid.update(command.temperature_setpoint, snapshot.temperature, dt_seconds)
            else:
                self.pid.reset()
            command = replace(command, heater_percent=heater)
            self.safety.validate_command(command)
            self.backend.apply(command, dt_seconds)
            performed = getattr(self.backend, "last_flow_writes", ())
            if performed:
                command = replace(command, flow_write_performed=tuple(performed))
            status = self.controller.status(timestamp)
            if self.telemetry is not None:
                self.telemetry.write(snapshot, command, status)
            return RuntimeFrame(snapshot=snapshot, command=command, status=status)
        except Exception as exc:
            self.pid.reset()
            self.controller.trip(str(exc))
            self.backend.safe_shutdown()
            raise

    def safe_shutdown(self) -> None:
        self.pid.reset()
        self.controller.stop()
        self.backend.safe_shutdown()
