import unittest

from specmass.devices.base import ControlCommand, SensorSnapshot
from specmass.devices.hardware_shadow import HardwareShadowBackend
from specmass.models import ProcessProgram, ProcessStage, TemperatureMode
from specmass.pid import PIDController, PIDGains
from specmass.runtime import SpecMassRuntime
from specmass.state_machine import ProcessController


class FakeReadOnlyHardware:
    channel_names = ("Temperature", "Temperature2")
    flow_channel_names = ("Ch0", "Ch1", "Ch2", "Ch3")
    monitored_devices = ("ADAM4118", "Brooks1")
    poll_interval_ms = 1000

    def __init__(self) -> None:
        self.read_count = 0
        self.apply_count = 0
        self.closed = False

    def read(self, timestamp: float) -> SensorSnapshot:
        self.read_count += 1
        return SensorSnapshot(
            timestamp=timestamp,
            temperature=20.0,
            temperatures=(20.0, 21.0),
            flows=(1.0, 2.0, 3.0, 4.0),
            masses={},
        )

    def apply(self, command: ControlCommand, dt_seconds: float) -> None:
        del command, dt_seconds
        self.apply_count += 1
        raise AssertionError("The shadow wrapper must never call the reader output path")

    def safe_shutdown(self) -> None:
        self.closed = True


def shadow_program() -> ProcessProgram:
    return ProcessProgram(
        (
            ProcessStage(
                name="shadow",
                start_temperature=30.0,
                end_temperature=30.0,
                temperature_mode=TemperatureMode.ISOTHERMAL,
                temperature_rate_per_minute=0.0,
                start_flows=(5.0, 6.0, 7.0, 8.0),
                end_flows=(5.0, 6.0, 7.0, 8.0),
                flow_rates_per_minute=(0.0, 0.0, 0.0, 0.0),
                duration_seconds=10.0,
            ),
        )
    )


class HardwareShadowTests(unittest.TestCase):
    def test_apply_captures_calculation_without_calling_reader_apply(self):
        reader = FakeReadOnlyHardware()
        backend = HardwareShadowBackend(reader)
        backend.apply(
            ControlCommand(
                temperature_setpoint=30.0,
                flow_setpoints=(5.0, 6.0, 7.0, 8.0),
                valve_states=(True, False),
                heater_percent=25.0,
                flow_write_enabled=(True, True, True, True),
            ),
            1.0,
        )

        self.assertEqual(reader.apply_count, 0)
        self.assertEqual(backend.output_commands_sent, 0)
        self.assertEqual(backend.calculated_command_count, 1)
        self.assertEqual(
            backend.last_calculated_command.flow_write_enabled,
            (False, False, False, False),
        )
        self.assertEqual(
            backend.last_calculated_command.flow_write_performed,
            (False, False, False, False),
        )
        self.assertEqual(backend.last_calculated_command.heater_percent, 25.0)
        self.assertEqual(backend.last_calculated_command.valve_states, (True, False))

    def test_runtime_logs_requested_values_with_every_write_masked_off(self):
        reader = FakeReadOnlyHardware()
        backend = HardwareShadowBackend(reader)
        controller = ProcessController()
        controller.load(shadow_program())
        controller.start(0.0)
        runtime = SpecMassRuntime(
            backend=backend,
            controller=controller,
            pid=PIDController(PIDGains(kc=2.0, ti_seconds=10.0, td_seconds=0.0)),
        )

        runtime.step(0.0, 1.0)
        frame = runtime.step(1.0, 1.0)

        self.assertEqual(frame.command.flow_setpoints, (5.0, 6.0, 7.0, 8.0))
        self.assertEqual(frame.command.flow_write_enabled, (False,) * 4)
        self.assertEqual(frame.command.flow_write_performed, (False,) * 4)
        self.assertGreater(frame.command.heater_percent, 0.0)
        self.assertEqual(reader.apply_count, 0)
        self.assertEqual(backend.output_commands_sent, 0)

    def test_shutdown_closes_only_the_read_only_input_backend(self):
        reader = FakeReadOnlyHardware()
        backend = HardwareShadowBackend(reader)
        backend.safe_shutdown()
        self.assertTrue(reader.closed)
        self.assertEqual(backend.output_commands_sent, 0)


if __name__ == "__main__":
    unittest.main()
