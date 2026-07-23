from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
from typing import Mapping

from ..hiden import (
    HidenEnvironmentConfig,
    HidenScanDefinition,
    HidenScanPlan,
    hiden_mass_stimuli,
)
from .serial_transport import SerialTransaction


class HidenProtocolError(ValueError):
    pass


class HidenIdentityCodec:
    """The isolated, non-initializing identity query found in the legacy driver."""

    @staticmethod
    def identity_command() -> bytes:
        return b"pget name\r"

    @staticmethod
    def parse_identity_response(response: bytes) -> str:
        try:
            text = response.decode("ascii").strip("\r\n \t")
        except UnicodeDecodeError as exc:
            raise HidenProtocolError("Hiden identity response is not ASCII") from exc
        if not text:
            raise HidenProtocolError("Hiden identity response is empty")
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
            text = text[1:-1].strip()
        if not text:
            raise HidenProtocolError("Hiden identity response contains an empty quoted string")
        if any(ord(char) < 32 for char in text):
            raise HidenProtocolError("Hiden identity response contains control characters")
        if text.casefold().startswith(("error", "err ", "err:")):
            raise HidenProtocolError(f"Hiden returned an error response: {text}")
        return text


class HidenIdentityReadOnlyClient:
    """Exposes only `pget name`; no initialization or state-changing API exists."""

    def __init__(self, transport: SerialTransaction) -> None:
        self.transport = transport

    def read_identity(self) -> str:
        response = self.transport.transact(HidenIdentityCodec.identity_command())
        return HidenIdentityCodec.parse_identity_response(response)

    def close(self) -> None:
        self.transport.close()


@dataclass(frozen=True, slots=True)
class HidenScanSample:
    elapsed_milliseconds: int
    stimuli: tuple[float, ...]
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class HidenAcquisitionCycle:
    scans: tuple[HidenScanSample, ...]


