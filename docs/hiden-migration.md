# Hiden mass-spectrometer migration boundary

This note records offline evidence only. No COM3 connection was opened and no
Hiden command was sent while reconstructing this boundary.

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

This identity path is implemented and unit-tested but has not been executed on
the instrument. Do not combine it with the temperature/flow monitor and do not
run it while LabVIEW or MASsoft owns COM3.

## Next controlled milestone

The next live step, if approved, is one bounded `pget name` transaction while
the old application and MASsoft are closed. A successful response should match
the saved instrument identity `HAL RC RGA 201 #16359`. Regardless of outcome,
the port must then be closed without sending initialization or standby.

Actual mass acquisition is a later milestone requiring a reviewed command
sequence, explicit authorization for mode/filament/SEM changes, a fail-safe
shutdown procedure, and comparison with a short known-good LabVIEW scan.
