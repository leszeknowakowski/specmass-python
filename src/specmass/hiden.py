from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import struct
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


DEFAULT_HIDEN_MASS_NAMES: dict[float, str] = {
    4.0: "He",
    18.0: "H2O",
    28.0: "N2",
    32.0: "O2",
    40.0: "Ar",
    44.0: "CO2",
}

HIDEN_ENVIRONMENT_SETTINGS_NAME = "EnvironmentSettings.json"


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
class HidenEnvironmentDevice:
    name: str
    index: int
    format_string: str
    group_membership: int
    values_by_mode: tuple[float, ...]
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    resolution: float | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Hiden environment device name cannot be empty")
        if self.index < 0:
            raise ValueError("Hiden environment device index cannot be negative")
        if self.format_string not in ("%d", "%.2f"):
            raise ValueError(
                f"Unsupported Hiden environment format {self.format_string!r} "
                f"for {self.name}"
            )
        if not self.values_by_mode or not all(isfinite(value) for value in self.values_by_mode):
            raise ValueError(f"Hiden environment values for {self.name} are invalid")
        for label, value in (
            ("minimum", self.minimum),
            ("maximum", self.maximum),
            ("resolution", self.resolution),
        ):
            if value is not None and not isfinite(value):
                raise ValueError(
                    f"Hiden environment {label} for {self.name} is invalid"
                )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(
                f"Hiden environment limits for {self.name} are reversed"
            )
        if self.resolution is not None and self.resolution <= 0:
            raise ValueError(
                f"Hiden environment resolution for {self.name} must be positive"
            )


@dataclass(frozen=True, slots=True)
class HidenEnvironmentConfig:
    mass_spec_name: str
    modes: tuple[str, ...]
    devices: tuple[HidenEnvironmentDevice, ...]
    autozero_supported: bool

    def __post_init__(self) -> None:
        if not self.mass_spec_name.strip():
            raise ValueError("Hiden environment identity cannot be empty")
        if not self.modes or any(not mode.strip() for mode in self.modes):
            raise ValueError("Hiden environment must define named operating modes")
        if not self.devices:
            raise ValueError("Hiden environment must contain devices")
        if len({device.name.casefold() for device in self.devices}) != len(self.devices):
            raise ValueError("Hiden environment contains duplicate device names")
        if any(len(device.values_by_mode) != len(self.modes) for device in self.devices):
            raise ValueError("Every Hiden environment device must define every mode")

    @property
    def normalized_mass_spec_name(self) -> str:
        text = self.mass_spec_name.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
            text = text[1:-1].strip()
        return text


@dataclass(frozen=True, slots=True)
class HidenEnvironmentSettings:
    """Program-local overrides for one Hiden global operating mode."""

    mode: str
    values: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if not self.mode.strip():
            raise ValueError("Hiden environment mode cannot be empty")
        names: set[str] = set()
        for name, value in self.values:
            normalized = name.strip().casefold()
            if not normalized:
                raise ValueError("Hiden environment parameter name cannot be empty")
            if normalized in names:
                raise ValueError(
                    f"Duplicate Hiden environment parameter: {name!r}"
                )
            if not isfinite(value):
                raise ValueError(
                    f"Hiden environment value for {name!r} is not finite"
                )
            names.add(normalized)

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any]
    ) -> "HidenEnvironmentSettings":
        raw_parameters = data.get("Parameters", ())
        if not isinstance(raw_parameters, Sequence) or isinstance(
            raw_parameters, (str, bytes, bytearray)
        ):
            raise ValueError("Hiden environment Parameters must be an array")
        values: list[tuple[str, float]] = []
        for index, item in enumerate(raw_parameters):
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"Hiden environment Parameters[{index}] must be an object"
                )
            if "Value" not in item:
                raise ValueError(
                    f"Hiden environment Parameters[{index}] has no Value"
                )
            values.append((str(item.get("Name", "")), float(item["Value"])))
        return cls(
            mode=str(data.get("Global mode", "RGA")),
            values=tuple(values),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "Global mode": self.mode,
            "Parameters": [
                {"Name": name, "Value": value} for name, value in self.values
            ],
        }


