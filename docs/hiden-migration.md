# Hiden mass-spectrometer migration boundary

The scan protocol was reconstructed and implemented offline. No COM3 connection
was opened and no Hiden command was sent while implementing or testing the scan
path. The earlier, separately approved identity-only query is recorded below.

## Deployed configuration

The authoritative instrument-computer configuration is serial COM3 at 921600
baud, no parity, eight data bits, one stop bit and a 1000 ms timeout, with
`EnableMS:true`. LabVIEW stores one stop bit as the NI-VISA enum value `10`.
The copied local `Builds/data/MSDevTh` remains stale at COM4 with
`EnableMS:false` and must not be deployed over the instrument copy.

The named mass table contains H2O 18, N2 28, O2 32, He 4, Ar 40 and CO2 44.
The supplied program requests six single-point scans at masses 18, 28, 30, 32,
44 and 46. Masses 30 and 46 therefore have no configured chemical label. All
six scans use the SEM input, scan mode 1, 100 percent dwell and settle,
autorange limits -9 through -7, and start range -9. Filament F1 is selected;
autozero is enabled only for the first scan.

`python -m specmass.hiden` validates this configuration entirely offline and
emits a JSON report with zero port/query/output counters.

## Driver behavior recovered offline

The bundled Hiden manual says the MSIU protocol consists of ASCII command and
response strings terminated by carriage return. It also makes clear that the
LabVIEW driver is for acquisition rather than tuning.

Extracting temporary copies of the bundled LabVIEW VIs revealed these command
families:

- default setup uses `lset mode 0`, which puts the instrument in standby;
- configuration interrogation includes `pset terse 1`, `pget name`, `lid#`,
  `lunt`, `lmin`, `lmax` and `lres` operations;
- current environment values use `lget` operations;
- scan configuration uses `sset output`, `start`, `stop`, `step`, `mode`,
  `input`, `dwell`, `settle`, ranges, autozero, options and environment data;
- scan execution uses `lini Ascans`, `sjob lget Ascans`, `data all`, cycle and
  report configuration;
- normal acquisition stop/close paths place the instrument in standby.

These findings are sufficient to reject a generic "read-only mass monitor".
Both the scan-module and partial-pressure-gauge paths change instrument state.
The manual specifically says the partial-pressure path selects an operating
mode, sets the requested device, turns the ion beam on, may autorange the input,
then turns the beam off and restores the device value.

## Implemented safe boundary

`HidenIdentityReadOnlyClient` exposes only the exact `pget name` request found
in the legacy device-interrogation VI. It has no initialization, standby,
filament, mode, range or scan method. The inventory CLI additionally requires
both `--probe-hiden-identity` and `--allow-read-queries` before it can open
COM3, and its report carries separate zero counters for every state-changing
command category.

This identity path was executed once on 22 July 2026 after verifying that
LabVIEW, MASsoft and the Python monitor were not running. COM3 returned the
quoted ASCII identity `"HAL RC RGA 201 #16359"`, matching the saved instrument
identity. The report recorded one identity query and zero initialization,
standby, filament, scan or other state-change commands. A postflight process
check confirmed that the one-shot Python process had exited and released COM3.

## Implemented scan execution and acquisition

`HidenScanClient` now reproduces the recovered LabVIEW scan sequence. Before
changing state it sends `pget name` and requires an exact match with the
identity in the selected serial-number `.cfg`. It then sends the LabVIEW setup
preamble, uploads group-1 environment values from the binary DTLG file with the
program's F1/F2 selection applied, configures every scan row with report 17,
starts `Ascans`, obtains the job ID, and begins data streaming.

The report-17 parser is incremental: it retains partial serial responses,
decodes the driver's `/elapsed-time/data` records, supports scalar trend values
and braced linear arrays, verifies each returned value count against the
configured stimulus axis, rejects instrument errors and non-finite numbers,
applies the legacy division by relative sensitivity and relative gain, and caps
its buffer. The main GUI integration deliberately
accepts only unique single-point mass trend scans for now. Completed cycles are
mapped to named mass channels, plotted, and logged beside temperature and flow
data.

Normal completion, an acquisition exception, a failed partial start, and window
close all use the same best-effort shutdown sequence:

1. `stop <validated job id>` when a job was created;
2. `data stop`;
3. `lset mode 0`;
4. `lset enable 0`;
5. close COM3.

The GUI exposes this path only when all of `--shadow-run`,
`--hiden-acquisition`, `--allow-read-hardware`, and `--allow-hiden-control` are
present. Its banner explicitly says that COM3 scan/filament commands are
enabled. Furnace heater, VICI valve, ADAM4050, and Brooks setpoint writes remain
disabled in this milestone.

## Offline scan editor

The PyQt5 GUI now mirrors the LabVIEW monitor, stage-configuration, and Hiden
environment/scan screens. Its new-scan dialog mirrors the four LabVIEW tabs:
Environment, Scan, Detector, and Advanced. It supports trend and linear mass
scans, the full detector list shown by the deployed editor, autozero, ranges,
dwell/settle percentages, relative factors, cycle controls, and the two raw
legacy advanced fields. The Hiden screen can add, edit, and remove these
definitions and explicitly save the selected program's `ScanSettings.msdef`.
Edit reopens the same four-tab dialog with the selected scan populated and
preserves unknown legacy fields when applying the known values. The editor
validates the same typed `HidenScanPlan` used by the offline report, preserves
unknown top-level settings, writes atomically, and prompts before discarding
unsaved changes.

This editor has no serial client or device-upload callback. The displayed
environment values are a read-only reference snapshot and the **Upload to
device** button is disabled. The saved identity is labeled with a tooltip that
states that opening the screen does not repeat the COM3 identity query.
The copied Hiden manual confirms that Environment-tab edits are returned as
global mass-spectrometer I/O-device values, separately from the scan array.
Consequently that tab's Change control remains disabled rather than inventing
an incompatible `ScanSettings.msdef` representation.

## Next controlled milestone

The implementation and fake-transport verification are complete; physical scan
validation is not. The next step is one short operator-observed COM3 run with
LabVIEW and MASsoft closed, a reviewed disposable trend-scan program, and an
explicit decision to exercise the state-changing sequence. The resulting mass
values, timing, stop behavior, filament state, and TDMS channels should then be
compared with a short known-good LabVIEW scan before broader use.

The first operator start attempt on 23 July 2026 reached scan configuration and
the instrument rejected `sset row 0` with `C042`; no acquisition began. This
confirmed that the deployed unit uses the same one-based row numbering shown by
the LabVIEW scan editor. The driver now emits rows 1 through N and rejects row
zero locally. A controlled retry remains required.

The next retry passed scan configuration and started acquisition, then exposed
the report-17 record prefix: the deployed stream writes elapsed time as
`/elapsed/`, exactly as annotated in the LabVIEW block diagram. The original
Python parser expected `elapsed/` and therefore reported an empty field. It now
requires and consumes both slash delimiters and includes a bounded raw frame
fragment in future framing errors.
