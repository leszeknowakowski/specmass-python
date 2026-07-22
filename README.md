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
3. **Environment and scan configuration** is the offline Hiden scan editor.
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

The scan editor preserves the legacy JSON field names, validates the complete
scan plan, and atomically replaces only the selected program folder's
`ScanSettings.msdef`. Unsaved changes require Save or Discard before leaving.
Opening the screen, adding/editing/removing masses, and saving do not open COM3. The
environment-parameter table is read-only and **Upload to device** is visibly
disabled until Hiden write commands have been implemented and validated.
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

## Offline Hiden scan inspection

The Hiden migration currently stops before mass-spectrometer control. The
deployed connection and a program's `ScanSettings.msdef` can be parsed and
validated without opening COM3:

```bat
python -m specmass.hiden --builds "D:\_SpecMass\Builds" --program "D:\path\to\program" --output "hiden-offline.json"
```

The report normalizes the legacy NI-VISA serial enums, lists every scan and
resolved mass label, and records explicit zero-I/O counters. The supplied real
program contains six single-point SEM scans at masses 18, 28, 30, 32, 44 and
46 using filament F1.

Mass acquisition is not a passive read. The Hiden scan driver selects an
operating mode, sets scan and detector parameters, controls the ion beam, and
may change detector range. A separately gated inventory option contains only
the isolated `pget name` identity query, with no initialization or scan API,
but it must not be run on COM3 until a dedicated live-test step is agreed.
See `docs/hiden-migration.md` for the command boundary reconstructed from the
copied driver.
