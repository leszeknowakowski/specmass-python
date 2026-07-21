import unittest

from specmass.pid import PIDController, PIDGains


class PIDTests(unittest.TestCase):
    def test_output_is_bounded(self):
        pid = PIDController(PIDGains(kc=20.0, ti_seconds=2.0, td_seconds=0.0))
        self.assertEqual(pid.update(100.0, 0.0, 0.5), 100.0)
        self.assertEqual(pid.update(0.0, 100.0, 0.5), 0.0)

    def test_integral_accumulates_without_saturation(self):
        pid = PIDController(PIDGains(kc=1.0, ti_seconds=10.0, td_seconds=0.0))
        first = pid.update(1.0, 0.0, 1.0)
        second = pid.update(1.0, 0.0, 1.0)
        self.assertGreater(second, first)

    def test_invalid_timestep_is_rejected(self):
        pid = PIDController(PIDGains(kc=1.0, ti_seconds=1.0, td_seconds=0.0))
        with self.assertRaises(ValueError):
            pid.update(1.0, 0.0, 0.0)


if __name__ == "__main__":
    unittest.main()

