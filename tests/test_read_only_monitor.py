import unittest

from specmass.devices.base import ControlCommand, SensorSnapshot
from specmass.devices.read_only_monitor import ReadOnlyHardwareMonitorBackend
from specmass.devices.serial_transport import HardwareDisabledError


class FakeTemperatureMonitor:
    channel_names = ("Temperature", "Temperature2")

    def __init__(self):
        self.closed = False

    def read(self, timestamp):
        return SensorSnapshot(
            timestamp=timestamp,
            temperature=24.2,
            temperatures=(24.2, 25.1),
            masses={},
        )

    def safe_shutdown(self):
        self.closed = True


class FakeFlowClient:
    channel_count = 4

    def __init__(self):
        self.closed = False

    def read_all(self):
        return (-0.48, -0.26, -0.02, 0.0)

    def close(self):
        self.closed = True


class ReadOnlyHardwareMonitorTests(unittest.TestCase):
    def test_combines_live_temperatures_and_flows(self):
        backend = ReadOnlyHardwareMonitorBackend(FakeTemperatureMonitor(), FakeFlowClient())
        snapshot = backend.read(12.5)
        self.assertEqual(snapshot.timestamp, 12.5)
        self.assertEqual(snapshot.temperatures, (24.2, 25.1))
        self.assertEqual(snapshot.flows, (-0.48, -0.26, -0.02, 0.0))
        self.assertEqual(backend.monitored_devices, ("ADAM4118", "Brooks1"))

    def test_rejects_every_control_command(self):
        backend = ReadOnlyHardwareMonitorBackend(FakeTemperatureMonitor(), FakeFlowClient())
        with self.assertRaises(HardwareDisabledError):
            backend.apply(ControlCommand.safe(flow_count=4), 1.0)

    def test_closes_both_readers(self):
        temperatures = FakeTemperatureMonitor()
        flows = FakeFlowClient()
        backend = ReadOnlyHardwareMonitorBackend(temperatures, flows)
        backend.safe_shutdown()
        self.assertTrue(temperatures.closed)
        self.assertTrue(flows.closed)


if __name__ == "__main__":
    unittest.main()
