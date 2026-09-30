# iseg NHQ 208L HV supply, probe and emulator

Device-side helpers for a second HV supply joining the CAEN DT1470ET for the
2026 PSM beamtime: an **iseg NHQ 208L** (NIM, dual channel, RS232), unit
**481198**, firmware **2.06**, **2 channels**, **8 kV / 1 mA** module maximum.
Only **channel 2 ("B")** is in use — channel 1 answers the protocol
normally but is set to HV-OFF and manual control (`T1=011`: bit 8 HV-ON
switch off, bit 2 MAN, bit 1 display selection). The tools still address
channel 1 for diagnosis; nothing here drives it.

| file | what it is |
|---|---|
| `iseg_nhq_protocol.py` | the command table, value-format parsers, status words and module-status bits, shared by both tools |
| `iseg_nhq_probe.py` | CLI client: `info`, `dump`, `mon`, `set`, `ramp`, `off`, `watch`, `raw`; prints the raw wire exchange with `--verbose` |
| `fake_iseg_nhq.py` | pty emulator: echoes every byte, models the ramp, `MAN`/`OFF`/`UMAX`/`?WCN`/`????`/`?TOT`, both number formats, a dead channel 1 |
| `99-iseg-nhq.rules` | udev rule for the Keyspan USA-19H adapter |
| `tests/test_fake_with_probe.py` | stdlib `unittest`: drives the emulator through the probe's module API |
| `doc/` | the two vendor manuals |

## Standard library only

Python 3.10+, **`os.open` + `termios`, no pyserial** — the same reason as
`drivers/caen_hv/README.md:3-5`: nothing in this workspace installs pyserial,
and these files have to run unchanged on the PSI DAQ machine and inside the
`pioneer-midas` / `testbeam-midas` container without a pip install.

## Wiring and switch checklist

* **Connector**: 9-pin female D-Sub on the unit. Use a straight **1:1**
  extension cable, **not** a null modem — RxD is pin 2, TxD pin 3, GND pin 5
  on both ends.
* **Adapter**: the **Keyspan USA-19H** (USB `06cd:0121`) works, confirmed on
  pinky. The **Prolific PL2303** (USB `067b:2303`) was **silent** on two
  machines (the laptop and pinky) — treat a PL2303 as suspect, not the unit,
  if a link test shows no echo. The **WSL laptop kernel has no `keyspan`
  module**, so laptop-side tests need a different adapter than the Keyspan;
  a PL2303-class adapter that actually works, or running from pinky, are the
  alternatives.
* **CONTROL switch**: must be in the **lower (DAC)** position for RS232 to
  change the output. In the upper (manual) position commands are accepted
  and answered but have no effect.
* **HV-ON switch**: must be on, per channel, for that channel to output
  anything.
* **KILL switch**: a hard front-panel cutoff, separate from HV-ON.
* **Vmax / Imax rotary switches**: step in **10 %** increments of the module
  maximum (8000 V / 1000 uA here), so e.g. position 2 on Vmax = 20 % = 1600 V.
  `M<ch>` and `N<ch>` report the current position as a percentage; they are
  the hardware ceiling and cannot express an arbitrary software limit — see
  `--max-v` below.
* **Polarity**: a rotary switch on the **side cover**. **Never change it
  under power.**

## Protocol summary

Full detail and the reasoning behind each point is in the
`iseg_nhq_protocol.py` module docstring; this is the shape of it.

* **9600 8N1**, no flow control. Every command line is ASCII terminated by
  `\r\n`.
* **Per-character echo handshake**: the host sends one byte and must wait for
  the unit to echo that exact byte before sending the next. This applies to
  the terminator too.
* Bench-confirmed on this unit: there is **no second copy of the echoed
  line and no blank line** in front of the answer — the answer follows the
  echoed `\n` immediately, in the same read chunk. `sync()` sends a bare
  `\r\n` to resynchronise and is answered by `????` on this unit (except
  right after an already-desynchronised exchange, when a stale leftover
  line comes back instead).
