#!/usr/bin/env python3
"""Emulator for an iseg NHQ dual channel HV supply on a pty.

Prints the slave device path on the first line of stdout (flushed) and then
speaks the NHQ RS232 protocol for ever.  Standard library only.

    ./fake_iseg_nhq.py                        # 3000 V / 4 mA, precision format
    ./fake_iseg_nhq.py --fmt standard         # the NHQ x0x number format
    ./fake_iseg_nhq.py --manual               # CONTROL switch up: nothing moves
    ./fake_iseg_nhq.py --hv-off               # front panel HV-ON switch off
    ./fake_iseg_nhq.py --ch1-dead             # channel 1 never answers
    ./fake_iseg_nhq.py --meas-lag 3           # U trails the ramp by 3 s
    ./fake_iseg_nhq.py --corrupt-echo-every 3 # break the echo handshake once
    ./fake_iseg_nhq.py --preset 2:1230        # ch2 already on at 1230 V
    ./fake_iseg_nhq.py --autostart 2:8        # ch2 autostart armed (A2=8)
    ./fake_iseg_nhq.py --umax-limit 1000      # ? UMAX above 1000 V, M lies

The bench unit (NHQ 208L, 2026-09-21) is::

    ./fake_iseg_nhq.py --id 481198 --sw 2.06 --vmax 8000V --imax 1000uA \\
                       --fmt standard --mlimit 70 --nlimit 50 --break-time 3

What it reproduces, and why each piece matters to the client:

* **the echo handshake** -- every byte that arrives is echoed back at once,
  so ``IsegNHQ._send_echoed`` has something to wait for.  ``--echo-delay``
  slows the echo down, ``--corrupt-echo-every`` breaks it on purpose.
* **the break time** -- answer characters are written one at a time with
  ``W`` milliseconds of silence in between (3 ms from the factory), exactly
  as the real unit does, so the client's line reader really is exercised on
  dribbled input rather than on one tidy write.
* **the two number formats** -- the precision series (NHQ x2xx) answers
  ``+00500-01`` for 50 V, the standard series (NHQ x0x) ``+0050``;
  ``--fmt`` picks one.  Current is always mantissa and exponent in amperes.
* **the ramp** -- ``D`` only stores a set point; the output moves at ``V``
  V/s in a 10 Hz loop after ``G``, or straight away when autostart (``A=8``)
  is on.  ``S`` follows with ``L2H`` / ``H2L`` / ``ON``.
* **the read-back lag** -- ``--meas-lag S`` makes ``U`` answer the model
  voltage of ``S`` seconds ago while the status word still turns ``ON`` the
  moment the set point is reached, which is what the bench 208L does
  (2026-09-21: ``S=ON`` at U = -46 V for D = 50).  It is what the settle
  check in the client's ``watch`` is tested against.
* **the front panel winning** -- ``--manual`` and ``--hv-off`` keep
  answering commands while the output stays put, as the manual describes.
* **the restore latch** -- after a current trip the channel stays down until
  the status word has been read *and* ``G`` sent.  With autostart armed
  (``A<n>=8``) the read of the status word is enough on its own and the
  ramp restarts there and then, which is the trap ``--yes`` guards the
  client against (manual x2xx p.8).
* **the framing oddities seen on the bench** -- the unit answers the bare
  synchronisation ``\\r\\n`` with ``????`` (``--sync-answer``), can have a
  stale answer left over from an earlier session (``--stale-answer``), and
  prefixes the status word with ``S<n>=`` (``--bare-status`` for the shape
  the manual shows).  ``--extra-crlf``, ``--stray-line``, ``--tot-on-next``
  and ``--die-after`` stand in for the framing accidents the client has to
  survive but that no healthy unit produces on demand.
* **a unit that is already running** -- ``--preset CH:V`` starts channel
  ``CH`` with ``D`` = ``V`` and the output settled there, the state a MIDAS
  frontend restart finds the S5 PMT in; ``--autostart CH:A`` starts with the
  ``A`` flags already set, as if armed from an earlier session.
* **a UMAX the client could not see coming** -- ``--umax-limit V`` makes
  ``D`` above ``V`` answer ``? UMAX`` and clamp to ``V`` while ``M`` keeps
  reporting the rotary switch as before: a switch turned down between the
  client's ``M`` read and its ``D`` write, which no amount of checking on the
  client side can rule out.

Every write command (``X<n>=v``) and every ``G`` is logged to stderr with a
timestamp; ``--log-reads`` logs every other command line as ``READ <line>``
too, so a test can prove that something (``S<n>`` with autostart armed) was
never asked.
"""

