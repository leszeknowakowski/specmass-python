import importlib.util
import tempfile
import unittest
from pathlib import Path

from specmass.devices.base import ControlCommand, SensorSnapshot
from specmass.state_machine import ControllerStatus, MSMState
from specmass.tdms_telemetry import TdmsTelemetryWriter, mass_stimuli_from_scan_settings


@unittest.skipUnless(importlib.util.find_spec("nptdms"), "npTDMS unavailable")
class TdmsTelemetryTests(unittest.TestCase):
    def test_scan_settings_are_mapped_to_legacy_mass_names(self):
        settings = {
            "ScansParameters": [
                {"Device to scan": "mass", "Start value": 18, "Stop value": 18},
                {"Device to scan": "mass", "Start value": 30, "Stop value": 30},
                {"Device to scan": "voltage", "Start value": 1, "Stop value": 1},
            ]
        }
        self.assertEqual(
            mass_stimuli_from_scan_settings(settings),
            {"H2O": 18.0, "Mass[30]": 30.0},
        )

    def test_round_trip_has_explicit_time_and_legacy_groups(self):
        from nptdms import TdmsFile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.tdms"
            writer = TdmsTelemetryWriter(
                path,
                flow_channels=1,
                mass_stimuli={"H2O": 18.0},
                nominal_increment_seconds=0.1,
                temperature_names=("Temperature", "Temperature2"),
                root_properties={
                    "SpecMass_Mode": "ReadOnlyHardwareMonitor",
                    "SpecMass_OutputCommandsEnabled": 0,
                },
            )
            status = ControllerStatus(MSMState.RUNNING_STAGE, 0, 0.0, 0.0, False, False)
            writer.write(
                SensorSnapshot(0.0, 24.0, (1.0,), {"H2O": 1e-8}, (24.0, 25.0)),
                ControlCommand(24.0, (1.0,), (), 10.0, (), (True,)),
                status,
            )
            writer.write(
                SensorSnapshot(0.37, 24.2, (1.1,), {"H2O": 2e-8}, (24.2, 25.3)),
                ControlCommand(24.5, (1.1,), (), 12.0, (False,)),
                status,
            )
            writer.close()

            tdms = TdmsFile.read(path)
            self.assertEqual(tdms["Time"]["ElapsedSeconds"][:].tolist(), [0.0, 0.37])
            self.assertEqual(tdms["Temperature"]["Temperature"][:].tolist(), [24.0, 24.2])
            self.assertEqual(tdms["Temperature"]["Temperature2"][:].tolist(), [25.0, 25.3])
            self.assertEqual(tdms["Flows"]["Ch0"][:].tolist(), [1.0, 1.1])
            self.assertEqual(tdms["FlowSetpoints"]["Ch0"][:].tolist(), [1.0, 1.1])
            self.assertEqual(
                tdms["FlowSetpoints"]["Ch0_WriteEnabled"][:].tolist(),
                [1, 0],
            )
            self.assertEqual(
                tdms["FlowSetpoints"]["Ch0_WritePerformed"][:].tolist(),
                [1, 0],
            )
            self.assertEqual(tdms["Masses_Scan1"]["H2O"].properties["Stimulus"], 18.0)
            self.assertEqual(tdms["Control"]["MSMState"][:].tolist(), [7, 7])
            self.assertEqual(tdms.properties["SpecMass_Mode"], "ReadOnlyHardwareMonitor")
            self.assertEqual(tdms.properties["SpecMass_OutputCommandsEnabled"], 0)


if __name__ == "__main__":
    unittest.main()
