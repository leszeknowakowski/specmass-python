# Migration notes and traceability

The authoritative LabVIEW application remains in
`../labview-org/_SpecMass_3/_SpecMass`. This Python directory is a separate,
hardware-safe replacement under development.

## Implemented mapping

| Python behavior | LabVIEW source concept |
| --- | --- |
| `MSMState` values 0–14 | `MSMState.ctl` |
| `.msdef` folder reader | `MSDefFileMgr_Read.vi` |
| `ProcessStage` fields | `ProcessStageData.ctl` |
| stage timing | `GetProcessStateDuration.vi` |
| temperature setpoint | `MSMTh_TempValuesToPID.vi` |
| isothermal flow ramp | `MSMTh_FlowsValuesToDevices.vi` |
| constant/impulse valves | `MSMTh_ValvesValuesToDevices.vi` |
| stage sequencing/cooling | `MSMTh_StateMachine.vi` and its state handlers |
| bounded controller | behavior boundary of `PIDTh_PID.vi` |

The legacy spellings `Polithermal` and `Impluse` are accepted when parsing, as
are corrected spellings. Stage files are naturally sorted to prevent `Stage10`
from preceding `Stage2`. Configuration JSON accepts the deployed trailing
commas found in `BrooksDevTh` and `ValveDevTh`.

## Intentional safety boundary

Only `SimulatedBackend` is connected to the runtime. It cannot open COM or TCP ports. Heater output is
represented as a percentage in memory; it is never converted into an ADAM 4050
relay command. A production backend must default to disabled and implement
explicit connection, watchdog, sensor-validity, over-temperature, emergency
stop, and safe-shutdown behavior before it can be selected.

## Still to validate against the running installation

1. Capture representative real `.msdef` program folders and compare calculated
   setpoints step by step.
2. Confirm whether an impulse begins in the opposite valve state, and verify the
   exact pulse-boundary convention.
3. Confirm temperature stabilization tolerance/time settings and cooling stop
   semantics from the deployed UI configuration.
4. Inventory the exact serial commands, replies, timeouts, and error behavior for
   Hiden HPR20, Brooks, ADAM 4050/4118, and VICI devices.
5. Compare the Python PID response with recorded LabVIEW input/output traces;
   the current PID is safe and conventional, not claimed to be bit-identical to
   the NI implementation.
6. Build the operator UI only after the state/command trace matches the running
   application.

## First deployed-run validation

The supplied `Cu2O_15M_15_07_26_deN2O` program contains a 500-second
isothermal stage followed by a 24–700 °C ramp at 10 °C/min. The calculated ramp
duration is 4,056 seconds, matching the value saved by LabVIEW. Its temperature
record is 4,571.5 seconds long: 15.5 seconds longer than the two programmed
stages. Treating that as logging/startup lead places the ramp at 515.5 seconds
and its endpoint exactly at the end of the record. A linear fit of the primary
temperature channel gives approximately 10.01 °C/min and about seven seconds of
thermal lag.

The six mass channels contain 16,496 samples each, but their `wf_increment` is
saved as 0.001 seconds, encoding only 16.495 seconds for a 4,571.5-second run.
Those waveform timestamps are therefore not a valid absolute time axis. The
Python logger must record real acquisition timestamps rather than reproduce this
legacy metadata defect.

The supplied stages request zero on every Brooks channel while the TDMS run has
`Flows/Ch1` near 30. `Ch1` is zero-based application indexing and corresponds to
physical Brooks channel 2. The operator confirmed that it was changed directly
from the Brooks front panel, not from SpecMass.

The deployed `Builds/data/BrooksDevTh` file configures all four channels (`0`
through `3`) as both inputs and outputs on `COM13`; it contains no per-channel
external/application selector. The project conditional symbol is
`SimulateDevices=FALSE`. `UseFlowManualSetpoint` instead chooses between the
stage setpoint and a manual value entered in the LabVIEW GUI, both of which are
application-side values.

The persistence of the front-panel value is explained by
`Brooks025x/_Device_WriteData.vi`: its `PrevData` cache is initialized to four
zeros, and it calls `BR254 Set Flow Setpoint.vi` only when a requested value
differs from the cached value. Since this program requested zero throughout, it
did not emit a new per-channel setpoint after the front panel was changed to 30.
Python reproduces that change-only gate. It additionally retains an explicit
monitor-only mode as a safety extension. New CSV and TDMS logs distinguish
write permission (`WriteEnabled`) from a command actually emitted on that
sample (`WritePerformed`).