* A read (`S`) answers `S2=ON ` (channel number and `=` prefixed, trailing
  space, differing from the manual's bare `xxx`). A write (`D2=50`) answers
  with an **empty line**. `G2` (start the ramp) answers `S2=L2H` while
  ramping and settles to `S2=ON`.
* Value formats, both bench-confirmed as the **standard series**: voltage is
  `{sign}{4-digit integer}`, e.g. `-0049` = -49 V; current is
  `{mantissa}{exponent}`, e.g. `0000-6` = 0 x 10^-6 A, i.e. 1 uA resolution.
* `? UMAX@=5600` (note the stray `@` this unit inserts before `=`) is **not**
  a simple refusal: the unit **clamps the set point to the limit and stores
  it** — after `D2=9999` on a 5600 V limit, `D2` reads back `5600`. A caller
  that gets this reply has already changed the set point.
* `S` is a **state-changing read** when autostart is armed (`A<n>=8`):
  reading the status word after a permanent shut-off (KILL, trip, ERR/INH)
  is enough to make the unit restore the previous set voltage on its own.
  Anything that polls `S` checks `A` first.

## Safety features of the CLI

* **Autostart guard**: `dump`, `ramp`, `watch` and `mon S` all read `A<ch>`
  before touching `S`, and refuse to proceed if autostart is armed unless
  `--yes` is given — because reading `S` on an autostart-armed channel can
  by itself restore a previously-set voltage.
* **MAN / OFF refusal**: `ramp` checks the channel's status word and refuses
  to start a ramp while the channel reports `MAN` (CONTROL switch in manual)
  or `OFF` (HV-ON switch off), unless `--yes`.
* **Hardware limit check**: `ramp` reads `M<ch>` (the Vmax rotary switch
  position) and refuses a target above `M% x Vmax` before ever sending `D`.
* **Software ceiling**: `--max-v`, default **1300 V**, applied to every set
  point regardless of `--yes` — because the Vmax rotary switch only steps
  in 10 % of the module maximum and cannot express an arbitrary limit like
  1300 V. `--max-v 0` disables it.
* **Read-back before `G`**: after writing `D`, `set_voltage_guarded` reads
  `D` back before starting the ramp, so a clamped/UMAX set point (see
  above) is caught before the unit ramps to the wrong number.
* **`off` as recovery**: sets the channel's `D` to 0 and starts the ramp
  down — the standard way back to a known-safe state after any of the
  above trips.
* **Exclusive port lock**: opening the port takes `flock(LOCK_EX | LOCK_NB)`
  on the fd (the MIDAS frontend takes the same lock), so the CLI and the
  frontend can never talk on one port at once. If the port is held the CLI
  prints `port <path> in use (MIDAS frontend running? stop scfe first)` to
  stderr and exits 3; stop `scfe` (or the other CLI) and retry.

## Running it

This is the **manual path**: no daemon involved, install is "copy the
directory" (there is nothing to `pip install`), and it is what a shifter
runs by hand if the MIDAS frontend (`IsegHV`, see "MIDAS frontend" below)
is down. Stop `scfe` first: it holds the port lock.

```sh
cd beamtime2026_pie5/drivers/iseg_nhq

# identity, break time
./iseg_nhq_probe.py --port /dev/ttyUSB0 info

# every readable value on both channels
./iseg_nhq_probe.py --port /dev/ttyUSB0 dump --ch 1 2

# single value
./iseg_nhq_probe.py --port /dev/ttyUSB0 mon U --ch 2

# write one command letter (refused without --yes for D and A=8)
./iseg_nhq_probe.py --port /dev/ttyUSB0 --yes set V 20 --ch 2

# set a voltage and start the ramp (checked against A, M x Vmax, --max-v, and MAN/OFF)
./iseg_nhq_probe.py --port /dev/ttyUSB0 ramp --ch 2 --to 50 --speed 20

# poll S, U, I at 1 Hz until the channel settles: the status word has to
# reach ON (or OFF) *and* the measured U has to come within --settle-v of D
# (default 2 V). The 208L says S=ON as soon as its internal set point is
# reached while the read-back is still a few volts out and moving -- on the
# bench (2026-09-21) it reported ON at U = -46 V for D = 50, and at
# U = -25 V for D = 0, catching up a few seconds later. --settle-v 0 stops
# on the status word alone; S=OFF ends the watch at once either way.
./iseg_nhq_probe.py --port /dev/ttyUSB0 watch --ch 2 --settle-v 2

# ramp channel 2 back to 0 V (the recovery path)
./iseg_nhq_probe.py --port /dev/ttyUSB0 off --ch 2

# hand-written command line, no guards at all
./iseg_nhq_probe.py --port /dev/ttyUSB0 raw 'S2'

# raise the software ceiling for a target above the default 1300 V
./iseg_nhq_probe.py --port /dev/ttyUSB0 --max-v 1600 ramp --ch 2 --to 1500
```

Common options: `--port` (default `/dev/ttyUSB0`), `--timeout` (inter-
character deadline for an answer, raised automatically if the unit's break
time asks for more), `--echo-timeout` (per-byte echo timeout), `--max-v`
(see above), `--verbose` (every byte both ways, to stderr).

Exit codes: `0` fine, `1` the unit answered an error line (or something
unparsable), `2` the tool refused locally or the arguments were wrong,
`3` I/O trouble (timeout, echo out of step, link died, cannot open the port).

### Against the fake, no hardware needed

```sh
cd beamtime2026_pie5/drivers/iseg_nhq

./fake_iseg_nhq.py --break-time 3 > /tmp/iseg_pty.txt 2> /tmp/iseg_fake.log &
sleep 1; PORT=$(head -1 /tmp/iseg_pty.txt)

./iseg_nhq_probe.py --port "$PORT" info
./iseg_nhq_probe.py --port "$PORT" dump --ch 1 2
./iseg_nhq_probe.py --port "$PORT" --yes ramp --ch 2 --to 20
./iseg_nhq_probe.py --port "$PORT" watch --ch 2

# the same with the read-back lag of the real unit: U answers the model
# voltage of 3 s ago while S already says ON, so watch has to wait for it
./fake_iseg_nhq.py --meas-lag 3 > /tmp/iseg_pty.txt 2> /tmp/iseg_fake.log &

kill %1
```

### Tests

```sh
cd beamtime2026_pie5 && python3 -m unittest discover -s drivers/iseg_nhq/tests
```

Stdlib `unittest`, no hardware — each test starts its own emulator on a
fresh pty. Runs in about **31 s**, **75 tests**.

## Hardware log — 2026-09-21

Bench, on pinky, over the **Keyspan USA-19H** adapter at `/dev/ttyUSB0`,
9600 8N1. The Prolific PL2303 adapter was tried first and was silent on
both the laptop (WSL/usbipd) and pinky — an adapter fault, not the unit;
the laptop also cannot drive the Keyspan because the WSL kernel (6.18) has
no `keyspan` module, so this whole session ran from pinky.

### Raw link test

```
sync CRLF echoed OK; post-sync drain: S2=ON  (stale unsolicited line, left over from an earlier session)
#  -> 481198;2.06;8000V;1000uA
W  -> 003
S2 -> echo of \n arrived together with "S2=ON ", answer immediately follows the echoed LF, no blank line
```

### Read-only dump, both channels (second run; sync CRLF this time answered with `????`)

| command | channel 2 (B, in use) | channel 1 (broken/unused) |
|---|---|---|
| `A` | 000 | 000 |
| `T` | 000 | 011 — 8 HV-ON switch OFF, 2 MAN control, 1 display U |
| `S` | S2=ON | S1=OFF |
| `M` | 070 % | 050 % |
| `N` | 070 % | 020 % |
| `D` | 0000 | 0000 |
| `V` | 002 V/s | 002 V/s |
| `L` | 0000 | 0000 |
| `U` | -0000 | -0000 — standard-series format, sign + 4-digit integer volts; negative polarity |
| `I` | 0000-6 | 0000-6 — 4-digit mantissa, exponent -6, i.e. 1 uA resolution |

Channel 1's "broken" symptom, as observed: it answers the protocol
normally, it is just parked at HV-OFF and manual control (`T1=011`). It is
not silent and does not answer an error code — nothing further was done
with it.

### 50 V ramp sequence, channel 2, open output, negative polarity

Driven with the raw commands directly (four invocations, before `ramp` had
its guards wired against this exact sequence):

```
V2=20 -> (empty line, write ack)
D2=50 -> (empty line, write ack)
G2    -> S2=L2H
      S2 -> S2=L2H     U2 -> -0025          (mid-ramp, ~20 V/s)
  ~5 s later:
      S2 -> S2=ON      U2 -> -0049   I2 -> 0000-6   D2 -> 0050
D2=0  -> (empty line, write ack)
G2    -> S2=H2L
      S2 -> S2=H2L     U2 -> -0036
  later:
      S2 -> S2=ON      U2 -> -0000   I2 -> 0000-6
```

Readback -49 V for a 50 V set point is within the 1 V display/measurement
resolution of the standard series. Every new process's sync `\r\n` was
answered by `????`, except right after a previous unfinished exchange, when
a stale answer line came back instead.

### UMAX clamp finding and recovery

```
D2=9999 -> ? UMAX@=5600          (stray '@' before '=', limit = M2 70% x 8000 V = 5600 V)
D2      -> 5600                  !! the unit CLAMPED the set point to the limit and STORED it;
                                     a following G2 would have ramped to -5600 V.
```

Recovery used: `D2=0`, then `G2` if the channel had already been started.
Autostart was `0` at the time, so nothing moved on its own.

```
D2=0 -> ack; D2 -> 0000   (set point cleared)
```

### Vmax rotary switch moves, channel B (final bench state)

| move | `M2` reads | hardware ceiling |
|---|---|---|
| 7 (as found) | 070 % | 5600 V |
| -> 1 | 010 % | 800 V |
| -> 2 (final) | 020 % | 1600 V |

Position 2 (1600 V hardware ceiling) plus the CLI's software ceiling
(`--max-v`, default 1300 V) is the bench state left at the end of this
session, 2026-09-21.

## MIDAS frontend

**Not yet hardware-tested.** It has run only against `fake_iseg_nhq.py`.
The first test on the real unit is in `DEPLOY-pinky.md`.

### What `IsegHV` is

`IsegHV` is a MIDAS equipment in the `scfe` slow-control frontend
(`scfe/scfe.cxx:199-218`), next to `CaenHV`. It uses MIDAS's stock `cd_hv`
class driver and the device driver `scfe/iseg_nhq_fe.{h,cxx}` (device name
`iseg NHQ`).

* **Event ID 9** (`scfe/scfe.cxx:200`).
* **One channel, named `S5`**, mapped to hardware channel 2. The ODB setting
  `Hardware Channel` (1 or 2, default 2) picks the unit output
  (`scfe/iseg_nhq_fe.cxx:173`). Channel 1 is never driven.
* **On and off are emulated.** The NHQ has no software on/off switch.
  `ChState` 1 writes `D2=<demand>`, reads it back, then sends `G2`.
  `ChState` 0 writes `D2=0` and sends `G2`, so the unit ramps to 0 V
  (`scfe/iseg_nhq_fe.h:136-139`).
* **The demand is kept while off.** A demand set while the channel is off is
  only stored. `Variables/Demand` shows the stored value, not 0 V
  (`scfe/iseg_nhq_fe.cxx:2114-2121`, `scfe/iseg_nhq_fe.h:136-139`).
* **Voltage ceiling** for every nonzero `D` write:
  `min(Voltage Limit, M% x Vmax, Max Voltage)`. `Voltage Limit` is the ODB
  setting. `M% x Vmax` is the Vmax rotary switch, read fresh from the unit
  before each write. `Max Voltage` is the driver setting, default 1300 V
  (`scfe/iseg_nhq_fe.cxx:1446-1467`). The driver reports this minimum as
  `Voltage Limit`, so MIDAS's `cd_hv` already clamps a higher VSET to it
  without a message. A set point that still reaches the driver above the
  ceiling (e.g. the Vmax switch was turned down since) is refused with a
  message and nothing is sent.
* **UMAX.** If the unit answers `? UMAX`, it has already clamped and stored
  the set point. The driver writes `D2=0` at once and posts an error
  (`scfe/iseg_nhq_fe.cxx:1580`).
* **One ramp register.** The NHQ has a single ramp speed `V2`, for up and
  down. `Ramp Up Speed` writes it (range 2 to 255 V/s). A write to
  `Ramp Down Speed` is refused with a message
  (`scfe/iseg_nhq_fe.cxx:2277`, `scfe/iseg_nhq_fe.cxx:2246`).
* **Current limit.** `Current Limit` writes the unit's trip register `L2` in
  microamps (1 count is 1 uA on this unit). 0 means no trip
  (`scfe/iseg_nhq_fe.cxx:2180-2217`). `Trip Time` is not supported and is
  ignored.
* **Autostart guard.** The driver reads `A2` at every connect. If it is not
  0, the driver never reads `S2` (a read can restore a shut-off voltage by
  itself), refuses every write except switching off, and sets the AUTOSTART
  status bit (`scfe/iseg_nhq_fe.h:53-59`, `scfe/iseg_nhq_fe.cxx:1424`).
  Switching off (ChState 0, also one latched while the link was down) writes
  `D2=0` only: with autostart (`A2=8`) the unit ramps to a new set point by
  itself, so no `G2` is sent (`scfe/iseg_nhq_fe.cxx:1785`). Clear autostart
  with the CLI (`set A 0`) while `scfe` is stopped, then restart `scfe`.
* **Magnitudes and Polarity.** Every voltage and current in ODB is a
  magnitude. The sign is published in `Variables/Polarity` from the unit's
  `T2` polarity bit (`scfe/iseg_nhq_fe.h:176-187`). S5 is negative.
* **Nothing is written at start or reconnect.** The driver reads `D2`, `L2`,
  `V2` and `M2` and adopts them. `ChState` starts as `D2 != 0`. The values
  MIDAS writes back at start are dropped when they equal what was read. A
  frontend restart never moves the voltage
  (`scfe/iseg_nhq_fe.h:136-146`). If the unit is found running above the
  `Voltage Limit`, the driver posts an error and does not ramp it down
  (`scfe/iseg_nhq_fe.cxx:1977`).
* **Trip rule.** While the unit shows TRP, ERR or INH, a `Demand` change is
  refused. A `G` would release the shut-off and ramp straight back up. The
  message is `S5 tripped (...): Demand not applied, set ChState 0 then 1`.
  Recover by setting `ChState` to 0 and then to 1
  (`scfe/iseg_nhq_fe.cxx:2158-2163`).
* **Unit on, ChState 0.** If the driver finds the unit on but `ChState` reads
  0, it refuses `Demand` until you set `ChState`
  (`scfe/iseg_nhq_fe.cxx:2150-2155`). Setting it to 1 keeps the unit on.
* **Front panel.** ON and Demand are refused when the HV-ON switch is off or
  CONTROL is on manual (`scfe/iseg_nhq_fe.cxx:1649`).
* **Zero threshold.** The driver asks `cd_hv` for a zero threshold of -1 V,
  so a trip to 0 V shows in `Variables/Measured` at once. `cd_hv` asks only
  when `Settings/Zero Threshold` is missing. On an existing tree set
  `/Equipment/IsegHV/Settings/Zero Threshold[0]` to -1 by hand
  (`scfe/iseg_nhq_fe.h:153-156`).

ODB driver settings, under
`/Equipment/IsegHV/Settings/Devices/iseg NHQ/` (`scfe/iseg_nhq_fe.cxx:171-176`):

| key | default | meaning |
|---|---|---|
| `Port` | `/dev/ttyUSB0` | serial device |
| `Hardware Channel` | 2 | unit output, 1 or 2 |
| `Max Voltage` | 1300 | software ceiling in V; 0 or less switches it off |
| `Echo Timeout ms` | 300 | wait for the echo of each byte |

### Status word

`Variables/ChStatus` is one DWORD. Bits 0-10 are the unit's `S` text (one
bit set), bits 17-23 are the `T` byte, bit 24 AUTOSTART, bit 25 TOT, bit 26
DSET (a set point is applied), bit 31 STALE (the last read failed)
(`scfe/iseg_nhq_fe.h:8-24`). The bit numbers live in `scfe/iseg_nhq_fe.h`
only. The page keeps a copy (`custom/caenhv.html:108-114`).

