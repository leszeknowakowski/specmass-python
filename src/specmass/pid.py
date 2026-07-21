from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class PIDGains:
    kc: float
    ti_seconds: float
    td_seconds: float

    def __post_init__(self) -> None:
        if not all(isfinite(value) for value in (self.kc, self.ti_seconds, self.td_seconds)):
            raise ValueError("PID gains must be finite")
        if self.kc < 0 or self.ti_seconds < 0 or self.td_seconds < 0:
            raise ValueError("PID gains cannot be negative")


class PIDController:
    """Bounded PID with derivative-on-measurement and conditional integration."""

    def __init__(
        self,
        gains: PIDGains,
        *,
        output_min: float = 0.0,
        output_max: float = 100.0,
    ) -> None:
        if output_min >= output_max:
            raise ValueError("PID output_min must be lower than output_max")
        self.gains = gains
        self.output_min = float(output_min)
        self.output_max = float(output_max)
        self._integral = 0.0
        self._previous_measurement: float | None = None
        self.output = self.output_min

    def reset(self, *, output: float | None = None) -> None:
        self._integral = 0.0
        self._previous_measurement = None
        self.output = self.output_min if output is None else self._clamp(output)

    def update(self, setpoint: float, measurement: float, dt_seconds: float) -> float:
        if dt_seconds <= 0 or not isfinite(dt_seconds):
            raise ValueError("PID timestep must be a finite positive number")
        error = setpoint - measurement
        derivative = 0.0
        if self._previous_measurement is not None:
            derivative = -(measurement - self._previous_measurement) / dt_seconds

        candidate_integral = self._integral + error * dt_seconds
        candidate = self._calculate(error, candidate_integral, derivative)
        saturated = self._clamp(candidate)
        drives_back_from_high = candidate > self.output_max and error < 0
        drives_back_from_low = candidate < self.output_min and error > 0
        if candidate == saturated or drives_back_from_high or drives_back_from_low:
            self._integral = candidate_integral
            saturated = self._clamp(self._calculate(error, self._integral, derivative))

        self._previous_measurement = measurement
        self.output = saturated
        return saturated

    def _calculate(self, error: float, integral: float, derivative: float) -> float:
        integral_term = integral / self.gains.ti_seconds if self.gains.ti_seconds > 0 else 0.0
        return self.gains.kc * (error + integral_term + self.gains.td_seconds * derivative)

    def _clamp(self, value: float) -> float:
        return min(self.output_max, max(self.output_min, float(value)))

