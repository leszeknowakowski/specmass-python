import unittest

from specmass.devices.hiden import (
    HidenIdentityCodec,
    HidenIdentityReadOnlyClient,
    HidenProtocolError,
)
from specmass.hardware_inventory import ConfiguredDevice, probe_hiden_identity_read_only


class FakeHidenTransport:
    def __init__(self, response: bytes = b"HAL RC RGA 201 #16359\r") -> None:
        self.response = response
        self.requests: list[bytes] = []
        self.closed = False

    def transact(self, request: bytes) -> bytes:
        self.requests.append(request)
        return self.response

    def close(self) -> None:
        self.closed = True


def configured_hiden() -> ConfiguredDevice:
    return ConfiguredDevice(
        thread_name="MSDevTh",
        role="Hiden mass spectrometer",
        device_name="MSDevTh",
        configured_port="COM3",
        baudrate=921600,
        enabled=True,
        input_channels=(),
        output_channels=(),
        port_status="available",
    )


class HidenIdentityTests(unittest.TestCase):
    def test_identity_codec_uses_the_isolated_legacy_query(self):
        self.assertEqual(HidenIdentityCodec.identity_command(), b"pget name\r")
        self.assertEqual(
            HidenIdentityCodec.parse_identity_response(b"HAL RC RGA 201 #16359\r"),
            "HAL RC RGA 201 #16359",
        )

    def test_identity_client_exposes_no_state_changing_operations(self):
        transport = FakeHidenTransport()
        client = HidenIdentityReadOnlyClient(transport)
        self.assertEqual(client.read_identity(), "HAL RC RGA 201 #16359")
        self.assertFalse(hasattr(client, "initialize"))
        self.assertFalse(hasattr(client, "set_mode"))
        self.assertFalse(hasattr(client, "set_filament"))
        self.assertFalse(hasattr(client, "start_scan"))
        self.assertEqual(transport.requests, [b"pget name\r"])

    def test_errors_and_empty_responses_fail_closed(self):
        for response in (b"", b"\r", b"Error 5\r", b"HAL\x01RC\r"):
            with self.subTest(response=response):
                with self.assertRaises(HidenProtocolError):
                    HidenIdentityCodec.parse_identity_response(response)

    def test_inventory_probe_reports_zero_state_change_commands(self):
        transport = FakeHidenTransport()
        result = probe_hiden_identity_read_only(
            configured_hiden(),
            transaction_factory=lambda _settings: transport,
        )
        self.assertEqual(result["identity"], "HAL RC RGA 201 #16359")
        self.assertEqual(result["state_change_commands"], 0)
        self.assertEqual(result["initialization_commands"], 0)
        self.assertEqual(result["standby_commands"], 0)
        self.assertEqual(result["filament_commands"], 0)
        self.assertEqual(result["scan_commands"], 0)
        self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()
