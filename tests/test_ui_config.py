import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt5 import QtWidgets

    from specmass.hiden import new_hiden_mass_scan
    from specmass.legacy import load_legacy_json
    from specmass.ui import SpecMassWindow
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


if __name__ == "__main__":
    unittest.main()
