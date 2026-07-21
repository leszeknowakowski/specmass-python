from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .devices.base import ControlCommand, SensorSnapshot


class SafetyTrip(RuntimeError):
    """Raised before applying an unsafe or unverifiable hardware command."""


@dataclass(frozen=True, slots=True)
class SafetyPolicy:
    maximum_temperature: float = 1000.0
    minimum_temperature: float = -50.0
    maximum_sample_age_seconds: float = 2.0
    future_timestamp_tolerance_seconds: float = 0.1

    def __post_init__(self) -> None:
        values = (
            self.maximum_temperature,
            self.minimum_temperature,
            self.maximum_sample_age_seconds,
            self.future_timestamp_tolerance_seconds,
        )
        if not all(isfinite(value) for value in values):
            raise ValueError("Safety policy values must be finite")
        if self.minimum_temperature >= self.maximum_temperature:
            raise ValueError("Minimum temperature must be lower than maximum temperature")
        if self.maximum_sample_age_seconds < 0 or self.future_timestamp_tolerance_seconds < 0:
            raise ValueError("Timestamp tolerances cannot be negative")

    def validate_snapshot(self, snapshot: SensorSnapshot, *, now: float) -> None:
        if not isfinite(snapshot.temperature):
            raise SafetyTrip("Temperature reading is not finite")
        if snapshot.temperature < self.minimum_temperature:
            raise SafetyTrip(
                f"Temperature {snapshot.temperature:.3f} is below the configured safety minimum"
            )
        if snapshot.temperature > self.maximum_temperature:
            raise SafetyTrip(
                f"Temperature {snapshot.temperature:.3f} exceeds the configured safety maximum"
            )
        age = now - snapshot.timestamp
        if age > self.maximum_sample_age_seconds:
            raise SafetyTrip(f"Temperature sample is stale by {age:.3f} seconds")
        if age < -self.future_timestamp_tolerance_seconds:
            raise SafetyTrip(f"Sensor timestamp is {-age:.3f} seconds in the future")
        if not all(isfinite(value) for value in snapshot.flows):
            raise SafetyTrip("A flow reading is not finite")
        if snapshot.masses and not all(isfinite(value) for value in snapshot.masses.values()):
            raise SafetyTrip("A mass-spectrometer reading is not finite")

    def validate_command(self, command: ControlCommand) -> None:
        values = (*command.flow_setpoints, command.heater_percent)
        if command.temperature_setpoint is not None:
            values = (*values, command.temperature_setpoint)
        if not all(isfinite(value) for value in values):
            raise SafetyTrip("A control command contains a non-finite number")
        if not 0.0 <= command.heater_percent <= 100.0:
            raise SafetyTrip("Heater command must be between 0 and 100 percent")
        if command.flow_write_enabled and len(command.flow_write_enabled) != len(command.flow_setpoints):
            raise SafetyTrip("Flow write-enable mask must match the flow setpoint count")
        if command.flow_write_performed and len(command.flow_write_performed) != len(command.flow_setpoints):
            raise SafetyTrip("Flow write-performed mask must match the flow setpoint count")
        if command.temperature_setpoint is not None and not (
            self.minimum_temperature <= command.temperature_setpoint <= self.maximum_temperature
        ):
            raise SafetyTrip(
                f"Temperature setpoint {command.temperature_setpoint:.3f} is outside the safety range"
            )
