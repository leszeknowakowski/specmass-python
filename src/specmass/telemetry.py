from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Protocol

from .devices.base import ControlCommand, SensorSnapshot
from .state_machine import ControllerStatus


class TelemetryWriter(Protocol):
    def write(self, snapshot: SensorSnapshot, command: ControlCommand, status: ControllerStatus) -> None: ...

    def close(self) -> None: ...


class CsvTelemetryWriter:
    def __init__(
        self,
        path: str | Path,
        *,
        flow_channels: int,
        mass_names: tuple[str, ...] = (),
        temperature_names: tuple[str, ...] = ("temperature",),
    ) -> None:
        if not temperature_names or len(set(temperature_names)) != len(temperature_names):
            raise ValueError("Temperature channel names must be non-empty and unique")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] = self.path.open("w", encoding="utf-8", newline="")
        self._mass_names = mass_names
        self._temperature_names = temperature_names
        self._started_utc_seconds = datetime.now(timezone.utc).timestamp()
        fieldnames = [
            "timestamp_s",
            "utc_seconds",
            "process_elapsed_s",
            "stage_elapsed_s",
            "state",
            "stage_index",
            *temperature_names,
            "temperature_setpoint",
            "heater_percent",
            *(f"flow_{index}" for index in range(flow_channels)),
            *(f"flow_setpoint_{index}" for index in range(flow_channels)),
            *(f"flow_write_enabled_{index}" for index in range(flow_channels)),
            *(f"flow_write_performed_{index}" for index in range(flow_channels)),
            *(f"mass_{name}" for name in mass_names),
        ]
        self._writer = csv.DictWriter(self._file, fieldnames=fieldnames)
        self._writer.writeheader()

    def write(self, snapshot: SensorSnapshot, command: ControlCommand, status: ControllerStatus) -> None:
        row: dict[str, object] = {
            "timestamp_s": snapshot.timestamp,
            "utc_seconds": self._started_utc_seconds + snapshot.timestamp,
            "process_elapsed_s": status.process_elapsed_seconds,
            "stage_elapsed_s": status.stage_elapsed_seconds,
            "state": status.state.name,
            "stage_index": "" if status.stage_index is None else status.stage_index,
            "temperature_setpoint": "" if command.temperature_setpoint is None else command.temperature_setpoint,
            "heater_percent": command.heater_percent,
        }
        temperatures = snapshot.temperatures or (snapshot.temperature,)
        row.update(
            {
                name: temperatures[index] if index < len(temperatures) else ""
                for index, name in enumerate(self._temperature_names)
            }
        )
        row.update({f"flow_{index}": value for index, value in enumerate(snapshot.flows)})
        row.update(
            {f"flow_setpoint_{index}": value for index, value in enumerate(command.flow_setpoints)}
        )
        write_enabled = command.flow_write_enabled or (True,) * len(command.flow_setpoints)
        row.update(
            {f"flow_write_enabled_{index}": int(value) for index, value in enumerate(write_enabled)}
        )
        write_performed = command.flow_write_performed or (False,) * len(command.flow_setpoints)
        row.update(
            {f"flow_write_performed_{index}": int(value) for index, value in enumerate(write_performed)}
        )
        masses = snapshot.masses or {}
        row.update({f"mass_{name}": masses.get(name, "") for name in self._mass_names})
        self._writer.writerow(row)
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "CsvTelemetryWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