from __future__ import annotations

import argparse
import errno
import math
import os
import pty
import re
import select
import sys
import termios
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import iseg_nhq_protocol as proto

UPDATE_HZ = 10.0

#: how close to the set point counts as "there" (V)
SETTLED_V = 1e-6

#: one command line: letter, optional channel digits, optional "=value"
_LINE_RE = re.compile(r"^([#A-Za-z])(\d*)(?:=(.*))?$")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
@dataclass
class Channel:
    """Mutable per-channel state, one per physical output."""

    number: int
    pol: str = "+"
    vset: float = 0.0        # D, the stored set point
    target: float = 0.0      # the set point the output is actually chasing
    vact: float = 0.0        # U, the output voltage right now
    ramp: int = 20           # V, software ramp speed in V/s
    trip: int = 0            # L, in units of the first current range; 0 = off
    auto: int = 0            # A, autostart / EEPROM flags
    mlimit: int = 100        # M, Vmax rotary switch in %
    nlimit: int = 100        # N, Imax rotary switch in %
    display: bool = True     # T bit 0
    tripped: bool = False    # permanent shut-off by the current trip
    status_read: bool = False  # S was read since the shut-off
    #: (monotonic time, vact) samples, newest last, kept only as far back
    #: as --meas-lag needs: what U answers is a sample out of here
    history: deque = field(default_factory=deque)


