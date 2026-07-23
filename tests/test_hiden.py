import tempfile
import unittest
from pathlib import Path
import struct

from specmass.hiden import (
    HidenConnectionConfig,
    HidenScanPlan,
    build_hiden_offline_report,
    find_hiden_environment_config,
    hiden_scan_label,
    load_hiden_environment_config,
    new_hiden_mass_scan,
)


DEPLOYED_CONNECTION = {
    "Resource": "COM3",
    "ConnType": 0,
    "Set to Standby (T)": True,
    "Force m/s interrogation (F)": False,
    "Enable comms logging (F)": False,
    "Comms log file path": None,
    "Baud Rate": 921600,
    "Parity": 0,
    "Data Bits": 8,
    "Stop Bits": 10,
    "Timeout": 1000,
    "TCPPort": 0,
    "MassesNames": [
        {"Mass": 32, "Name": "O2"},
        {"Mass": 28, "Name": "N2"},
        {"Mass": 18, "Name": "H2O"},
        {"Mass": 4, "Name": "He"},
        {"Mass": 40, "Name": "Ar"},
        {"Mass": 44, "Name": "CO2"},
    ],
    "EnableMS": True,
}


def scan(mass: float, *, autozero: bool = False) -> dict:
    return {
        "Device to scan": "mass",
        "Start value": mass,
        "Stop value": mass,
        "Increment": 1,
        "Relative  sensitivity": 1,
        "Relative gain": 1,
        "Scan mode": 1,
        "Input device": "SEM",
        "Dwell (%)": 100,
        "Settle (%)": 100,
        "Autorange High": -7,
        "Autorange Low": -9,
        "Start range": -9,
        "Use Autozero": autozero,
        "Options": "",
        "Changes to environment parameters": "",
        "Acquisition cycles": 0,
        "Min cycle time (sec)": 0,
    }


def _dtlg_string(value: str) -> bytes:
    encoded = value.encode("ascii")
    return struct.pack(">I", len(encoded)) + encoded


def _dtlg_string_array(values: tuple[str, ...]) -> bytes:
    return struct.pack(">I", len(values)) + b"".join(
        _dtlg_string(value) for value in values
    )


def _dtlg_numeric_array(code: str, values: tuple[int | float, ...]) -> bytes:
    return struct.pack(">I", len(values)) + struct.pack(
        f">{len(values)}{code}", *values
    )


def hiden_environment_dtlg() -> bytes:
    names = ("F1", "F2")
    modes = ("Shutdown", "RGA ")
    record = b"".join(
        (
            _dtlg_string_array(names),
            _dtlg_numeric_array("i", (52, 53)),
            _dtlg_string_array(("bool", "bool")),
            _dtlg_numeric_array("d", (0.0, 0.0)),
            _dtlg_numeric_array("d", (1.0, 1.0)),
            _dtlg_numeric_array("d", (1.0, 1.0)),
            struct.pack(">II4d", 2, 2, 0.0, 0.0, 0.0, 1.0),
            _dtlg_string_array(modes),
            _dtlg_string_array(("%d", "%d")),
            struct.pack(">I2B", 2, 1, 1),
            b"\x01",
            _dtlg_string('"HAL RC RGA 201 #16359"'),
        )
    )
    header = bytearray(24)
    header[:4] = b"DTLG"
    struct.pack_into(">I", header, 12, 20)
    struct.pack_into(">I", header, 20, 24)
    return bytes(header) + record


