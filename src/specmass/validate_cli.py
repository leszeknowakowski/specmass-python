from __future__ import annotations

import argparse
from pathlib import Path

from .validation import validate_legacy_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a LabVIEW program against its TDMS output")
    parser.add_argument("program", type=Path, help="folder containing ScanSettings.msdef and Stage*.msdef")
    parser.add_argument("tdms", type=Path, help="TDMS file produced by the same run")
    args = parser.parse_args()
    report = validate_legacy_run(args.program, args.tdms)

    print(f"Programmed stages: {report.program_duration_seconds:.3f} s")
    print(f"Temperature record: {report.recorded_duration_seconds:.3f} s")
    print(f"Logging lead/transition time: {report.logging_lead_seconds:.3f} s")
    if report.expected_first_ramp_start_seconds is not None:
        print(f"Expected first ramp start: {report.expected_first_ramp_start_seconds:.3f} s")
        print(f"Measured ramp: {report.measured_ramp_celsius_per_minute:.4f} °C/min")
        print(f"Estimated thermal lag: {report.estimated_thermal_lag_seconds:.3f} s")
    if report.mass_timebases and not report.mass_timebase_is_trustworthy:
        print("WARNING: TDMS mass-channel waveform timestamps do not span the recorded run.")
        for item in report.mass_timebases:
            if not item.spans_recorded_run:
                print(
                    f"  {item.group}/{item.channel}: encoded {item.encoded_duration_seconds:.3f} s; "
                    f"full-run interval would be about {item.implied_full_run_increment_seconds:.6f} s"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

