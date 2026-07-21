import unittest

from specmass.devices.adam4118 import (
    Adam4118Client,
    Adam4118Codec,
    Adam4118MonitorBackend,
    Adam4118ProtocolError,
)
from specmass.devices.base import ControlCommand
from specmass.devices.serial_transport import HardwareDisabledError


class FakeTransport:
    def __init__(self, reply: bytes):
        self.reply = reply
        self.requests = []
        self.closed = False

    def transact(self, request: bytes) -> bytes:
        self.requests.append(request)
        return self.reply

    def close(self):
        self.closed = True


class Adam4118Tests(unittest.TestCase):
    def test_read_commands_use_hex_address_and_documented_channel_syntax(self):
        self.assertEqual(Adam4118Codec.read_all_command(1), b"#01\r")
        self.assertEqual(Adam4118Codec.read_channel_command(1, 0), b"#010\r")
        self.assertEqual(Adam4118Codec.read_channel_command(0x2A, 7), b"#2A7\r")

    def test_single_engineering_value_is_parsed(self):
        self.assertEqual(
            Adam4118Codec.parse_single_engineering_response(b">+24.200\r"),
            24.2,
        )

    def test_all_engineering_values_are_parsed(self):
        values = Adam4118Codec.parse_all_engineering_response(
            b">+24.200+25.100-001.500+00.000\r",
            minimum_channels=2,
        )
        self.assertEqual(values, (24.2, 25.1, -1.5, 0.0))

    def test_invalid_or_rejected_responses_fail_closed(self):
        for raw in (b"?01\r", b"+24.2\r", b">+24.2garbage\r", b">\r"):
            with self.subTest(raw=raw), self.assertRaises(Adam4118ProtocolError):
                Adam4118Codec.parse_all_engineering_response(raw)

    def test_client_is_read_only(self):
        transport = FakeTransport(b">+24.200+25.100\r")
        client = Adam4118Client(transport, address=1)
        self.assertEqual(client.read_all(minimum_channels=2), (24.2, 25.1))
        self.assertEqual(transport.requests, [b"#01\r"])

    def test_monitor_backend_reads_named_temperatures_and_rejects_commands(self):
        transport = FakeTransport(b">+23.500+32.500+00.0276\r")
        backend = Adam4118MonitorBackend(
            Adam4118Client(transport, address=1),
            channel_names=("Temperature", "Temperature2"),
        )
        snapshot = backend.read(2.5)
        self.assertEqual(snapshot.temperature, 23.5)
        self.assertEqual(snapshot.temperatures, (23.5, 32.5))
        with self.assertRaises(HardwareDisabledError):
            backend.apply(ControlCommand.safe(), 0.5)
        self.assertEqual(transport.requests, [b"#01\r"])
        backend.safe_shutdown()
        self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()