class HidenScanCodec:
    """Command grammar recovered from the bundled Hiden LabVIEW driver."""

    CONFIG_PREAMBLE = (
        "l999 scan",
        "lini all",
        "lset mode 0",
        "lset mode 0",
        "sout NUL:",
        "data on",
        "sdel all",
        "tdel all",
        "pset terse 1",
        "pset points 70",
        "serr NUL:",
        "sset scan Ascans",
    )

    @staticmethod
    def encode_command(command: str) -> bytes:
        if not command or any(character in command for character in "\r\n\x00"):
            raise HidenProtocolError("Hiden command must be one non-empty line")
        try:
            return command.encode("ascii") + b"\r"
        except UnicodeEncodeError as exc:
            raise HidenProtocolError("Hiden command must contain ASCII only") from exc

    @staticmethod
    def parse_command_response(response: bytes, *, command: str) -> str:
        try:
            text = response.decode("ascii").strip("\r\n")
        except UnicodeDecodeError as exc:
            raise HidenProtocolError(
                f"Hiden response to {command!r} is not ASCII"
            ) from exc
        marker = text.find("*C")
        if marker >= 0:
            match = re.search(r"\*C\s*(-?\d+)", text[marker:])
            code = match.group(1) if match else "unknown"
            raise HidenProtocolError(
                f"Hiden rejected command {command!r} with error {code}: {text}"
            )
        return text

    @staticmethod
    def environment_commands(
        environment: HidenEnvironmentConfig,
        filament: str,
    ) -> tuple[str, ...]:
        selected = filament.strip().upper()
        if selected not in ("", "F1", "F2"):
            raise HidenProtocolError(f"Unknown Hiden filament: {filament!r}")
        try:
            rga_mode = next(
                index
                for index, mode in enumerate(environment.modes)
                if mode.strip().casefold() == "rga"
            )
        except StopIteration as exc:
            raise HidenProtocolError("Hiden environment has no RGA operating mode") from exc

        commands: list[str] = []
        for device in environment.devices:
            if device.group_membership != 1:
                continue
            values = list(device.values_by_mode)
            if device.name.upper() in ("F1", "F2"):
                values[rga_mode] = 1.0 if device.name.upper() == selected else 0.0
            rendered = " ".join(
                HidenScanCodec._format_environment_value(
                    value,
                    device.format_string,
                )
                for value in values
            )
            commands.append(f"lput {device.index} {rendered}")
        commands.append("lset enable 1")
        return tuple(commands)

    @staticmethod
    def scan_row_commands(
        row: int,
        scan: HidenScanDefinition,
        *,
        autozero_supported: bool,
    ) -> tuple[str, ...]:
        row = int(row)
        if row < 1:
            raise HidenProtocolError("Hiden scan rows are numbered from 1")
        commands = [
            f"sset row {row}",
            "sset report 17",
            f"sset output {scan.device_to_scan}",
            f"sset start {scan.start_value:.2f}",
            f"sset stop {scan.stop_value:.2f}",
            f"sset step {scan.increment:.2f}",
            f"sset mode {scan.scan_mode:d}",
            f"sset input {scan.input_device}",
        ]
        if scan.input_device.casefold() != "scans":
            commands.extend(
                (
                    f"sset high {scan.autorange_high:d}",
                    f"sset low {scan.autorange_low:d}",
                    f"sset current {scan.start_range:d}",
                )
            )
        commands.extend(
            (
                f"sset dwell {scan.dwell_percent:d}%",
                f"sset settle {scan.settle_percent:d}%",
            )
        )
        if autozero_supported:
            commands.append(f"sset zero {int(scan.use_autozero)}")
        if scan.options:
            commands.append(f"sset options {scan.options}")
        if scan.environment_changes:
            commands.append(f"sset env {scan.environment_changes}")
        return tuple(commands)

    @staticmethod
    def parse_job_id(response: str) -> str:
        tokens = [
            token.strip().strip("'\"")
            for token in response.split(",")
            if token.strip().strip("'\"")
        ]
        candidates = [
            token
            for token in tokens
            if token.casefold() not in ("ascans", "scan", "job")
        ]
        numeric = next((token for token in candidates if token.isdecimal()), None)
        job_id = numeric or (candidates[-1] if candidates else "")
        if not job_id or re.fullmatch(r"[A-Za-z0-9_.:-]+", job_id) is None:
            raise HidenProtocolError(
                f"Cannot extract a safe Hiden scan job ID from {response!r}"
            )
        return job_id

    @staticmethod
    def _format_environment_value(value: float, format_string: str) -> str:
        if format_string == "%d":
            return str(int(round(value)))
        if format_string == "%.2f":
            return f"{value:.2f}"
        raise HidenProtocolError(
            f"Unsupported Hiden environment format: {format_string!r}"
        )