@dataclass
class Unit:
    """The whole module: two channels plus the front panel switches."""

    ident: str = "483216"
    software: str = "2.05"
    vmax_text: str = "3000V"
    imax_text: str = "4mA"
    fmt: str = "precision"
    break_ms: int = 3
    echo_delay: float = 0.0
    manual: bool = False
    hv_off: bool = False
    kill: bool = False
    error_bits: int = 0
    load_ohm: float = 1e9
    meas_lag: float = 0.0    # U trails the model voltage by this long
    trip_res: float = 1e-9   # one unit of L, in amperes
    dead_channels: tuple[int, ...] = ()
    corrupt_every: int = 0
    corrupt_left: int = 0
    clamp_d: float | None = None    # silently clamp the D set point here
    umax_limit: float | None = None  # UMAX threshold that M does not show
    umax_junk: str = ""             # the bench unit sends "? UMAX@=5600"
    bare_status: bool = False       # S answers "ON " instead of "S2=ON "
    extra_crlf: bool = False        # an empty line in front of every answer
    sync_answer: str = "????"       # what the bare <CR><LF> gets back
    stale_answer: str = ""          # one unsolicited line after the sync
    stray_line: str = ""            # one unsolicited line after the next answer
    tot_left: int = 0               # answer ?TOT this many more times
    die_after: int = 0              # close the link after this many commands
    log_reads: bool = False         # log every read command, not only writes
    channels: dict[int, Channel] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _rx_bytes: int = 0
    commands_seen: int = 0
    syncs_seen: int = 0

    # -- derived numbers ---------------------------------------------------
    @property
    def vmax(self) -> float:
        return proto.parse_quantity(self.vmax_text)

    @property
    def imax(self) -> float:
        return proto.parse_quantity(self.imax_text)

    @property
    def digits(self) -> int:
        """Width of the integer part of Vmax, the standard series' field."""
        return max(1, len(str(int(round(self.vmax)))))

    @property
    def vexp(self) -> int:
        """Fixed exponent of the precision series, so the mantissa is 5 wide."""
        return self.digits - 5

    def limit_v(self, ch: Channel) -> float:
        """Vmax times the rotary switch setting: the ``? UMAX=`` threshold.

        ``--umax-limit`` overrides it without changing what ``M`` answers.
        """
        if self.umax_limit is not None:
            return self.umax_limit
        return self.vmax * ch.mlimit / 100.0

    def current_a(self, ch: Channel) -> float:
        """Output current through the modelled load, in amperes."""
        if self.load_ohm <= 0:
            return 0.0
        return abs(ch.vact) / self.load_ohm

    def measured_current_a(self, ch: Channel) -> float:
        """``I`` as measured: through the same lagged voltage ``U`` reports.

        The current trip keeps using :meth:`current_a`, which follows the
        model voltage, so ``--meas-lag`` cannot delay a trip.
        """
        if self.load_ohm <= 0:
            return 0.0
        return abs(self.measured_v(ch)) / self.load_ohm

    def measured_v(self, ch: Channel) -> float:
        """What ``U`` answers: the model voltage as it was ``meas_lag`` ago.

        The real 208L's ADC is behind its own ramp -- on the bench it read
        -46 V while the unit already called D = 50 V reached, and needed a
        few more seconds to get there (2026-09-21).  ``--meas-lag`` puts
        that delay in, so the settle logic in ``watch`` has something to
        wait for.  The status word deliberately does *not* use this: it
        follows the model voltage, and so turns ``ON`` while ``U`` is
        still moving, exactly as the hardware does.
        """
        if self.meas_lag <= 0.0 or not ch.history:
            return ch.vact
        cutoff = time.monotonic() - self.meas_lag
        value = ch.history[0][1]
        for stamp, volts in ch.history:
            if stamp > cutoff:
                break
            value = volts
        return value

    # -- number formatting -------------------------------------------------
    def format_voltage(self, ch: Channel, volts: float, signed: bool) -> str:
        """A voltage the way this series sends it.

        Precision series: polarity, a 5 digit mantissa and the fixed
        exponent, ``+00500-01`` for 50 V on a 3000 V unit.  Standard series:
        polarity and whole volts, ``+0050``.  ``D`` on the standard series
        carries no polarity, which is what ``signed=False`` is for.
        """
        sign = ch.pol if signed else ""
        if self.fmt == "standard":
            return f"{sign}{int(round(abs(volts))):0{self.digits}d}"
        mantissa = int(round(abs(volts) / 10.0 ** self.vexp))
        return f"{sign}{mantissa:05d}{self.vexp:+03d}"

    @staticmethod
    def format_current(amps: float, digits: int = 5) -> str:
        """Mantissa and signed exponent in amperes, ``12345-09``.

        Both series use this shape for ``I``; the exponent floats so that
        the mantissa keeps all of its digits.
        """
        if amps <= 0.0:
            return "0" * digits + "-12"
        exponent = int(math.floor(math.log10(amps))) - (digits - 1)
        mantissa = int(round(amps / 10.0 ** exponent))
        if mantissa >= 10 ** digits:
            mantissa //= 10
            exponent += 1
        return f"{mantissa:0{digits}d}{exponent:+03d}"

    def format_trip(self, ch: Channel) -> str:
        """``L`` read: amperes on the precision series, counts on the other."""
        if self.fmt == "standard":
            return f"{ch.trip:d}"
        return self.format_current(ch.trip * self.trip_res)

    # -- status ------------------------------------------------------------
    def status_word(self, ch: Channel) -> str:
        """The ``S<n>`` answer, front panel first as on the real unit."""
        if self.hv_off:
            return "OFF"
        if self.manual:
            return "MAN"
        if self.error_bits & 64:
            return "ERR"
        if self.error_bits & 32:
            return "INH"
        if ch.tripped:
            return "TRP"
        if ch.vact < ch.target - SETTLED_V:
            return "L2H"
        if ch.vact > ch.target + SETTLED_V:
            return "H2L"
        return "ON"

    def module_status(self, ch: Channel) -> int:
        """The ``T<n>`` byte, assembled from the switches and the flags."""
        value = self.error_bits & (128 | 64 | 32)
        if self.kill:
            value |= 16
        if self.hv_off:
            value |= 8
        if ch.pol == "+":
            value |= 4
        if self.manual:
            value |= 2
        if ch.display:
            value |= 1
        return value

    def moving_allowed(self, ch: Channel) -> bool:
        """Can the output follow the set point at all right now?

        The front panel wins over the interface, and a channel that has been
        shut off permanently stays down until it has been released.
        """
        return not (self.hv_off or self.manual or ch.tripped
                    or self.error_bits & (64 | 32))

    # -- the 10 Hz ramp ----------------------------------------------------
    def step(self, dt: float) -> None:
        with self.lock:
            now = time.monotonic()
            for ch in self.channels.values():
                if self.hv_off or ch.tripped or self.error_bits & (64 | 32):
                    ch.vact = 0.0
                    self._record(ch, now)
                    continue
                if self.manual:
                    self._record(ch, now)
                    continue
                delta = ch.target - ch.vact
                move = ch.ramp * dt
                if abs(delta) <= move:
                    ch.vact = ch.target
                else:
                    ch.vact += move if delta > 0 else -move
                if ch.trip > 0 and self.current_a(ch) > ch.trip * self.trip_res:
                    ch.tripped = True
                    ch.status_read = False
                    ch.vact = 0.0
                    ch.target = 0.0
                    self.log(f"TRIP ch{ch.number}: current above "
                             f"L{ch.number}={ch.trip}")
                self._record(ch, now)


    def _record(self, ch: Channel, now: float) -> None:
        """Keep just enough ramp history to answer ``U`` ``meas_lag`` late."""
        if self.meas_lag <= 0.0:
            return
        ch.history.append((now, ch.vact))
        horizon = now - self.meas_lag
        while len(ch.history) > 1 and ch.history[1][0] <= horizon:
            ch.history.popleft()

    # -- logging -----------------------------------------------------------
    @staticmethod
    def log(text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} {text}", file=sys.stderr, flush=True)

    # -- the echo handshake ------------------------------------------------
    def echo_byte(self, data: bytes) -> bytes:
        """What to echo for one received byte.

        Normally the byte itself.  With ``--corrupt-echo-every N`` every Nth
        byte comes back with its low bit flipped, which is what a link out
        of step looks like to the client: it raises ``IsegEchoError`` and
        has to ``sync()``.  ``--corrupt-echo-count`` limits how often that
        happens so a test can check the recovery on a clean link.
        """
        self._rx_bytes += 1
        if (self.corrupt_every > 0 and self.corrupt_left != 0
                and self._rx_bytes % self.corrupt_every == 0):
            if self.corrupt_left > 0:
                self.corrupt_left -= 1
            return bytes([data[0] ^ 0x01])
        return data


