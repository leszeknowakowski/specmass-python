import importlib.util
import unittest
from pathlib import Path

from specmass.validation import validate_legacy_run


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT.parent / "labview-org" / "data"
PROGRAM = DATA_ROOT / "Cu2O_15M_15_07_26_deN2O"
TDMS_FILES = tuple(DATA_ROOT.glob("Cu2O_15M_15_07_26_deN2O_*.tdms")) if DATA_ROOT.exists() else ()


@unittest.skipUnless(importlib.util.find_spec("nptdms") and PROGRAM.exists() and TDMS_FILES, "legacy sample unavailable")
class RealLegacyRunTests(unittest.TestCase):
    def test_program_timing_and_primary_temperature_match(self):
        report = validate_legacy_run(PROGRAM, TDMS_FILES[0])
        self.assertAlmostEqual(report.program_duration_seconds, 4556.0, places=6)
        self.assertAlmostEqual(report.recorded_duration_seconds, 4571.5, places=6)
        self.assertAlmostEqual(report.logging_lead_seconds, 15.5, places=6)
        self.assertAlmostEqual(report.expected_first_ramp_start_seconds, 515.5, places=6)
        self.assertAlmostEqual(report.measured_ramp_celsius_per_minute, 10.0, delta=0.05)
        self.assertAlmostEqual(report.estimated_thermal_lag_seconds, 7.0, delta=2.0)

    def test_mass_waveform_timebase_is_detected_as_invalid(self):
        report = validate_legacy_run(PROGRAM, TDMS_FILES[0])
        self.assertEqual(len(report.mass_timebases), 6)
        self.assertFalse(report.mass_timebase_is_trustworthy)
        self.assertTrue(all(not item.spans_recorded_run for item in report.mass_timebases))


if __name__ == "__main__":
    unittest.main()
