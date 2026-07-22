import csv
import tempfile
import unittest
from pathlib import Path

from specmass.devices.base import ControlCommand, SensorSnapshot
from specmass.state_machine import ControllerStatus, MSMState
from specmass.telemetry import CsvTelemetryWriter


class CsvTelemetryTests(unittest.TestCase):
    def test_monitor_channels_and_disabled_writes_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.csv"
            writer = CsvTelemetryWriter(
                path,
                flow_channels=4,
                temperature_names=("Temperature", "Temperature2"),
            )
            writer.write(
                SensorSnapshot(
                    1.25,
                    24.2,
                    (-0.48, -0.26, -0.02, 0.0),
                    {},
                    (24.2, 25.1),
                ),
                ControlCommand.safe(flow_count=4, flow_write_enabled=(False,) * 4),
                ControllerStatus(MSMState.IDLE, None, 1.25, 0.0, False, False),
            )
            writer.close()

            with path.open(encoding="utf-8", newline="") as source:
                row = next(csv.DictReader(source))
            self.assertEqual(row["Temperature"], "24.2")
            self.assertEqual(row["Temperature2"], "25.1")
            self.assertEqual(row["flow_0"], "-0.48")
            self.assertEqual(row["flow_3"], "0.0")
            self.assertEqual(row["flow_write_enabled_0"], "0")
            self.assertEqual(row["flow_write_performed_0"], "0")
            self.assertGreater(float(row["utc_seconds"]), 1_700_000_000)


if __name__ == "__main__":
    unittest.main()