def channel_is_dead(unit: Unit, ch: int) -> bool:
    """Is this channel the broken one, the one that answers nothing?

    Channel 1 of the real 208L on the bench looks broken; until the hardware
    session says what it actually does, ``--ch1-dead`` stands in for it with
    total silence (the client times out).  Everything the option means lives
    in this one function, so changing the symptom later is a one line edit
    here -- for instance returning an ``?WCN`` or a stale value instead.
    """
    return ch in unit.dead_channels


# --------------------------------------------------------------------------
# command handling
# --------------------------------------------------------------------------
#: returned by :func:`handle` when the unit stays silent
SILENCE = None


def handle(unit: Unit, line: str) -> str | None:
    """Turn one received command line into its answer line.

    ``None`` means the unit says nothing at all (the dead channel, and the
    bare ``\\r\\n`` sync when ``--sync-answer`` is empty).  ``""`` is the
    empty answer line every write command gets.

    The bench unit answers the synchronisation ``\\r\\n`` with ``????`` --
    it parses it as an empty command and complains -- so that is the
    default here.  The client has to drain it rather than read it as an
    answer, which is the whole point of reproducing it.
    """
    text = line.strip()
    if not text:                            # the synchronisation <CR><LF>
        unit.syncs_seen += 1
        return unit.sync_answer or SILENCE

    unit.commands_seen += 1
    if unit.tot_left > 0:
        unit.tot_left -= 1
        unit.log(f"?TOT for {text} (--tot-on-next)")
        return "?TOT"

    match = _LINE_RE.match(text)
    if match is None:
        return "????"
    letter, digits, value = match.groups()
    letter = letter.upper()

    spec = proto.COMMANDS.get(letter)
    if spec is None:
        return "????"

    # channel number
    ch_no: int | None = None
    if spec.per_channel:
        if not digits:
            return "????"
        ch_no = int(digits)
        if ch_no not in unit.channels:
            return "?WCN"
        if channel_is_dead(unit, ch_no):
            return SILENCE
    elif digits:
        return "????"

    if value is not None and not spec.settable:
        return "????"
    if value is None and not spec.answers:
        return "????"

    with unit.lock:
        if value is None:
            if unit.log_reads and letter != "G":
                unit.log(f"READ {text}")
            return _do_read(unit, letter, ch_no)
        answer = _do_write(unit, letter, ch_no, value.strip())
    if answer is not None and answer.startswith("?"):
        unit.log(f"SET {text} -> {answer}")
    else:
        unit.log(f"SET {text}")
    return answer