## Brooks 0254 protocol boundary

The pure `Brooks0254Codec` maps zero-based application channels to the 0254
input ports 01/03/05/07 and output ports 02/04/06/08. It implements `K` process
value reads, `P01` rate setpoints, response checksum validation, and strict
acknowledgement matching. The serial transport remains disabled unless
`hardware_enabled=True` is provided explicitly; the production application does
not expose that switch yet. `BrooksChangeOnlyDispatcher` reproduces the legacy
zero-initialized `PrevData` behavior. Live setpoint scaling must still be
verified before this client is connected to the runtime backend.

The first instrument probe exposed and resolved a checksum-boundary defect.
The two-byte `AZ` packet pre-limiter is not part of the checksum; the negated
modulo-256 sum starts with the comma immediately after `AZ` and ends with the
comma before the two hexadecimal checksum characters. The deployed controller
returned `C9`; including `AZ` incorrectly produced `2E`. The corrected codec is
covered by the manufacturer's documented `9E` identification-frame example.

The follow-up diagnostic identified the deployed unit as address `16773`,
firmware `V17.01.31`. Its `K` replies use response type `4`, the protocol's
polled-information format, rather than the type `2` form shown in the dedicated
`K` example. Both layouts place the rate/process value in the third payload
field. The codec accepts both layouts, requires an odd input port, verifies that
the returned port matches the requested channel, and continues to distinguish
short type-4 `Pzz` parameter acknowledgements from measured-flow packets.

The validated Brooks reader is now combined with the ADAM-4118 reader in an
explicit PyQt5 hardware-monitor mode. The GUI shows both temperature channels
and all four measured flows, while the monitor's Brooks client exposes no
setpoint operation and its backend rejects every control command.

Optional monitor logging now records both real temperature channels and all
four real Brooks inputs to CSV or TDMS. The TDMS hierarchy preserves the legacy
temperature/flow paths but uses explicit elapsed and UTC time channels, avoiding
the invalid mass-waveform timebase found in the supplied LabVIEW run.

The logging path was exercised against the deployed COM13/COM14 hardware with
a bounded offscreen GUI run. The resulting TDMS file round-tripped through
npTDMS with complete temperature and flow series, approximately one-second
sample spacing, and zero values in every write-enabled/write-performed channel.

The longer live CSV validation also passed: 1,503 samples over 25.03 minutes,
no missing/non-finite readings, no acquisition gap above 1.11 seconds, and a
clearly isolated thermocouple-touch excursion. All actuator fields remained
disabled/zero throughout the recording.

## Instrument inventory correction

The zero-I/O report made on the instrument computer shows that its active Hiden
configuration is COM3 at 921600 baud with `EnableMS:true`. The copied local
`Builds/data/MSDevTh` is stale: it contains COM4 and `EnableMS:false`. All four
other active configured ports match Device Manager: VICI COM23, Brooks COM13,
ADAM 4050 COM12, and ADAM 4118 COM14.

The read-only ADAM-4118 codec now implements the documented `#AAN` single-input
and `#AA` all-input commands and strict engineering-unit response parsing. The
guarded inventory CLI can issue one all-input query only with both
`--probe-adam4118` and `--allow-read-queries`; it contains no ADAM output API.

## Hiden offline reconstruction

The deployed `MSDevTh` connection and `ScanSettings.msdef` now have typed,
validated Python models and a zero-I/O report command. The supplied recipe was
validated as six single-point SEM scans at masses 18, 28, 30, 32, 44 and 46
using F1, with autozero only on the first point.

Offline extraction of temporary copies of the bundled Hiden VIs established
that normal scan and partial-pressure acquisition change operating mode,
devices, ion-beam state and potentially detector range. It therefore cannot be
added to the existing passive hardware monitor. Only the isolated `pget name`
identity query has been implemented, in a client with no initialization or
state-changing method; it remains untested on COM3 pending a separate live-test
decision. Detailed evidence is in `docs/hiden-migration.md`.