### Alarms

`IsegHV` has its own alarms, `IsegHV Ch0` and `IsegHV Comm`. They use the
shared `HV Alarm` class, so they go wherever that class goes (Slack on
pinky). Keys are under `/Equipment/IsegHV/Settings/Alarm/`
(`scfe/hv_alarm.cxx:365-383`, defaults from `scfe/scfe.cxx:110-127`):

| key | default | meaning |
|---|---|---|
| `Enabled` | y | switch the alarms of this equipment on or off, read every second |
| `Voltage Max` | 1300 V | `over voltage` above this |
| `Current Max` | 350 uA | `over current` above this |
| `Deviation Max` | 20 V | `deviation` when the difference between Demand and Measured is above this ... |
| `Deviation Hold s` | 30 | ... for this many seconds in a row, only while ChState is 1, the unit is on and not ramping |
| `Status Mask` | fault bits | ERR, INH, TRP, unknown S text, T ERR, T INH, AUTOSTART, TOT (`scfe/iseg_nhq_fe.h:88-92`) |
| `Clear After s` | 10 | how long the fault must stay away before the alarm clears |
| `Comm Timeout s` | 60 | how long the link must be dead before `IsegHV Comm` fires |

The reason text of `IsegHV Ch0` lists what is wrong: `over current`,
`over voltage`, `status <bits>`, `deviation N V`, `on but ChState OFF`
(voltage on the output while `ChState` is 0 for 5 s in a row, not ramping;
`scfe/hv_alarm.cxx:815-819`, `scfe/hv_alarm.cxx:156`).