def _do_read(unit: Unit, letter: str, ch_no: int | None) -> str:
    """Every read command; the caller holds the lock."""
    if letter == "#":
        return (f"{unit.ident};{unit.software};"
                f"{unit.vmax_text};{unit.imax_text}")
    if letter == "W":
        return f"{unit.break_ms:03d}"

    ch = unit.channels[ch_no or 1]
    if letter == "U":
        return unit.format_voltage(ch, unit.measured_v(ch), signed=True)
    if letter == "I":
        return unit.format_current(unit.measured_current_a(ch))
    if letter == "M":
        return f"{ch.mlimit:03d}"
    if letter == "N":
        return f"{ch.nlimit:03d}"
    if letter == "D":
        return unit.format_voltage(ch, ch.vset,
                                   signed=unit.fmt != "standard")
    if letter == "V":
        return f"{ch.ramp:03d}"
    if letter == "L":
        return unit.format_trip(ch)
    if letter == "A":
        return f"{ch.auto:03d}"
    if letter == "T":
        return f"{unit.module_status(ch):03d}"
    if letter == "S":
        return _do_status(unit, ch)
    if letter == "G":
        return _do_start(unit, ch)
    return "????"


def _do_status(unit: Unit, ch: Channel) -> str:
    """``S<n>``: the status word -- and the shut-off release with it.

    Reading the status word is what releases a permanent shut-off; the
    ``G`` after it then restores the voltage.  **With autostart armed no
    ``G`` is needed**: "the previous voltage setting will be restored with
    software ramp after 'Read status word'" (NHQ x2xx manual p.8).  That
    is reproduced here because it is the one way a read of this tool's
    own making can put high voltage back on an output.

    The word reported is the one from before the release, so a trip still
    shows up as ``TRP`` on the read that clears it.  The shape is
    ``S<n>=xxx`` as the bench unit answers it (``S2=ON ``, ``S1=OFF``),
    or the bare ``xxx`` of the manual with ``--bare-status``.
    """
    word = f"{unit.status_word(ch):<3s}"
    ch.status_read = True
    if ch.tripped and ch.auto & 8:
        ch.tripped = False
        ch.status_read = False
        ch.target = ch.vset
        unit.log(f"AUTOSTART ch{ch.number}: status read restored the ramp "
                 f"to D{ch.number}={ch.vset:g} V")
    return word if unit.bare_status else f"S{ch.number}={word}"


def _do_start(unit: Unit, ch: Channel) -> str:
    """``G<n>``: engage the set point, subject to the restore latch."""
    if ch.tripped or unit.error_bits & (64 | 32):
        if ch.tripped and ch.status_read:
            ch.tripped = False
            ch.status_read = False
            ch.target = ch.vset
    elif unit.moving_allowed(ch):
        ch.target = ch.vset
    unit.log(f"SET G{ch.number}")
    return f"S{ch.number}={unit.status_word(ch):<3s}"


def _do_write(unit: Unit, letter: str, ch_no: int | None, value: str) -> str:
    """Every ``X=v`` command; the caller holds the lock."""
    try:
        number = float(value)
    except ValueError:
        return "????"

    if letter == "W":
        if not 0 <= number <= 255:
            return "????"
        unit.break_ms = int(number)
        return ""

    ch = unit.channels[ch_no or 1]
    if letter == "D":
        if number < 0:
            return "????"
        if number > unit.limit_v(ch):
            # the unit does not refuse: it clamps the set point to the
            # hardware limit and keeps it (bench, 2026-09-21 -- D2=9999
            # on a 5600 V limit answered "? UMAX@=5600" and D2 then read
            # back 5600)
            ch.vset = unit.limit_v(ch)
            return (f"? UMAX{unit.umax_junk}="
                    f"{int(round(unit.limit_v(ch))):04d}")
        # --clamp-d stands for a unit that quietly stores less than it was
        # asked for; the client is supposed to notice on the read-back
        ch.vset = (number if unit.clamp_d is None
                   else min(number, unit.clamp_d))
        if ch.auto & 8 and unit.moving_allowed(ch):
            ch.target = ch.vset       # autostart: no G needed
        return ""
    if letter == "V":
        if not 2 <= number <= 255:
            return "????"
        ch.ramp = int(number)
        return ""
    if letter == "L":
        if number < 0:
            return "????"
        ch.trip = int(number)
        return ""
    if letter == "A":
        if not 0 <= number <= 255:
            return "????"
        ch.auto = int(number)
        return ""
    return "????"


