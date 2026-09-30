# First hardware test of the IsegHV equipment on pinky

**Not yet hardware-tested.** Everything below has run only against the pty fake
(`fake_iseg_nhq.py`). Fill in the "Result" lines as you go.

Target: the live `bt2026` experiment on **pinky**, with the NHQ output **open**
(no PMT connected). The new `scfe` is built in a second worktree and started by
hand in tmux. `~/bin/pi_scfe`, the main checkout and the `SlowControl` entry in
`/Programs` are not touched, so rollback is "stop the tmux `scfe`, start
`SlowControl` from the Programs page" (P5).

Style and pinky facts follow `drivers/caen_hv/DEPLOY-pinky.md`. What the
equipment does is in `README.md` ("MIDAS frontend"). Run this with the user at
the keyboard. Nothing here pushes to origin without a fresh yes from the user.

Voltages in this test stay at or below **100 V**. The driver setting
`Max Voltage` is set to 100 before the first start (P2). The frontend then
reports `Voltage Limit` = 100, and MIDAS's `cd_hv` clamps any higher VSET to
100 V before the driver sees it (checked on the laptop prototype).

## P0. Checks before touching anything (pinky, 10 min)

| # | Check | Command | Expect |
|---|---|---|---|
| 1 | Keyspan adapter present | `lsusb \| grep 06cd:0121` | one line, Keyspan USA-19H |
| 2 | Serial device | `ls -l /dev/ttyUSB* /dev/iseg_nhq*` | `/dev/ttyUSB0`, and `/dev/iseg_nhq0` if the udev rule is installed |
| 3 | udev rule | `ls -l /etc/udev/rules.d/99-iseg-nhq.rules` | present. If not: `sudo cp <checkout>/drivers/iseg_nhq/99-iseg-nhq.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger`. The `pinky` account must be in `dialout` |
| 4 | Nobody holds the port | `fuser -v /dev/ttyUSB0`; `pgrep -af 'ch5_voltage_tmp\|iseg_nhq_probe'` | no output. The old CLI copy `~/bt2026/ch5_voltage_tmp` has **no lock**: it must not run during this test |
| 5 | Autostart off | after P1: CLI `dump --ch 2`, line `A` | `000` (`A2 = 0`) |
| 6 | Front panel, channel 2 | look at the unit | CONTROL switch in the **lower (DAC)** position; HV-ON switch **on** for channel 2; KILL switch position noted (ENABLE means a hard cut is possible); Vmax rotary position noted (`M2` reads it as a percent, last bench value was 020 %, i.e. 1600 V) |
| 7 | Polarity switch | look at the side cover | negative for S5. **Never change it under power** |
| 8 | Event ID 9 free | `odbedit -e bt2026 -c 'ls -lr /Equipment' \| grep "Event ID"` | no `0x0009` (list of IDs in use: `drivers/caen_hv/DEPLOY-pinky.md:16`, plus 8 for `CaenHV`) |
| 9 | Name `IsegHV` free | `odbedit -e bt2026 -c 'ls /Equipment'` | no `IsegHV` |
| 10 | Which branch is the main checkout on | `git -C ~/bt2026/beamtime2026_pie5 branch --show-current`; `git -C ~/bt2026/beamtime2026_pie5 worktree list` | write both down. P6 needs `develop` to be free in the worktree list |
| 11 | Nothing else changes the S5 voltage | ask the shift / run coordinator | agreed |

Result: ______________________________________________

## P1. Get the branch, build, talk to the unit without MIDAS

The branch `feature/iseg-nhq-frontend` must be **committed** to leave the
laptop. A push or a bundle carries commits only. Committing is done only when
the user asks.