Separate from the alarms, `hv_alarm` posts one error message when `ChState`
is 1 but the unit is not on for 5 s: `ChState ON but board says off (STAT
...) - check HV-ON/KILL switches, CONTROL on DAC`
(`scfe/hv_alarm.cxx:845-861`, `scfe/scfe.cxx:112`). A switch-on that the
driver refuses or that fails (Demand 0 or unknown, front panel, autostart,
ceiling, no link) unticks the box itself within a poll and says why, so this
message is left for a unit that went off by itself with the box ticked (see
`iseg_on_refused` in `scfe/iseg_nhq_fe.cxx`).

### Running it

**Through the frontend.** `IsegHV` runs inside the same `scfe` binary as
`XYTable`, `Degrader` and `CaenHV`. On pinky that is the MIDAS program
`SlowControl`. Start and stop it from the mhttpd **Programs** page. The
first hardware test starts a hand-built copy in tmux instead; see
`DEPLOY-pinky.md`. To build, see `drivers/caen_hv/DEPLOY-pinky.md` section 2
(same `cmake -S scfe -B scfe/build`).

**By hand, without MIDAS (the manual path).** The CLI in this directory does
the same job on one channel. Use it when `scfe` is down and the voltage has
to change. The commands are under "Running it" above.

**The port lock.**

