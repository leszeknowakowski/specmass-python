# SpecMass Python migration

This directory contains the new Python implementation beside the authoritative
LabVIEW source. The LabVIEW tree is not modified.

The first milestone is deliberately hardware-safe:

- reads the existing `.msdef` stage format and legacy JSON configuration files;
- models the 15-state LabVIEW process state machine;
- computes temperature, flow, and valve commands deterministically;
- includes a bounded PID controller and CSV telemetry writer;
- runs against a simulated furnace and devices only.
- validates legacy stage timing against a matching TDMS output file.

No serial port, relay, valve, mass spectrometer, or flow-controller command is
sent by this version. Real drivers will be added behind the same interfaces only
after their command protocols and safety interlocks have been validated.

## Run the tests

From this directory, with Python 3.11 or newer:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## Run the simulator

```powershell
$env:PYTHONPATH = "src"
python -m specmass.cli --output simulation.csv --cool-to 50
```

Manual Brooks-style overrides can be exercised without hardware. This keeps
channel 1 at 30 even when the loaded stages request another value:

```powershell
python -m specmass.cli --program "D:\path\to\program" --flow-override 1=30
```

The supplied run used physical Brooks channel 2 (zero-based application `Ch1`)
at 30 from the controller front panel while the program requested zero. Reproduce
the legacy write-on-change behavior with:

```powershell
python -m specmass.cli --program "D:\path\to\program" --front-panel-flow 1=30
```

Because the LabVIEW-side cached request also began at zero, no setpoint write
was sent and the local value remained 30. TDMS output distinguishes a channel
that may be written (`WriteEnabled`) from a write actually emitted on that
sample (`WritePerformed`).

Python also offers a stricter monitor-only mode. This is an explicit safety
extension, not the mechanism that kept channel 2 at 30 in the supplied run:

```powershell
python -m specmass.cli --program "D:\path\to\program" --external-flow 1=30
```

With the optional TDMS dependency installed, choosing a `.tdms` output name
produces TDMS instead. The file includes `Time/ElapsedSeconds` and
`Time/UtcSeconds` channels so irregular acquisition timing is preserved:

```powershell
python -m specmass.cli --output simulation.tdms
```

The PyQt5/pyqtgraph desktop simulator can load and run the same program folders.
Install the project once to receive the GUI packages:

```powershell
python -m pip install -e .
```

Then run it with:

```powershell
python -m specmass.ui --program "D:\path\to\program-folder"
```

The header now follows the LabVIEW three-screen workflow:

1. **Monitor screen** shows the live/simulated dashboard and plots.
2. **Config screen** shows the selected program's stages, temperature settings,
   valves, and Brooks flow settings. **New program** creates `Stage1.msdef` and
   `ScanSettings.msdef` in a new or empty operator-chosen folder. Stages can be
   added, copied, removed, and edited; changes remain pending until **Save
   program**. Before replacing or removing an existing stage file, Save places
   a recovery copy below the program's `.specmass-backup` folder.
3. **Environment and scan configuration** is the file-based Hiden scan editor.
   Select the large `+` button to open the LabVIEW-style four-tab editor:
   **Environment**, **Scan**, **Detector**, and **Advanced**. Scan supports both
   single-mass trend acquisition and linear from/to/step sweeps. Detector
   exposes the legacy input-device list, autozero, ranges, dwell, settle,
   relative sensitivity, and relative gain. Select a row and use **Edit** (or
   double-click it) to reopen the populated four-tab editor, or use `−` to
   remove it, then explicitly select **Save ScanSettings.msdef**.

Simulation `.tdms` output (or `.csv` when `nptdms` is unavailable) is created
directly in the loaded program folder with a timestamped, non-overwriting name.
This keeps the stage definitions, scan settings, and run output together.
**Wait for cooling** is enabled by default: after the final stage, safe outputs
remain active while plotting and logging continue down to the selected cooling
threshold. Uncheck it before START to close the run immediately after the safe
stop sequence instead.

