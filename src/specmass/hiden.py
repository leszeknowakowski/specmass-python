from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
from typing import Any, Mapping, Sequence

from .legacy import load_legacy_json


_PARITY_NAMES = {
    0: "none",
    1: "odd",
    2: "even",
    3: "mark",
    4: "space",
}

# NI-VISA stores stop-bit choices as an integer enum rather than a count.
_STOP_BITS = {
    10: 1.0,
    15: 1.5,
    20: 2.0,
}


@dataclass(frozen=True, slots=True)
class HidenMassDefinition:
    mass: float
    name: str

    def __post_init__(self) -> None:
        if not isfinite(self.mass) or self.mass <= 0:
            raise ValueError(f"Hiden mass must be positive and finite, not {self.mass!r}")
        if not self.name.strip():
            raise ValueError("Hiden mass name cannot be empty")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HidenMassDefinition":
        return cls(mass=float(data["Mass"]), name=str(data["Name"]))


@dataclass(frozen=True, slots=True)
class HidenConnectionConfig:
    resource: str
    connection_type: int
    set_to_standby: bool
    force_interrogation: bool
    enable_comms_logging: bool
    comms_log_file_path: str | None
    baud_rate: int
    parity_code: int
    data_bits: int
    stop_bits_code: int
    timeout_ms: int
    tcp_port: int
    masses: tuple[HidenMassDefinition, ...]
    enabled: bool

    def __post_init__(self) -> None:
        if not self.resource.strip():
            raise ValueError("Hiden resource cannot be empty")
        if self.connection_type not in (0, 1):
            raise ValueError(f"Unknown Hiden connection type: {self.connection_type}")
        if self.baud_rate <= 0:
            raise ValueError("Hiden baud rate must be positive")
        if self.parity_code not in _PARITY_NAMES:
            raise ValueError(f"Unknown NI-VISA parity code: {self.parity_code}")
        if self.data_bits not in range(5, 9):
            raise ValueError(f"Hiden data bits must be between 5 and 8, not {self.data_bits}")
        if self.stop_bits_code not in _STOP_BITS:
            raise ValueError(f"Unknown NI-VISA stop-bit code: {self.stop_bits_code}")
        if self.timeout_ms <= 0:
            raise ValueError("Hiden timeout must be positive")
        if self.tcp_port < 0 or self.tcp_port > 65535:
            raise ValueError(f"Invalid Hiden TCP port: {self.tcp_port}")
        mass_values = tuple(item.mass for item in self.masses)
        mass_names = tuple(item.name.casefold() for item in self.masses)
        if len(set(mass_values)) != len(mass_values):
            raise ValueError("Hiden MassesNames contains duplicate masses")
        if len(set(mass_names)) != len(mass_names):
            raise ValueError("Hiden MassesNames contains duplicate names")

    @property
    def connection_name(self) -> str:
        return "serial" if self.connection_type == 0 else "tcp"

    @property
    def parity_name(self) -> str:
        return _PARITY_NAMES[self.parity_code]

    @property
    def stop_bits(self) -> float:
        return _STOP_BITS[self.stop_bits_code]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HidenConnectionConfig":
        raw_masses = _mapping_sequence(data.get("MassesNames", ()), "MassesNames")
        return cls(
            resource=str(data.get("Resource", "")),
            connection_type=int(data.get("ConnType", 0)),
            set_to_standby=bool(data.get("Set to Standby (T)", True)),
            force_interrogation=bool(data.get("Force m/s interrogation (F)", False)),
            enable_comms_logging=bool(data.get("Enable comms logging (F)", False)),
            comms_log_file_path=(
                str(data["Comms log file path"])
                if data.get("Comms log file path") is not None
                else None
            ),
            baud_rate=int(data.get("Baud Rate", 19200)),
            parity_code=int(data.get("Parity", 0)),
            data_bits=int(data.get("Data Bits", 8)),
            stop_bits_code=int(data.get("Stop Bits", 10)),
            timeout_ms=int(data.get("Timeout", 1000)),
            tcp_port=int(data.get("TCPPort", 0)),
            masses=tuple(HidenMassDefinition.from_mapping(item) for item in raw_masses),
            enabled=bool(data.get("EnableMS", False)),
        )


