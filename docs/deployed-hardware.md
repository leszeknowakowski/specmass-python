# Deployed SpecMass hardware map

This map combines the copied LabVIEW deployment with the zero-I/O inventory
captured on the instrument computer on 21 July 2026. No device was queried to
create the inventory.

## Host computer

- Windows 10 Pro, version 1909, OS build 18363.1556.
- AMD64, 64-bit Python 3.13.14.

## Active LabVIEW device configuration

`Builds/data/DevMgrTh` loads these five device threads:

| Thread | Device | Configured connection | Screenshot result | Status |
| --- | --- | --- | --- | --- |
| `MSDevTh` | Hiden mass spectrometer | COM3, 921600 baud | Hiden HAL COM3, USB VID:PID 10C4:86B1, serial 16359 | Active |
| `ValveDevTh` | VICI actuator | COM23, 9600 baud | EDG VCOM Port 23 exists | Active |
| `BrooksDevTh` | Brooks 0254, four channels | COM13, 9600 baud legacy default | EDG VCOM Port 13 exists | Active |
| `DIOTh` | ADAM 4050 digital output | COM12, 9600 baud | EDG VCOM Port 12 exists | Active heater/output device |
| `AIOTh` | ADAM 4118 temperature input | COM14, 9600 baud | EDG VCOM Port 14 exists | Active, channels `Temperature` and `Temperature2` |

Device Manager also shows EDG VCOM ports COM11, COM21, COM22 and COM24, plus
the standard communications port COM1. They are not selected by the active
`DevMgrTh` configuration. `AlicatMassFlowDevTh` exists as a file and references
COM1, but it is not in the active thread list.

The instrument report corrects an important stale-copy discrepancy. The local
copied `Builds/data/MSDevTh` says COM4 and `EnableMS:false`, while the file used
by the running instrument says COM3 and `EnableMS:true`. The instrument copy is
authoritative for live behavior; do not deploy the stale file over it.

## Safe inventory on the instrument computer

The default inventory command reads files and asks Windows for the COM-port
list. It does not open any COM port and cannot write a setpoint, valve state, or
heater output.

From `cmd.exe`, after copying `specmass-python` to the instrument computer:

```bat
cd /d "D:\path\to\specmass-python"
python -m pip install pyserial
inventory_hardware.cmd "D:\path\to\_SpecMass\Builds"
```

Do not reuse a `.venv` copied from another computer: its Python launcher stores
the original interpreter path. The batch file detects such an environment and
falls back to the `python` command installed on the instrument computer.

Return `specmass-hardware-inventory.json` from the project directory. It will
contain the Windows/Python architecture, COM-port hardware identifiers, the
configuration-to-port comparison, and explicit zero-I/O safety counters.

## ADAM-4118 passive temperature probe

The first hardware protocol implemented in Python is the input-only path used
for temperature acquisition. The deployed settings are COM14, 9600 baud,
address 1, module 4118, engineering-units format, and named values
`Temperature` and `Temperature2`.

The manufacturer documents `#AA` followed by carriage return as the read-all
analog-input command, where `AA` is the two-digit hexadecimal address. For
address 1, Python sends exactly `#01` plus carriage return and parses the signed
engineering-unit values. The implementation exposes no ADAM output operation.

Only after closing `MassSpec.exe` so it releases COM14, and while the process is
idle, run the updated project from `cmd.exe`:

```bat
cd /d D:\specmass-python
set PYTHONPATH=D:\specmass-python\src
python -m specmass.hardware_inventory --builds "D:\_SpecMass\Builds" --output "D:\specmass-python\adam4118-read-only.json" --probe-adam4118 --allow-read-queries
```

This opens COM14 and sends one input query. It sends no heater, relay, valve, or
flow setpoint command. The Brooks probe remains separate and should not be run
at the same time as LabVIEW.

The instrument returned eight valid engineering-unit values:

```text
23.5, 32.5, 0.0276, 0.0050, 0.0015, 0.0008, 0.0006, 0.0006
```

The configured name order maps the first two to `Temperature = 23.5 °C` and
`Temperature2 = 32.5 °C`. This validates COM14, address 1, 9600 baud, the
`#01` read frame, response parsing, and channel ordering.

## Read-only graphical temperature monitor