**Way A, push (needs the user's explicit yes, every time):**

```bash
# laptop, in the worktree scratch/worktrees/beamtime2026_pie5-iseg-hv
git push origin feature/iseg-nhq-frontend
# pinky
cd ~/bt2026/beamtime2026_pie5 && git fetch origin
```

**Way B, bundle (no push):**

```bash
# laptop, in the worktree
git bundle create /tmp/iseg-hv.bundle origin/develop..feature/iseg-nhq-frontend
scp /tmp/iseg-hv.bundle pinky:~/bt2026/
# pinky (the clone must already have the commit the branch starts from)
cd ~/bt2026/beamtime2026_pie5
git fetch origin
git fetch ~/bt2026/iseg-hv.bundle feature/iseg-nhq-frontend:feature/iseg-nhq-frontend
```

Then the second worktree and the build:

```bash
cd ~/bt2026/beamtime2026_pie5
mkdir -p ~/bt2026/worktrees
git worktree add ~/bt2026/worktrees/iseg-hv feature/iseg-nhq-frontend
cd ~/bt2026/worktrees/iseg-hv
export MIDASSYS=/home/pinky/packages/midas
cmake -S scfe -B scfe/build && cmake --build scfe/build -j4
ls -l scfe/build/scfe
```

The build goes into the worktree's own `scfe/build`. It does not replace the
binary behind `~/bin/pi_scfe`.

Optional, 31 s: `python3 -m unittest discover -s drivers/iseg_nhq/tests`.

Talk to the unit with the new CLI. MIDAS is not involved yet. Only one program
may hold the port, so nothing else may run:

```bash
cd ~/bt2026/worktrees/iseg-hv/drivers/iseg_nhq
./iseg_nhq_probe.py --port /dev/ttyUSB0 info
./iseg_nhq_probe.py --port /dev/ttyUSB0 dump --ch 2 | tee ~/bt2026/iseg-test-dump-before.txt
```

Expect `481198;2.06;8000V;1000uA` from `info`. From `dump` write down `A`
(must be 000), `T`, `S`, `M`, `D`, `V`, `L`, `U`, `I`. `D` is the set point
`scfe` will adopt.

Result: ______________________________________________

## P2. ODB preparation, before the first start

The frontend creates every key it needs. These are the ones that must be set
**before** it reads them for the first time. Use `odbedit -e bt2026`.

```bash
# driver settings for the unit (the IsegHV tree does not exist yet)
odbedit -e bt2026 -c 'mkdir "/Equipment/IsegHV/Settings/Devices/iseg NHQ"'
odbedit -e bt2026 -c 'create STRING "/Equipment/IsegHV/Settings/Devices/iseg NHQ/Port"'
odbedit -e bt2026 -c 'set "/Equipment/IsegHV/Settings/Devices/iseg NHQ/Port" /dev/ttyUSB0'
odbedit -e bt2026 -c 'create FLOAT "/Equipment/IsegHV/Settings/Devices/iseg NHQ/Max Voltage"'
odbedit -e bt2026 -c 'set "/Equipment/IsegHV/Settings/Devices/iseg NHQ/Max Voltage" 100'

# alarms off for the first start: the HV Alarm class posts to Slack
odbedit -e bt2026 -c 'mkdir /Equipment/IsegHV/Settings/Alarm'
odbedit -e bt2026 -c 'create BOOL /Equipment/IsegHV/Settings/Alarm/Enabled'
odbedit -e bt2026 -c 'set /Equipment/IsegHV/Settings/Alarm/Enabled n'

# test page: new key, absolute path to the worktree's page. /Custom/CaenHV stays as it is.
odbedit -e bt2026 -c 'create STRING /Custom/HV-test'
odbedit -e bt2026 -c 'set /Custom/HV-test /home/pinky/bt2026/worktrees/iseg-hv/custom/caenhv.html'
```

Check what you set:

```bash
odbedit -e bt2026 -c 'ls -l "/Equipment/IsegHV/Settings/Devices/iseg NHQ"'
odbedit -e bt2026 -c 'ls /Equipment/IsegHV/Settings/Alarm'
odbedit -e bt2026 -c 'ls -l /Custom'
```

Notes:

* `Settings/Zero Threshold` is set to -1 V by the driver only when the key does
  not exist. That holds on this fresh `IsegHV` tree. If you ever restart with an
  existing tree that lacks it, set `/Equipment/IsegHV/Settings/Zero Threshold[0]`
  to -1 by hand (`scfe/iseg_nhq_fe.h:153-156`).
* `Alarm/Enabled n` silences only `IsegHV`. The `CaenHV` alarms are unchanged.
  The key is read every second, so it can be switched live in P4.
* If `Port` or `Max Voltage` come back with the default value after the first
  start (the record was re-created), set them again and check `Max Voltage`
  reads 100 **before** any voltage is entered.
* If `/Custom/Path` is rewritten by another frontend (see `custom/README.md`),
  the absolute path in `/Custom/HV-test` still works.

Result: ______________________________________________

## P3. Swap the frontend

1. Record what `CaenHV` looks like now:
   ```bash
   mkdir -p ~/bt2026/iseg-test
   odbedit -e bt2026 -c 'ls -lr /Equipment/CaenHV/Variables' > ~/bt2026/iseg-test/caenhv-before.txt
   odbedit -e bt2026 -c 'ls -lr "/Equipment/CaenHV/Settings"' > ~/bt2026/iseg-test/caenhv-settings-before.txt
   ```
   Note `Demand`, `Measured`, `ChState`. Pick a moment with no run starting and
   no stage move in progress.
2. On the mhttpd **Programs** page stop `SlowControl`. `XYTable`, `Degrader` and
   `CaenHV` control is off until step 3. The CAEN board keeps its voltages.
3. Start the worktree binary in tmux:
   ```bash
   tmux new -s iseghv
   export MIDASSYS=/home/pinky/packages/midas
   ~/bt2026/worktrees/iseg-hv/scfe/build/scfe -e bt2026
   ```
   Detach with `Ctrl-b d`. It registers as the client `SlowControl`, so the
   Programs page shows it as running (auto-restart is off).
4. Check in **Messages** (in this order, roughly):
   ```
   HV alarms active for CaenHV, 4 channels, class HV Alarm
   HV alarms active for IsegHV, 1 channels, class HV Alarm
   iseg NHQ 481198 sw 2.06 8000V/1000uA ch2 D=<your D2>V ON|OFF V=<n> L=<n>uA M=<n>% A=0
   ```
5. Check:
   * `CaenHV`: `ls -lr /Equipment/CaenHV/Variables` equals `caenhv-before.txt`
     (Measured may differ by noise). Messages shows no CaenHV write. `ChState`
     did not change.
   * `XYTable` and `Degrader` show normal readings on their pages.
   * `IsegHV`: `Variables/ChState` is 1 if `D2` was nonzero, else 0.
     `Variables/Demand` equals `D2` (or 0 if the unit had none). **The unit's
     `D2`, `S2` and front panel did not change.** Confirm with the display and
     with a later CLI `dump` (needs scfe stopped, so only after P4).
   * The page **HV-test** (left menu) shows both tables. Status for `S5` is
     green, orange or light grey, not grey `no data`.
   * If `D2` was above 100 V, Messages shows `S5 at N V above limit 100 V:
     switch off or lower Demand`. The driver does **not** ramp it down. Handle
     it by hand.

If anything in step 5 fails: go to P5 rollback at once.

Result: ______________________________________________

## P4. Tests, at 100 V or less, output open

Set `Alarm/Enabled` back to `n` if it is not. Watch **Messages** and the
**HV-test** page. Write the result on each line.

| # | Action | Expected | Result |
|---|---|---|---|
| 1 | Click VSET, type `50`, Enter. Tick On/Off, confirm | Messages `S5 on: D2=50 V, G2 -> ...`. Status green `ramping up`, then `ON`. VMON goes to about 50 V at the `Ramp` speed. IMON about 0 (open output) | |
| 2 | Untick On/Off | Messages `S5 off: D2=0, G2 -> ...; demand 50 V kept`. Status `ramping down`, then `0 V set`. VMON to 0. **VSET still shows 50** | |
| 3 | Tick On/Off again | ramps back to 50 V without retyping | |
| 4 | Set `Current Limit` (Trip) to `40` (click the Trip cell) | Messages `current trip L2 = 40 uA`. CLI later shows `L2 = 040` | |
| 5 | Set `Ramp Up Speed` to `20`: `odbedit -e bt2026 -c 'set "/Equipment/IsegHV/Settings/Ramp Up Speed[0]" 20'` | Messages `ramp V2 = 20 V/s (up and down)`. A new ramp takes 20 V/s | |
| 6 | Set `Ramp Down Speed` to `30` the same way | Messages `Ramp Down Speed refused: one ramp register V2 ...`. Nothing changes on the unit | |
| 7 | Channel off. Flip the channel 2 **HV-ON** switch off, tick On/Off, confirm | Status orange `HV-ON switch off`. Messages `ChState ON refused: front panel HV-ON switch off (CONTROL to DAC, HV-ON on)`. The box stays ticked in ODB, so after 5 s a `ChState ON but board says off` message follows. Untick the box, flip HV-ON back on | |
| 8 | Channel off. Flip **CONTROL** to manual, tick On/Off, confirm | Status orange `manual (CONTROL not on DAC)`. Messages `ChState ON refused: front panel in manual control (CONTROL to DAC, HV-ON on)`. Untick the box, put CONTROL back to DAC | |
| 9 | Move **KILL** to the other position and back | Status shows orange `KILL enabled` whenever the switch is in ENABLE, so it reads orange in one of the two positions. Return it to the position noted in P0 | |
| 10 | With the channel on, type VSET `150` | VSET snaps back to `100` and the unit ramps to 100 V: `cd_hv` clamps Demand to `Voltage Limit` (100) without a message. It never goes above 100 V | |
| 11 | Switch to off. Type VSET `150` (channel off) | Messages `150 V refused: limit 100 V ...` at once (the ceiling is checked while off too, as far as it is known). The driver does not store it; ticking On/Off later must not ramp to 150 V. Type an allowed value | |
| 12 | Set `Alarm/Enabled` to `y` (only for this test). With the channel on at 50 V, unplug the serial cable | Status purple `stale (...)`. After 60 s alarm `IsegHV Comm` (mhttpd banner), and a Slack post. Messages `HV /dev/ttyUSB0 lost ...` | |
| 13 | Plug the cable back in | Within about 5 s Messages `back: NHQ 481198 ...`. Status returns. The alarm clears. **No `D`/`G` sent**: the set point is unchanged | |
| 14 | Set `Alarm/Enabled` back to `n` (or leave `y` if the user decides) | the key holds | |
| 15 | With scfe running: `~/bt2026/worktrees/iseg-hv/drivers/iseg_nhq/iseg_nhq_probe.py --port /dev/ttyUSB0 info` | prints `port /dev/ttyUSB0 in use (MIDAS frontend running? stop scfe first)` and exits **3** (`echo $?`) | |
| 16 | Channel on at 50 V. Restart the tmux `scfe` (Ctrl-C in tmux, start it again) | after restart: same D2, `ChState` 1, the unit keeps ramping or holding, **nothing written** (no `S5 on:` / `S5 off:` line, no `D2=` change on the display) | |

Then switch the channel off, wait for `0 V set`, and look at the unit display.
Optional: stop scfe and run the CLI `dump --ch 2` to compare with
`iseg-test-dump-before.txt`; start scfe again.

Result: ______________________________________________

## P5. Decide: keep running, or roll back

**Keep:** leave the tmux session `iseghv` running. The hand-started `scfe` is
`SlowControl` for now. Tell the shift that `SlowControl` on the Programs page
was started by hand.

**Roll back:**

```bash
tmux attach -t iseghv          # Ctrl-C, then exit
# or stop it from the Programs page
# then start SlowControl from the Programs page (runs pi_scfe as before)
```

* The `IsegHV` ODB tree left behind is harmless.
* If `IsegHV` is not to run at all, set `/Equipment/IsegHV/Common/Enabled` to
  `n`: `odbedit -e bt2026 -c 'set /Equipment/IsegHV/Common/Enabled n'`.
  (Old `pi_scfe` has no `IsegHV`, so this only matters if the new binary
  runs.)
* If you delete `/Equipment/IsegHV`, delete `/History/Links/System/IsegHV*`
  entries too, otherwise `mlogger` refuses to start on a dangling link
  (`drivers/caen_hv/DEPLOY-pinky.md`, section 6).
* Remove `/Custom/HV-test` if the page is not needed.
* The unit keeps its own settings. Nothing on it needs undoing except to
  switch channel 2 off.

Decision and time: ______________________________________________

## P6. Merge into develop on pinky (only if P3 and P4 pass)

A failed line is fixed on the branch and retested first. Run in the pinky
worktree `~/bt2026/worktrees/iseg-hv`.

1. `git fetch origin`. If `origin/develop` has moved since the branch point,
   rebuild and repeat the P3 read-back and a short P4 (lines 1, 2, 3) before
   merging.
2. Check that no other worktree has `develop` (P0 line 10). Then:
   ```bash
   git switch develop
   git merge --ff-only origin/develop
   git merge --no-ff feature/iseg-nhq-frontend \
      -m "Merge feature/iseg-nhq-frontend: IsegHV (S5 NHQ) equipment + HV page"
   ```
3. Rebuild from the merged `develop`, restart the tmux `scfe` from that build,
   and check that `CaenHV` and `IsegHV` read back unchanged (P3 step 5).
4. `git push origin develop` and the feature branch **only after the user's
   explicit yes, on the spot**.
5. Record the merge commit here and in the memory note:
   merge commit `________________`, date `__________`.

Pointing `pi_scfe` and `/Custom/CaenHV` at the merged code in the main checkout
is part of the later S5 changeover, not this step.

## Later: S5 changeover

Not part of the first test. Do it as its own step, with the PMT connected:

* Connect the PMT. Set `Max Voltage` back to 1300:
  `odbedit -e bt2026 -c 'set "/Equipment/IsegHV/Settings/Devices/iseg NHQ/Max Voltage" 1300'`.
* Alarm thresholds under `/Equipment/IsegHV/Settings/Alarm/`: `Current Max[0]`
  350 uA, `Deviation Max[0]` 20 V, `Deviation Hold s` 30 s (the driver defaults
  in `scfe/scfe.cxx:110-127`). Set `Alarm/Enabled` to `y` and agree with the
  shift what to do on an alarm (`README.md`, shifter guide).
* Put the merged code in the main checkout, rebuild `scfe/build` there so
  `~/bin/pi_scfe` picks it up, and start `SlowControl` from the Programs page.
  Stop the tmux `scfe` first: only one program may hold the port.
* Point `/Custom/CaenHV` at the merged `custom/caenhv.html` in the main
  checkout. Remove `/Custom/HV-test` and the worktree
  (`git worktree remove ~/bt2026/worktrees/iseg-hv`).
* Retire the old CLI copy `~/bt2026/ch5_voltage_tmp` (it has no lock). The new
  CLI in `drivers/iseg_nhq/` is the manual path.