* The frontend and the CLI both take `flock(LOCK_EX | LOCK_NB)` on the port.
  Only one can hold it.
* While `scfe` holds the port the CLI prints
  `port <path> in use (MIDAS frontend running? stop scfe first)` and exits
  with code 3 (`drivers/iseg_nhq/iseg_nhq_probe.py:157-163`, `:1326-1328`).
* So: stop `scfe` first, then run the CLI, then start `scfe` again. Stopping
  `SlowControl` also stops `XYTable`, `Degrader` and `CaenHV` control.
  The voltage on the supplies stays where it is.
* `flock` only excludes openers of the same device node. Run the CLI on the
  same host, and in the same container, as `scfe`. A host and a container
  with their own `/dev` nodes do not see each other's lock
  (`scfe/iseg_nhq_fe.h:157-160`).
* The old copy `~/bt2026/ch5_voltage_tmp` on pinky has **no lock**. Do not
  use it while `scfe` runs.
* After a CLI session, start `scfe`. It adopts the unit's `D2` and on/off
  state and writes nothing. If `ChState` in ODB disagrees, the driver posts
  a message (`S5 is on at N V but ChState shows OFF: set ChState (1 keeps it
  on)`) and refuses `Demand` until you set `ChState` to match
  (`scfe/iseg_nhq_fe.cxx:1313`).

