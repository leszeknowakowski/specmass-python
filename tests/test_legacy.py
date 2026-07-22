import tempfile
import unittest
from pathlib import Path

from specmass.legacy import load_legacy_json, load_program, loads_legacy_json, save_legacy_json


class LegacyReaderTests(unittest.TestCase):
    def test_accepts_trailing_commas(self):
        self.assertEqual(loads_legacy_json('{"channels": [1, 2,],}'), {"channels": [1, 2]})

    def test_trailing_comma_repair_does_not_change_string_content(self):
        value = loads_legacy_json('{"text": "literal ,} and ,]", "items": [1,],}')
        self.assertEqual(value, {"text": "literal ,} and ,]", "items": [1]})

    def test_program_uses_natural_stage_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ScanSettings.msdef").write_text("{}", encoding="utf-8")
            template = (
                '{{"Name":"{name}","StartTemp":20,"EndTemp":20,'
                '"TempMode":0,"TempA":0,"Duration":1}}'
            )
            (root / "Stage10.msdef").write_text(template.format(name="ten"), encoding="utf-8")
            (root / "Stage2.msdef").write_text(template.format(name="two"), encoding="utf-8")
            program = load_program(root)
        self.assertEqual([item.name for item in program.stages], ["two", "ten"])

    def test_atomic_writer_round_trips_legacy_scan_settings(self):
        value = {
            "Filament": "F1",
            "ScansParameters": [{"Start value": 18.0, "Use Autozero": True}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ScanSettings.msdef"
            save_legacy_json(path, value)
            loaded = load_legacy_json(path)
            temporary_files = list(path.parent.glob(".ScanSettings.msdef.*.tmp"))
        self.assertEqual(loaded, value)
        self.assertEqual(temporary_files, [])


if __name__ == "__main__":
    unittest.main()
