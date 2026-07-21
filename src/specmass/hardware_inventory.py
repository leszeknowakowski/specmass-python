from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
from typing import Any, Callable, Iterable

from .devices.adam4118 import Adam4118Client, Adam4118Codec
from .devices.brooks0254 import Brooks0254Codec
from .devices.serial_transport import PySerialTransaction, SerialSettings, SerialTransaction
from .legacy import load_legacy_json


_DEVICE_ROLES = {
    "MSDevTh": "Hiden mass spectrometer",
    "ValveDevTh": "VICI valve actuator",
    "BrooksDevTh": "Brooks flow controller",
    "DIOTh": "ADAM 4050 digital output",
    "AIOTh": "ADAM 4118 temperature input",
}

_DEFAULT_BAUDRATES = {
    # BrooksDevTh does not store a baud rate in the deployed configuration.
    # The legacy LabVIEW driver initializes this connection at 9600 baud.
    "BrooksDevTh": 9600,
}


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
    device: str
    description: str = ""
    hardware_id: str = ""


@dataclass(frozen=True, slots=True)
class ConfiguredDevice:
    thread_name: str
    role: str
    device_name: str
    configured_port: str | None
    baudrate: int | None
    enabled: bool
    input_channels: tuple[str, ...]
    output_channels: tuple[str, ...]
    port_status: str
    observed_description: str | None = None
    device_address: int | None = None
    module_type: str | None = None
    input_range: int | None = None
    data_format: str | None = None


