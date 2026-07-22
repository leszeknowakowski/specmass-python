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
