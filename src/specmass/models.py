from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from math import isfinite
from typing import Any, Mapping, Sequence


class TemperatureMode(IntEnum):
    ISOTHERMAL = 0
    POLYTHERMAL = 1

    @classmethod
    def parse(cls, value: Any) -> "TemperatureMode":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            aliases = {
                "0": cls.ISOTHERMAL,
                "isothermal": cls.ISOTHERMAL,
                "1": cls.POLYTHERMAL,
                "polithermal": cls.POLYTHERMAL,
                "polythermal": cls.POLYTHERMAL,
            }
            if normalized in aliases:
                return aliases[normalized]
        try:
            return cls(int(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unknown temperature mode: {value!r}") from exc


class ValveMode(IntEnum):
    CONSTANT = 0
    IMPULSE = 1

    @classmethod
    def parse(cls, value: Any) -> "ValveMode":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            aliases = {
                "0": cls.CONSTANT,
                "const": cls.CONSTANT,
                "constant": cls.CONSTANT,
                "1": cls.IMPULSE,
                "impluse": cls.IMPULSE,  # spelling used by the LabVIEW typedef
                "impulse": cls.IMPULSE,
                "pulse": cls.IMPULSE,
            }
            if normalized in aliases:
                return aliases[normalized]
        try:
            return cls(int(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unknown valve mode: {value!r}") from exc


def _numbers(values: Sequence[Any] | None) -> tuple[float, ...]:
    return tuple(float(value) for value in (values or ()))


def _states(values: Sequence[Any] | None) -> tuple[bool, ...]:
    return tuple(bool(value) for value in (values or ()))


def _lookup(data: Mapping[str, Any], legacy: str, default: Any) -> Any:
    if legacy in data:
        return data[legacy]
    snake = "".join(("_" + char.lower()) if char.isupper() else char for char in legacy).lstrip("_")
    return data.get(snake, default)


@dataclass(frozen=True, slots=True)
class ProcessStage:
    name: str
    start_temperature: float
    end_temperature: float
    temperature_mode: TemperatureMode
    temperature_rate_per_minute: float
    start_flows: tuple[float, ...] = ()
    end_flows: tuple[float, ...] = ()
    flow_rates_per_minute: tuple[float, ...] = ()
    valve_initial_states: tuple[bool, ...] = ()
    valve_pulse_lengths: tuple[float, ...] = ()
    valve_pulse_gaps: tuple[float, ...] = ()
    valve_modes: tuple[ValveMode, ...] = ()
    auto_start: bool = False
    duration_seconds: float = 0.0
    stabilize_temperature: bool = False

    def __post_init__(self) -> None:
        scalar_values = (
            self.start_temperature,
            self.end_temperature,
            self.temperature_rate_per_minute,
            self.duration_seconds,
        )
        sequence_values = (
            *self.start_flows,
            *self.end_flows,
            *self.flow_rates_per_minute,
            *self.valve_pulse_lengths,
            *self.valve_pulse_gaps,
        )
        if not self.name.strip():
            raise ValueError("Stage name cannot be empty")
        if not all(isfinite(value) for value in (*scalar_values, *sequence_values)):
            raise ValueError(f"Stage {self.name!r} contains a non-finite number")
        if self.duration_seconds < 0:
            raise ValueError("Stage duration cannot be negative")
        if len(self.start_flows) != len(self.end_flows):
            raise ValueError("StartFlow and EndFlow must have the same length")
        if self.flow_rates_per_minute and len(self.flow_rates_per_minute) != len(self.start_flows):
            raise ValueError("FlowA must be empty or match the flow channel count")
        valve_count = len(self.valve_initial_states)
        for label, values in (
            ("ValvePulseLength", self.valve_pulse_lengths),
            ("ValvePulseGap", self.valve_pulse_gaps),
            ("ValveMode", self.valve_modes),
        ):
            if values and len(values) != valve_count:
                raise ValueError(f"{label} must be empty or match the valve count")
        if any(value < 0 for value in (*self.valve_pulse_lengths, *self.valve_pulse_gaps)):
            raise ValueError("Valve pulse length and gap cannot be negative")
        if (
            self.temperature_mode is TemperatureMode.POLYTHERMAL
            and self.start_temperature != self.end_temperature
            and self.temperature_rate_per_minute == 0
        ):
            raise ValueError("A polythermal stage with a temperature change needs a non-zero TempA")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, default_name: str = "stage") -> "ProcessStage":
        valve_states = _states(_lookup(data, "ValveStates", ()))
        valve_count = len(valve_states)
        raw_modes = _lookup(data, "ValveMode", ()) or ()
        raw_lengths = _numbers(_lookup(data, "ValvePulseLength", ()))
        raw_gaps = _numbers(_lookup(data, "ValvePulseGap", ()))
        return cls(
            name=str(_lookup(data, "Name", default_name)),
            start_temperature=float(_lookup(data, "StartTemp", 0.0)),
            end_temperature=float(_lookup(data, "EndTemp", 0.0)),
            temperature_mode=TemperatureMode.parse(_lookup(data, "TempMode", 0)),
            temperature_rate_per_minute=float(_lookup(data, "TempA", 0.0)),
            start_flows=_numbers(_lookup(data, "StartFlow", ())),
            end_flows=_numbers(_lookup(data, "EndFlow", ())),
            flow_rates_per_minute=_numbers(_lookup(data, "FlowA", ())),
            valve_initial_states=valve_states,
            valve_pulse_lengths=raw_lengths or (0.0,) * valve_count,
            valve_pulse_gaps=raw_gaps or (0.0,) * valve_count,
            valve_modes=tuple(ValveMode.parse(value) for value in raw_modes)
            or (ValveMode.CONSTANT,) * valve_count,
            auto_start=bool(_lookup(data, "AutoStart", False)),
            duration_seconds=float(_lookup(data, "Duration", 0.0)),
            stabilize_temperature=bool(_lookup(data, "StabilizeTemp", False)),
        )

    def effective_duration_seconds(self) -> float:
        if self.temperature_mode is TemperatureMode.ISOTHERMAL:
            return self.duration_seconds
        delta = abs(self.end_temperature - self.start_temperature)
        if delta == 0:
            return 0.0
        return delta / abs(self.temperature_rate_per_minute) * 60.0

    def temperature_setpoint(self, elapsed_seconds: float) -> float:
        if self.temperature_mode is TemperatureMode.ISOTHERMAL:
            return self.start_temperature
        return _ramp(
            self.start_temperature,
            self.end_temperature,
            abs(self.temperature_rate_per_minute) * max(0.0, elapsed_seconds) / 60.0,
        )

    def flow_setpoints(self, elapsed_seconds: float) -> tuple[float, ...]:
        if self.temperature_mode is TemperatureMode.POLYTHERMAL:
            return self.start_flows
        rates = self.flow_rates_per_minute or (0.0,) * len(self.start_flows)
        elapsed_minutes = max(0.0, elapsed_seconds) / 60.0
        return tuple(
            _ramp(start, end, abs(rate) * elapsed_minutes)
            for start, end, rate in zip(self.start_flows, self.end_flows, rates, strict=True)
        )

    def valve_states_at(self, elapsed_seconds: float) -> tuple[bool, ...]:
        result: list[bool] = []
        elapsed = max(0.0, elapsed_seconds)
        for initial, length, gap, mode in zip(
            self.valve_initial_states,
            self.valve_pulse_lengths,
            self.valve_pulse_gaps,
            self.valve_modes,
            strict=True,
        ):
            if mode is ValveMode.CONSTANT or length <= 0:
                result.append(initial)
                continue
            period = length + gap
            in_pulse = period > 0 and elapsed % period < length
            result.append(not initial if in_pulse else initial)
        return tuple(result)


def _ramp(start: float, end: float, distance: float) -> float:
    if start < end:
        return min(end, start + distance)
    if start > end:
        return max(end, start - distance)
    return start


@dataclass(frozen=True, slots=True)
class ProcessProgram:
    stages: tuple[ProcessStage, ...]
    scan_settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.stages:
            raise ValueError("A process program must contain at least one stage")

