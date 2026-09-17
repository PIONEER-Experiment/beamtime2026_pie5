# CAEN HV (DT1470ET) probe, emulator and udev rule

Device-side helpers for the `scfe` CAEN HV slow-control equipment. Python 3.10+,
**standard library only** (`os.open` + `termios`, no pyserial), so the same files run on
the PSI DAQ machine and inside the `pioneer-midas` / `testbeam-midas` container.

| file | what it is |
|---|---|
| `caen_hv_protocol.py` | the one parameter / STAT-bit table, plus framing helpers, shared by both tools |
| `caen_hv_probe.py` | CLI client: `info`, `dump`, `mon`, `set`, `raw`; prints the raw wire exchange with `--verbose` |
| `fake_caen_hv.py` | pty emulator: ramps, currents, trips, `LOC:ERR`, forced STAT bits; logs every `SET` to stderr |
| `99-caen-hv.rules` | udev rule giving the USB device a stable `/dev/caen_hv0` |
| `tests/test_fake_with_probe.py` | stdlib `unittest`: drives the emulator through the probe's module API |

## Protocol summary — **as documented, to be confirmed on hardware**

Everything below is the published N1470-family ASCII protocol
(N1470 / N1470ET / DT1470ET / R1470ET / DT55xxE). It has **not yet been checked against our
unit**; verification step 1 of the plan (`caen_hv_probe.py info dump` on the real board) is
what confirms it. If anything differs, fix `caen_hv_protocol.py` — both tools and the C++
driver's constants follow from that one table.

Lines are `\r\n`-terminated; both tools accept `\r`, `\n` or `\r\n` and strip it.

```
request  $BD:00,CMD:MON,CH:0,PAR:VMON            reply  #BD:00,CMD:OK,VAL:1234.5
request  $BD:00,CMD:SET,CH:0,PAR:VSET,VAL:1000   reply  #BD:00,CMD:OK
request  $BD:00,CMD:MON,PAR:BDNAME               reply  #BD:00,CMD:OK,VAL:DT1470ET
```

Board-level parameters carry **no `CH` field**. Errors come back as `ERR` in the offending
field:

| reply | meaning |
|---|---|
| `#BD:00,CMD:ERR` | unknown or malformed command |
| `#BD:00,CH:ERR` | bad / missing channel |
| `#BD:00,PAR:ERR` | bad parameter (or read-only on SET) |
| `#BD:00,VAL:ERR` | bad value (out of range, or `VSET > MAXV`) |
| `#BD:00,LOC:ERR` | board is in **LOCAL** mode — every `SET` is refused |

Channel parameters:

| MON | SET | unit | note |
|---|---|---|---|
| `VSET` | yes | V | set point, **magnitude** (polarity is a rear switch) |
| `ISET` | yes | uA | current limit, board trips after `TRIP` seconds |
| `VMON` | — | V | measured voltage |
| `IMON` | — | uA | measured current |
| `MAXV` | yes | V | hardware voltage limit (non-volatile on the board) |
| `RUP` / `RDW` | yes | V/s | ramp-up / ramp-down speed |
| `TRIP` | yes | s | over-current trip time |
| `PDWN` | yes | `KILL`\|`RAMP` | power-down mode |
| `POL` | — | `+`\|`-` | read-only, reversible by a physical switch |
| `STAT` | — | integer | bitmask, see below |
| — | `ON` / `OFF` | — | no `VAL` field |

Board parameters: MON `BDNAME BDNCH BDFREL BDSNUM BDCTR`(`LOCAL`\|`REMOTE`)
`BDTERM BDILK BDILKM BDALARM`; SET `BDILKM`(`OPEN`\|`CLOSED`) and `BDCLR` (no `VAL`).

`STAT` bits:

| bit | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| name | `ON` | `RUP` | `RDW` | `OVC` | `OVV` | `UNV` | `MAXV` |

| bit | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
|---|---|---|---|---|---|---|---|
| name | `TRIP` | `OVP` | `OVT` | `DIS` | `KILL` | `ILK` | `NOCAL` |

Serial settings: **9600 8N1, raw**. The ET-generation boards enumerate as USB CDC-ACM
(`/dev/ttyACM<n>`), which ignores the baud rate; the setting is kept so the same code works
on an FTDI-based N1470 on `/dev/ttyUSB<n>`.

## Attaching the device

### PSI DAQ machine (native Linux)

```sh
sudo cp 99-caen-hv.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG dialout "$USER"      # log out and in again
ls -l /dev/caen_hv*                   # -> symlink to ttyACM0
```