### Shifter guide: S5 high voltage

Assumes the DAQ is idle (no run going). The S5 PMT is on channel 2 of the
NHQ. It is negative. The page shows magnitudes: type 1230, not -1230.

**1. Open the page.** In the mhttpd left menu open the HV page. On pinky
before the changeover it is still called **CaenHV**. During the first
hardware test it is **HV-test**. The page has two tables. The lower one
is **iseg NHQ 208L - S5 PMT**. It refreshes every second.

**2. Read the row.** Columns: Name, Pol (the sign, `-` for S5), VSET
(demand, V), VMON (measured, V), Trip (current trip, uA), IMON (measured
current, uA), V limit, Ramp (V/s, read only), On/Off, Status, Alarm.
Under the row: `Unit: comm alarm ok` and `HV alarms enabled y`.

**3. What the Status cell means** (from `custom/caenhv.html:126-182`):

| colour | text | meaning |
|---|---|---|
| grey | `no data` | nothing read yet, or the frontend is not running |
| purple | `stale (...)` | the last read from the unit failed; the words in brackets are the last good status. Check the serial cable and `Unit: comm alarm` |
| red | `AUTOSTART armed - frontend read-only` | the unit's autostart flag is set; MIDAS does not read the status word and refuses every change except switching off (untick On/Off still ramps S5 to 0 V). Stop `scfe`, use the CLI (`set A 0`), restart |
| red | `TRIPPED (current trip)` | the unit shut the output off because IMON went above Trip |
| red | `Vmax/Imax exceeded` | the unit's hard limit was hit |
| red | `INHIBIT` | the INHIBIT input is or was active |
| red | `unknown status text`, `unit timeout (re-initialised)` | the unit answered something odd; see Messages |
| orange | `HV-ON switch off` | front panel: output disabled. MIDAS cannot switch it on |
| orange | `manual (CONTROL not on DAC)` | front panel: CONTROL switch is up. Commands have no effect |
| orange | `KILL enabled` | the KILL switch is in the enable position; the unit cuts the output hard if it triggers |
| green | `ramping up` / `ramping down` | the output is moving to VSET (or to 0 V) |
| green | `ON` | the output has reached a nonzero VSET. `(QUA)` added: the output quality is not given at present |
| light grey | `0 V set` | the unit reports ON but the set point is 0 V. This is the switched-off state |
| light grey | `off` | no status bit is set |