class HidenDataStreamParser:
    """Incrementally decode the MSIU report-17 stream used by the LabVIEW VI."""

    def __init__(self, scan_plan: HidenScanPlan) -> None:
        self.scan_plan = scan_plan
        self._buffer = ""

    def clear(self) -> None:
        self._buffer = ""

    def feed(self, response: bytes) -> None:
        try:
            text = response.decode("ascii")
        except UnicodeDecodeError as exc:
            raise HidenProtocolError("Hiden acquisition data is not ASCII") from exc
        self._buffer += text
        if len(self._buffer) > 4_000_000:
            raise HidenProtocolError("Hiden acquisition buffer exceeded its safety limit")

    def pop_cycle(self) -> HidenAcquisitionCycle | None:
        frame_start = self._buffer.find("[")
        error_markers = tuple(
            marker
            for marker in (self._buffer.find("!"), self._buffer.find("*C"))
            if marker >= 0 and (frame_start < 0 or marker < frame_start)
        )
        if error_markers:
            error_marker = min(error_markers)
            message = self._buffer[max(0, error_marker - 40) : error_marker + 80]
            self._buffer = ""
            raise HidenProtocolError(f"Hiden acquisition stream reported an error: {message!r}")
        if frame_start < 0:
            if len(self._buffer) > 4096:
                self._buffer = self._buffer[-4096:]
            return None

        cursor = frame_start + 1
        samples: list[HidenScanSample] = []
        for scan in self.scan_plan.scans:
            slash = self._buffer.find("/", cursor)
            if slash < 0:
                return None
            elapsed_text = self._buffer[cursor:slash].strip(" \t\r\n,]")
            match = re.search(r"(-?\d+)\s*$", elapsed_text)
            if match is None:
                raise HidenProtocolError(
                    f"Invalid Hiden elapsed-time field: {elapsed_text!r}"
                )
            elapsed = int(match.group(1))
            cursor = slash + 1
            while cursor < len(self._buffer) and self._buffer[cursor] in " \t\r\n":
                cursor += 1
            if cursor >= len(self._buffer):
                return None

            if self._buffer[cursor] == "{":
                end = self._buffer.find("}", cursor + 1)
                if end < 0:
                    return None
                values = self._parse_values(self._buffer[cursor + 1 : end])
                cursor = end + 1
                if cursor < len(self._buffer) and self._buffer[cursor] == ",":
                    cursor += 1
            else:
                end = self._buffer.find(",", cursor)
                if end < 0:
                    return None
                values = self._parse_values(self._buffer[cursor:end])
                cursor = end + 1

            stimuli = scan.stimuli()
            if len(values) != len(stimuli):
                raise HidenProtocolError(
                    f"Hiden scan returned {len(values)} values for "
                    f"{len(stimuli)} stimuli"
                )
            # The legacy scan module reports the detector input divided by both
            # operator-entered relative factors.
            divisor = scan.relative_sensitivity * scan.relative_gain
            values = tuple(value / divisor for value in values)
            samples.append(
                HidenScanSample(
                    elapsed_milliseconds=elapsed,
                    stimuli=stimuli,
                    values=values,
                )
            )

        self._buffer = self._buffer[cursor:]
        return HidenAcquisitionCycle(tuple(samples))

    @staticmethod
    def _parse_values(text: str) -> tuple[float, ...]:
        tokens = [token.strip() for token in text.split(",") if token.strip()]
        if not tokens:
            raise HidenProtocolError("Hiden scan returned no values")
        try:
            values = tuple(float(token) for token in tokens)
        except ValueError as exc:
            raise HidenProtocolError(f"Invalid Hiden data values: {text!r}") from exc
        if not all(isfinite(value) for value in values):
            raise HidenProtocolError("Hiden scan returned a non-finite data value")
        return values