# --------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------
def _make_pty() -> tuple[int, int, str]:
    """Open a pty pair in raw mode and return (master, slave, slave path)."""
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[0] = 0  # iflag: no CR/NL translation, no flow control
    attrs[1] = 0  # oflag: no post-processing
    attrs[3] = 0  # lflag: no echo (we do our own), no canonical mode
    cc = list(attrs[6])
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    attrs[6] = cc
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    return master, slave, os.ttyname(slave)


def _write(master: int, data: bytes) -> bool:
    """Write to the pty, reporting whether the client is still there."""
    try:
        os.write(master, data)
    except OSError as err:
        if err.errno in (errno.EIO, errno.EAGAIN):
            return False
        raise
    return True


def write_answer(unit: Unit, master: int, text: str) -> None:
    """Send one answer line character by character, honouring the break time.

    The real unit leaves ``W`` milliseconds of silence between characters so
    that a slow host can keep up; sleeping for it here means the client
    never sees a whole line in one read.
    """
    gap = unit.break_ms / 1000.0
    prefix = proto.TERMINATOR if unit.extra_crlf else ""
    data = (prefix + text + proto.TERMINATOR).encode("ascii")
    for index in range(len(data)):
        if index:
            time.sleep(gap)
        if not _write(master, data[index:index + 1]):
            return


def serve(unit: Unit, master: int) -> bool:
    """Echo every byte, assemble command lines, answer them.

    Returns ``True`` when it stopped because ``--die-after`` closed the
    link, so that the caller does not close the descriptor twice.
    """
    pending = ""
    while True:
        if not select.select([master], [], [], 0.2)[0]:
            continue
        try:
            data = os.read(master, 4096)
        except OSError as err:
            if err.errno in (errno.EIO, errno.EAGAIN):
                time.sleep(0.05)   # nobody attached to the slave side yet
                continue
            raise
        if not data:
            time.sleep(0.05)
            continue
        for index in range(len(data)):
            byte = data[index:index + 1]
            if unit.echo_delay:
                time.sleep(unit.echo_delay)
            if not _write(master, unit.echo_byte(byte)):
                break
            pending += byte.decode("ascii", "replace")
            if not pending.endswith("\n"):
                continue
            line, pending = pending.rstrip("\r\n"), ""
            was_sync = not line.strip()
            answer = handle(unit, line)
            if answer is not None:
                write_answer(unit, master, answer)
            # a line left over from an earlier session, still coming out
            # of the unit when the host opens the port
            if was_sync and unit.stale_answer and unit.syncs_seen == 1:
                unit.log(f"stale answer {unit.stale_answer!r} after the sync")
                write_answer(unit, master, unit.stale_answer)
            # an unsolicited line right behind a perfectly good answer:
            # the next command must not read it as its own
            if not was_sync and unit.stray_line:
                unit.log(f"stray line {unit.stray_line!r}")
                write_answer(unit, master, unit.stray_line)
                unit.stray_line = ""
            if unit.die_after and unit.commands_seen >= unit.die_after:
                unit.log(f"closing the link after {unit.commands_seen} "
                         f"commands (--die-after)")
                os.close(master)
                return True


