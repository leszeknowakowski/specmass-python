import unittest

from specmass.devices.simulated import SimulatedBackend
from specmass.devices.base import ControlCommand
from specmass.models import ProcessProgram, ProcessStage, TemperatureMode
from specmass.pid import PIDController, PIDGains
from specmass.runtime import SpecMassRuntime
from specmass.safety import SafetyPolicy, SafetyTrip
from specmass.state_machine import MSMState, ProcessController


def make_program(temperature=20.0):
    return ProcessProgram(
        (
            ProcessStage(
                name="stage",
                start_temperature=temperature,
                end_temperature=temperature,
                temperature_mode=TemperatureMode.ISOTHERMAL,
                temperature_rate_per_minute=0.0,
                duration_seconds=1.0,
            ),
        )
    )


class SafetyRuntimeTests(unittest.TestCase):
    def runtime(self, *, maximum_temperature=100.0, stage_temperature=20.0):
        backend = SimulatedBackend(flow_channels=0)
        controller = ProcessController()
        controller.load(make_program(stage_temperature))
        controller.start(0.0)
        runtime = SpecMassRuntime(
            backend=backend,
            controller=controller,
            pid=PIDController(PIDGains(kc=1.0, ti_seconds=1.0, td_seconds=0.0)),
            safety=SafetyPolicy(maximum_temperature=maximum_temperature),
        )
        return runtime, backend, controller

    def test_overtemperature_trips_before_output(self):
        runtime, backend, controller = self.runtime(maximum_temperature=50.0)
        backend.temperature = 60.0
        with self.assertRaises(SafetyTrip):
            runtime.step(0.0, 0.1)
        self.assertIs(controller.state, MSMState.ERROR)
        self.assertEqual(backend.last_command.heater_percent, 0.0)

    def test_out_of_range_setpoint_trips_before_output(self):
        runtime, backend, controller = self.runtime(
            maximum_temperature=30.0,
            stage_temperature=40.0,
        )
        runtime.step(0.0, 0.1)
        with self.assertRaises(SafetyTrip):
            runtime.step(0.1, 0.1)
        self.assertIs(controller.state, MSMState.ERROR)
        self.assertEqual(backend.last_command.heater_percent, 0.0)

    def test_default_simulated_furnace_has_range_for_700_celsius_recipe(self):
        backend = SimulatedBackend(flow_channels=0)
        command = ControlCommand(temperature_setpoint=700.0, heater_percent=100.0)
        for _ in range(500):
            backend.apply(command, 1.0)
        self.assertGreater(backend.temperature, 700.0)

    def test_external_simulated_flow_ignores_application_writes(self):
        backend = SimulatedBackend(flow_channels=1)
        backend.set_external_flow(0, 30.0)
        backend.apply(
            ControlCommand(flow_setpoints=(0.0,), flow_write_enabled=(False,)),
            1.0,
        )
        self.assertEqual(backend.flows, [30.0])
        backend.safe_shutdown()
        self.assertEqual(backend.flows, [30.0])

    def test_front_panel_flow_persists_when_cached_app_request_is_unchanged(self):
        backend = SimulatedBackend(flow_channels=1)
        backend.set_front_panel_flow(0, 30.0)
        backend.apply(ControlCommand(flow_setpoints=(0.0,)), 1.0)
        self.assertEqual(backend.flows, [30.0])
        self.assertEqual(backend.last_flow_writes, (False,))

        backend.apply(ControlCommand(flow_setpoints=(10.0,)), 1.0)
        self.assertEqual(backend.flows, [10.0])
        self.assertEqual(backend.last_flow_writes, (True,))

        backend.apply(ControlCommand(flow_setpoints=(10.0,)), 1.0)
        self.assertEqual(backend.last_flow_writes, (False,))


if __name__ == "__main__":
    unittest.main()