### This laptop (Windows + WSL2)

Windows side, in an **administrator** PowerShell:

```powershell
usbipd list                           # find the 21e1:0003 busid
usbipd bind   --busid <busid>         # once per device
usbipd attach --wsl --busid <busid>   # after every reboot / replug
```

WSL side:

```sh
sudo modprobe cdc_acm                 # kernel has CONFIG_USB_ACM=m
lsusb | grep 21e1                     # 21e1:0003 CAEN SPA
ls -l /dev/ttyACM0
sudo chmod 666 /dev/ttyACM0           # for this session only...
# ...or install 99-caen-hv.rules as above, which also works under WSL
```

Container: the running `testbeam-midas` has no `--device`, so it must be recreated once with
`--device /dev/ttyACM0` (see verification step 7.2 of the plan). The emulator needs nothing —
its pty lives inside whatever container it runs in.

## Running the tools

```sh
cd beamtime2026_pie5

# board identity and interlock state
drivers/caen_hv/caen_hv_probe.py --port /dev/caen_hv0 info

# every parameter of every channel, STAT decoded, as an aligned table
drivers/caen_hv/caen_hv_probe.py --port /dev/caen_hv0 dump

# single parameter; "bd" instead of a channel number for board parameters
drivers/caen_hv/caen_hv_probe.py --port /dev/caen_hv0 mon 0 VMON
drivers/caen_hv/caen_hv_probe.py --port /dev/caen_hv0 mon bd BDCTR

# SET is refused without --yes
drivers/caen_hv/caen_hv_probe.py --port /dev/caen_hv0 --yes set 0 VSET 500
drivers/caen_hv/caen_hv_probe.py --port /dev/caen_hv0 --yes set 0 ON

# hand-written command; the "$BD:nn," prefix is added when missing
drivers/caen_hv/caen_hv_probe.py --port /dev/caen_hv0 raw 'CMD:MON,PAR:BDFREL'
```

Common options: `--port` (default `/dev/ttyACM0`), `--bd` (default 0), `--timeout` (default
1.0 s), `--verbose` (prints every raw request and reply to stderr).

Exit codes: `0` ok, `1` the board answered `*:ERR` (request and raw reply are printed),
`2` refused locally (missing `--yes`, read-only parameter), `3` I/O error or timeout.

Emulator options: `--bdname` (default `DT1470ET`), `--nch` (4), `--bd` (0),
`--load-ohm` (1e6, i.e. `IMON` = 1 uA per volt), `--fault-current UA` (forces `IMON`),
`--stat-bits MASK` (extra `STAT` bits OR'ed in; accepts `0x..`), `--pol -,+,+,-`,
`--local` (all `SET`s answer `LOC:ERR`), `--clip-vset` (clip `VSET` to `MAXV` instead of
answering `VAL:ERR`). It prints the pty path as the first stdout line and logs every `SET`
to stderr with a timestamp.

## Using the CaenHV equipment (shifters)

Once `scfe` is running, mhttpd's built-in equipment page for `CaenHV` renders the
`/Equipment/CaenHV/Variables` arrays directly (`cd_hv` fills `Settings/Editable`, so no custom
page was built). Per channel you will see:

- **Demand** [V] — the requested set point; editable.
- **Measured** [V] — VMON, ramping toward Demand.
- **Current** [uA] — IMON.
- **ChState** — on/off; editable.
- **ChStatus** — the raw STAT bitmask (see decoding below).
- **Polarity** — `+1`/`-1`, read-only, mirrors the rear physical switch.

All voltages and currents are **magnitudes**; the sign lives only in `Polarity`. This matches
the board itself, which shows unsigned `VSET`/`VMON`.

### Setting a voltage / switching a channel

- Set a voltage: write `/Equipment/CaenHV/Variables/Demand[<ch>]` (or the mhttpd form field).
  If the value is above `Settings/Voltage Limit[<ch>]`, MIDAS corrects `Demand` back down to
  the limit and the board's `MAXV` is set to the same limit — you cannot get a demand through
  that exceeds the software limit.
- Switch a channel on/off: write `/Equipment/CaenHV/Variables/ChState[<ch>]` (0/1).
  Switching **on** is only forwarded once the frontend has read the board's status at least
  once (so a channel is never switched on from a guessed state); switching **off** is always
  forwarded. A request to switch on that cannot be confirmed is logged as an error and dropped.
- **Current Limit** (`Settings/Current Limit[<ch>]`, uA) is sent to the board's `ISET`; the
  board itself trips the channel after `Settings/Trip Time[<ch>]` seconds of over-current —
  this protection works even if the frontend is down.
- **Ramp Up/Down Speed** (`Settings/Ramp Up Speed[<ch>]` / `Ramp Down Speed[<ch>]`, V/s) set
  `RUP`/`RDW` on the board; the board does the ramping itself (`DF_HW_RAMP`), not the frontend.

### Alarms

Settings live under `/Equipment/CaenHV/Settings/Alarm/`:

| key | meaning |
|---|---|
| `Enabled` | master on/off for the frontend-side alarm check |
| `Voltage Max[4]` | per-channel V threshold |
| `Current Max[4]` | per-channel uA threshold |
| `Status Mask` | STAT bits that count as an alarm condition (default `OVC\|OVV\|TRIP\|OVP\|OVT\|ILK`) |
| `Clear After s` | how long the condition must be gone before the alarm auto-clears |
| `Comm Timeout s` | how long every channel may read NaN before a comm alarm fires |

Two alarms per channel/equipment show up under class **"HV Alarm"**:

- **`CaenHV Ch<i>`** — that channel's Measured exceeds Voltage Max, Current exceeds Current
  Max, or a `Status Mask` bit is set in ChStatus. The message names the channel and the reason
  (voltage/current/status bits).
- **`CaenHV Comm`** — every channel has read NaN (board unreachable) for longer than
  `Comm Timeout s`.

Clearing: the frontend checks once a second and calls `al_reset_alarm` automatically once the
bad condition has been gone for `Clear After s` (or, for Comm, as soon as a real reading comes
back). There is **no private latch** — if you reset the alarm in mhttpd while the condition is
still true, it re-triggers on the alarm's own `Check interval` (a MIDAS alarm setting, default
60 s), not instantly, so it can look "stuck cleared" for up to that interval.