class HidenOfflineTests(unittest.TestCase):
    def test_new_mass_scan_matches_legacy_shape_and_has_operator_label(self):
        definition = new_hiden_mass_scan(18, use_autozero=True)
        plan = HidenScanPlan.from_mapping(
            {"Filament": "F1", "ScansParameters": [definition]}
        )
        self.assertEqual(plan.scans[0].start_value, 18.0)
        self.assertEqual(plan.scans[0].stop_value, 18.0)
        self.assertEqual(
            hiden_scan_label(definition),
            "H2O  —  m/z 18  ·  SEM · autozero",
        )

    def test_linear_scan_factory_preserves_all_editor_fields(self):
        definition = new_hiden_mass_scan(
            0.4,
            stop_mass=200.0,
            increment=0.01,
            input_device="Faraday",
            autorange_high=-5,
            autorange_low=-12,
            start_range=-7,
            dwell_percent=80,
            settle_percent=20,
            relative_sensitivity=1.25,
            relative_gain=0.75,
            options="raw options",
            environment_changes="raw environment",
            acquisition_cycles=3,
            minimum_cycle_time_seconds=0.5,
        )
        parsed = HidenScanPlan.from_mapping(
            {"Filament": "F1", "ScansParameters": [definition]}
        ).scans[0]
        self.assertFalse(parsed.is_single_point)
        self.assertEqual((parsed.start_value, parsed.stop_value), (0.4, 200.0))
        self.assertEqual(parsed.increment, 0.01)
        self.assertEqual(parsed.input_device, "Faraday")
        self.assertEqual(parsed.acquisition_cycles, 3)
        self.assertEqual(parsed.options, "raw options")
        self.assertEqual(parsed.environment_changes, "raw environment")

    def test_deployed_connection_normalizes_ni_visa_enums(self):
        config = HidenConnectionConfig.from_mapping(DEPLOYED_CONNECTION)
        self.assertEqual(config.resource, "COM3")
        self.assertEqual(config.connection_name, "serial")
        self.assertEqual(config.parity_name, "none")
        self.assertEqual(config.stop_bits, 1.0)
        self.assertEqual(config.baud_rate, 921600)
        self.assertTrue(config.enabled)

    def test_scan_plan_parses_the_deployed_single_point_sem_shape(self):
        plan = HidenScanPlan.from_mapping(
            {"Filament": "F1", "ScansParameters": [scan(18, autozero=True), scan(28)]}
        )
        self.assertEqual(plan.filament, "F1")
        self.assertTrue(all(item.is_single_point for item in plan.scans))
        self.assertEqual(plan.scans[0].input_device, "SEM")
        self.assertTrue(plan.scans[0].use_autozero)

    def test_scan_plan_rejects_per_row_cycle_settings_the_device_would_ignore(self):
        first = scan(18)
        second = scan(28)
        second["Acquisition cycles"] = 3
        with self.assertRaisesRegex(ValueError, "same acquisition cycle count"):
            HidenScanPlan.from_mapping(
                {"Filament": "F1", "ScansParameters": [first, second]}
            )

    def test_scan_range_must_contain_start_range(self):
        bad = scan(18)
        bad["Start range"] = -10
        with self.assertRaisesRegex(ValueError, "inside the autorange limits"):
            HidenScanPlan.from_mapping({"Filament": "F1", "ScansParameters": [bad]})

    def test_scan_text_rejects_command_injection(self):
        bad = scan(18)
        bad["Options"] = "normal\rdata stop"
        with self.assertRaisesRegex(ValueError, "control character"):
            HidenScanPlan.from_mapping({"Filament": "F1", "ScansParameters": [bad]})

    def test_linear_scan_builds_the_exact_stimulus_axis(self):
        definition = new_hiden_mass_scan(0.4, stop_mass=0.6, increment=0.1)
        parsed = HidenScanPlan.from_mapping(
            {"Filament": "F1", "ScansParameters": [definition]}
        ).scans[0]
        self.assertEqual(len(parsed.stimuli()), 3)
        self.assertAlmostEqual(parsed.stimuli()[-1], 0.6)

    def test_environment_dtlg_parser_and_safe_file_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment_path = root / "16359.cfg"
            environment_path.write_bytes(hiden_environment_dtlg())
            environment = load_hiden_environment_config(environment_path)
            found = find_hiden_environment_config(root)

        self.assertEqual(found.name, "16359.cfg")
        self.assertEqual(environment.normalized_mass_spec_name, "HAL RC RGA 201 #16359")
        self.assertEqual(environment.modes, ("Shutdown", "RGA "))
        self.assertTrue(environment.autozero_supported)
        self.assertEqual(environment.devices[0].index, 52)
        self.assertEqual(environment.devices[1].values_by_mode, (0.0, 1.0))

    def test_report_is_explicitly_offline_and_resolves_known_mass_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builds = root / "Builds"
            data = builds / "data"
            program = root / "Program"
            data.mkdir(parents=True)
            program.mkdir()
            import json

            (data / "MSDevTh").write_text(json.dumps(DEPLOYED_CONNECTION), encoding="utf-8")
            (program / "ScanSettings.msdef").write_text(
                json.dumps(
                    {
                        "Filament": "F1",
                        "ScansParameters": [scan(18), scan(30), scan(44)],
                    }
                ),
                encoding="utf-8",
            )
            report = build_hiden_offline_report(builds, program_directory=program)

        self.assertEqual(report["safety"]["ports_opened"], 0)
        self.assertEqual(report["safety"]["device_queries_sent"], 0)
        self.assertFalse(report["safety"]["passive_mass_acquisition_available"])
        self.assertEqual(report["scan_plan"]["resolved_labels"], ["H2O", "Mass[30]", "CO2"])


if __name__ == "__main__":
    unittest.main()