def ramp_loop(unit: Unit) -> None:
    dt = 1.0 / UPDATE_HZ
    last = time.monotonic()
    while True:
        time.sleep(dt)
        now = time.monotonic()
        unit.step(now - last)
        last = now


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", dest="ident", default="483216",
                        help="unit number in the # reply")
    parser.add_argument("--sw", default="2.05", help="software release")
    parser.add_argument("--vmax", default="3000V", help="Vout max, e.g. 6kV")
    parser.add_argument("--imax", default="4mA", help="Iout max, e.g. 500uA")
    parser.add_argument("--fmt", choices=("precision", "standard"),
                        default="precision",
                        help="number format: NHQ x2xx or NHQ x0x")
    parser.add_argument("--break-time", type=int, default=3, metavar="MS",
                        help="gap between answer characters (the W command)")
    parser.add_argument("--echo-delay", type=float, default=0.0,
                        metavar="S", help="delay before echoing each byte")
    parser.add_argument("--ramp", type=int, default=20, metavar="VPS",
                        help="initial ramp speed of both channels")
    parser.add_argument("--manual", action="store_true",
                        help="CONTROL switch up: commands do nothing, S=MAN")
    parser.add_argument("--hv-off", action="store_true",
                        help="front panel HV-ON switch off: S=OFF")
    parser.add_argument("--kill", action="store_true",
                        help="KILL switch enabled (T bit 16)")
    parser.add_argument("--pol", default="+,+", metavar="+,-",
                        help="polarity per channel")
    parser.add_argument("--error-bits", type=lambda s: int(s, 0), default=0,
                        metavar="MASK",
                        help="extra T bits: 64 ERR, 32 INH, 128 QUA")
    parser.add_argument("--load-ohm", type=float, default=1e9,
                        help="load on the outputs; I = U / R (default 1e9)")
    parser.add_argument("--meas-lag", type=float, default=0.0, metavar="S",
                        dest="meas_lag",
                        help="U answers the model voltage of this many "
                             "seconds ago, the way the real 208L's read-back "
                             "trails its own ramp; the status word still "
                             "turns ON when the set point is reached")
    parser.add_argument("--trip-res", type=float, default=1e-9, metavar="A",
                        help="one count of the L current trip, in amperes")
    parser.add_argument("--mlimit", type=int, default=100, metavar="PCT",
                        help="Vmax rotary switch position in %% (M)")
    parser.add_argument("--nlimit", type=int, default=100, metavar="PCT",
                        help="Imax rotary switch position in %% (N)")
    parser.add_argument("--clamp-d", type=float, default=None, metavar="V",
                        help="store min(D, this): a unit that quietly "
                             "keeps less than it was asked for")
    parser.add_argument("--umax-limit", type=float, default=None,
                        metavar="V",
                        help="answer ? UMAX (and clamp) above this many volts "
                             "while M still reports the rotary switch")
    parser.add_argument("--preset", action="append", default=[],
                        metavar="CH:V",
                        help="start channel CH on, D=V and the output settled "
                             "at V (repeatable)")
    parser.add_argument("--autostart", action="append", default=[],
                        metavar="CH:A",
                        help="start channel CH with the A flags set to A, "
                             "e.g. 2:8 for autostart armed (repeatable)")
    parser.add_argument("--umax-junk", default="", metavar="TEXT",
                        help="what the unit puts between UMAX and '=' "
                             "(the bench 208L sends '@')")
    parser.add_argument("--bare-status", action="store_true",
                        help="S answers 'ON ' as the manual shows, not "
                             "'S2=ON ' as the bench unit does")
    parser.add_argument("--extra-crlf", action="store_true",
                        help="send an empty line in front of every answer")
    parser.add_argument("--sync-answer", default="????", metavar="TEXT",
                        help="answer to the bare <CR><LF> ('' for silence); "
                             "the bench unit says ????")
    parser.add_argument("--stale-answer", default="", metavar="TEXT",
                        help="one unsolicited line right after the first "
                             "sync, as if left over from an earlier session")
    parser.add_argument("--stray-line", default="", metavar="TEXT",
                        help="one unsolicited line right after the next "
                             "answer")
    parser.add_argument("--tot-on-next", type=int, default=0, metavar="N",
                        help="answer ?TOT to the next N commands")
    parser.add_argument("--die-after", type=int, default=0, metavar="N",
                        help="close the link after N commands")
    parser.add_argument("--log-reads", action="store_true",
                        help="log every read command as 'READ <line>', not "
                             "only the writes")
    parser.add_argument("--ch1-dead", action="store_true",
                        help="channel 1 answers nothing at all. NOTE: not "
                             "what the bench 208L does -- its channel 1 "
                             "answers normally and reports OFF / MAN "
                             "(T1=011). Kept for the silent-device case.")
    parser.add_argument("--corrupt-echo-every", type=int, default=0,
                        metavar="N", help="echo every Nth byte wrongly")
    parser.add_argument("--corrupt-echo-count", type=int, default=1,
                        metavar="K",
                        help="how many bytes to corrupt, -1 for all of them")
    return parser


