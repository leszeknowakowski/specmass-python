from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .legacy import load_program
from .models import TemperatureMode


@dataclass(frozen=True, slots=True)
class ChannelTimebase:
    group: str
    channel: str
    samples: int
    encoded_increment_seconds: float
    encoded_duration_seconds: float
    implied_full_run_increment_seconds: float
    spans_recorded_run: bool


@dataclass(frozen=True, slots=True)
class LegacyRunValidation:
    program_duration_seconds: float
    recorded_duration_seconds: float
    logging_lead_seconds: float
    expected_first_ramp_start_seconds: float | None
    measured_ramp_celsius_per_minute: float | None
    measured_ramp_origin_seconds: float | None
    estimated_thermal_lag_seconds: float | None
    reference_temperature_channel: str
    mass_timebases: tuple[ChannelTimebase, ...]

    @property
    def mass_timebase_is_trustworthy(self) -> bool:
        return all(item.spans_recorded_run for item in self.mass_timebases)


def validate_legacy_run(
    program_directory: str | Path,
    tdms_path: str | Path,
    *,
    temperature_group: str = "Temperature",
    temperature_channel: str = "Temperature",
) -> LegacyRunValidation:
    """Compare a LabVIEW program timeline with one of its TDMS output files."""
    try:
        from nptdms import TdmsFile
    except ImportError as exc:
        raise RuntimeError(
            "TDMS validation needs the optional dependency: pip install 'specmass[tdms]'"
        ) from exc

    program = load_program(program_directory)
    tdms = TdmsFile.read(tdms_path)
    reference = tdms[temperature_group][temperature_channel]
    reference_values = tuple(float(value) for value in reference[:])
    reference_increment = _waveform_increment(reference)
    recorded_duration = max(0, len(reference_values) - 1) * reference_increment
    program_duration = sum(stage.effective_duration_seconds() for stage in program.stages)
    logging_lead = recorded_duration - program_duration

    first_ramp_index = next(
        (
            index
            for index, stage in enumerate(program.stages)
            if stage.temperature_mode is TemperatureMode.POLYTHERMAL
            and stage.start_temperature != stage.end_temperature
        ),
        None,
    )
    expected_ramp_start: float | None = None
    measured_rate: float | None = None
    measured_origin: float | None = None
    lag: float | None = None
    if first_ramp_index is not None:
        ramp = program.stages[first_ramp_index]
        expected_ramp_start = logging_lead + sum(
            stage.effective_duration_seconds() for stage in program.stages[:first_ramp_index]
        )
        lower = min(ramp.start_temperature, ramp.end_temperature)
        upper = max(ramp.start_temperature, ramp.end_temperature)
        margin = (upper - lower) * 0.1
        points = (
            (index * reference_increment, value)
            for index, value in enumerate(reference_values)
            if lower + margin <= value <= upper - margin
        )
        slope, intercept = _linear_fit(points)
        if slope != 0:
            measured_rate = abs(slope) * 60.0
            measured_origin = (ramp.start_temperature - intercept) / slope
            lag = measured_origin - expected_ramp_start

    mass_timebases: list[ChannelTimebase] = []
    for group in tdms.groups():
        if not group.name.startswith("Masses_"):
            continue
        for channel in group.channels():
            sample_count = len(channel)
            encoded_increment = _waveform_increment(channel)
            encoded_duration = max(0, sample_count - 1) * encoded_increment
            implied_increment = (
                recorded_duration / (sample_count - 1) if sample_count > 1 else 0.0
            )
            tolerance = max(reference_increment, recorded_duration * 0.01)
            mass_timebases.append(
                ChannelTimebase(
                    group=group.name,
                    channel=channel.name,
                    samples=sample_count,
                    encoded_increment_seconds=encoded_increment,
                    encoded_duration_seconds=encoded_duration,
                    implied_full_run_increment_seconds=implied_increment,
                    spans_recorded_run=abs(encoded_duration - recorded_duration) <= tolerance,
                )
            )

    return LegacyRunValidation(
        program_duration_seconds=program_duration,
        recorded_duration_seconds=recorded_duration,
        logging_lead_seconds=logging_lead,
        expected_first_ramp_start_seconds=expected_ramp_start,
        measured_ramp_celsius_per_minute=measured_rate,
        measured_ramp_origin_seconds=measured_origin,
        estimated_thermal_lag_seconds=lag,
        reference_temperature_channel=f"{temperature_group}/{temperature_channel}",
        mass_timebases=tuple(mass_timebases),
    )


def _waveform_increment(channel: Any) -> float:
    try:
        increment = float(channel.properties["wf_increment"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"TDMS channel {channel.path} has no valid wf_increment") from exc
    if increment <= 0:
        raise ValueError(f"TDMS channel {channel.path} has a non-positive wf_increment")
    return increment


def _linear_fit(points: Iterable[tuple[float, float]]) -> tuple[float, float]:
    count = 0
    sum_x = sum_y = sum_xx = sum_xy = 0.0
    for x, y in points:
        count += 1
        sum_x += x
        sum_y += y
        sum_xx += x * x
        sum_xy += x * y
    if count < 2:
        raise ValueError("Not enough temperature samples to estimate the ramp")
    denominator = count * sum_xx - sum_x * sum_x
    if denominator == 0:
        raise ValueError("Temperature sample times cannot be fitted")
    slope = (count * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / count
    return slope, intercept