def default_hiden_environment_settings(
    environment: HidenEnvironmentConfig,
    *,
    mode: str = "RGA",
) -> HidenEnvironmentSettings:
    mode_index = _hiden_environment_mode_index(environment, mode)
    return HidenEnvironmentSettings(
        mode=environment.modes[mode_index].strip(),
        values=tuple(
            (device.name, device.values_by_mode[mode_index])
            for device in environment.devices
            if device.group_membership & 1
        ),
    )


def validate_hiden_environment_settings(
    settings: HidenEnvironmentSettings,
    environment: HidenEnvironmentConfig,
) -> None:
    _hiden_environment_mode_index(environment, settings.mode)
    devices = {
        device.name.casefold(): device
        for device in environment.devices
        if device.group_membership & 1
    }
    for name, value in settings.values:
        device = devices.get(name.strip().casefold())
        if device is None:
            raise ValueError(
                f"Unknown Hiden global environment parameter: {name!r}"
            )
        if device.minimum is not None and value < device.minimum:
            raise ValueError(
                f"Hiden environment {name} value {value:g} is below "
                f"{device.minimum:g}"
            )
        if device.maximum is not None and value > device.maximum:
            raise ValueError(
                f"Hiden environment {name} value {value:g} is above "
                f"{device.maximum:g}"
            )
        if device.format_string == "%d" and value != round(value):
            raise ValueError(f"Hiden environment {name} requires an integer value")


def apply_hiden_environment_settings(
    environment: HidenEnvironmentConfig,
    settings: HidenEnvironmentSettings,
) -> HidenEnvironmentConfig:
    """Return a copy with program-local values applied to one operating mode."""
    validate_hiden_environment_settings(settings, environment)
    mode_index = _hiden_environment_mode_index(environment, settings.mode)
    overrides = {
        name.strip().casefold(): value for name, value in settings.values
    }
    devices: list[HidenEnvironmentDevice] = []
    for device in environment.devices:
        value = overrides.get(device.name.casefold())
        if value is None:
            devices.append(device)
            continue
        values_by_mode = list(device.values_by_mode)
        values_by_mode[mode_index] = value
        devices.append(
            replace(device, values_by_mode=tuple(values_by_mode))
        )
    return replace(environment, devices=tuple(devices))


def load_hiden_environment_settings(
    program_directory: str | Path,
    environment: HidenEnvironmentConfig,
) -> HidenEnvironmentSettings:
    path = Path(program_directory) / HIDEN_ENVIRONMENT_SETTINGS_NAME
    if not path.is_file():
        return default_hiden_environment_settings(environment)
    raw = load_legacy_json(path)
    if not isinstance(raw, Mapping):
        raise ValueError(f"Hiden environment settings must be an object: {path}")
    settings = HidenEnvironmentSettings.from_mapping(raw)
    validate_hiden_environment_settings(settings, environment)
    return settings