def unit_from_args(args: argparse.Namespace) -> Unit:
    unit = Unit(ident=args.ident, software=args.sw, vmax_text=args.vmax,
                imax_text=args.imax, fmt=args.fmt,
                break_ms=args.break_time, echo_delay=args.echo_delay,
                manual=args.manual, hv_off=args.hv_off, kill=args.kill,
                error_bits=args.error_bits, load_ohm=args.load_ohm,
                meas_lag=args.meas_lag, trip_res=args.trip_res,
                dead_channels=(1,) if args.ch1_dead else (),
                corrupt_every=args.corrupt_echo_every,
                corrupt_left=args.corrupt_echo_count,
                clamp_d=args.clamp_d, umax_limit=args.umax_limit,
                umax_junk=args.umax_junk,
                bare_status=args.bare_status,
                extra_crlf=args.extra_crlf, sync_answer=args.sync_answer,
                stale_answer=args.stale_answer, stray_line=args.stray_line,
                tot_left=args.tot_on_next, die_after=args.die_after,
                log_reads=args.log_reads)
    pols = [p.strip() for p in args.pol.split(",")]
    for index, number in enumerate(proto.CHANNELS):
        pol = pols[index] if index < len(pols) and pols[index] in "+-" else "+"
        unit.channels[number] = Channel(number=number, pol=pol,
                                        ramp=args.ramp, mlimit=args.mlimit,
                                        nlimit=args.nlimit)
    for text in args.preset:
        number, volts = _channel_value(text, "--preset")
        ch = unit.channels[number]
        ch.vset = ch.target = ch.vact = volts
    for text in args.autostart:
        number, flags = _channel_value(text, "--autostart")
        unit.channels[number].auto = int(flags)
    return unit


def _channel_value(text: str, option: str) -> tuple[int, float]:
    """Split ``CH:VALUE`` for ``--preset`` / ``--autostart``."""
    try:
        ch_text, value_text = text.split(":", 1)
        number, value = int(ch_text), float(value_text)
    except ValueError:
        raise SystemExit(f"{option} wants CH:VALUE, got {text!r}") from None
    if number not in proto.CHANNELS:
        raise SystemExit(f"{option}: channel {number} does not exist")
    if value < 0:
        raise SystemExit(f"{option}: {value:g} is negative, give a magnitude")
    return number, value


def _fix_negative_values(argv: list[str]) -> list[str]:
    """Let ``--pol -,+`` work: argparse would read the value as a flag."""
    out: list[str] = []
    skip = False
    for index, token in enumerate(argv):
        if skip:
            skip = False
            continue
        if token == "--pol" and index + 1 < len(argv):
            out.append(f"--pol={argv[index + 1]}")
            skip = True
        else:
            out.append(token)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = _fix_negative_values(list(argv) if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(argv)
    unit = unit_from_args(args)
    master, slave, path = _make_pty()
    print(path, flush=True)
    flags = [name for name, on in (("MAN", unit.manual), ("HV-OFF",
             unit.hv_off), ("KILL", unit.kill),
             ("ch1 dead", bool(unit.dead_channels))) if on]
    print(f"fake iseg NHQ {unit.ident} ({unit.fmt}, {unit.vmax_text}/"
          f"{unit.imax_text}, W={unit.break_ms} ms) on {path}"
          f"{'  [' + ', '.join(flags) + ']' if flags else ''}",
          file=sys.stderr, flush=True)
    threading.Thread(target=ramp_loop, args=(unit,), daemon=True).start()
    died = False
    try:
        died = serve(unit, master)
    except KeyboardInterrupt:
        pass
    finally:
        if not died:
            os.close(master)
        os.close(slave)
    if died:
        # stay alive with the master closed so the slave keeps existing
        # and the client sees a dead link rather than a missing device
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
