import unittest

from specmass.devices.hiden import (
    HidenDataStreamParser,
    HidenProtocolError,
    HidenScanClient,
    HidenScanCodec,
    HidenTrendAcquisition,
)
from specmass.hiden import (
    HidenEnvironmentConfig,
    HidenEnvironmentDevice,
    HidenEnvironmentSettings,
    HidenScanPlan,
    apply_hiden_environment_settings,
    new_hiden_mass_scan,
)


class ScriptedTransport:
    def __init__(
        self,
        data_responses: tuple[bytes, ...] = (),
        *,
        fail_command: str | None = None,
        identity: str = '"HAL RC RGA 201 #16359"',
    ) -> None:
        self.data_responses = list(data_responses)
        self.fail_command = fail_command
        self.identity = identity
        self.commands: list[str] = []
        self.closed = False

    def transact(self, request: bytes) -> bytes:
        command = request.decode("ascii").removesuffix("\r")
        self.commands.append(command)
        if command == self.fail_command:
            raise TimeoutError(f"simulated timeout for {command}")
        if command == "pget name":
            return f"{self.identity}\r\n".encode("ascii")
        if command == "sjob lget Ascans":
            return b"Ascans,42\r\n"
        if command == "data":
            if not self.data_responses:
                raise AssertionError("Unexpected Hiden data poll")
            return self.data_responses.pop(0)
        return b"OK\r\n"

    def close(self) -> None:
        self.closed = True


def environment() -> HidenEnvironmentConfig:
    return HidenEnvironmentConfig(
        mass_spec_name='"HAL RC RGA 201 #16359"',
        modes=("Shutdown", "RGA "),
        devices=(
            HidenEnvironmentDevice("F1", 52, "%d", 1, (0.0, 0.0)),
            HidenEnvironmentDevice("F2", 53, "%d", 1, (0.0, 1.0)),
            HidenEnvironmentDevice("emission", 60, "%.2f", 1, (0.0, 1000.0)),
            HidenEnvironmentDevice("SEM", 61, "%.2f", 4, (0.0, 1.0)),
        ),
        autozero_supported=True,
    )


def trend_plan(*masses: float) -> HidenScanPlan:
    return HidenScanPlan.from_mapping(
        {
            "Filament": "F1",
            "ScansParameters": [
                new_hiden_mass_scan(mass, use_autozero=True) for mass in masses
            ],
        }
    )