Two global gates silence *all* alarms regardless of the above: `/Runinfo/Online Mode` and
`/Alarms/Alarm system active`. System messages for this alarm class are rate-limited by
`System message interval` under `/Alarms/Classes/HV Alarm` — one `TALK` line per burst, not
one per second.

### `LOC:ERR` / stale status / missing port

- If the serial port does not exist when `scfe` starts (cable not yet attached), the equipment
  still comes up (one error message, values NaN, `ChStatus` = `0x80000000`); the driver retries
  opening the port every 5 s and logs one info message when the board answers. The same
  applies to a cable pulled at run time — no frontend restart is needed.
- If the board's front-panel/touchscreen is in **LOCAL** mode, every `SET` command is refused
  (`#BD:00,LOC:ERR`) and the frontend logs "board in LOCAL mode, sets are ignored". Put the
  board in **REMOTE** on its touchscreen to accept ODB/mhttpd changes again; monitoring
  (Measured/Current/ChStatus) keeps working either way.
- `Variables/Current` is never NaN: while the board is unreachable it keeps the last good
  reading, and a channel whose current has **never** been read shows **-1 uA** (a magnitude
  cannot be negative, so -1 unambiguously means "no reading yet"; it also appears in the
  database). Reason: the MIDAS `hv` class driver stops updating `Current` for the rest of the
  session once it has seen a NaN there (`hv.cxx`, Current block has no NaN rescue).
- `ChStatus` bit **31** is a driver-private "stale" flag (not a real device bit): it is set
  when the last STAT read failed, so a value like `0x80000009` means bits 0 (ON) and 3 (OVC)
  are the last known-good reading, but they may be out of date — comms are down (watch for the
  `CaenHV Comm` alarm).

### Restart behaviour

The board is the source of truth: `scfe` uses `DF_PRIO_DEVICE`, so **restarting the frontend
never changes the output voltage**. On start, ODB `Demand` and the various limits are refilled
from whatever the board currently has (VSET, MAXV, ISET, RUP/RDW, TRIP, on/off state) — stale
ODB values from before the restart are overwritten, not pushed to the supply.

### Decoding ChStatus (to be confirmed on hardware)

Bit numbers below come from the documented N1470-family manual
(`drivers/caen_hv/caen_hv_protocol.py`, `scfe/caen_hv_fe.cxx`) and are unverified against the
real DT1470ET until verification step 1 (`caen_hv_probe.py info dump`) is run:

| bit | name | meaning |
|---|---|---|
| 0 | ON | channel is on |
| 1 | RUP | ramping up |
| 2 | RDW | ramping down |
| 3 | OVC | over current |
| 4 | OVV | over voltage |
| 5 | UNV | under voltage |
| 6 | MAXV | at MAXV / set point clipped |
| 7 | TRIP | tripped |
| 8 | OVP | over power |
| 9 | OVT | over temperature |
| 10 | DIS | disabled |
| 11 | KILL | killed |
| 12 | ILK | interlocked |
| 13 | NOCAL | not calibrated |
| 31 | (driver-private) | last STAT read failed ("stale"), not a board bit |