Orange can be followed by `; ON`, `; ramping up`, etc., which says what the
output is doing behind the switch.

**4. Switch on.**

1. Check VSET. To change it, click the VSET cell, type the voltage in V
   (magnitude), press Enter.
2. Tick the On/Off box. A dialog asks for confirmation. Click yes.
3. Status goes green `ramping up`, then `ON`. VMON follows VSET at the Ramp
   speed. IMON for the S5 PMT at nominal voltage is around 300 uA.
4. If Status stays orange or nothing moves, read the MIDAS **Messages**
   page. The frontend says why (front panel switch, autostart, ceiling).

**5. Switch off.** Untick the box. No confirmation. The unit writes 0 V and
ramps down (green `ramping down`, then light grey `0 V set`). VSET keeps its
value, so ticking the box again returns to the same voltage.

**6. Change the voltage.**

* While on: type the new value in VSET. It is applied at once and the
  unit ramps. Do not change it by more than you need.
* While off: the value is only stored.
* A value above `V limit` is clamped to `V limit` by MIDAS without a message:
  VSET snaps back to the limit and the unit goes there. Check VSET after
  typing. (If the Vmax switch was turned down, Messages may instead say
  `N V refused: limit L V = min(...)`; type an allowed value.)
* Do not change the ramp speed unless told to. Use the CLI or ODB
  (`Ramp Up Speed`); `Ramp Down Speed` cannot be changed.