def _hiden_environment_mode_index(
    environment: HidenEnvironmentConfig,
    mode: str,
) -> int:
    normalized = mode.strip().casefold()
    try:
        return next(
            index
            for index, candidate in enumerate(environment.modes)
            if candidate.strip().casefold() == normalized
        )
    except StopIteration as exc:
        modes = ", ".join(repr(item.strip()) for item in environment.modes)
        raise ValueError(
            f"Unknown Hiden environment mode {mode!r}; expected one of {modes}"
        ) from exc


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
        if not 0.001 <= self.relative_sensitivity <= 1000.0:
            raise ValueError(
                "Hiden relative sensitivity must be between 0.001 and 1000"
            )
        if not 0.001 <= self.relative_gain <= 1000.0:
            raise ValueError("Hiden relative gain must be between 0.001 and 1000")
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
        if self.stop_value > self.start_value and self.increment < 0:
            raise ValueError("Hiden scan increment must be positive for an increasing scan")
        if self.stop_value < self.start_value and self.increment > 0:
            raise ValueError("Hiden scan increment must be negative for a decreasing scan")
        for label, value in (
            ("device", self.device_to_scan),
            ("input device", self.input_device),
            ("options", self.options),
            ("environment changes", self.environment_changes),
        ):
            _validate_hiden_command_text(
                value,
                label=label,
                allow_empty=label not in ("device", "input device"),
            )

    @property
    def is_single_point(self) -> bool:
        return self.start_value == self.stop_value

    def stimuli(self) -> tuple[float, ...]:
        if self.is_single_point:
            return (self.start_value,)
        span = (self.stop_value - self.start_value) / self.increment
        point_count = int(round(span)) + 1
        if point_count < 2 or point_count > 1_000_000:
            raise ValueError(f"Hiden scan contains an invalid point count: {point_count}")
        endpoint = self.start_value + (point_count - 1) * self.increment
        tolerance = max(1e-9, abs(self.increment) * 1e-6)
        if abs(endpoint - self.stop_value) > tolerance:
            raise ValueError(
                "Hiden scan range must be an integer number of increments"
            )
        return tuple(self.start_value + index * self.increment for index in range(point_count))

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
        if len({scan.acquisition_cycles for scan in self.scans}) != 1:
            raise ValueError(
                "Every Hiden scan row must use the same acquisition cycle count"
            )
        if len(
            {scan.minimum_cycle_time_seconds for scan in self.scans}
        ) != 1:
            raise ValueError(
                "Every Hiden scan row must use the same minimum cycle time"
            )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HidenScanPlan":
        raw_scans = _mapping_sequence(data.get("ScansParameters", ()), "ScansParameters")
        return cls(
            filament=str(data.get("Filament", "")).upper(),
            scans=tuple(HidenScanDefinition.from_mapping(item) for item in raw_scans),
        )


def new_hiden_mass_scan(
    mass: float,
    *,
    stop_mass: float | None = None,
    increment: float = 1.0,
    scan_mode: int = 1,
    input_device: str = "SEM",
    use_autozero: bool = False,
    autorange_high: int = -7,
    autorange_low: int = -9,
    start_range: int = -9,
    dwell_percent: int = 100,
    settle_percent: int = 100,
    relative_sensitivity: float = 1.0,
    relative_gain: float = 1.0,
    options: str = "",
    environment_changes: str = "",
    acquisition_cycles: int = 0,
    minimum_cycle_time_seconds: float = 0.0,
) -> dict[str, Any]:
    """Create one legacy-compatible trend or linear mass-scan definition."""
    stop_value = float(mass if stop_mass is None else stop_mass)
    scan: dict[str, Any] = {
        "Device to scan": "mass",
        "Start value": float(mass),
        "Stop value": stop_value,
        "Increment": float(increment),
        "Relative  sensitivity": float(relative_sensitivity),
        "Relative gain": float(relative_gain),
        "Scan mode": int(scan_mode),
        "Input device": str(input_device),
        "Dwell (%)": int(dwell_percent),
        "Settle (%)": int(settle_percent),
        "Autorange High": int(autorange_high),
        "Autorange Low": int(autorange_low),
        "Start range": int(start_range),
        "Use Autozero": bool(use_autozero),
        "Options": str(options),
        "Changes to environment parameters": str(environment_changes),
        "Acquisition cycles": int(acquisition_cycles),
        "Min cycle time (sec)": float(minimum_cycle_time_seconds),
    }
    HidenScanDefinition.from_mapping(scan)
    return scan


def hiden_scan_label(
    scan: Mapping[str, Any],
    *,
    names_by_mass: Mapping[float, str] | None = None,
) -> str:
    """Return a compact operator-facing label without losing scan details."""
    definition = HidenScanDefinition.from_mapping(scan)
    if definition.device_to_scan.casefold() == "mass":
        if definition.is_single_point:
            mass = definition.start_value
            names = DEFAULT_HIDEN_MASS_NAMES if names_by_mass is None else names_by_mass
            name = names.get(mass, f"Mass {mass:g}")
            target = f"{name}  —  m/z {mass:g}"
        else:
            target = f"Mass sweep  —  {definition.start_value:g} to {definition.stop_value:g}"
    else:
        target = definition.device_to_scan
    autozero = " · autozero" if definition.use_autozero else ""
    return f"{target}  ·  {definition.input_device}{autozero}"


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


def hiden_input_range_device(
    environment: HidenEnvironmentConfig,
    input_device: str,
) -> HidenEnvironmentDevice | None:
    """Return the range device paired with one Hiden input device."""
    normalized = input_device.strip().casefold()
    if normalized == "scans":
        return None
    range_name = "nul_range" if normalized == "nul-dev" else f"{normalized}_range"
    return next(
        (
            device
            for device in environment.devices
            if device.name.casefold() == range_name
        ),
        None,
    )


