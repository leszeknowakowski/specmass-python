from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class SensorSnapshot:
    timestamp: float
    temperature: float
    flows: tuple[float, ...] = ()
    masses: Mapping[str, float] | None = None
    temperatures: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class ControlCommand:
    temperature_setpoint: float | None = None
    flow_setpoints: tuple[float, ...] = ()
    valve_states: tuple[bool, ...] = ()
    heater_percent: float = 0.0
    flow_write_enabled: tuple[bool, ...] = ()
    flow_write_performed: tuple[bool, ...] = ()

    @classmethod
    def safe(
        cls,
        flow_count: int = 0,
        valve_count: int = 0,
        *,
        flow_write_enabled: tuple[bool, ...] | None = None,
    ) -> "ControlCommand":
        return cls(
            temperature_setpoint=None,
            flow_setpoints=(0.0,) * flow_count,
            valve_states=(False,) * valve_count,
            heater_percent=0.0,
            flow_write_enabled=(True,) * flow_count if flow_write_enabled is None else flow_write_enabled,
            flow_write_performed=(False,) * flow_count,
        )


class DeviceBackend(Protocol):
    def read(self, timestamp: float) -> SensorSnapshot: ...

    def apply(self, command: ControlCommand, dt_seconds: float) -> None: ...

    def safe_shutdown(self) -> None: ...
