import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt5 import QtWidgets

    from specmass.hiden import new_hiden_mass_scan
    from specmass.legacy import load_legacy_json
    from specmass.ui import MassScanDialog, SpecMassWindow
except ImportError:
    QtWidgets = None


@unittest.skipIf(QtWidgets is None, "PyQt5 is not installed")
class ConfigurationGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.program_path = Path(self.temporary.name)
        (self.program_path / "Stage1.msdef").write_text(
            json.dumps(
                {
                    "Name": "Stage1",
                    "StartTemp": 24,
                    "EndTemp": 24,
                    "TempMode": "Isothermal",
                    "TempA": 0,
                    "StartFlow": [0, 0, 0, 0],
                    "EndFlow": [0, 0, 0, 0],
                    "FlowA": [0, 0, 0, 0],
                    "ValveStates": [0, 1],
                    "Duration": 60,
                }
            ),
            encoding="utf-8",
        )
        (self.program_path / "ScanSettings.msdef").write_text(
            json.dumps(
                {
                    "Filament": "F1",
                    "ScansParameters": [new_hiden_mass_scan(18, use_autozero=True)],
                }
            ),
            encoding="utf-8",
        )
        self.window = SpecMassWindow(initial_program=self.program_path)

    def tearDown(self):
        self.window._scan_settings_dirty = False
        self.window._stage_settings_dirty = False
        self.window.close()
        self.temporary.cleanup()

    def test_three_screen_navigation_populates_stage_and_scan_views(self):
        self.assertEqual(self.window.page_stack.count(), 3)
        self.window._show_program_config()
        self.assertIs(
            self.window.page_stack.currentWidget(), self.window.program_config_page
        )
        self.assertEqual(self.window.stage_list.count(), 1)
        self.assertEqual(self.window.stage_flow_list.count(), 4)
        self.window._show_hiden_config()
        self.assertIs(
            self.window.page_stack.currentWidget(), self.window.hiden_config_page
        )
        self.assertEqual(self.window.hiden_scan_list.count(), 1)
        self.assertIn("H2O", self.window.hiden_scan_list.item(0).text())
        self.assertFalse(self.window.hiden_upload_button.isEnabled())

    def test_offline_editor_saves_added_mass_without_device_path(self):
        self.window._show_hiden_config()
        self.window._working_scans().append(new_hiden_mass_scan(44))
        self.window._set_hiden_dirty()
        self.window._refresh_hiden_scan_list(select_row=1)
        self.assertTrue(self.window._save_hiden_settings(announce=False))
        saved = load_legacy_json(self.program_path / "ScanSettings.msdef")
        self.assertEqual(
            [scan["Start value"] for scan in saved["ScansParameters"]],
            [18.0, 44.0],
        )
        self.assertFalse(self.window._scan_settings_dirty)

    def test_scan_dialog_matches_labview_tabs_and_linear_detector_fields(self):
        dialog = MassScanDialog(initial_mass=18)
        self.addCleanup(dialog.close)
        self.assertEqual(
            [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())],
            ["Environment", "Scan", "Detector", "Advanced"],
        )
        self.assertFalse(dialog.environment_change_button.isEnabled())
        self.assertEqual(
            [dialog.input_device_box.itemText(index) for index in range(dialog.input_device_box.count())],
            list(MassScanDialog.INPUT_DEVICES),
        )

        trend = dialog.scan_definition()
        self.assertEqual((trend["Start value"], trend["Stop value"]), (18.0, 18.0))
        self.assertEqual(trend["Input device"], "SEM")
        self.assertEqual(trend["Autorange Low"], -13)

        dialog.scan_type_box.setCurrentText("linear")
        dialog.scan_start_spin.setValue(0.4)
        dialog.scan_stop_spin.setValue(200.0)
        dialog.scan_step_spin.setValue(0.01)
        dialog.continuous_scan_check.setChecked(False)
        dialog.acquisition_cycles_spin.setValue(4)
        dialog.input_device_box.setCurrentText("auxiliary2")
        dialog.relative_sensitivity_spin.setValue(1.5)
        dialog.relative_gain_spin.setValue(2.0)
        dialog.options_edit.setText("option")
        dialog.environment_changes_edit.setPlainText("environment")
        linear = dialog.scan_definition()
        self.assertEqual(
            (linear["Start value"], linear["Stop value"], linear["Increment"]),
            (0.4, 200.0, 0.01),
        )
        self.assertEqual(linear["Input device"], "auxiliary2")
        self.assertEqual(linear["Acquisition cycles"], 4)
        self.assertEqual(linear["Relative  sensitivity"], 1.5)
        self.assertEqual(linear["Relative gain"], 2.0)
        self.assertEqual(linear["Options"], "option")
        self.assertEqual(linear["Changes to environment parameters"], "environment")

    def test_stage_add_copy_remove_and_save_use_recovery_backup(self):
        self.window._show_program_config()
        self.window.stage_list.setCurrentRow(0)
        self.window.stage_duration.setValue(77.0)
        self.window.stage_pulse_length.setValue(2.5)
        self.assertEqual(
            self.window._stage_working[0]["ValvePulseLength"], [2.5, 0.0]
        )

        self.window._add_stage()
        self.assertEqual(self.window.stage_list.count(), 2)
        self.window.stage_list.setCurrentRow(0)
        self.window._copy_stage()
        self.assertEqual(self.window.stage_list.count(), 3)
        self.assertEqual(self.window._stage_working[2]["Duration"], 77.0)
        self.assertTrue(self.window._save_stage_settings(announce=False))
        self.assertTrue((self.program_path / "Stage2.msdef").is_file())
        self.assertTrue((self.program_path / "Stage3.msdef").is_file())

        self.window.stage_list.setCurrentRow(1)
        with patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.Yes,
        ):
            self.window._remove_stage()
        self.assertEqual(self.window.stage_list.count(), 2)
        self.assertTrue(self.window._save_stage_settings(announce=False))
        self.assertFalse((self.program_path / "Stage2.msdef").exists())
        self.assertTrue(
            any(
                (backup / "removed" / "Stage2.msdef").is_file()
                for backup in (self.program_path / ".specmass-backup").iterdir()
            )
        )

    def test_existing_scan_populates_editor_and_round_trips(self):
        original = new_hiden_mass_scan(
            28.0,
            stop_mass=44.0,
            increment=0.25,
            input_device="auxiliary2",
            use_autozero=True,
            autorange_high=-5,
            autorange_low=-13,
            start_range=-7,
            dwell_percent=80,
            settle_percent=60,
            relative_sensitivity=1.5,
            relative_gain=2.0,
            options="option",
            environment_changes="environment",
            acquisition_cycles=3,
            minimum_cycle_time_seconds=0.5,
        )
        dialog = MassScanDialog(initial_scan=original, scan_number=4)
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.editing_scan_spin.value(), 4)
        self.assertEqual(dialog.scan_type_box.currentText(), "linear")
        self.assertEqual(dialog.input_device_box.currentText(), "auxiliary2")
        self.assertEqual(dialog.scan_definition(), original)

    def test_edit_scan_replaces_known_values_and_preserves_unknown_legacy_fields(self):
        self.window._show_hiden_config()
        original = self.window._working_scans()[0]
        original["Legacy extension"] = "keep"
        changed = new_hiden_mass_scan(44.0, input_device="Faraday")
        fake_dialog = Mock()
        fake_dialog.exec_.return_value = QtWidgets.QDialog.Accepted
        fake_dialog.scan_definition.return_value = changed

        with patch("specmass.ui.MassScanDialog", return_value=fake_dialog):
            self.window._edit_hiden_scan()

        edited = self.window._working_scans()[0]
        self.assertEqual(edited["Start value"], 44.0)
        self.assertEqual(edited["Input device"], "Faraday")
        self.assertEqual(edited["Legacy extension"], "keep")
        self.assertTrue(self.window._scan_settings_dirty)

    def test_simulation_output_path_is_inside_program_and_never_overwrites(self):
        existing = self.program_path / "specmass_sim_20260722_180000.csv"
        existing.write_text("keep", encoding="utf-8")
        with patch("specmass.ui.time.strftime", return_value="20260722_180000"):
            output = self.window._simulation_output_path("csv")
        self.assertEqual(
            output,
            self.program_path / "specmass_sim_20260722_180000_2.csv",
        )
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")

    def test_cooling_wait_can_be_disabled_before_start(self):
        self.assertTrue(self.window.wait_for_cooling_check.isChecked())
        self.assertEqual(self.window._configured_cooling_temperature(), 50.0)

        self.window.wait_for_cooling_check.setChecked(False)
        self.assertFalse(self.window.cooling_spin.isEnabled())
        self.assertIsNone(self.window._configured_cooling_temperature())

        self.window.wait_for_cooling_check.setChecked(True)
        self.window.cooling_spin.setValue(75.0)
        self.assertTrue(self.window.cooling_spin.isEnabled())
        self.assertEqual(self.window._configured_cooling_temperature(), 75.0)


if __name__ == "__main__":
    unittest.main()