def validate_hiden_scan_ranges(
    plan: HidenScanPlan,
    environment: HidenEnvironmentConfig,
) -> None:
    """Validate scan range values before a serial transport can be opened."""
    deployed_16359 = environment.normalized_mass_spec_name.casefold().endswith(
        "#16359"
    )
    for row, scan in enumerate(plan.scans, start=1):
        range_device = hiden_input_range_device(environment, scan.input_device)
        if range_device is None:
            if scan.input_device.casefold() == "scans":
                continue
            raise ValueError(
                f"Hiden scan {row} input device {scan.input_device!r} has no "
                "range definition in the instrument configuration"
            )
        for label, value in (
            ("autorange high", scan.autorange_high),
            ("autorange low", scan.autorange_low),
            ("start range", scan.start_range),
        ):
            if range_device.minimum is not None and value < range_device.minimum:
                raise ValueError(
                    f"Hiden scan {row} {scan.input_device} {label} {value} is "
                    f"below the device limit {range_device.minimum:g}"
                )
            if range_device.maximum is not None and value > range_device.maximum:
                raise ValueError(
                    f"Hiden scan {row} {scan.input_device} {label} {value} is "
                    f"above the device limit {range_device.maximum:g}"
                )
            if (
                range_device.minimum is not None
                and range_device.resolution is not None
            ):
                steps = (value - range_device.minimum) / range_device.resolution
                if abs(steps - round(steps)) > 1e-9:
                    raise ValueError(
                        f"Hiden scan {row} {scan.input_device} {label} {value} "
                        f"does not match the device resolution "
                        f"{range_device.resolution:g}"
                    )
        if (
            deployed_16359
            and scan.input_device.casefold() == "sem"
            and scan.autorange_low == -13
        ):
            raise ValueError(
                f"Hiden scan {row} SEM autorange low -13 was rejected by "
                "HAL #16359 with error 049. Use the validated LabVIEW value "
                "-9 (and start range -9)."
            )


def load_hiden_environment_config(path: str | Path) -> HidenEnvironmentConfig:
    """Read the LabVIEW DTLG environment cache written by the Hiden driver."""
    source = Path(path)
    data = source.read_bytes()
    if len(data) < 20 or data[:4] != b"DTLG":
        raise ValueError(f"Hiden environment is not a LabVIEW DTLG file: {source}")
    descriptor_end = struct.unpack_from(">I", data, 12)[0]
    if descriptor_end + 4 > len(data):
        raise ValueError(f"Hiden environment descriptor is truncated: {source}")
    data_offset = struct.unpack_from(">I", data, descriptor_end)[0]
    reader = _HidenDatalogReader(data, data_offset, source)

    names = reader.strings("device names")
    indices = reader.integers("device indices")
    units = reader.strings("device units")
    minima = reader.floats("device minima")
    maxima = reader.floats("device maxima")
    resolutions = reader.floats("device resolutions")
    rows, columns, values = reader.float_matrix("values by mode")
    modes = reader.strings("operating modes")
    formats = reader.strings("device formats")
    memberships = reader.bytes("group memberships")
    autozero_supported = reader.boolean("autozero support")
    mass_spec_name = reader.string("mass spectrometer name")

    device_count = len(names)
    lengths = {
        "indices": len(indices),
        "units": len(units),
        "minima": len(minima),
        "maxima": len(maxima),
        "resolutions": len(resolutions),
        "matrix rows": rows,
        "formats": len(formats),
        "memberships": len(memberships),
    }
    mismatched = {label: count for label, count in lengths.items() if count != device_count}
    if mismatched:
        raise ValueError(
            f"Hiden environment arrays do not match {device_count} devices: {mismatched}"
        )
    if columns != len(modes):
        raise ValueError(
            f"Hiden environment matrix has {columns} modes but names {len(modes)}"
        )

    devices = tuple(
        HidenEnvironmentDevice(
            name=names[index],
            index=indices[index],
            format_string=formats[index],
            group_membership=memberships[index],
            values_by_mode=tuple(values[index]),
            unit=units[index],
            minimum=minima[index],
            maximum=maxima[index],
            resolution=resolutions[index],
        )
        for index in range(device_count)
    )
    return HidenEnvironmentConfig(
        mass_spec_name=mass_spec_name,
        modes=tuple(modes),
        devices=devices,
        autozero_supported=autozero_supported,
    )


