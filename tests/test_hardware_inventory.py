import tempfile
import unittest
from pathlib import Path

from specmass.devices.brooks0254 import Brooks0254Codec
from specmass.hardware_inventory import (
    ConfiguredDevice,
    SerialPortInfo,
    build_report,
    load_build_inventory,
    probe_adam4118_read_only,
    probe_brooks_read_only,
)


def brooks_response(frame: str) -> bytes:
    information = frame.encode("ascii")
    checksum = Brooks0254Codec.response_checksum(information)
    return information + f"{checksum:02X}\r\n".encode("ascii")


class FakeBrooksTransport:
    def __init__(self):
        self.requests = []
        self.closed = False

    def transact(self, request: bytes) -> bytes:
        self.requests.append(request)
        if request == b"AZI\r":
            return b"AZ,00909,0254,4\r\n"
        channel = (int(request[3:5]) - 1) // 2
        port = 2 * channel + 1
        return brooks_response(
            f"AZ,00909.{port:02d},2,xxxxxxxx.xx,00162871.43,{10.0 + channel:g},X,X,X,X,X,X,"
        )

    def close(self):
        self.closed = True


class FakeAdamTransport:
    def __init__(self):
        self.requests = []
        self.closed = False

    def transact(self, request: bytes) -> bytes:
        self.requests.append(request)
        return b">+24.200+25.100+00.000+00.000\r"

    def close(self):
        self.closed = True


class HardwareInventoryTests(unittest.TestCase):
    def make_builds(self, root: Path) -> Path:
        builds = root / "Builds"
        data = builds / "data"
        data.mkdir(parents=True)
        (data / "DevMgrTh").write_text(
            '{"DeviceThNames":["MSDevTh","BrooksDevTh","AIOTh"]}',
            encoding="utf-8",
        )
        (data / "MSDevTh").write_text(
            '{"Resource":"COM4","Baud Rate":921600,"EnableMS":false}',
            encoding="utf-8",
        )
        (data / "BrooksDevTh").write_text(
            '{"DevName":"Brooks1","Source":"COM13",'
            '"InputChannels":["0","1","2","3"],'
            '"OutputChannels":["0","1","2","3"],}',
            encoding="utf-8",
        )
        (data / "AIOTh").write_text(
            '{"DevName":"ADAM4118","Source":"COM14","BaudRate":9600,'
            '"DeviceAddress":1,"ModuleType":"4118","InputRange":8,'
            '"DataFormat":"EngineeringUnits","InputChannels":["Temperature","Temperature2"],}' ,
            encoding="utf-8",
        )
        return builds

    def test_inventory_maps_build_configuration_to_observed_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            builds = self.make_builds(Path(directory))
            devices = load_build_inventory(
                builds,
                observed_ports=(
                    SerialPortInfo("COM13", "EDG VCOM Port 13"),
                    SerialPortInfo("COM14", "EDG VCOM Port 14"),
                    SerialPortInfo("COM3", "Hiden HAL Interface Unit"),
                ),
            )
        by_name = {device.thread_name: device for device in devices}
        self.assertEqual(by_name["BrooksDevTh"].port_status, "available")
        self.assertEqual(by_name["BrooksDevTh"].baudrate, 9600)
        self.assertEqual(by_name["AIOTh"].port_status, "available")
        self.assertEqual(by_name["AIOTh"].device_address, 1)
        self.assertEqual(by_name["AIOTh"].module_type, "4118")
        self.assertEqual(by_name["MSDevTh"].port_status, "missing")
        self.assertFalse(by_name["MSDevTh"].enabled)

    def test_report_marks_inventory_as_no_io(self):
        with tempfile.TemporaryDirectory() as directory:
            builds = self.make_builds(Path(directory))
            report = build_report(builds, observed_ports=())
        self.assertEqual(report["safety"]["ports_opened_by_inventory"], 0)
        self.assertEqual(report["safety"]["output_commands_sent_by_inventory"], 0)

    def test_successful_empty_enumeration_marks_configured_ports_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            builds = self.make_builds(Path(directory))
            report = build_report(builds, observed_ports=())
        self.assertTrue(
            all(device["port_status"] == "missing" for device in report["configured_devices"])
        )
        self.assertEqual(len(report["warnings"]), 3)

    def test_brooks_probe_sends_only_identify_and_read_commands(self):
        device = ConfiguredDevice(
            thread_name="BrooksDevTh",
            role="Brooks flow controller",
            device_name="Brooks1",
            configured_port="COM13",
            baudrate=9600,
            enabled=True,
            input_channels=("0", "1", "2", "3"),
            output_channels=("0", "1", "2", "3"),
            port_status="available",
        )
        transport = FakeBrooksTransport()
        result = probe_brooks_read_only(device, transaction_factory=lambda _settings: transport)
        self.assertEqual(result["measured_flows"], [10.0, 11.0, 12.0, 13.0])
        self.assertEqual(result["setpoint_writes"], 0)
        self.assertEqual(transport.requests[0], b"AZI\r")
        self.assertTrue(all(b"P01" not in request for request in transport.requests))
        self.assertTrue(transport.closed)

    def test_adam4118_probe_sends_one_read_all_command(self):
        device = ConfiguredDevice(
            thread_name="AIOTh",
            role="ADAM 4118 temperature input",
            device_name="ADAM4118",
            configured_port="COM14",
            baudrate=9600,
            enabled=True,
            input_channels=("Temperature", "Temperature2"),
            output_channels=(),
            port_status="available",
            device_address=1,
            module_type="4118",
            input_range=8,
            data_format="EngineeringUnits",
        )
        transport = FakeAdamTransport()
        result = probe_adam4118_read_only(
            device,
            transaction_factory=lambda settings: transport,
        )
        self.assertEqual(transport.requests, [b"#01\r"])
        self.assertEqual(result["named_values"], {"Temperature": 24.2, "Temperature2": 25.1})
        self.assertEqual(result["output_commands"], 0)
        self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()
