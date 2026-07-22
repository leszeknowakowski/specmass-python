from __future__ import annotations

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