def find_hiden_environment_config(builds_directory: str | Path) -> Path:
    root = Path(builds_directory)
    candidates = sorted(
        path
        for path in root.glob("*.cfg")
        if path.is_file() and path.stem.isdigit()
    )
    if not candidates:
        raise FileNotFoundError(
            f"No serial-number Hiden environment .cfg exists in {root}"
        )
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise ValueError(
            f"Multiple Hiden environment files exist in {root}; cannot choose safely: {names}"
        )
    return candidates[0]


def hiden_mass_stimuli(
    plan: HidenScanPlan,
    *,
    names_by_mass: Mapping[float, str] | None = None,
) -> dict[str, float]:
    names = DEFAULT_HIDEN_MASS_NAMES if names_by_mass is None else names_by_mass
    result: dict[str, float] = {}
    for scan in plan.scans:
        if scan.device_to_scan.casefold() != "mass" or not scan.is_single_point:
            continue
        name = names.get(scan.start_value, f"Mass[{scan.start_value:g}]")
        if name in result:
            raise ValueError(f"Duplicate live Hiden mass label: {name}")
        result[name] = scan.start_value
    return result


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


def _validate_hiden_command_text(
    value: str,
    *,
    label: str,
    allow_empty: bool,
) -> None:
    if not value and allow_empty:
        return
    if not value:
        raise ValueError(f"Hiden scan {label} cannot be empty")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"Hiden scan {label} must contain ASCII only") from exc
    if any(byte < 32 or byte == 127 for byte in encoded):
        raise ValueError(f"Hiden scan {label} contains a control character")


class _HidenDatalogReader:
    def __init__(self, data: bytes, offset: int, source: Path) -> None:
        if offset < 0 or offset > len(data):
            raise ValueError(f"Invalid Hiden DTLG data offset in {source}")
        self.data = data
        self.offset = offset
        self.source = source

    def _take(self, size: int, label: str) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise ValueError(f"Truncated Hiden environment {label}: {self.source}")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def _count(self, label: str) -> int:
        count = struct.unpack(">I", self._take(4, f"{label} count"))[0]
        if count > 1_000_000:
            raise ValueError(f"Unreasonable Hiden environment {label} count: {count}")
        return count

    def string(self, label: str) -> str:
        size = self._count(label)
        try:
            return self._take(size, label).decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Hiden environment {label} is not ASCII") from exc

    def strings(self, label: str) -> tuple[str, ...]:
        return tuple(self.string(f"{label}[{index}]") for index in range(self._count(label)))

    def integers(self, label: str) -> tuple[int, ...]:
        count = self._count(label)
        raw = self._take(count * 4, label)
        return tuple(struct.unpack(f">{count}i", raw))

    def floats(self, label: str) -> tuple[float, ...]:
        count = self._count(label)
        raw = self._take(count * 8, label)
        return tuple(struct.unpack(f">{count}d", raw))

    def float_matrix(
        self, label: str
    ) -> tuple[int, int, tuple[tuple[float, ...], ...]]:
        rows = self._count(f"{label} rows")
        columns = self._count(f"{label} columns")
        if rows and columns > 1_000_000 // rows:
            raise ValueError(f"Unreasonable Hiden environment {label} shape")
        raw = self._take(rows * columns * 8, label)
        flat = struct.unpack(f">{rows * columns}d", raw)
        values = tuple(
            tuple(flat[row * columns : (row + 1) * columns])
            for row in range(rows)
        )
        return rows, columns, values

    def bytes(self, label: str) -> tuple[int, ...]:
        return tuple(self._take(self._count(label), label))

    def boolean(self, label: str) -> bool:
        raw = self._take(1, label)[0]
        if raw not in (0, 1):
            raise ValueError(f"Invalid Hiden environment boolean {label}: {raw}")
        return bool(raw)


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
