from __future__ import annotations

import argparse
from math import isfinite
from pathlib import Path

from .devices.simulated import SimulatedBackend
from .legacy import load_program
from .pid import PIDController, PIDGains
from .runtime import SpecMassRuntime
from .safety import SafetyPolicy
from .state_machine import MSMState, ProcessController
from .telemetry import CsvTelemetryWriter
from .tdms_telemetry import TdmsTelemetryWriter, mass_stimuli_from_scan_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SpecMass without hardware against the deterministic simulator")
    parser.add_argument("--program", type=Path, default=_default_program())
    parser.add_argument("--output", type=Path, default=Path("simulation.csv"))
    parser.add_argument("--dt", type=float, default=0.1, help="simulation timestep in seconds")
    parser.add_argument("--max-time", type=float, default=300.0, help="maximum simulated seconds")
    parser.add_argument(
        "--cool-to",
        type=float,
        default=50.0,
        help="continue recording natural cooling to this temperature (default: 50 °C)",
    )
    parser.add_argument(
        "--flow-override",
        action="append",
        default=[],
        metavar="CHANNEL=VALUE",
        help="persistent manual flow setpoint; may be repeated, for example 1=30",
    )
    parser.add_argument(
        "--external-flow",
        action="append",
        default=[],
        metavar="CHANNEL=VALUE",
        help="simulate a front-panel-owned monitor-only channel, for example 1=30",
    )
    parser.add_argument(
        "--front-panel-flow",
        action="append",
        default=[],
        metavar="CHANNEL=VALUE",
        help="simulate a Brooks front-panel change, for example app channel 1 / physical channel 2 as 1=30",
    )
    args = parser.parse_args()
    if args.dt <= 0 or args.max_time <= 0:
        parser.error("--dt and --max-time must be positive")

    program = load_program(args.program)
    flow_count = max((len(stage.start_flows) for stage in program.stages), default=0)
    backend = SimulatedBackend(flow_channels=flow_count)
    controller = ProcessController(
        temperature_tolerance=0.5,
        stability_seconds=0.5,
        cooling_temperature=args.cool_to,
    )
    pid = PIDController(PIDGains(kc=20.0, ti_seconds=10.0, td_seconds=0.0))
    controller.load(program)
    overridden_channels: set[int] = set()
    for raw_override in args.flow_override:
        try:
            channel_text, value_text = raw_override.split("=", 1)
            channel = int(channel_text)
            controller.set_manual_flow_override(channel, float(value_text))
            overridden_channels.add(channel)
        except (ValueError, TypeError) as exc:
            parser.error(f"invalid --flow-override {raw_override!r}: {exc}")
    for raw_external in args.external_flow:
        try:
            channel_text, value_text = raw_external.split("=", 1)
            channel = int(channel_text)
            if channel in overridden_channels:
                raise ValueError("a channel cannot be both app-overridden and external")
            value = float(value_text)
            if not isfinite(value) or value < 0:
                raise ValueError("external simulated flow must be finite and non-negative")
            controller.set_flow_channel_external(channel, True)
            backend.set_external_flow(channel, value)
        except (ValueError, TypeError) as exc:
            parser.error(f"invalid --external-flow {raw_external!r}: {exc}")
    for raw_front_panel in args.front_panel_flow:
        try:
            channel_text, value_text = raw_front_panel.split("=", 1)
            channel = int(channel_text)
            if channel in overridden_channels:
                raise ValueError("a channel cannot have both an app override and a front-panel value")
            value = float(value_text)
            if not isfinite(value) or value < 0:
                raise ValueError("front-panel simulated flow must be finite and non-negative")
            backend.set_front_panel_flow(channel, value)
        except (ValueError, TypeError) as exc:
            parser.error(f"invalid --front-panel-flow {raw_front_panel!r}: {exc}")
    controller.start(0.0)

    if args.output.suffix.lower() == ".tdms":
        log = TdmsTelemetryWriter(
            args.output,
            flow_channels=flow_count,
            mass_stimuli=mass_stimuli_from_scan_settings(program.scan_settings),
            nominal_increment_seconds=args.dt,
        )
    else:
        log = CsvTelemetryWriter(args.output, flow_channels=flow_count)
    with log:
        runtime = SpecMassRuntime(
            backend=backend,
            controller=controller,
            pid=pid,
            safety=SafetyPolicy(maximum_temperature=1200.0),
            telemetry=log,
        )
        timestamp = 0.0
        while timestamp <= args.max_time:
            frame = runtime.step(timestamp, args.dt)
            if controller.state is MSMState.START and controller.status(timestamp).waiting_for_confirmation:
                controller.confirm_next_stage()
            if frame.status.completed:
                break
            timestamp += args.dt
        else:
            runtime.safe_shutdown()
            raise RuntimeError("Simulation did not complete before --max-time")

    runtime.safe_shutdown()
    print(f"Simulation completed at t={timestamp:.1f}s; telemetry: {args.output.resolve()}")
    return 0


def _default_program() -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "demo_program"


if __name__ == "__main__":
    raise SystemExit(main())