class HidenScanClient:
    """State-changing Hiden scan client with a fail-safe standby/disable path."""

    def __init__(
        self,
        transport: SerialTransaction,
        environment: HidenEnvironmentConfig,
    ) -> None:
        self.transport = transport
        self.environment = environment
        self.identity: str | None = None
        self.job_id: str | None = None
        self.scan_plan: HidenScanPlan | None = None
        self.command_log: list[str] = []
        self.active = False
        self._state_changes_started = False
        self._parser: HidenDataStreamParser | None = None

    def start(self, plan: HidenScanPlan) -> str:
        if self.active:
            raise RuntimeError("Hiden scan acquisition is already active")
        self.scan_plan = plan
        self.job_id = None
        self._parser = HidenDataStreamParser(plan)
        try:
            identity_response = self._transact("pget name")
            self.identity = HidenIdentityCodec.parse_identity_response(identity_response)
            if self.identity != self.environment.normalized_mass_spec_name:
                raise HidenProtocolError(
                    "Hiden identity does not match the selected environment file: "
                    f"{self.identity!r} != {self.environment.normalized_mass_spec_name!r}"
                )

            self._state_changes_started = True
            for command in HidenScanCodec.CONFIG_PREAMBLE:
                self._send(command)
            for command in HidenScanCodec.environment_commands(
                self.environment,
                plan.filament,
            ):
                self._send(command)

            first_scan = plan.scans[0]
            self._send(f"sset interval {first_scan.minimum_cycle_time_seconds:f}")
            # The instrument and LabVIEW scan editor use one-based scan rows.
            for row, scan in enumerate(plan.scans, start=1):
                for command in HidenScanCodec.scan_row_commands(
                    row,
                    scan,
                    autozero_supported=self.environment.autozero_supported,
                ):
                    self._send(command)

            self._send(f"sset cycles {first_scan.acquisition_cycles:d}")
            self._send("lini Ascans")
            self.job_id = HidenScanCodec.parse_job_id(
                self._send("sjob lget Ascans")
            )
            self._send("data all")
            self.active = True
            return self.identity
        except Exception:
            if self._state_changes_started:
                self._best_effort_shutdown()
            self.transport.close()
            self.active = False
            self._state_changes_started = False
            raise

    def poll_cycle(self) -> HidenAcquisitionCycle | None:
        if not self.active or self._parser is None:
            raise RuntimeError("Hiden scan acquisition is not active")
        available = self._parser.pop_cycle()
        if available is not None:
            return available
        response = self._transact("data")
        self._parser.feed(response)
        return self._parser.pop_cycle()

    def safe_shutdown(self) -> None:
        try:
            if self._state_changes_started:
                self._best_effort_shutdown()
        finally:
            self.transport.close()
            self.active = False
            self._state_changes_started = False
            self.job_id = None
            if self._parser is not None:
                self._parser.clear()

    def close(self) -> None:
        self.safe_shutdown()

    def _send(self, command: str) -> str:
        response = self._transact(command)
        return HidenScanCodec.parse_command_response(response, command=command)

    def _transact(self, command: str) -> bytes:
        self.command_log.append(command)
        return self.transport.transact(HidenScanCodec.encode_command(command))

    def _best_effort_shutdown(self) -> None:
        commands: list[str] = []
        if self.job_id:
            commands.append(f"stop {self.job_id}")
        commands.extend(("data stop", "lset mode 0", "lset enable 0"))
        for command in commands:
            try:
                self._send(command)
            except Exception:
                continue


class HidenTrendAcquisition:
    """Map single-point mass scans into the application's live mass channels."""

    def __init__(
        self,
        client: HidenScanClient,
        plan: HidenScanPlan,
        *,
        names_by_mass: Mapping[float, str] | None = None,
    ) -> None:
        invalid = [
            index + 1
            for index, scan in enumerate(plan.scans)
            if scan.device_to_scan.casefold() != "mass" or not scan.is_single_point
        ]
        if invalid:
            raise ValueError(
                "Live SpecMass plotting currently requires single-point mass trend "
                f"scans; incompatible scan rows: {invalid}"
            )
        self.client = client
        self.plan = plan
        self.mass_stimuli = hiden_mass_stimuli(plan, names_by_mass=names_by_mass)
        if len(self.mass_stimuli) != len(plan.scans):
            raise ValueError("Every live Hiden scan must have a unique mass label")
        self._mass_names = tuple(self.mass_stimuli)
        self._latest: dict[str, float] = {}

    @property
    def active(self) -> bool:
        return self.client.active

    def start(self) -> str:
        self._latest = {}
        return self.client.start(self.plan)

    def read_masses(self) -> Mapping[str, float]:
        cycle = self.client.poll_cycle()
        if cycle is not None:
            if len(cycle.scans) != len(self._mass_names):
                raise HidenProtocolError("Hiden cycle scan count changed unexpectedly")
            self._latest = {
                name: sample.values[0]
                for name, sample in zip(self._mass_names, cycle.scans)
            }
        return dict(self._latest)

    def safe_shutdown(self) -> None:
        self.client.safe_shutdown()
