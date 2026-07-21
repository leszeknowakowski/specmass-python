import unittest

from specmass.models import ProcessStage, TemperatureMode, ValveMode


def stage(**overrides):
    values = dict(
        name="test",
        start_temperature=20.0,
        end_temperature=80.0,
        temperature_mode=TemperatureMode.POLYTHERMAL,
        temperature_rate_per_minute=30.0,
        start_flows=(1.0,),
        end_flows=(3.0,),
        flow_rates_per_minute=(1.0,),
        valve_initial_states=(False,),
        valve_pulse_lengths=(2.0,),
        valve_pulse_gaps=(3.0,),
        valve_modes=(ValveMode.IMPULSE,),
    )
    values.update(overrides)
    return ProcessStage(**values)


class ProcessStageTests(unittest.TestCase):
    def test_polythermal_duration_and_temperature_ramp(self):
        item = stage()
        self.assertEqual(item.effective_duration_seconds(), 120.0)
        self.assertEqual(item.temperature_setpoint(60.0), 50.0)
        self.assertEqual(item.temperature_setpoint(999.0), 80.0)

    def test_polythermal_flow_is_constant(self):
        self.assertEqual(stage().flow_setpoints(90.0), (1.0,))

    def test_isothermal_flow_ramps_and_clamps(self):
        item = stage(
            temperature_mode=TemperatureMode.ISOTHERMAL,
            duration_seconds=300.0,
            start_flows=(3.0,),
            end_flows=(1.0,),
            flow_rates_per_minute=(0.5,),
        )
        self.assertEqual(item.flow_setpoints(120.0), (2.0,))
        self.assertEqual(item.flow_setpoints(999.0), (1.0,))

    def test_impulse_valve_uses_opposite_state_during_pulse(self):
        item = stage()
        self.assertEqual(item.valve_states_at(0.0), (True,))
        self.assertEqual(item.valve_states_at(2.5), (False,))
        self.assertEqual(item.valve_states_at(5.0), (True,))

    def test_legacy_enum_spellings_are_supported(self):
        item = ProcessStage.from_mapping(
            {
                "Name": "legacy",
                "TempMode": "Polithermal",
                "StartTemp": 10,
                "EndTemp": 11,
                "TempA": 1,
                "ValveStates": [0],
                "ValveMode": ["Impluse"],
                "ValvePulseLength": [1],
                "ValvePulseGap": [1],
            }
        )
        self.assertIs(item.temperature_mode, TemperatureMode.POLYTHERMAL)
        self.assertEqual(item.valve_modes, (ValveMode.IMPULSE,))


if __name__ == "__main__":
    unittest.main()