### Testing on the laptop

A full end-to-end chain without hardware — or with the real board over WSL/USB passthrough —
is kept at `scratch/caen-hv-standalone/start.sh`: a standalone MIDAS experiment (`caenhv`) that
runs `scfe` alone (XYTable/Degrader disabled in its ODB) with `mhttpd` on `localhost:8080`. It
requires the `testbeam-midas` container to be recreated once with the CAEN device passed
through and port 8080 published; `start-midas-container.sh` already does this automatically
(`[ -e /dev/ttyACM0 ] && DEV="--device /dev/ttyACM0"`) whenever `/dev/ttyACM0` exists on the
host at container-creation time — recreate the container (`docker rm testbeam-midas` then
re-run the start script) after plugging the device in for the first time. See verification
steps 7.1–7.4 of the CAEN HV frontend plan for the full walk-through (WSL `usbipd`
attach/`modprobe cdc_acm`, container recreate, standalone experiment start, and the control
checks with outputs unloaded and low voltage).

### Data logging

`python/pioneer/rundb/logger.py` picks up every `/Equipment/CaenHV/Variables/<Variable>` array
automatically (no logger changes needed) and logs it with the label
`CaenHV[<Variable>]:<Name>`, where `<Name>` comes from `Settings/Names[<ch>]`. Set
`Settings/Names` to the real detector channel names (not "HV%CH n") before a run so the
database rows are meaningful. Note: if the equipment is later renamed away from `CaenHV`,
`Settings` (including `Names`) does not carry over automatically — it must be re-entered under
the new equipment name.

## Verifying the emulator with the probe

Run this exact sequence; it is the no-hardware smoke test for both tools.

```sh
cd beamtime2026_pie5/drivers/caen_hv

# 1. start the emulator and remember its pty
./fake_caen_hv.py --pol -,+,+,- > /tmp/caen_pty.txt 2> /tmp/caen_fake.log &
sleep 1; PORT=$(head -1 /tmp/caen_pty.txt); echo "$PORT"

# 2. identity -> BDNAME DT1470ET, BDNCH 4, BDCTR REMOTE
./caen_hv_probe.py --port "$PORT" info

# 3. set a voltage and switch on (raw exchange visible)
./caen_hv_probe.py --port "$PORT" --verbose --yes set 0 VSET 600
./caen_hv_probe.py --port "$PORT" --yes set 0 ON

# 4. after ~1 s VMON has ramped ~50 V (RUP = 50 V/s), STAT = ON,RUP, IMON = VMON uA
sleep 1.2; ./caen_hv_probe.py --port "$PORT" dump

# 5. error paths: read-only, missing --yes, unknown parameter, VSET > MAXV
./caen_hv_probe.py --port "$PORT" --yes set 0 POL +      ; echo "rc=$?"   # 2
./caen_hv_probe.py --port "$PORT"        set 0 VSET 10   ; echo "rc=$?"   # 2
./caen_hv_probe.py --port "$PORT" mon 0 BOGUS            ; echo "rc=$?"   # 1, PAR:ERR
./caen_hv_probe.py --port "$PORT" --yes set 0 VSET 9999  ; echo "rc=$?"   # 1, VAL:ERR

# 6. every SET is in the emulator log, with a timestamp
kill %1; cat /tmp/caen_fake.log

# 7. LOCAL mode: MON works, every SET answers LOC:ERR
./fake_caen_hv.py --local > /tmp/caen_pty2.txt 2>/dev/null &
sleep 1; PORT2=$(head -1 /tmp/caen_pty2.txt)
./caen_hv_probe.py --port "$PORT2" mon 0 VMON               # 0.0
./caen_hv_probe.py --port "$PORT2" --yes set 0 VSET 100     # rc=1, #BD:00,LOC:ERR
kill %1

# 8. forced STAT bits: bit 3 = OVC
./fake_caen_hv.py --stat-bits 8 > /tmp/caen_pty3.txt 2>/dev/null &
sleep 1; ./caen_hv_probe.py --port "$(head -1 /tmp/caen_pty3.txt)" mon 0 STAT   # 8
kill %1
```

The same checks, automated:

```sh
cd beamtime2026_pie5
python3 -m unittest discover -s drivers/caen_hv/tests
```