@dataclass(frozen=True, slots=True)
class HidenScanDefinition:
    device_to_scan: str
    start_value: float
    stop_value: float
    increment: float
    relative_sensitivity: float
    relative_gain: float
    scan_mode: int
    input_device: str
    dwell_percent: int
    settle_percent: int
    autorange_high: int
    autorange_low: int
    start_range: int
    use_autozero: bool
    options: str
    environment_changes: str
    acquisition_cycles: int
    minimum_cycle_time_seconds: float

    def __post_init__(self) -> None:
        values = (
            self.start_value,
            self.stop_value,
            self.increment,
            self.relative_sensitivity,
            self.relative_gain,
            self.minimum_cycle_time_seconds,
        )
        if not all(isfinite(value) for value in values):
            raise ValueError("Hiden scan contains a non-finite number")
        if not self.device_to_scan.strip():
            raise ValueError("Hiden scan device cannot be empty")
        if not self.input_device.strip():
            raise ValueError("Hiden scan input device cannot be empty")
        if self.increment == 0:
            raise ValueError("Hiden scan increment cannot be zero")
        if self.relative_sensitivity <= 0 or self.relative_gain <= 0:
            raise ValueError("Hiden relative sensitivity and gain must be positive")
        if not 0 <= self.dwell_percent <= 100:
            raise ValueError("Hiden dwell percentage must be between 0 and 100")
        if not 0 <= self.settle_percent <= 100:
            raise ValueError("Hiden settle percentage must be between 0 and 100")
        if self.autorange_low > self.autorange_high:
            raise ValueError("Hiden autorange low cannot exceed autorange high")
        if not self.autorange_low <= self.start_range <= self.autorange_high:
            raise ValueError("Hiden start range must be inside the autorange limits")
        if self.acquisition_cycles < 0:
            raise ValueError("Hiden acquisition cycles cannot be negative")
        if self.minimum_cycle_time_seconds < 0:
            raise ValueError("Hiden minimum cycle time cannot be negative")

    @property
    def is_single_point(self) -> bool:
        return self.start_value == self.stop_value

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HidenScanDefinition":
        return cls(
            device_to_scan=str(data.get("Device to scan", "")),
            start_value=float(data.get("Start value", 0.0)),
            stop_value=float(data.get("Stop value", 0.0)),
            increment=float(data.get("Increment", 0.0)),
            relative_sensitivity=float(data.get("Relative  sensitivity", 1.0)),
            relative_gain=float(data.get("Relative gain", 1.0)),
            scan_mode=int(data.get("Scan mode", 0)),
            input_device=str(data.get("Input device", "")),
            dwell_percent=int(data.get("Dwell (%)", 0)),
            settle_percent=int(data.get("Settle (%)", 0)),
            autorange_high=int(data.get("Autorange High", 0)),
            autorange_low=int(data.get("Autorange Low", 0)),
            start_range=int(data.get("Start range", 0)),
            use_autozero=bool(data.get("Use Autozero", False)),
            options=str(data.get("Options", "")),
            environment_changes=str(data.get("Changes to environment parameters", "")),
            acquisition_cycles=int(data.get("Acquisition cycles", 0)),
            minimum_cycle_time_seconds=float(data.get("Min cycle time (sec)", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class HidenScanPlan:
    filament: str
    scans: tuple[HidenScanDefinition, ...]

    def __post_init__(self) -> None:
        if self.filament.upper() not in ("", "F1", "F2"):
            raise ValueError(f"Unknown Hiden filament selection: {self.filament!r}")
        if not self.scans:
            raise ValueError("A Hiden scan plan must contain at least one scan")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HidenScanPlan":
        raw_scans = _mapping_sequence(data.get("ScansParameters", ()), "ScansParameters")
        return cls(
            filament=str(data.get("Filament", "")).upper(),
            scans=tuple(HidenScanDefinition.from_mapping(item) for item in raw_scans),
        )


def load_hiden_connection(builds_directory: str | Path) -> HidenConnectionConfig:
    path = Path(builds_directory) / "data" / "MSDevTh"
    if not path.is_file():
        raise FileNotFoundError(f"Hiden configuration does not exist: {path}")
    raw = load_legacy_json(path)
    if not isinstance(raw, Mapping):
        raise ValueError(f"Hiden configuration must be a JSON object: {path}")
    return HidenConnectionConfig.from_mapping(raw)


def load_hiden_scan_plan(program_directory: str | Path) -> HidenScanPlan:
    path = Path(program_directory) / "ScanSettings.msdef"
    if not path.is_file():
        raise FileNotFoundError(f"Hiden scan settings do not exist: {path}")
    raw = load_legacy_json(path)
    if not isinstance(raw, Mapping):
        raise ValueError(f"Hiden scan settings must be a JSON object: {path}")
    return HidenScanPlan.from_mapping(raw)


def build_hiden_offline_report(
    builds_directory: str | Path,
    *,
    program_directory: str | Path | None = None,
) -> dict[str, Any]:
    connection = load_hiden_connection(builds_directory)
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "builds_directory": str(Path(builds_directory).resolve()),
        "connection": {
            **asdict(connection),
            "connection_name": connection.connection_name,
            "parity_name": connection.parity_name,
            "stop_bits": connection.stop_bits,
        },
        "safety": {
            "ports_opened": 0,
            "device_queries_sent": 0,
            "output_commands_sent": 0,
            "passive_mass_acquisition_available": False,
            "reason": (
                "The legacy scan and partial-pressure paths select an operating mode, "
                "set devices, and control the ion beam; they are not passive reads."
            ),
        },
    }
    if program_directory is not None:
        plan = load_hiden_scan_plan(program_directory)
        names_by_mass = {item.mass: item.name for item in connection.masses}
        report["program_directory"] = str(Path(program_directory).resolve())
        report["scan_plan"] = {
            **asdict(plan),
            "resolved_labels": [
                names_by_mass.get(scan.start_value, f"Mass[{scan.start_value:g}]")
                if scan.device_to_scan.casefold() == "mass" and scan.is_single_point
                else scan.device_to_scan
                for scan in plan.scans
            ],
            "single_point_scans": sum(scan.is_single_point for scan in plan.scans),
        }
    return report


def _mapping_sequence(value: Any, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be an array")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{label}[{index}] must be an object")
        result.append(item)
    return tuple(result)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect legacy Hiden connection and scan settings without opening COM3"
    )
    parser.add_argument("--builds", type=Path, required=True)
    parser.add_argument("--program", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("specmass-hiden-offline.json"),
    )
    args = parser.parse_args()
    report = build_hiden_offline_report(args.builds, program_directory=args.program)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Offline Hiden report written to {args.output.resolve()}")
    print("No serial port was opened and no Hiden command was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
