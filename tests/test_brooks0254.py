import unittest

from specmass.devices.brooks0254 import (
    Brooks0254Client,
    Brooks0254Codec,
    Brooks0254ReadOnlyClient,
    BrooksChangeOnlyDispatcher,
    BrooksProtocolError,
)
from specmass.devices.serial_transport import HardwareDisabledError, PySerialTransaction, SerialSettings


def response(frame: str) -> bytes:
    information = frame.encode("ascii")
    checksum = Brooks0254Codec.response_checksum(information)
    return information + f"{checksum:02X}\r\n".encode("ascii")


class FakeTransport:
    def __init__(self, reply: bytes):
        self.reply = reply
        self.requests = []

    def transact(self, request: bytes) -> bytes:
        self.requests.append(request)
        return self.reply

    def close(self):
        pass


class FakeSetpointWriter:
    def __init__(self):
        self.writes = []

    def set_flow(self, channel, value):
        self.writes.append((channel, value))


class BrooksCodecTests(unittest.TestCase):
    def test_commands_use_documented_input_and_output_ports(self):
        self.assertEqual(Brooks0254Codec.identify_command(), b"AZI\r")
        self.assertEqual(Brooks0254Codec.read_flow_command(0), b"AZ.01K\r")
        self.assertEqual(Brooks0254Codec.read_flow_command(3), b"AZ.07K\r")
        self.assertEqual(Brooks0254Codec.set_flow_command(0, 30), b"AZ.02P01=30\r")
        self.assertEqual(Brooks0254Codec.set_flow_command(3, 1.5), b"AZ.08P01=1.5\r")
        self.assertEqual(
            Brooks0254Codec.set_flow_command(3, 10, unit_address=909),
            b"AZ00909.08P01=10\r",
        )

    def test_measured_flow_response_is_parsed_and_checksummed(self):
        raw = response("AZ,00909.01,2,xxxxxxxx.xx,00162871.43,-0000003.27,X,X,X,X,X,X,")
        self.assertEqual(Brooks0254Codec.parse_flow_response(raw), -3.27)

    def test_deployed_firmware_polled_flow_response_is_parsed(self):
        raw = (
            b"AZ,16773.01,4,  215239.20,  215239.20,      -0.48,"
            b"      -0.48,08214,X,X,X,X,X,C8\r\n"
        )
        self.assertEqual(
            Brooks0254Codec.parse_flow_response(raw, expected_port=1),
            -0.48,
        )

    def test_parameter_acknowledgement_is_not_accepted_as_flow(self):
        raw = response("AZ,00909.01,4,P01,30,")
        with self.assertRaises(BrooksProtocolError):
            Brooks0254Codec.parse_flow_response(raw)

    def test_flow_response_must_match_requested_port(self):
        raw = response("AZ,00909.03,4,0,0,-0.24,0,0,X,X,X,X,X,")
        with self.assertRaisesRegex(BrooksProtocolError, "port 3, expected 1"):
            Brooks0254Codec.parse_flow_response(raw, expected_port=1)

    def test_read_only_client_has_no_setpoint_api_and_reads_all_channels(self):
        class FlowTransport:
            def __init__(self):
                self.requests = []

            def transact(inner_self, request):
                inner_self.requests.append(request)
                channel = (int(request[3:5]) - 1) // 2
                port = 2 * channel + 1
                return response(f"AZ,00909.{port:02d},4,0,0,{channel + 0.5},0,0,X,X,X,X,X,")

            def close(inner_self):
                pass

        transport = FlowTransport()
        client = Brooks0254ReadOnlyClient(transport)
        self.assertEqual(client.read_all(), (0.5, 1.5, 2.5, 3.5))
        self.assertFalse(hasattr(client, "set_flow"))
        self.assertEqual(
            transport.requests,
            [b"AZ.01K\r", b"AZ.03K\r", b"AZ.05K\r", b"AZ.07K\r"],
        )

    def test_checksum_excludes_az_packet_prelimiter(self):
        frame = b"AZ,06022,4,Brooks Instrument,Model 0254,08,V10.05.13,FE00,"
        self.assertEqual(Brooks0254Codec.response_checksum(frame), 0x9E)

    def test_checksum_mismatch_is_rejected(self):
        with self.assertRaises(BrooksProtocolError):
            Brooks0254Codec.parse_response(b"AZ,00909.01,4,P01,30.0,00\r\n")

    def test_client_validates_setpoint_acknowledgement(self):
        transport = FakeTransport(response("AZ,00909.04,4,P01,30,"))
        client = Brooks0254Client(transport)
        parsed = client.set_flow(1, 30)
        self.assertEqual(parsed.port, 4)
        self.assertEqual(transport.requests, [b"AZ.04P01=30\r"])

    def test_serial_transport_refuses_port_when_disabled(self):
        transport = PySerialTransaction(SerialSettings("COM13"))
        with self.assertRaises(HardwareDisabledError):
            transport.open()

    def test_change_only_dispatcher_preserves_front_panel_value_for_cached_zero(self):
        writer = FakeSetpointWriter()
        dispatcher = BrooksChangeOnlyDispatcher(writer, channel_count=4)

        self.assertEqual(dispatcher.apply((0.0, 0.0, 0.0, 0.0)), (False,) * 4)
        self.assertEqual(writer.writes, [])

        self.assertEqual(
            dispatcher.apply((0.0, 10.0, 0.0, 0.0)),
            (False, True, False, False),
        )
        self.assertEqual(writer.writes, [(1, 10.0)])
        self.assertEqual(dispatcher.apply((0.0, 10.0, 0.0, 0.0)), (False,) * 4)

    def test_change_only_dispatcher_honors_monitor_only_mask(self):
        writer = FakeSetpointWriter()
        dispatcher = BrooksChangeOnlyDispatcher(writer, channel_count=1)
        self.assertEqual(dispatcher.apply((30.0,), write_enabled=(False,)), (False,))
        self.assertEqual(dispatcher.previous_setpoints, (0.0,))
        self.assertEqual(writer.writes, [])


if __name__ == "__main__":
    unittest.main()