**7. Alarms.** An alarm shows in the Alarm cell, on the mhttpd banner and in
Slack. The alarm text names the reasons.

| alarm text | what it means | what to do |
|---|---|---|
| `IsegHV Ch0`: `over current` | IMON is above `Current Max` (350 uA) | Look for a light leak or a bad base. If IMON keeps rising, switch off (untick). Tell the run coordinator |
| `IsegHV Ch0`: `over voltage` | VMON is above `Voltage Max` (1300 V) | Switch off. Do not raise VSET. Tell the run coordinator |
| `IsegHV Ch0`: `status ...` (TRP, ERR, INH, AUTOSTART, TOT) | a fault bit is set | TRP: see step 8. ERR: Vmax or Imax hit, switch off and call the expert. INH: check the INHIBIT cable. AUTOSTART: use the CLI, see the Status table. TOT: the unit re-initialised itself, check the state before switching on |
| `IsegHV Ch0`: `deviation N V` | VMON has been more than 20 V from VSET for 30 s while on and not ramping | The read-back of the NHQ can lag by up to about 25 V after a ramp, so wait one minute. If it stays, switch off and call the expert |
| `IsegHV Ch0`: `on but ChState OFF` | the output carries voltage but the On/Off box is unticked. Someone used the front panel or the CLI, or a switch-off did not take | Do not just tick the box. First read the actual state: Status, VMON, and the front panel. Then tick the box (keeps it on) or switch the HV off at the front panel HV-ON switch |
| `IsegHV Comm` | the frontend has had no data from the unit for 60 s (`status stale`, or no readings) | Check the USB-serial adapter and cable on pinky (`ls /dev/ttyUSB*`), and that the unit is powered. Messages says `HV ... lost`. The frontend reconnects by itself every 5 s and posts `back:` when it is back. If it does not come back, use the CLI (step 9) |

A separate error message (not an alarm) `ChState ON but board says off
(STAT ...)` means the box is ticked but the unit is not on. Check the HV-ON
switch, KILL and CONTROL on the front panel.

**8. Recover after a trip.**

1. Status is red `TRIPPED`. The output is 0 V. A Demand change is refused
   (`S5 tripped ...: Demand not applied, set ChState 0 then 1`).
2. Find the cause first (light on the PMT, cable, rate). Do not just retry.
3. Untick On/Off (ChState 0). Status goes to `0 V set`.
4. Check VSET, then tick On/Off (ChState 1) and confirm. The output ramps
   back up.
5. If it trips again, leave it off and call the expert.

**9. When `scfe` is down (CLI fallback).** Symptoms: Status grey `no data`
for good, and the `SlowControl` program is not running on the Programs page.

1. On pinky, from the checkout that has the code:
   `cd ~/bt2026/beamtime2026_pie5/drivers/iseg_nhq`.
2. Check that `scfe` is really stopped (the CLI exits 3 if it is not).
3. Look at the unit: `./iseg_nhq_probe.py --port /dev/ttyUSB0 dump --ch 2`.
4. Set the voltage (negative unit, positive number):
   `./iseg_nhq_probe.py --port /dev/ttyUSB0 ramp --ch 2 --to 1230 --speed 20`
   (add `--max-v` only if told to go above 1300 V).
5. Switch off: `./iseg_nhq_probe.py --port /dev/ttyUSB0 off --ch 2`.
6. Start `SlowControl` again from the Programs page. It adopts what the
   unit is doing. Set `ChState` to match if a message asks for it.

Use `--port` as in the ODB `Port` setting. Do not use `~/bt2026/ch5_voltage_tmp`.

## Manuals

`doc/iseg_NHQx2x_RS232_manual_v3.06.pdf` (precision series) and
`doc/iseg_NHQx0x_RS232_manual_v2.04.pdf` (standard series). "208L" is in
neither manual's model table — the `#` identity reply gives the real
Vmax/Imax, which is what the tools use, never the model name.