The scan editor preserves the legacy JSON field names, validates the complete
scan plan, and atomically replaces only the selected program folder's
`ScanSettings.msdef`. Unsaved changes require Save or Discard before leaving.
Opening the screen, adding/editing/removing masses, and saving do not open COM3. The
environment-parameter table is read-only and **Upload to device** is visibly
disabled. A saved scan plan is uploaded only when an explicitly authorized
Hiden acquisition run starts; editing never changes a live instrument.
The Environment tab in the new-scan dialog is also a read-only reference: the
copied Hiden manual confirms those values are global live-device state returned
separately from the scan array. The Advanced tab maps directly to the legacy
`Options` and `Changes to environment parameters` scan fields without sending
them anywhere.

Optionally supply `--builds` with a simulated program to resolve the configured
mass names from the copied `Builds/data/MSDevTh` file. This is file-only access:

```powershell
python -m specmass.ui --program "D:\path\to\program-folder" --builds "D:\_SpecMass\Builds"
```

The default red-banner mode uses `SimulatedBackend` and cannot open serial or
TCP connections. A separate, explicitly enabled ADAM-4118 monitor mode can open
COM14 for temperature reads only; it cannot run a program or apply an actuator
command. pyqtgraph supplies native zoom, pan, auto-range, and plot export
controls; right-click a plot to open its menu.

To parse an existing LabVIEW program folder instead of the included example:

```powershell
python -m specmass.cli --program "D:\path\to\program-folder"
```

## Validate an existing LabVIEW run

TDMS support is optional:

```powershell
python -m pip install -e ".[tdms]"
python -m specmass.validate_cli "D:\path\to\program" "D:\path\to\run.tdms"
```

The validator compares programmed and recorded durations, estimates the measured
temperature ramp and thermal lag, and warns about unusable TDMS timebases.

See `docs/migration-notes.md` for the traceability map and the remaining
hardware work.

## Inventory the deployed computer without controlling hardware

The copied `Builds/data` configuration can be compared with the COM ports on
the instrument computer without opening any port. From `cmd.exe`:

```bat
python -m pip install pyserial
inventory_hardware.cmd "D:\path\to\_SpecMass\Builds"
```

The command creates `specmass-hardware-inventory.json`. Its default mode sends
zero device queries and zero output commands. See `docs/deployed-hardware.md`
for the mapping derived from the supplied deployment and screenshots.

After reviewing that inventory, the ADAM-4118 temperature input can be tested
independently. Close LabVIEW first so COM14 is not already open, keep the
process idle, and run:

```bat
set PYTHONPATH=D:\specmass-python\src
python -m specmass.hardware_inventory --builds "D:\_SpecMass\Builds" --output "D:\specmass-python\adam4118-read-only.json" --probe-adam4118 --allow-read-queries
```

This opt-in probe sends one documented read-all-input command and has no output
command path.

The Brooks 0254 flow inputs can be checked separately on COM13. This probe
sends identification and `K` measured-value requests only:

```bat
python -m specmass.hardware_inventory --builds "D:\_SpecMass\Builds" --output "brooks-read-only.json" --probe-brooks --allow-read-queries
```

It validates packet checksums, input-port/channel correspondence, and both the
type-2 and deployed firmware's type-4 measured-value layouts. It never sends a
`P01` setpoint command.

The same validated input can be displayed continuously in a read-only PyQt5
monitor. Copy the current project to the instrument computer, close LabVIEW,
and run:

```bat
python -m pip install -e .
set PYTHONPATH=D:\specmass-python\src
python -m specmass.ui --temperature-monitor --builds "D:\_SpecMass\Builds" --allow-read-hardware
```

This mode polls COM14 every 500 ms. All actuator controls are disabled, and its
backend rejects every control command rather than silently ignoring one.

After both read-only probes report `ok`, temperatures and all four Brooks flow
channels can be monitored together:

```bat
python -m specmass.ui --hardware-monitor --builds "D:\_SpecMass\Builds" --allow-read-hardware
```

The combined monitor reads COM14 and COM13 approximately once per second. Its
Brooks client has no setpoint method, and the shared backend rejects all heater,
valve, and flow commands. Do not run it concurrently with LabVIEW.

### Hardware shadow run

Shadow mode executes a selected program against the same live ADAM4118 and
Brooks inputs while calculating the heater percentage, valve states, and flow
setpoints that a future hardware backend would request. It does not send those
values to any device:

```bat
python -m specmass.ui --shadow-run --program "D:\path\to\program" --builds "D:\_SpecMass\Builds" --allow-read-hardware
```

Close LabVIEW first because shadow mode repeatedly sends the validated read
queries on COM14 and COM13. It does not open COM3, COM12, or COM23. The yellow
banner and device table identify the mode; Brooks manual controls are disabled.
The shadow wrapper contains only the existing read-only clients, never calls
their rejecting `apply` path, forces every flow write-enabled/performed value
to zero, and records an internal output-command count of zero.

Select **START** to begin the program. Live measurements and calculated
requests are written to a non-overwriting `specmass_shadow_*.tdms` (or `.csv`)
file in the program folder. TDMS root metadata records
`SpecMass_Mode=HardwareShadowRun` and `SpecMass_OutputCommandsEnabled=0`.
Ports are closed on completion, error, or normal window close.

Because shadow mode cannot heat the real furnace, start-temperature
stabilization advances only if the live furnace is already within the program's
tolerance. Use a disposable program whose first stage starts near the current
temperature for the initial validation. **Wait for cooling** remains optional
and uses the live primary thermocouple when enabled.

Add a new output filename to record the same live samples. Existing files are
never overwritten:

```bat
python -m specmass.ui --hardware-monitor --builds "D:\_SpecMass\Builds" --allow-read-hardware --monitor-output "D:\specmass-python-git\monitor_20260722.csv"
```

CSV rows are flushed after every sample. For a TDMS log, install the optional
dependency and use a `.tdms` filename:

```bat
python -m pip install -e ".[tdms]"
python -m specmass.ui --hardware-monitor --builds "D:\_SpecMass\Builds" --allow-read-hardware --monitor-output "D:\specmass-python-git\monitor_20260722.tdms"
```

TDMS preserves the legacy `Temperature/Temperature`,
`Temperature/Temperature2`, and `Flows/Ch0` through `Ch3` paths while adding
exact elapsed and UTC time channels. Close the GUI normally to finalize a TDMS
file. For a bounded unattended check, `--monitor-duration 60` closes the monitor
normally after one minute.

## Hiden scan inspection and acquisition

The deployed connection, binary `Builds\16359.cfg` environment, and a program's
`ScanSettings.msdef` can be parsed and validated without opening COM3:

```bat
python -m specmass.hiden --builds "D:\_SpecMass\Builds" --program "D:\path\to\program" --output "hiden-offline.json"
```

The report normalizes the legacy NI-VISA serial enums, lists every scan and
resolved mass label, and records explicit zero-I/O counters. The supplied real
program contains six single-point SEM scans at masses 18, 28, 30, 32, 44 and
46 using filament F1.

Mass acquisition is not a passive read. The Hiden scan driver selects an
operating mode, uploads the environment and scan/detector parameters, controls
the selected filament/ion beam, and may change detector range. The implemented
driver follows the recovered LabVIEW command order, parses incremental report-17
data, and always attempts scan stop, data stop, standby, filament disable, and
port close on completion or error.

The first live integration is intentionally a shadow run: Hiden acquisition is
enabled, while heater, valve, and Brooks setpoint writes remain disabled. It
requires both Hiden-specific opt-ins:

```bat
python -m specmass.ui --shadow-run --hiden-acquisition --program "D:\path\to\program" --builds "D:\_SpecMass\Builds" --allow-read-hardware --allow-hiden-control
```

This mode currently accepts only single-point mass trend rows, which are the
shape used by the supplied program. Values are plotted live and written to
`specmass_hiden_shadow_*.tdms` (or `.csv`). The low-level parser supports linear
report-17 rows, but linear sweep display/storage is not yet connected to the
main time-series GUI. For safety, the Hiden backend is bound to the program and
scan plan loaded at application startup; restart the application after editing
or selecting a different program.

The command is state-changing and has not yet been validated against COM3.
Before its first controlled run, close LabVIEW and MASsoft, verify the intended
filament and detector ranges in `ScanSettings.msdef`, and keep an operator at
the instrument. See `docs/hiden-migration.md` for the recovered sequence and
shutdown boundary.
