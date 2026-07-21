from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Protocol


class HardwareDisabledError(RuntimeError):
    pass


class SerialTransaction(Protocol):
    def transact(self, request: bytes) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SerialSettings:
    port: str
    baudrate: int = 9600
    timeout_seconds: float = 1.0
    write_timeout_seconds: float = 1.0
    read_terminator: bytes = b"\n"

    def __post_init__(self) -> None:
        if not self.port.strip():
            raise ValueError("Serial port cannot be empty")
        if self.baudrate <= 0 or self.timeout_seconds <= 0 or self.write_timeout_seconds <= 0:
            raise ValueError("Serial timing values must be positive")
        if not isinstance(self.read_terminator, bytes) or not self.read_terminator:
            raise ValueError("Serial read terminator must be non-empty bytes")


class PySerialTransaction:
    """Guarded request/response transport; disabled unless explicitly opted in."""

    def __init__(self, settings: SerialSettings, *, hardware_enabled: bool = False) -> None:
        self.settings = settings
        self.hardware_enabled = hardware_enabled
        self._serial = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    def open(self) -> None:
        if not self.hardware_enabled:
            raise HardwareDisabledError(
                f"Hardware access is disabled; refusing to open {self.settings.port}"
            )
        if self.is_open:
            return
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError(
                "Serial hardware support needs: pip install 'specmass[hardware]'"
            ) from exc
        self._serial = serial.Serial(
            port=self.settings.port,
            baudrate=self.settings.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.settings.timeout_seconds,
            write_timeout=self.settings.write_timeout_seconds,
        )

    def transact(self, request: bytes) -> bytes:
        if not self.is_open:
            self.open()
        assert self._serial is not None
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write(request)
            self._serial.flush()
            response = self._serial.read_until(self.settings.read_terminator)
        if not response:
            raise TimeoutError(f"No response from {self.settings.port}")
        return bytes(response)

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None
