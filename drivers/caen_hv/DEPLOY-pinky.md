# Deploying the CaenHV equipment into the pinky MIDAS experiment

Target: the running `bt2026` experiment on **pinky** (`/home/pinky/bt2026/beamtime2026_pie5`,
frontend program `SlowControl`, started as `pi_scfe`, equipments `XYTable` (ID 7) and `Degrader`
(ID 6)). Facts about pinky below come from the ODB dump `scratch/online/run00162.json` in the
testbeam-env checkout; anything marked **check** was not verifiable from the laptop.

What you get: a third equipment `CaenHV` (event ID 8) in the same `scfe` binary, the alarm
module, a custom page, and the probe/fake tools. Nothing in the existing equipments changes.

## 0. Before you start (5 min, laptop or pinky)

| Check | Why | Command / expectation |
|---|---|---|
| Branch | all work is on `feature/caen-hv-frontend` (4 commits + a hardware-session commit) | `git log --oneline develop..feature/caen-hv-frontend` |
| Event ID 8 free on pinky | IDs in use: 0,1,6,7,21,103,107,111,112,113,120,121,140,301,401,410 | `odbedit -e bt2026 -c 'ls -lr /Equipment' \| grep "Event ID"` — no `0x0008` |
| Equipment name free | `Quad HV` exists (musip), `CaenHV` does not | `odbedit -e bt2026 -c 'ls /Equipment'` |
| CMake version | `scfe/CMakeLists.txt` requires **3.37** | `cmake --version` on pinky — **check**; if older, build with the overlay trick in `scratch/caen-hv-standalone/build.sh` or lower the requirement in a local copy |
| MIDAS tree | `hv.cxx` includes `mstrlcpy.h`, found under `$MIDASSYS/include/mscb` on the pioneer-midas image | `ls $MIDASSYS/include/mscb/mstrlcpy.h $MIDASSYS/include/mstrlcpy.h` — **check** which exists; `CMakeLists.txt` already adds both `include` and `include/mscb` |
| `pi_scfe` | the ODB starts `SlowControl` with the command `pi_scfe`, but the CMake target installs `scfe` | `which pi_scfe; file $(which pi_scfe)` — **check** whether it is a symlink/wrapper to the built `scfe`, and where the build directory is |
| USB | the DT1470ET must be on USB (ID `21e1:0003`), the board in **REMOTE** | `lsusb \| grep 21e1`, `ls -l /dev/ttyACM*` |

## 1. Hardware and OS (pinky, root once)

1. Plug the DT1470ET USB into pinky. `dmesg` shows `cdc_acm ...: ttyACM<n>: USB ACM device`.
2. Install the udev rule for a stable name and group access:
   ```bash
   sudo cp /home/pinky/bt2026/beamtime2026_pie5/drivers/caen_hv/99-caen-hv.rules /etc/udev/rules.d/
   sudo udevadm control --reload && sudo udevadm trigger
   ls -l /dev/caen_hv0          # -> /dev/ttyACM<n>, group dialout, 0660
   sudo usermod -aG dialout pinky   # then re-login (or newgrp dialout) for the pinky account
   ```
3. Put the board in **REMOTE** on its touchscreen and set each channel's front switch as
   wanted (KILL / OFF / ON). Remote control cannot override KILL/OFF; the frontend reports them
   as `ChStatus` bits 11 / 10 and refuses `ON` with a clear message.
4. Confirm the protocol from the pinky account (no MIDAS involved):
   ```bash
   cd /home/pinky/bt2026/beamtime2026_pie5
   python3 drivers/caen_hv/caen_hv_probe.py --port /dev/caen_hv0 info
   python3 drivers/caen_hv/caen_hv_probe.py --port /dev/caen_hv0 dump
   ```
   Expect `BDNAME DT1470ET`, `BDNCH 4`, `BDCTR REMOTE`. Note the VSET/ISET/MAXV the board
   currently holds — the frontend adopts them as its initial `Demand`/limits (see §4).

