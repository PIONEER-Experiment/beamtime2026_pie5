# PIONEER Online Repo for the 2026 Phasespace Measurement in PiE5

## Slow control frontend (scfe)

`scfe/` builds a single MIDAS slow-control frontend with three equipments:

| equipment | event ID | device |
|---|---|---|
| `Degrader` | 6 | Patrick's `pi_generic`-based degrader stage |
| `XYTable` | 7 | ISEL XY table |
| `CaenHV` | 8 | CAEN DT1470ET HV supply, stock `cd_hv` class driver |

### Building

Requires `MIDASSYS` set in the environment (e.g. `/software/midas` in the
`pioneer-midas`/`testbeam-midas` container):

```sh
cmake -S scfe -B scfe/build
cmake --build scfe/build
```

Note: `scfe/CMakeLists.txt` sets `cmake_minimum_required(VERSION 3.37)`, but the
`pioneer-midas` container image currently ships CMake 3.31. Either build on a host/container
with CMake ≥ 3.37, or lower the `cmake_minimum_required` version locally to match what is
installed (do not commit that downgrade upstream without checking it is still compatible).

For the CAEN HV equipment specifically — shifter instructions (setting voltages, alarms,
`LOC:ERR`, testing without hardware) and the probe/emulator dev tools — see
[`drivers/caen_hv/README.md`](drivers/caen_hv/README.md).