class HidenScanExecutionTests(unittest.TestCase):
    def test_program_global_environment_value_changes_uploaded_lput_value(self):
        configured = apply_hiden_environment_settings(
            environment(),
            HidenEnvironmentSettings(
                mode="RGA",
                values=(("emission", 250.0),),
            ),
        )

        commands = HidenScanCodec.environment_commands(
            configured,
            filament="F1",
        )

        self.assertIn("lput 60 0.00 250.00", commands)
        self.assertNotIn("lput 60 0.00 1000.00", commands)

    def test_local_environment_override_uses_recovered_legacy_payload(self):
        definition = HidenScanPlan.from_mapping(
            {
                "Filament": "F1",
                "ScansParameters": [
                    new_hiden_mass_scan(
                        18.0,
                        environment_changes=(
                            "lset emission 250.00, lset electron-energy 70.00"
                        ),
                    )
                ],
            }
        ).scans[0]

        commands = HidenScanCodec.scan_row_commands(
            1,
            definition,
            autozero_supported=True,
        )

        self.assertIn(
            "sset env lset emission 250.00, lset electron-energy 70.00",
            commands,
        )

    def test_executes_recovered_scan_sequence_acquires_and_stops_safely(self):
        transport = ScriptedTransport(
            (b"prefix[/100/1.25,", b"/105/2.50,]\r\n")
        )
        plan = trend_plan(18.0, 28.0)
        client = HidenScanClient(transport, environment())
        acquisition = HidenTrendAcquisition(
            client,
            plan,
            names_by_mass={18.0: "H2O", 28.0: "N2"},
        )

        identity = acquisition.start()
        first = acquisition.read_masses()
        second = acquisition.read_masses()
        acquisition.safe_shutdown()

        self.assertEqual(identity, "HAL RC RGA 201 #16359")
        self.assertEqual(first, {})
        self.assertEqual(second, {"H2O": 1.25, "N2": 2.5})
        self.assertEqual(transport.commands[0], "pget name")
        self.assertEqual(
            transport.commands[1:13],
            [
                "l999 scan",
                "lini all",
                "lset mode 0",
                "lset mode 0",
                "sout NUL:",
                "data on",
                "sdel all",
                "tdel all",
                "pset terse 1",
                "pset points 70",
                "serr NUL:",
                "sset scan Ascans",
            ],
        )
        self.assertIn("lput 52 0 1", transport.commands)
        self.assertIn("lput 53 0 0", transport.commands)
        self.assertIn("lput 60 0.00 1000.00", transport.commands)
        self.assertNotIn("lput 61 0.00 1.00", transport.commands)
        self.assertIn("sset row 1", transport.commands)
        self.assertIn("sset row 2", transport.commands)
        self.assertNotIn("sset row 0", transport.commands)
        self.assertIn("sset report 17", transport.commands)
        self.assertIn("sset zero 1", transport.commands)
        self.assertLess(
            transport.commands.index("sjob lget Ascans"),
            transport.commands.index("data all"),
        )
        self.assertEqual(
            transport.commands[-4:],
            ["stop 42", "data stop", "lset mode 0", "lset enable 0"],
        )
        self.assertTrue(transport.closed)
        self.assertFalse(acquisition.active)

    def test_linear_report_17_frame_is_decoded_incrementally(self):
        plan = HidenScanPlan.from_mapping(
            {
                "Filament": "F2",
                "ScansParameters": [
                    new_hiden_mass_scan(0.4, stop_mass=0.6, increment=0.1)
                ],
            }
        )
        parser = HidenDataStreamParser(plan)
        parser.feed(b"[/250/{1.0,")
        self.assertIsNone(parser.pop_cycle())
        parser.feed(b"2.0,3.0},]\n")
        cycle = parser.pop_cycle()

        self.assertIsNotNone(cycle)
        assert cycle is not None
        self.assertEqual(cycle.scans[0].elapsed_milliseconds, 250)
        self.assertEqual(cycle.scans[0].values, (1.0, 2.0, 3.0))
        self.assertEqual(len(cycle.scans[0].stimuli), 3)
        self.assertAlmostEqual(cycle.scans[0].stimuli[-1], 0.6)

    def test_scan_row_zero_is_rejected_before_it_can_reach_the_device(self):
        with self.assertRaisesRegex(HidenProtocolError, "numbered from 1"):
            HidenScanCodec.scan_row_commands(
                0,
                trend_plan(18.0).scans[0],
                autozero_supported=True,
            )

    def test_report_17_requires_the_leading_elapsed_time_delimiter(self):
        parser = HidenDataStreamParser(trend_plan(18.0))
        parser.feed(b"[10/1.0,]\n")
        with self.assertRaisesRegex(
            HidenProtocolError,
            "missing '/' before elapsed time",
        ):
            parser.pop_cycle()

    def test_stream_rejects_instrument_error_and_non_finite_data(self):
        plan = trend_plan(18.0)
        parser = HidenDataStreamParser(plan)
        parser.feed(b"*C 17 invalid scan\r\n")
        with self.assertRaisesRegex(HidenProtocolError, "reported an error"):
            parser.pop_cycle()

        parser.feed(b"[/10/NaN,]\n")
        with self.assertRaisesRegex(HidenProtocolError, "non-finite"):
            parser.pop_cycle()

    def test_reported_value_applies_legacy_relative_factors(self):
        plan = HidenScanPlan.from_mapping(
            {
                "Filament": "F1",
                "ScansParameters": [
                    new_hiden_mass_scan(
                        18.0,
                        relative_sensitivity=2.0,
                        relative_gain=4.0,
                    )
                ],
            }
        )
        parser = HidenDataStreamParser(plan)
        parser.feed(b"[/10/8.0,]\n")
        cycle = parser.pop_cycle()

        self.assertIsNotNone(cycle)
        assert cycle is not None
        self.assertEqual(cycle.scans[0].values, (1.0,))

    def test_partial_start_failure_returns_instrument_to_standby_and_closes(self):
        transport = ScriptedTransport(fail_command="lini all")
        client = HidenScanClient(transport, environment())

        with self.assertRaises(TimeoutError):
            client.start(trend_plan(18.0))

        self.assertEqual(
            transport.commands[-3:],
            ["data stop", "lset mode 0", "lset enable 0"],
        )
        self.assertTrue(transport.closed)
        self.assertFalse(client.active)

    def test_identity_mismatch_sends_no_state_changing_command(self):
        transport = ScriptedTransport(identity='"different instrument"')
        client = HidenScanClient(transport, environment())

        with self.assertRaisesRegex(HidenProtocolError, "does not match"):
            client.start(trend_plan(18.0))

        self.assertEqual(transport.commands, ["pget name"])
        self.assertTrue(transport.closed)

    def test_live_application_rejects_linear_scans(self):
        plan = HidenScanPlan.from_mapping(
            {
                "Filament": "F1",
                "ScansParameters": [
                    new_hiden_mass_scan(18.0, stop_mass=19.0, increment=0.5)
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "single-point mass trend"):
            HidenTrendAcquisition(
                HidenScanClient(ScriptedTransport(), environment()),
                plan,
            )


if __name__ == "__main__":
    unittest.main()