def discover_serial_ports() -> tuple[tuple[SerialPortInfo, ...], str | None]:
    """Enumerate Windows serial ports without opening any of them."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return (), "pyserial is not installed; port enumeration was skipped"
    ports = tuple(
        SerialPortInfo(
            device=str(port.device),
            description=str(port.description or ""),
            hardware_id=str(port.hwid or ""),
        )
        for port in list_ports.comports()
    )
    return ports, None


def load_build_inventory(
    builds_directory: str | Path,
    *,
    observed_ports: Iterable[SerialPortInfo] = (),
    ports_checked: bool | None = None,
) -> tuple[ConfiguredDevice, ...]:
    builds = Path(builds_directory)
    data = builds / "data"
    if not data.is_dir():
        raise FileNotFoundError(f"Build data directory does not exist: {data}")
    manager_path = data / "DevMgrTh"
    manager = load_legacy_json(manager_path)
    thread_names = tuple(str(name) for name in manager.get("DeviceThNames", ()))
    available = {port.device.upper(): port for port in observed_ports}
    if ports_checked is None:
        ports_checked = bool(available)
    result: list[ConfiguredDevice] = []
    for thread_name in thread_names:
        config_path = data / thread_name
        if not config_path.is_file():
            result.append(
                ConfiguredDevice(
                    thread_name=thread_name,
                    role=_DEVICE_ROLES.get(thread_name, "Unknown device"),
                    device_name=thread_name,
                    configured_port=None,
                    baudrate=None,
                    enabled=False,
                    input_channels=(),
                    output_channels=(),
                    port_status="configuration-missing",
                )
            )
            continue
        config = load_legacy_json(config_path)
        port = config.get("Source", config.get("Resource"))
        configured_port = str(port) if port is not None else None
        raw_baudrate = config.get("BaudRate", config.get("Baud Rate"))
        baudrate = (
            int(raw_baudrate)
            if raw_baudrate is not None
            else _DEFAULT_BAUDRATES.get(thread_name)
        )
        enabled = bool(config.get("EnableMS", True))
        observed = available.get(configured_port.upper()) if configured_port else None
        if configured_port is None:
            port_status = "not-configured"
        elif not ports_checked:
            port_status = "not-checked"
        elif observed is None:
            port_status = "missing"
        else:
            port_status = "available"
        result.append(
            ConfiguredDevice(
                thread_name=thread_name,
                role=_DEVICE_ROLES.get(thread_name, "Unknown device"),
                device_name=str(config.get("DevName", thread_name)),
                configured_port=configured_port,
                baudrate=baudrate,
                enabled=enabled,
                input_channels=tuple(str(value) for value in config.get("InputChannels", ())),
                output_channels=tuple(str(value) for value in config.get("OutputChannels", ())),
                port_status=port_status,
                observed_description=observed.description if observed is not None else None,
                device_address=(
                    int(config["DeviceAddress"])
                    if config.get("DeviceAddress") is not None
                    else None
                ),
                module_type=(
                    str(config["ModuleType"])
                    if config.get("ModuleType") is not None
                    else None
                ),
                input_range=(
                    int(config["InputRange"])
                    if config.get("InputRange") is not None
                    else None
                ),
                data_format=(
                    str(config["DataFormat"])
                    if config.get("DataFormat") is not None
                    else None
                ),
            )
        )
    return tuple(result)


def system_information() -> dict[str, str]:
    windows_release, windows_version, _service_pack, _ptype = platform.win32_ver()
    return {
        "platform": platform.platform(),
        "windows_release": windows_release,
        "windows_version": windows_version,
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_architecture": platform.architecture()[0],
        "processor_architecture": os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        "processor_architecture_w6432": os.environ.get("PROCESSOR_ARCHITEW6432", ""),
    }


def probe_brooks_read_only(
    device: ConfiguredDevice,
    *,
    transaction_factory: Callable[[SerialSettings], SerialTransaction] | None = None,
) -> dict[str, Any]:
    """Send identification and measured-flow queries only; never a P01 write."""
    if device.thread_name != "BrooksDevTh":
        raise ValueError("Read-only Brooks probe needs the BrooksDevTh configuration")
    if not device.configured_port or not device.baudrate:
        raise ValueError("Brooks port and baud rate must be configured")
    settings = SerialSettings(
        port=device.configured_port,
        baudrate=device.baudrate,
        timeout_seconds=1.0,
        write_timeout_seconds=1.0,
    )
    factory = transaction_factory or (
        lambda serial_settings: PySerialTransaction(serial_settings, hardware_enabled=True)
    )
    transport = factory(settings)
    requests: list[str] = []
    responses: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        identify_request = Brooks0254Codec.identify_command()
        requests.append(identify_request.decode("ascii").replace("\r", "\\r"))
        identify_response = transport.transact(identify_request)
        responses.append(
            {
                "kind": "identify",
                "request": requests[-1],
                "raw_ascii": _escaped_ascii(identify_response),
                "raw_hex": identify_response.hex(" "),
            }
        )
        flows: list[float | None] = []
        for channel in range(len(device.input_channels)):
            request = Brooks0254Codec.read_flow_command(channel)
            request_text = request.decode("ascii").replace("\r", "\\r")
            requests.append(request_text)
            try:
                raw = transport.transact(request)
            except Exception as exc:
                message = f"channel {channel} transport error: {type(exc).__name__}: {exc}"
                errors.append(message)
                responses.append(
                    {
                        "kind": "measured_flow",
                        "channel": channel,
                        "request": request_text,
                        "error": message,
                    }
                )
                flows.append(None)
                continue
            record: dict[str, Any] = {
                "kind": "measured_flow",
                "channel": channel,
                "request": request_text,
                "raw_ascii": _escaped_ascii(raw),
                "raw_hex": raw.hex(" "),
            }
            try:
                parsed = Brooks0254Codec.parse_response(raw)
                record.update(
                    {
                        "checksum_valid": True,
                        "unit_address": parsed.unit_address,
                        "port": parsed.port,
                        "response_type": parsed.response_type,
                        "payload": parsed.payload,
                    }
                )
                value = Brooks0254Codec.parse_flow_response(raw, expected_port=2 * channel + 1)
            except Exception as exc:
                message = f"channel {channel} protocol error: {type(exc).__name__}: {exc}"
                errors.append(message)
                record["error"] = message
                value = None
            record["measured_flow"] = value
            responses.append(record)
            flows.append(value)
        return {
            "status": "ok" if not errors else "protocol-error",
            "port": device.configured_port,
            "identification_raw_ascii": _escaped_ascii(identify_response),
            "measured_flows": flows,
            "requests": requests,
            "responses": responses,
            "errors": errors,
            "setpoint_writes": 0,
        }
    finally:
        transport.close()


def _escaped_ascii(raw: bytes) -> str:
    return (
        raw.decode("ascii", errors="backslashreplace")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def probe_adam4118_read_only(
    device: ConfiguredDevice,
    *,
    transaction_factory: Callable[[SerialSettings], SerialTransaction] | None = None,
) -> dict[str, Any]:
    """Read ADAM-4118 analog inputs; this protocol path contains no output command."""
    if device.thread_name != "AIOTh":
        raise ValueError("Read-only ADAM-4118 probe needs the AIOTh configuration")
    if not device.configured_port or not device.baudrate:
        raise ValueError("ADAM-4118 port and baud rate must be configured")
    if device.data_format not in (None, "EngineeringUnits"):
        raise ValueError(
            f"ADAM-4118 probe only supports EngineeringUnits, not {device.data_format!r}"
        )
    address = 1 if device.device_address is None else device.device_address
    settings = SerialSettings(
        port=device.configured_port,
        baudrate=device.baudrate,
        timeout_seconds=1.0,
        write_timeout_seconds=1.0,
        read_terminator=b"\r",
    )
    factory = transaction_factory or (
        lambda serial_settings: PySerialTransaction(serial_settings, hardware_enabled=True)
    )
    transport = factory(settings)
    request = Adam4118Codec.read_all_command(address)
    try:
        client = Adam4118Client(transport, address=address)
        values = client.read_all(minimum_channels=max(1, len(device.input_channels)))
        named_values = {
            name: values[index]
            for index, name in enumerate(device.input_channels)
        }
        return {
            "status": "ok",
            "port": device.configured_port,
            "device_address": address,
            "request": request.decode("ascii").replace("\r", "\\r"),
            "all_engineering_values": values,
            "named_values": named_values,
            "read_queries": 1,
            "output_commands": 0,
        }
    finally:
        transport.close()


def build_report(
    builds_directory: str | Path,
    *,
    observed_ports: Iterable[SerialPortInfo] | None = None,
    discovery_error: str | None = None,
) -> dict[str, Any]:
    if observed_ports is None:
        discovered, discovered_error = discover_serial_ports()
        ports = discovered
        discovery_error = discovery_error or discovered_error
    else:
        ports = tuple(observed_ports)
    devices = load_build_inventory(
        builds_directory,
        observed_ports=ports,
        ports_checked=discovery_error is None,
    )
    warnings: list[str] = []
    for device in devices:
        if device.configured_port and device.port_status == "missing":
            qualifier = "disabled " if not device.enabled else ""
            warnings.append(
                f"{qualifier}{device.thread_name} expects {device.configured_port}, "
                "but that port was not enumerated"
            )
    return {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "builds_directory": str(Path(builds_directory).resolve()),
        "system": system_information(),
        "serial_discovery_error": discovery_error,
        "observed_ports": [asdict(port) for port in ports],
        "configured_devices": [asdict(device) for device in devices],
        "warnings": warnings,
        "safety": {
            "ports_opened_by_inventory": 0,
            "device_queries_sent_by_inventory": 0,
            "output_commands_sent_by_inventory": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory SpecMass deployment configuration without opening hardware ports"
    )
    parser.add_argument("--builds", type=Path, required=True, help="folder containing MassSpec.exe and data")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("specmass-hardware-inventory.json"),
        help="JSON report path",
    )
    parser.add_argument(
        "--probe-brooks",
        action="store_true",
        help="also send Brooks identification and measured-flow query frames",
    )
    parser.add_argument(
        "--probe-adam4118",
        action="store_true",
        help="also send one ADAM-4118 read-all-analog-input query",
    )
    parser.add_argument(
        "--allow-read-queries",
        action="store_true",
        help="explicitly permit read-only serial query frames; never permits setpoint/output writes",
    )
    args = parser.parse_args()
    if (args.probe_brooks or args.probe_adam4118) and not args.allow_read_queries:
        parser.error("read-only probes require --allow-read-queries")

    report = build_report(args.builds)
    if args.probe_adam4118:
        adam = next(
            (
                ConfiguredDevice(**device)
                for device in report["configured_devices"]
                if device["thread_name"] == "AIOTh"
            ),
            None,
        )
        if adam is None:
            report["adam4118_read_only_probe"] = {"status": "not-configured"}
        else:
            try:
                report["adam4118_read_only_probe"] = probe_adam4118_read_only(adam)
            except Exception as exc:
                report["adam4118_read_only_probe"] = {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "output_commands": 0,
                }
    if args.probe_brooks:
        brooks = next(
            (
                ConfiguredDevice(**device)
                for device in report["configured_devices"]
                if device["thread_name"] == "BrooksDevTh"
            ),
            None,
        )
        if brooks is None:
            report["brooks_read_only_probe"] = {"status": "not-configured"}
        else:
            try:
                report["brooks_read_only_probe"] = probe_brooks_read_only(brooks)
            except Exception as exc:
                report["brooks_read_only_probe"] = {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "setpoint_writes": 0,
                }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Hardware inventory written to {args.output.resolve()}")
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    if args.probe_brooks:
        print(f"Brooks read-only probe: {report['brooks_read_only_probe']['status']}")
    if args.probe_adam4118:
        print(f"ADAM-4118 read-only probe: {report['adam4118_read_only_probe']['status']}")
    if not args.probe_brooks and not args.probe_adam4118:
        print("No serial port was opened and no device query was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
