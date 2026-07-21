from __future__ import annotations

from math import isfinite
import re
from typing import Sequence

from .base import ControlCommand, SensorSnapshot
from .serial_transport import HardwareDisabledError, SerialTransaction


class Adam4118ProtocolError(RuntimeError):
    pass


class Adam4118Codec:
    """ASCII command codec for the ADAM-4118 analog-input module."""

    _ENGINEERING_VALUE = re.compile(r"[+-](?:\d+(?:\.\d*)?|\.\d+)")

    @classmethod
    def read_channel_command(cls, address: int, channel: int) -> bytes:
        return f"#{cls._address(address)}{cls._channel(channel)}\r".encode("ascii")

    @classmethod
    def read_all_command(cls, address: int) -> bytes:
        return f"#{cls._address(address)}\r".encode("ascii")

    @classmethod
    def parse_single_engineering_response(cls, raw: bytes) -> float:
        values = cls._parse_engineering_values(raw)
        if len(values) != 1:
            raise Adam4118ProtocolError(
                f"Expected one ADAM-4118 value, received {len(values)}"
            )
        return values[0]

    @classmethod
    def parse_all_engineering_response(
        cls,
        raw: bytes,
        *,
        minimum_channels: int = 1,
    ) -> tuple[float, ...]:
        if minimum_channels < 1:
            raise ValueError("minimum_channels must be positive")
        values = cls._parse_engineering_values(raw)
        if len(values) < minimum_channels:
            raise Adam4118ProtocolError(
                f"Expected at least {minimum_channels} ADAM-4118 values, received {len(values)}"
            )
        return values

    @classmethod
    def _parse_engineering_values(cls, raw: bytes) -> tuple[float, ...]:
        try:
            text = raw.decode("ascii").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise Adam4118ProtocolError("ADAM-4118 response is not ASCII") from exc
        if text.startswith("?"):
            raise Adam4118ProtocolError(f"ADAM-4118 rejected the command: {text!r}")
        if not text.startswith(">"):
            raise Adam4118ProtocolError(f"Unexpected ADAM-4118 response: {text!r}")
        payload = text[1:]
        matches = cls._ENGINEERING_VALUE.findall(payload)
        if not matches or "".join(matches) != payload:
            raise Adam4118ProtocolError(
                f"Malformed ADAM-4118 engineering-unit response: {text!r}"
            )
        values = tuple(float(value) for value in matches)
        if not all(isfinite(value) for value in values):
            raise Adam4118ProtocolError("ADAM-4118 returned a non-finite value")
        return values

    @staticmethod
    def _address(address: int) -> str:
        address = int(address)
        if not 0 <= address <= 0xFF:
            raise ValueError("ADAM-4118 address must be between 0 and 255")
        return f"{address:02X}"

    @staticmethod
    def _channel(channel: int) -> int:
        channel = int(channel)
        if not 0 <= channel <= 7:
            raise ValueError("ADAM-4118 channel must be between 0 and 7")
        return channel


class Adam4118Client:
    """Read-only client over an injected guarded serial transport."""

    def __init__(self, transport: SerialTransaction, *, address: int = 1) -> None:
        self.transport = transport
        self.address = int(address)

    def read_channel(self, channel: int) -> float:
        request = Adam4118Codec.read_channel_command(self.address, channel)
        response = self.transport.transact(request)
        return Adam4118Codec.parse_single_engineering_response(response)

    def read_all(self, *, minimum_channels: int = 1) -> tuple[float, ...]:
        request = Adam4118Codec.read_all_command(self.address)
        response = self.transport.transact(request)
        return Adam4118Codec.parse_all_engineering_response(
            response,
            minimum_channels=minimum_channels,
        )

    def close(self) -> None:
        self.transport.close()


class Adam4118MonitorBackend:
    """Temperature-only backend which deliberately rejects every control command."""

    def __init__(
        self,
        client: Adam4118Client,
        *,
        channel_names: Sequence[str] = ("Temperature", "Temperature2"),
    ) -> None:
        names = tuple(str(name) for name in channel_names)
        if not names:
            raise ValueError("At least one ADAM-4118 channel name is required")
        self.client = client
        self.channel_names = names

    def read(self, timestamp: float) -> SensorSnapshot:
        values = self.client.read_all(minimum_channels=len(self.channel_names))
        selected = tuple(values[: len(self.channel_names)])
        return SensorSnapshot(
            timestamp=float(timestamp),
            temperature=selected[0],
            temperatures=selected,
            flows=(),
            masses={},
        )

    def apply(self, command: ControlCommand, dt_seconds: float) -> None:
        del command, dt_seconds
        raise HardwareDisabledError(
            "ADAM-4118 monitor backend is read-only and rejects all control commands"
        )

    def safe_shutdown(self) -> None:
        self.client.close()
