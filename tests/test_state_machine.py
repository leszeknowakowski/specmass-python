import unittest

from specmass.devices.base import SensorSnapshot
from specmass.models import ProcessProgram, ProcessStage, TemperatureMode
from specmass.state_machine import MSMState, ProcessController


def make_stage(name, *, auto_start=True, duration=1.0):
    return ProcessStage(
        name=name,
        start_temperature=20.0,
        end_temperature=20.0,
        temperature_mode=TemperatureMode.ISOTHERMAL,
        temperature_rate_per_minute=0.0,
        start_flows=(1.0,),
        end_flows=(1.0,),
        flow_rates_per_minute=(0.0,),
        auto_start=auto_start,
        duration_seconds=duration,
    )


def snapshot(timestamp, temperature=20.0):
    return SensorSnapshot(timestamp=timestamp, temperature=temperature, flows=(0.0,), masses={})


class StateMachineTests(unittest.TestCase):
    def test_single_stage_reaches_ready_after_safe_stop(self):
        controller = ProcessController()
        controller.load(ProcessProgram((make_stage("one"),)))
        controller.start(0.0)
        for timestamp in (0.0, 0.1, 0.2, 1.3, 1.4, 1.5, 1.6, 1.7):
            controller.tick(snapshot(timestamp))
        self.assertIs(controller.state, MSMState.READY_FOR_START)
        self.assertTrue(controller.status(1.7).completed)

    def test_completed_program_can_start_again_without_reload(self):
        controller = ProcessController()
        controller.load(ProcessProgram((make_stage("one"),)))

        controller.start(0.0)
        for timestamp in (0.0, 0.1, 0.2, 1.3, 1.4, 1.5, 1.6, 1.7):
            controller.tick(snapshot(timestamp))
        self.assertIs(controller.state, MSMState.READY_FOR_START)
        self.assertEqual(controller.stage_index, 0)

        controller.start(10.0)
        for timestamp in (10.0, 10.1, 10.2):
            controller.tick(snapshot(timestamp))
        self.assertIs(controller.state, MSMState.RUNNING_STAGE)
        self.assertEqual(controller.stage_index, 0)
        self.assertFalse(controller.status(10.2).completed)

        for timestamp in (11.3, 11.4, 11.5, 11.6, 11.7):
            controller.tick(snapshot(timestamp))
        self.assertIs(controller.state, MSMState.READY_FOR_START)
        self.assertTrue(controller.status(11.7).completed)

    def test_non_auto_stage_waits_for_confirmation(self):
        controller = ProcessController()
        program = ProcessProgram((make_stage("one"), make_stage("two", auto_start=False)))
        controller.load(program)
        controller.start(0.0)
        for timestamp in (0.0, 0.1, 0.2, 1.3, 1.4, 1.5):
            controller.tick(snapshot(timestamp))
        self.assertIs(controller.state, MSMState.START)
        self.assertTrue(controller.status(1.5).waiting_for_confirmation)
        controller.confirm_next_stage()
        self.assertIs(controller.state, MSMState.START_STAGE)

    def test_stop_waits_for_cooling_threshold(self):
        controller = ProcessController(cooling_temperature=30.0)
        controller.load(ProcessProgram((make_stage("one"),)))
        controller.start(0.0)
        controller.stop()
        command = controller.tick(snapshot(1.0, temperature=40.0))
        self.assertIs(controller.state, MSMState.PRE_STOP)
        self.assertEqual(command.heater_percent, 0.0)
        controller.tick(snapshot(2.0, temperature=29.0))
        self.assertIs(controller.state, MSMState.STOP)

    def test_last_stage_removes_heater_request_immediately(self):
        controller = ProcessController()
        controller.load(ProcessProgram((make_stage("one"),)))
        controller.start(0.0)
        for timestamp in (0.0, 0.1, 0.2, 1.3):
            controller.tick(snapshot(timestamp))
        self.assertIs(controller.state, MSMState.END_TEMP_STABILIZATION)
        controller.tick(snapshot(1.4))
        self.assertIs(controller.state, MSMState.END_STAGE)
        command = controller.tick(snapshot(1.5))
        self.assertIs(controller.state, MSMState.PRE_STOP)
        self.assertIsNone(command.temperature_setpoint)
        self.assertEqual(command.heater_percent, 0.0)

    def test_polythermal_stage_cannot_be_force_continued(self):
        polythermal = ProcessStage(
            name="ramp",
            start_temperature=20.0,
            end_temperature=30.0,
            temperature_mode=TemperatureMode.POLYTHERMAL,
            temperature_rate_per_minute=10.0,
        )
        controller = ProcessController()
        controller.load(ProcessProgram((polythermal,)))
        controller.start(0.0)
        for timestamp in (0.0, 0.1, 0.2):
            controller.tick(snapshot(timestamp))
        self.assertIs(controller.state, MSMState.RUNNING_STAGE)
        with self.assertRaises(RuntimeError):
            controller.force_continue_stage()

    def test_manual_flow_override_takes_precedence_and_can_be_cleared(self):
        controller = ProcessController()
        controller.load(ProcessProgram((make_stage("one", duration=10.0),)))
        controller.set_manual_flow_override(0, 30.0)
        controller.start(0.0)
        controller.tick(snapshot(0.0))
        controller.tick(snapshot(0.1))
        command = controller.tick(snapshot(0.2))
        self.assertEqual(command.flow_setpoints, (30.0,))
        controller.clear_manual_flow_override(0)
        command = controller.tick(snapshot(0.3))
        self.assertEqual(command.flow_setpoints, (1.0,))

    def test_invalid_manual_flow_override_is_rejected(self):
        controller = ProcessController()
        controller.load(ProcessProgram((make_stage("one"),)))
        with self.assertRaises(ValueError):
            controller.set_manual_flow_override(1, 30.0)
        with self.assertRaises(ValueError):
            controller.set_manual_flow_override(0, -1.0)

    def test_external_flow_channel_is_never_write_enabled(self):
        controller = ProcessController()
        controller.load(ProcessProgram((make_stage("one", duration=10.0),)))
        controller.set_flow_channel_external(0)
        controller.start(0.0)
        for timestamp in (0.0, 0.1, 0.2):
            command = controller.tick(snapshot(timestamp))
        self.assertEqual(command.flow_setpoints, (1.0,))
        self.assertEqual(command.flow_write_enabled, (False,))
        controller.stop()
        safe_command = controller.tick(snapshot(0.3))
        self.assertEqual(safe_command.flow_setpoints, (0.0,))
        self.assertEqual(safe_command.flow_write_enabled, (False,))


if __name__ == "__main__":
    unittest.main()
