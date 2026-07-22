from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from typing import Any, Mapping

from .devices.base import ControlCommand, SensorSnapshot
from .state_machine import ControllerStatus


class TdmsTelemetryWriter:
    """Buffered TDMS logger with an explicit, correct time channel."""

    def __init__(
        self,
        path: str | Path,
        *,
        flow_channels: int,
        mass_stimuli: Mapping[str, float] | None = None,
        nominal_increment_seconds: float = 0.1,
        run_name: str | None = None,
        temperature_names: tuple[str, ...] = ("Temperature",),
        root_properties: Mapping[str, Any] | None = None,
    ) -> None:
        if flow_channels < 0:
            raise ValueError("Flow channel count cannot be negative")
        if nominal_increment_seconds <= 0:
            raise ValueError("Nominal TDMS increment must be positive")
        if not temperature_names or len(set(temperature_names)) != len(temperature_names):
            raise ValueError("Temperature channel names must be non-empty and unique")
        if "Setpoint" in temperature_names:
            raise ValueError("Setpoint is reserved for the temperature command channel")
        if importlib.util.find_spec("nptdms") is None:
            raise RuntimeError(
                "TDMS logging needs the optional dependency: pip install 'specmass[tdms]'"
            )
        self.path = Path(path)
        self.flow_channels = flow_channels
        self.mass_stimuli = dict(mass_stimuli or {})
        self.nominal_increment_seconds = float(nominal_increment_seconds)
        self.run_name = run_name or self.path.stem
        self.temperature_names = temperature_names
        self.root_properties = dict(root_properties or {})
        self._started_utc = datetime.now(timezone.utc)
        self._elapsed: list[float] = []
        self._temperatures: list[list[float]] = [[] for _ in temperature_names]
        self._setpoints: list[float] = []
        self._heater: list[float] = []
        self._states: list[int] = []
        self._stage_indices: list[int] = []
        self._flows: list[list[float]] = [[] for _ in range(flow_channels)]
        self._flow_setpoints: list[list[float]] = [[] for _ in range(flow_channels)]
        self._flow_write_enabled: list[list[bool]] = [[] for _ in range(flow_channels)]
        self._flow_write_performed: list[list[bool]] = [[] for _ in range(flow_channels)]
        self._masses: dict[str, list[float]] = {name: [] for name in self.mass_stimuli}
        self._closed = False

    def write(self, snapshot: SensorSnapshot, command: ControlCommand, status: ControllerStatus) -> None:
        if self._closed:
            raise RuntimeError("Cannot write to a closed TDMS logger")
        row_index = len(self._elapsed)
        self._elapsed.append(float(snapshot.timestamp))
        incoming_temperatures = snapshot.temperatures or (snapshot.temperature,)
        for index, values in enumerate(self._temperatures):
            values.append(
                float(incoming_temperatures[index])
                if index < len(incoming_temperatures)
                else float("nan")
            )
        self._setpoints.append(
            float("nan") if command.temperature_setpoint is None else float(command.temperature_setpoint)
        )
        self._heater.append(float(command.heater_percent))
        self._states.append(int(status.state))
        self._stage_indices.append(-1 if status.stage_index is None else int(status.stage_index))
        for index, values in enumerate(self._flows):
            values.append(float(snapshot.flows[index]) if index < len(snapshot.flows) else float("nan"))
            self._flow_setpoints[index].append(
                float(command.flow_setpoints[index])
                if index < len(command.flow_setpoints)
                else float("nan")
            )
            write_enabled = command.flow_write_enabled or (True,) * len(command.flow_setpoints)
            self._flow_write_enabled[index].append(
                bool(write_enabled[index]) if index < len(write_enabled) else False
            )
            write_performed = command.flow_write_performed or (False,) * len(command.flow_setpoints)
            self._flow_write_performed[index].append(
                bool(write_performed[index]) if index < len(write_performed) else False
            )

        incoming_masses = snapshot.masses or {}
        for name in incoming_masses:
            if name not in self._masses:
                self._masses[name] = [float("nan")] * row_index
        for name, values in self._masses.items():
            value = incoming_masses.get(name)
            values.append(float("nan") if value is None else float(value))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import numpy as np
            from nptdms.writer import ChannelObject, GroupObject, RootObject, TdmsWriter
        except ImportError as exc:
            raise RuntimeError(
                "TDMS logging needs the optional dependency: pip install 'specmass[tdms]'"
            ) from exc

        start = np.datetime64(self._started_utc.replace(tzinfo=None), "us")
        start_epoch = self._started_utc.timestamp()
        waveform_properties = {
            "wf_start_time": start,
            "wf_start_offset": 0.0,
            "wf_increment": self.nominal_increment_seconds,
            "wf_xname": "Nominal Time",
            "wf_xunit_string": "s",
            "SpecMass_ExplicitTimeChannel": "/'Time'/'ElapsedSeconds'",
        }
        root_properties = {
            "name": self.run_name,
            "SpecMass_FormatVersion": "1.0",
            "SpecMass_TimebaseNote": (
                "Use Time/ElapsedSeconds for exact acquisition times; wf_increment is nominal."
            ),
            **self.root_properties,
        }
        objects = [
            RootObject(root_properties),
            GroupObject("Time"),
            ChannelObject("Time", "ElapsedSeconds", np.asarray(self._elapsed, dtype=np.float64)),
            ChannelObject(
                "Time",
                "UtcSeconds",
                np.asarray([start_epoch + value for value in self._elapsed], dtype=np.float64),
                {"unit_string": "seconds since Unix epoch UTC"},
            ),
            GroupObject("Temperature"),
            *(
                ChannelObject(
                    "Temperature",
                    name,
                    np.asarray(values, dtype=np.float64),
                    dict(waveform_properties, NI_ChannelName=name),
                )
                for name, values in zip(self.temperature_names, self._temperatures)
            ),
            ChannelObject(
                "Temperature",
                "Setpoint",
                np.asarray(self._setpoints, dtype=np.float64),
                dict(waveform_properties, NI_ChannelName="Setpoint"),
            ),
            GroupObject("Control"),
            ChannelObject("Control", "HeaterPercent", np.asarray(self._heater, dtype=np.float64)),
            ChannelObject("Control", "MSMState", np.asarray(self._states, dtype=np.int32)),
            ChannelObject("Control", "StageIndex", np.asarray(self._stage_indices, dtype=np.int32)),
        ]
        if self._flows:
            objects.append(GroupObject("Flows"))
            objects.extend(
                ChannelObject(
                    "Flows",
                    f"Ch{index}",
                    np.asarray(values, dtype=np.float64),
                    dict(waveform_properties, NI_ChannelName=f"Ch{index}"),
                )
                for index, values in enumerate(self._flows)
            )
            objects.append(GroupObject("FlowSetpoints"))
            objects.extend(
                ChannelObject(
                    "FlowSetpoints",
                    f"Ch{index}",
                    np.asarray(values, dtype=np.float64),
                    dict(waveform_properties, NI_ChannelName=f"Ch{index}"),
                )
                for index, values in enumerate(self._flow_setpoints)
            )
            objects.extend(
                ChannelObject(
                    "FlowSetpoints",
                    f"Ch{index}_WriteEnabled",
                    np.asarray(values, dtype=np.uint8),
                    {"NI_ChannelName": f"Ch{index}_WriteEnabled"},
                )
                for index, values in enumerate(self._flow_write_enabled)
            )
            objects.extend(
                ChannelObject(
                    "FlowSetpoints",
                    f"Ch{index}_WritePerformed",
                    np.asarray(values, dtype=np.uint8),
                    {"NI_ChannelName": f"Ch{index}_WritePerformed"},
                )
                for index, values in enumerate(self._flow_write_performed)
            )
        for scan_index, (name, values) in enumerate(self._masses.items(), start=1):
            group = f"Masses_Scan{scan_index}"
            stimulus = self.mass_stimuli.get(name)
            properties = dict(waveform_properties, NI_ChannelName=name, Scan=scan_index)
            if stimulus is not None:
                properties["Stimulus"] = float(stimulus)
            objects.append(GroupObject(group, {"DateTime": start}))
            objects.append(ChannelObject(group, name, np.asarray(values, dtype=np.float64), properties))

        with TdmsWriter(self.path) as writer:
            writer.write_segment(objects)

    def __enter__(self) -> "TdmsTelemetryWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


_COMMON_MASS_NAMES = {
    4.0: "He",
    18.0: "H2O",
    28.0: "N2",
    32.0: "O2",
    40.0: "Ar",
    44.0: "CO2",
}


def mass_stimuli_from_scan_settings(settings: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    parameters = settings.get("ScansParameters", ())
    if not isinstance(parameters, (list, tuple)):
        return result
    for item in parameters:
        if not isinstance(item, Mapping) or str(item.get("Device to scan", "")).lower() != "mass":
            continue
        try:
            start = float(item["Start value"])
            stop = float(item["Stop value"])
        except (KeyError, TypeError, ValueError):
            continue
        if start != stop:
            continue
        name = _COMMON_MASS_NAMES.get(start, f"Mass[{start:g}]")
        result[name] = start
    return result