The PyQt5 UI now has an explicit monitor-only mode. It polls the validated
ADAM-4118 input every 500 ms and plots both temperature channels. The Start,
Stop, stage, flow, and cooling controls are disabled; the monitor backend also
rejects every control command at the Python API boundary.

Close `MassSpec.exe`, install the GUI dependencies once, and launch from
`cmd.exe`:

```bat
cd /d D:\specmass-python
python -m pip install -e .
set PYTHONPATH=D:\specmass-python\src
python -m specmass.ui --temperature-monitor --builds "D:\_SpecMass\Builds" --allow-read-hardware
```

Do not run this monitor concurrently with LabVIEW because both applications
would contend for COM14.

## Brooks read-only checksum validation

The first COM13 read probe reached the controller but Python initially reported
received checksum `C9` versus calculated `2E`. The response was valid: Python
had incorrectly included the `AZ` packet pre-limiter in the negated modulo-256
sum. The Brooks protocol defines the checksum frame as beginning with the comma
immediately after `AZ`. Correcting that boundary converts `2E` to `C9`; no
setpoint command was sent during this diagnosis.

The raw diagnostic then confirmed unit address `16773`, firmware `V17.01.31`,
and valid type-4 polled measurement packets for all four input ports. Their
third payload fields were `-0.48`, `-0.24`, `-0.04`, and `-0.04` ml/min, matching
the magnitude and channel ordering visible in the legacy LabVIEW flow graph.
Python now supports both the type-2 and type-4 measured-flow layouts without
relaxing checksum, input-port, or requested-channel validation.

## Combined read-only hardware monitor

The PyQt5 UI can now plot the validated ADAM-4118 temperatures and Brooks 0254
flows together using `--hardware-monitor --allow-read-hardware`. It polls COM14
and COM13 approximately once per second. The flow reader is a separate
`Brooks0254ReadOnlyClient` with no setpoint API, the combined backend rejects
every control command, and all actuator widgets remain disabled.

The monitor accepts an optional `--monitor-output` path ending in `.csv` or
`.tdms`. It refuses to overwrite an existing file. CSV is flushed per sample;
TDMS retains the legacy temperature and flow group/channel paths and adds exact
`Time/ElapsedSeconds` and `Time/UtcSeconds` channels. TDMS root properties mark
the session as `ReadOnlyHardwareMonitor` and output commands as disabled.

An offscreen smoke run on the instrument PC produced a finalized 6.3 kB TDMS
file with 11 complete samples. Consecutive sample intervals were 0.990-1.042 s;
both temperature channels and all four flow channels contained live values.
Every `WriteEnabled` and `WritePerformed` sample was zero, and the root metadata
reported `SpecMass_OutputCommandsEnabled=0`.

## Hardware shadow runner

The next layer reuses that exact combined read-only monitor inside
`HardwareShadowBackend`. The normal process controller and PID calculate stage
temperature, heater, valve, and Brooks requests, but the wrapper has no
actuator client and never calls the monitor backend's rejecting `apply` method.
The runtime additionally forces all Brooks write-enabled and write-performed
masks false before logging.

Shadow mode is selected only with the explicit combination `--shadow-run`,
`--program`, `--builds`, and `--allow-read-hardware`. It polls COM14 and COM13
approximately once per second and does not open the Hiden, heater, or valve
ports. The GUI uses a persistent yellow safety banner, disables manual flow
controls, labels ADAM4118 and Brooks1 as read-only, and labels all output devices
as disabled. Shadow TDMS logs carry `HardwareShadowRun` mode metadata and a
zero output-command flag.

This implementation has automated zero-dispatch coverage but has not yet been
run against the physical installation. The first live validation must use a
disposable program starting near the measured furnace temperature, with
LabVIEW closed, and must inspect the resulting TDMS write masks and root
metadata before any later migration milestone.

A subsequent operator-observed stability run produced 1,503 complete CSV rows
over 1,501.93 s (25.03 min). Timestamps were strictly increasing with a median
1.0000 s interval, maximum 1.1013 s interval, and no gap over 1.5 s. There were
no missing or non-finite sensor values. Touching the primary thermocouple
produced a clear 24.5 to 34.4 degrees C excursion lasting about 89 s, while
Temperature2 and the four Brooks baselines remained stable. Heater output and
every flow write-enabled/write-performed field remained zero for the full run.