## 2. Code (pinky account)

```bash
cd /home/pinky/bt2026/beamtime2026_pie5
git fetch origin
git checkout feature/caen-hv-frontend      # or develop once the PR is merged
```
Build exactly as `SlowControl` is built today (**check** the existing build dir; the ODB says the
binary was built from `/home/pinky/bt2026/beamtime2026_pie5/scfe/scfe.cxx`):
```bash
export MIDASSYS=/home/pinky/packages/midas      # check: the MIDAS install pinky uses
cmake -S scfe -B scfe/build && cmake --build scfe/build -j4
```
Zero errors expected; `hv.cxx` (MIDAS's own class driver) compiles from `$MIDASSYS`. If cmake
complains about the 3.37 requirement, see §0. Make sure whatever `pi_scfe` resolves to points at
the new binary (re-run `cmake --install` if that is how it was installed, or update the symlink).

Do **not** restart the frontend yet.

## 3. ODB preparation (before the first start)

Run with `odbedit -e bt2026`. The frontend creates every key itself; the ones below are the ones
you want set *before* it reads them for the first time.

```bash
# serial port (default in the code is /dev/caen_hv0, so this is only needed if you skipped the udev rule)
odbedit -e bt2026 -c 'mkdir "/Equipment/CaenHV/Settings/Devices/CAEN HV"'
odbedit -e bt2026 -c 'create STRING "/Equipment/CaenHV/Settings/Devices/CAEN HV/Port"'
odbedit -e bt2026 -c 'set "/Equipment/CaenHV/Settings/Devices/CAEN HV/Port" /dev/caen_hv0'

# custom page: absolute path, so musip's quads_config_fe rewriting /Custom/Path does not matter
odbedit -e bt2026 -c 'create STRING /Custom/CaenHV'
odbedit -e bt2026 -c 'set /Custom/CaenHV /home/pinky/bt2026/beamtime2026_pie5/custom/caenhv.html'
```
Alarm class: the frontend creates `/Alarms/Classes/HV Alarm` completely on first start (system
message on, no run stop). If HV alarms should go to Slack like `DAQ Alarm` does, afterwards copy
its `Execute command` (`/home/pinky/bt2026/scripts/send_to_slack.sh '%s'`) and `Execute
interval` into `HV Alarm`.

## 4. First start and what to expect

1. On the mhttpd **Programs** page stop `SlowControl`, then start it (or restart `pi_scfe` the way
   it is normally run). Watch **Messages**:
   ```
   HV alarms active for CaenHV, 4 channels, class HV Alarm
   CAEN HV DT1470ET fw 1.08 sn 33997 ctrl REMOTE
   Settings/Editable collapsed to one string for mhttpd (eqtable.js:600 vs hv.cxx:802)
   ```
   `ctrl LOCAL` means the touchscreen is still in LOCAL; monitoring works, every SET is refused.
2. **The board is the source of truth at start** (`DF_PRIO_DEVICE`): `Variables/Demand`,
   `Settings/Voltage Limit` (MAXV), `Current Limit` (ISET), `Trip Time`, `Ramp Up/Down Speed`
   and `Variables/ChState` are all read *from the supply*. Nothing is written to it except an
   idempotent re-send of those same values. **No output voltage changes on start or restart.**
3. Check `/Equipment/CaenHV/Common/Enabled` is `y`. A brand-new equipment can come up
   `Enabled = n` because mfe preserves that key from the ODB (see
   `midas_files/wavedream-scalar-readout/docs/REGISTRY.md`); if so set it to `y` and restart.
4. Now configure, in the ODB or on the **CaenHV** custom page (left menu):
   - `Settings/Names[0..3]` — detector-meaningful names (the DB labels are
     `CaenHV[Measured]:<name>`; the part before a `%` is a display group).
   - `Settings/Voltage Limit[i]` — the software limit; also written to the board as MAXV.
     A `Demand` above it is corrected back to the limit.
   - `Settings/Current Limit[i]` (ISET, uA) and `Trip Time[i]` — the board's own protection.
   - `Settings/Alarm/Voltage Max[i]`, `Current Max[i]` (defaults 8000 V / 3000 uA are
     deliberately non-alarming), `Clear After s`, `Comm Timeout s`, `Status Mask`.
   - `Settings/Update Threshold Measured` (1 V) / `Current` (0.1 uA) — DB logging granularity.
5. Set a voltage: `Variables/Demand[i]` (V, magnitude; polarity is the rear switch, shown in
   `Variables/Polarity`). Switch on: `Variables/ChState[i] = 1`. Both are editable on the
   Equipment → CaenHV page and on the custom page.

## 5. Verify (10 min)

- Equipment → CaenHV renders (not blank) and shows Demand/Measured/Current/ChState/ChStatus/Polarity.
- Set `Demand` on an unloaded channel to a small value, `ChState = 1`: `Measured` ramps at the
  board's RUP, `Current` reads, `ChStatus` shows `ON`; `ChState = 0` ramps down. A channel whose
  front switch is OFF/KILL gives `ch N: ON accepted, not executed (STAT DIS|KILL) - check front
  switch` in Messages and, after 5 s, an `hv_alarm` warning.
- Temporarily set `Settings/Alarm/Current Max[i]` below the running current → `CaenHV Ch<i>`
  alarm within ~2 s; restore → it clears after `Clear After s`.
- Pull the USB cable for a minute → `CaenHV Comm` alarm; plug it back → readings return without a
  restart (`... is answering again`).
- History: mlogger picks the new variables up automatically (`Log history = 10`); check the
  History page for `CaenHV`.
- Run DB: only if `python/pioneer/rundb/logger.py` is running against `bt2026` (it is **not**
  in pinky's `/Programs` list in the run-162 dump — **check**). Then
  `SELECT equipment, channel, label, reading FROM logs.slow_control WHERE equipment='CaenHV'
  ORDER BY id DESC LIMIT 20;`

## 6. Rollback

`git checkout <previous commit>` and rebuild; restart `SlowControl`. The `/Equipment/CaenHV`
tree can stay (harmless) — if you delete it, also delete `/History/Links/System/CaenHV*` entries,
otherwise **mlogger refuses to start** on a dangling link (REGISTRY.md). `/Custom/CaenHV` can
simply be deleted. The board keeps its own settings; nothing on it needs undoing.

## Known MIDAS issues you will hit (documented, worked around, to be reported upstream)

1. `mfe.cxx:1348-1357 message_print()` copies every `cm_msg` into a 160-byte stack buffer without
   a bound: a message ≥ 159 characters **aborts the frontend**. All our messages are kept
   under 120 characters; do not add long ones.
2. `hv.cxx` writes `Settings/Editable` as a 2-element string array, `eqtable.js:600` calls
   `.toLowerCase()` on it → the equipment page renders blank. The frontend collapses the key to
   the single string `Demand,ChState` on every start (one INFO line).
3. `hv.cxx`'s `Variables/Current` block has no NaN rescue: once NaN, it is never updated again.
   The driver therefore never reports NaN current (last good value, or **-1 uA** = never read).

## Open items after deployment

- Confirm on the real unit whether `VSET > MAXV` answers `VAL:ERR` or clips (the fake defaults
  to `VAL:ERR`); the frontend never sends such a value because of the software limit.
- `python/pioneer/rundb/logger.py` de-duplicates on `channel` without the equipment name
  (`logs.last_sc_update`, 1 s `upd_time` granularity): same-second updates of `Measured[0]` from
  `CaenHV` and `XYTable` can drop a row. Separate fix with Patrick (key on `(equipment, channel)`).
- Rename `CaenHV` to the detector it powers before the run if wanted; `Settings` do not follow a
  rename, and the DB labels carry the equipment name.
