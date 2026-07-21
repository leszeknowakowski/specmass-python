from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Protocol, Sequence

from .serial_transport import SerialTransaction


class BrooksProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrooksResponse:
    unit_address: int
    port: int
    response_type: int
    payload: tuple[str, ...]
    checksum: int


class BrooksSetpointWriter(Protocol):
    def set_flow(self, channel: int, value: float) -> object: ...


class BrooksChangeOnlyDispatcher:
    """Reproduce the legacy driver's zero-initialized PrevData write gate."""

    def __init__(
        self,
        writer: BrooksSetpointWriter,
        *,
        channel_count: int = 4,
        initial_setpoints: Sequence[float] | None = None,
    ) -> None:
        if channel_count < 0:
            raise ValueError("Brooks channel count cannot be negative")
        values = (
            [0.0] * channel_count
            if initial_setpoints is None
            else [float(value) for value in initial_setpoints]
        )
        if len(values) != channel_count:
            raise ValueError("Initial Brooks setpoint count does not match channel count")
        if not all(isfinite(value) and value >= 0 for value in values):
            raise ValueError("Initial Brooks setpoints must be finite and non-negative")
        self.writer = writer
        self._previous = values

    @property
    def previous_setpoints(self) -> tuple[float, ...]:
        return tuple(self._previous)

    def apply(
        self,
        setpoints: Sequence[float],
        *,
        write_enabled: Sequence[bool] | None = None,
    ) -> tuple[bool, ...]:
        values = [float(value) for value in setpoints]
        if len(values) != len(self._previous):
            raise ValueError("Brooks setpoint count does not match channel count")
        enabled = (
            [True] * len(values)
            if write_enabled is None
            else [bool(value) for value in write_enabled]
        )
        if len(enabled) != len(values):
            raise ValueError("Brooks write-enable count does not match channel count")

        performed = [False] * len(values)
        for channel, value in enumerate(values):
            if not isfinite(value) or value < 0:
                raise ValueError("Brooks flow setpoints must be finite and non-negative")
            if not enabled[channel] or value == self._previous[channel]:
                continue
            self.writer.set_flow(channel, value)
            self._previous[channel] = value
            performed[channel] = True
        return tuple(performed)


class Brooks0254Codec:
    """Pure ASCII codec for the Brooks 0251/0254 Appendix C protocol."""

    @staticmethod
    def identify_command() -> bytes:
        return b"AZI\r"

    @classmethod
    def read_flow_command(cls, channel: int, *, unit_address: int | None = None) -> bytes:
        return f"{cls._address(2 * cls._channel(channel) + 1, unit_address)}K\r".encode("ascii")

    @classmethod
    def set_flow_command(
        cls,
        channel: int,
        value: float,
        *,
        unit_address: int | None = None,
    ) -> bytes:
        value = float(value)
        if not isfinite(value) or value < 0:
            raise ValueError("Brooks flow setpoint must be a finite non-negative number")
        port = 2 * cls._channel(channel) + 2
        return f"{cls._address(port, unit_address)}P01={value:g}\r".encode("ascii")

    @staticmethod
    def response_checksum(information_frame: bytes) -> int:
        """Return the documented negated modulo-256 byte sum."""
        return (-sum(information_frame)) & 0xFF

    @classmethod
    def parse_response(cls, raw: bytes, *, verify_checksum: bool = True) -> BrooksResponse:
        try:
            text = raw.decode("ascii").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise BrooksProtocolError("Brooks response is not ASCII") from exc
        try:
            information, checksum_text = text.rsplit(",", 1)
            checksum = int(checksum_text, 16)
        except (ValueError, TypeError) as exc:
            raise BrooksProtocolError(f"Malformed Brooks response: {text!r}") from exc
        information_frame = (information + ",").encode("ascii")
        expected = cls.response_checksum(information_frame)
        if verify_checksum and checksum != expected:
            raise BrooksProtocolError(
                f"Brooks checksum mismatch: received {checksum:02X}, calculated {expected:02X}"
            )
        parts = information.split(",")
        if len(parts) < 4 or parts[0] != "AZ" or "." not in parts[1]:
            raise BrooksProtocolError(f"Unexpected Brooks response frame: {text!r}")
        try:
            address_text, port_text = parts[1].split(".", 1)
            unit_address = int(address_text)
            port = int(port_text)
            response_type = int(parts[2])
        except ValueError as exc:
            raise BrooksProtocolError(f"Invalid Brooks response address/type: {text!r}") from exc
        return BrooksResponse(
            unit_address=unit_address,
            port=port,
            response_type=response_type,
            payload=tuple(parts[3:]),
            checksum=checksum,
        )

    @classmethod
    def parse_flow_response(cls, raw: bytes, *, verify_checksum: bool = True) -> float:
        response = cls.parse_response(raw, verify_checksum=verify_checksum)
        if response.response_type != 2 or len(response.payload) < 3:
            raise BrooksProtocolError("Response is not a measured-channel value packet")
        try:
            return float(response.payload[2])
        except ValueError as exc:
            raise BrooksProtocolError("Measured flow is not numeric") from exc

    @staticmethod
    def _channel(channel: int) -> int:
        channel = int(channel)
        if not 0 <= channel <= 3:
            raise ValueError("Brooks 0254 channel must be between 0 and 3")
        return channel

    @staticmethod
    def _address(port: int, unit_address: int | None) -> str:
        if unit_address is None:
            return f"AZ.{port:02d}"
        unit_address = int(unit_address)
        if not 0 <= unit_address <= 65535:
            raise ValueError("Brooks unit address must be between 0 and 65535")
        return f"AZ{unit_address:05d}.{port:02d}"


class Brooks0254Client:
    """Small driver over an injected guarded transport; it never opens by itself."""

    def __init__(self, transport: SerialTransaction, *, unit_address: int | None = None) -> None:
        self.transport = transport
        self.unit_address = unit_address

    def read_flow(self, channel: int) -> float:
        request = Brooks0254Codec.read_flow_command(channel, unit_address=self.unit_address)
        return Brooks0254Codec.parse_flow_response(self.transport.transact(request))

    def set_flow(self, channel: int, value: float) -> BrooksResponse:
        request = Brooks0254Codec.set_flow_command(channel, value, unit_address=self.unit_address)
        response = Brooks0254Codec.parse_response(self.transport.transact(request))
        expected_port = 2 * int(channel) + 2
        if response.response_type != 4 or response.port != expected_port:
            raise BrooksProtocolError("Setpoint acknowledgement does not match the requested channel")
        if len(response.payload) < 2 or response.payload[0] != "P01":
            raise BrooksProtocolError("Setpoint acknowledgement does not contain P01")
        try:
            returned_value = float(response.payload[1])
        except ValueError as exc:
            raise BrooksProtocolError("Setpoint acknowledgement value is not numeric") from exc
        if abs(returned_value - float(value)) > 1e-9:
            raise BrooksProtocolError(
                f"Setpoint acknowledgement returned {returned_value}, expected {float(value)}"
            )
        return response

    def close(self) -> None:
        self.transport.close()
