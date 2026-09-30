#!/usr/bin/env python3
"""Probe / debug CLI for an iseg NHQ high voltage supply (RS232).

Standard library only (``os.open`` + ``termios``, no pyserial) so it runs
unchanged on the PSI DAQ machine and inside the pioneer-midas container.

    ./iseg_nhq_probe.py --port /dev/ttyUSB0 info
    ./iseg_nhq_probe.py --port /dev/ttyUSB0 dump --ch 1 2
    ./iseg_nhq_probe.py --port /dev/ttyUSB0 mon U --ch 2
    ./iseg_nhq_probe.py --port /dev/ttyUSB0 set V 20 --ch 2
    ./iseg_nhq_probe.py --port /dev/ttyUSB0 ramp --ch 2 --to 50 --speed 20
    ./iseg_nhq_probe.py --port /dev/ttyUSB0 watch --ch 2
    ./iseg_nhq_probe.py --port /dev/ttyUSB0 off --ch 2
    ./iseg_nhq_probe.py --port /dev/ttyUSB0 --max-v 1600 ramp --ch 2 --to 1500

Every set point is checked against a software ceiling, ``--max-v``,
1300 V by default: the Vmax rotary switch only steps in 10 % of the module
maximum (800 V at a time on the 8 kV unit here), so the number the
detector wants cannot be fenced off in hardware.  ``--yes`` does not lift
that ceiling; ``--max-v 0`` switches it off.

The wire protocol is documented in ``iseg_nhq_protocol.py``.  The short
version: 9600 8N1, every command line is ASCII terminated by ``\\r\\n``, the
host sends it one byte at a time and waits for the unit to echo each byte
before sending the next, and the answer follows the echoed terminator --
possibly after an empty line, which is why the reader skips those.  A write
command (``X<n>=v``) answers with an empty line, a read command with one
value line, and ``G<n>`` with ``S<n>=xxx``.

Reads that are not reads
------------------------
``G`` starts a ramp, so ``mon G`` is refused; use ``ramp``.  ``S`` is worse,
because it looks harmless: reading the status word releases a permanent
shut-off, and with autostart set (``A<n>=8``) the unit then restores the
previous set voltage on its own (NHQ x2xx manual p.8).  ``dump`` therefore
reads ``A`` and ``T`` before ``S`` and warns loudly when autostart is on,
and ``watch`` and ``mon S`` refuse outright unless ``--yes`` is given.

Exit codes: 0 fine, 1 the unit answered an error line or something we could
not parse, 2 the tool refused locally or the arguments were wrong, 3 I/O
trouble (timeout, echo out of step, the link died, cannot open the port).
``--verbose`` prints every byte in both directions to stderr.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import select
import sys
import termios
import time

import iseg_nhq_protocol as proto
from iseg_nhq_protocol import CurrentTrip, Identity, ProtocolError


class IsegNHQError(RuntimeError):
    """The unit answered with an error line (``????``, ``?WCN``, ...)."""

    def __init__(self, command: str, reply: str, meaning: str) -> None:
        self.command = command
        self.reply = reply.strip()
        self.meaning = meaning
        super().__init__(f"{self.reply!r} ({meaning}) for {command!r}")


class IsegNHQTimeout(TimeoutError):
    """No terminated answer line arrived within the timeout."""


class IsegEchoError(IOError):
    """The unit did not echo the byte that was sent.

    Carries the byte that went out and whatever came back (``b""`` when
    nothing did).  The two sides are out of step after this; call
    :meth:`IsegNHQ.sync` before using the link again.
    """

    def __init__(self, sent: bytes, received: bytes) -> None:
        self.sent = sent
        self.received = received
        got = "nothing (timeout)" if not received else repr(received)
        super().__init__(f"echo mismatch: sent {sent!r}, got {got}")


class IsegLinkError(IOError):
    """The serial link went away: end of file, or the device disappeared.

    Distinct from a timeout, which means the unit is there and quiet.  A
    USB serial adapter being unplugged, or the emulator exiting, shows up
    here.  Retrying is pointless; the port has to be opened again.
    """


class IsegRefused(RuntimeError):
    """The tool refused to do something, no bytes were sent."""


class PortBusy(OSError):
    """Another process (the MIDAS frontend, another CLI) holds the port."""


class IsegNHQ:
    """Blocking client for the NHQ echo-handshake protocol."""

    #: what ``self.series`` is assumed to be until a reply says otherwise
    DEFAULT_SERIES = "precision"

    def __init__(self, port: str = "/dev/ttyUSB0", timeout: float = 1.0,
                 echo_timeout: float = 0.3, verbose: bool = False,
                 probe_on_open: bool = True) -> None:
        self.port = port
        #: read ``W`` and one ``U`` in :meth:`open`.  Off means the open
        #: sends nothing but the synchronisation ``\\r\\n``: for a unit
        #: that must not be touched, and for tests that count bytes.
        self.probe_on_open = probe_on_open
        #: inter-character deadline for an answer, not a whole-line budget
        self.timeout = timeout
        self.echo_timeout = echo_timeout
        self.verbose = verbose
        self.fd = -1
        self._buf = ""
        #: the unit's break time in ms, read from ``W`` in :meth:`open`
        self.break_ms = 3
        #: ``"precision"`` / ``"standard"``, or ``None`` while unknown
        self.series: str | None = None

    @property
    def series_or_default(self) -> str:
        """The series to format numbers for before one has been detected."""
        return self.series or self.DEFAULT_SERIES

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        """Open the port, synchronise, and learn what is on the other end.

        After the bare ``\\r\\n`` the unit is asked for its break time and
        for one voltage: the first sets the timeouts (a unit told to leave
        255 ms between characters cannot answer inside a 1 s line budget),
        the second says which series the numbers are in.  Both are best
        effort -- a channel that does not answer must not stop the tool
        from opening -- but if the open itself fails the file descriptor is
        closed again rather than left dangling.
        """
        if self.fd >= 0:
            return
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        # Exclusive advisory lock, shared with the C++ MIDAS driver, which
        # takes the same flock(LOCK_EX | LOCK_NB) on its fd: the CLI can
        # never interleave with the frontend on one port.  flock locks
        # belong to the open file description and work on tty devices and
        # pty slaves alike.  Closing the fd releases the lock.
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            os.close(self.fd)
            self.fd = -1
            if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise PortBusy(f"port {self.port} in use (MIDAS frontend "
                               f"running? stop scfe first)") from None
            raise
        try:
            try:
                self._configure(self.fd)
            except termios.error:
                pass  # not a real tty (a pty from the emulator) -- keep going
            self.sync()
            if self.probe_on_open:
                self._adopt_break_time()
                self.detect_series()
        except BaseException:
            os.close(self.fd)
            self.fd = -1
            raise

    def _adopt_break_time(self) -> None:
        """Read ``W`` and widen the timeouts to match it.

        The unit dribbles its answer out with ``W`` ms of silence between
        characters, so the per-character deadline has to be comfortably
        longer than ``W`` and the echo deadline too.  A larger value asked
        for in the constructor wins; this only ever raises them.
        """
        try:
            self.break_ms = self.break_time()
        except (IsegNHQError, IsegNHQTimeout, IsegEchoError, ValueError):
            return
        gap = self.break_ms / 1000.0
        self.timeout = max(self.timeout, 4.0 * gap + 0.2)
        self.echo_timeout = max(self.echo_timeout, gap + 0.2)

    def detect_series(self) -> str | None:
        """Read ``U`` until a channel answers and note which series it is.

        The ``#`` identity does not say whether this is an NHQ x2xx or an
        NHQ x0x, and the two disagree about the format of ``D`` and the
        meaning of ``L``; the shape of a voltage reply does say.  Errors
        and dead channels are swallowed -- the series simply stays unknown
        and the precision format is assumed.
        """
        if self.series is not None:
            return self.series
        for ch in proto.CHANNELS:
            try:
                self.send("U", ch)
            except (IsegNHQError, IsegNHQTimeout, IsegEchoError):
                continue
            if self.series is not None:
                return self.series
        return self.series

    def _note_series(self, letter: str, answer: str) -> None:
        """Learn the series from a ``U`` / ``D`` / ``L`` answer."""
        if self.series is not None or letter not in ("U", "D", "L"):
            return
        found = proto.series_of_reply(answer)
        if found is not None:
            self.series = found

    @staticmethod
    def _configure(fd: int) -> None:
        """Raw 9600 8N1, no flow control, non-blocking reads."""
        attrs = termios.tcgetattr(fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        iflag = 0
        oflag = 0
        lflag = 0
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag,
                           termios.B9600, termios.B9600, cc])

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "IsegNHQ":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- raw byte level ----------------------------------------------------
    def _trace(self, arrow: str, data: bytes) -> None:
        if self.verbose:
            hexed = " ".join(f"{b:02x}" for b in data)
            print(f"{arrow} {hexed}  {data!r}", file=sys.stderr)

    def _write(self, data: bytes) -> None:
        """Write all of ``data``, however many syscalls that takes.

        ``os.write`` on a non-blocking tty is free to take only part of
        the buffer, or none of it, and a full output queue raises
        ``BlockingIOError`` -- neither is an error, both mean "wait and
        write the rest".
        """
        view = memoryview(data)
        sent = 0
        deadline = time.monotonic() + max(self.timeout, 1.0)
        while sent < len(view):
            try:
                sent += os.write(self.fd, view[sent:])
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise IsegNHQTimeout(
                        f"{self.port} would not take {len(view) - sent} "
                        f"more byte(s) of {bytes(view)!r}")
                select.select([], [self.fd], [], 0.05)
        self._trace(">>>", data)

    def _read_some(self, deadline: float) -> bytes:
        """Read whatever is available, waiting until ``deadline`` at most.

        A readable descriptor that then yields nothing is end of file: the
        other end is gone (the adapter unplugged, the emulator exited).
        That is an :class:`IsegLinkError`, not something to retry -- the
        old code returned ``b""`` here and spun until the timeout.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b""
        if not select.select([self.fd], [], [], remaining)[0]:
            return b""
        try:
            data = os.read(self.fd, 256)
        except BlockingIOError:
            return b""
        except OSError as err:
            raise IsegLinkError(
                f"{self.port} failed while reading: {err}") from err
        if not data:
            raise IsegLinkError(f"{self.port} closed by the other end")
        self._trace("<<<", data)
        return data

    #: the longest break time the unit can be set to (``W``), in seconds.
    #: Before ``W`` has been read the worst case is all we know, and it is
    #: what a half-received line has to be given time to finish.
    MAX_BREAK_S = 0.255

    def drain(self, seconds: float | None = None) -> bytes:
        """Read and throw away whatever is coming, until the line is quiet.

        Two patiences, because the cost of getting this wrong is a
        half-swallowed line that then corrupts the echo of the next
        command.  In the middle of a line -- something has arrived and it
        does not end in ``\\n`` yet -- the next character is waited for as
        long as the slowest possible break time takes (``4 x W + 50 ms``,
        or 255 ms if ``W`` is not known yet, which is exactly the case
        during the sync in :meth:`open`).  Once nothing is pending, or a
        whole line has come in, ``4 x W + 50 ms`` of silence is taken as
        the end of it.

        The alternative -- waiting out a fixed budget every time -- costs
        a third of a second on every open, and still guesses wrong on a
        unit with a long break time.
        """
        self._buf = ""
        short = self._drain_time(0.0)
        long = max(short, 4.0 * self.MAX_BREAK_S)
        cap = time.monotonic() + (max(1.5, long * 2) if seconds is None
                                  else seconds)
        seen = b""
        idle_end = time.monotonic() + short
        while True:
            now = time.monotonic()
            until = min(cap, idle_end)
            if now >= until:
                return seen
            data = self._read_some(until)
            if not data:
                continue
            seen += data
            mid_line = not seen.endswith(b"\n")
            idle_end = time.monotonic() + (long if mid_line else short)

    def _drain_time(self, floor: float) -> float:
        """At least ``floor``, and always long enough for a whole line.

        The unit spaces its output by the break time, so a stale answer
        still coming out takes ``W`` ms per character; ``4 x W + 50 ms``
        is the same budget the answer reader uses for an idle line.
        """
        return max(floor, 4.0 * self.break_ms / 1000.0 + 0.05)

    def sync(self, drain_time: float | None = None) -> bytes:
        """Send a bare ``\\r\\n`` without the echo handshake, then drain.

        The manual says only that this "assures synchronisation" between
        computer and supply, and asks for it once after opening the port.
        It is used here as the way back from an :class:`IsegEchoError` or a
        timeout as well, because it is the only recovery the manual
        offers -- but note what is and is not promised: the unit is *not*
        documented to answer it, and nothing says its input parser is
        reset.  What this call definitely does is throw away every byte
        still in flight on our side.
        """
        if self.fd < 0:
            self.open()
            return b""
        self._write(proto.TERMINATOR.encode("ascii"))
        return self.drain(None if drain_time is None
                          else self._drain_time(drain_time))

    def _send_echoed(self, data: bytes) -> None:
        """Write ``data`` one byte at a time, waiting for each echo."""
        for index in range(len(data)):
            byte = data[index:index + 1]
            self._write(byte)
            deadline = time.monotonic() + self.echo_timeout
            echo = b""
            while not echo:
                chunk = self._read_some(deadline)
                if chunk:
                    echo = chunk
                elif time.monotonic() >= deadline:
                    raise IsegEchoError(byte, b"")
            if echo[:1] != byte:
                raise IsegEchoError(byte, echo)
            if len(echo) > 1:
                # the unit answered faster than we read; keep the surplus
                self._buf += echo[1:].decode("ascii", "replace")

    # -- line level --------------------------------------------------------
    def _take_line(self) -> str | None:
        """Pop one ``\\n``-terminated line out of the buffer, empty ones too."""
        index = self._buf.find("\n")
        if index < 0:
            return None
        line = self._buf[:index].rstrip("\r")
        self._buf = self._buf[index + 1:]
        return line

    def _read_line(self, timeout: float | None = None) -> str:
        """Read one answer line.  An empty line is a valid answer.

        ``timeout`` is an **inter-character** deadline: it is restarted
        every time a byte arrives.  The unit spaces its output out by the
        break time and a long line at ``W=255`` takes seconds, so a budget
        for the whole line would either have to be enormous or would cut
        healthy answers in half.  What it still catches is silence.
        """
        line = self._take_line()
        if line is not None:
            return line
        idle = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + idle
        while True:
            if time.monotonic() >= deadline:
                raise IsegNHQTimeout(
                    f"no answer from {self.port}: nothing received for "
                    f"{idle:g} s (buffer {self._buf!r})")
            data = self._read_some(deadline)
            if not data:
                continue
            deadline = time.monotonic() + idle      # a byte: start again
            self._buf += data.decode("ascii", "replace")
            line = self._take_line()
            if line is not None:
                return line

    def _empty_line_idle(self) -> float:
        """How long to keep looking for an answer behind empty lines."""
        return max(4.0 * self.break_ms / 1000.0, 0.05)

    def _read_answer(self) -> str:
        """Read the answer, skipping empty lines in front of it.

        The vendor's example (NHQ x2xx manual p.9) reads eight characters
        after the echo of ``U1\\r\\n``, which is one more than a
        standard-series ``+1234\\r\\n``: there is probably a stray ``\\n`` or
        ``\\r\\n`` between the echo and the answer.  Rather than guess, keep
        taking lines until one has text in it.  A write command answers
        with nothing but empty lines, so the search gives up after an
        inter-line idle of ``max(4 x break time, 50 ms)`` and reports the
        empty answer -- the write acknowledgement.
        """
        line = self._read_line()
        if line != "":
            return line
        while True:
            try:
                line = self._read_line(timeout=self._empty_line_idle())
            except IsegNHQTimeout:
                return ""
            if line != "":
                return line

    def _flush_input(self) -> None:
        """Throw away anything unread, in our buffer and in the kernel's.

        Whatever is in the port now belongs to a command that already
        finished (or to a unit that re-initialised itself); reading it as
        this command's answer is how the two sides end up one answer apart
        for the rest of the session.
        """
        self._buf = ""
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except (termios.error, OSError):
            pass    # a pty, or a descriptor that does not support it

    def command(self, text: str) -> str:
        """Send one command line and return its answer.

        The input is flushed first, so a stray line left over from an
        earlier command cannot be mistaken for this one's answer.  The echo
        handshake then consumes the echo of the whole line including the
        terminator, and what follows -- after any empty lines, see
        :meth:`_read_answer` -- is the answer: the value for a read, an
        empty string for a write, ``S<n>=xxx`` for ``G``.

        An error line raises :class:`IsegNHQError`; ``?TOT`` means the unit
        has re-initialised itself, so the link is resynchronised before the
        exception goes out.  A timeout or a broken echo also resynchronises
        before re-raising: after either of those the two sides are out of
        step, and every caller wants the same recovery.
        """
        if self.fd < 0:
            self.open()
        self._flush_input()
        try:
            self._send_echoed((text + proto.TERMINATOR).encode("ascii"))
            answer = self._read_answer()
        except (IsegNHQTimeout, IsegEchoError):
            self._sync_quietly()
            raise
        meaning = proto.is_error(answer)
        if meaning is not None:
            if answer.strip().startswith("?TOT"):
                # the unit is re-initialising; put the link back in step
                # before anyone can send the next command
                self._sync_quietly()
            raise IsegNHQError(text, answer, meaning)
        return answer.strip()

    def _sync_quietly(self) -> None:
        """:meth:`sync` for error paths: never raise out of the recovery."""
        try:
            self.sync(0.1)
        except (OSError, TimeoutError):
            pass

    def send(self, letter: str, ch: int | None = None,
             value: float | int | str | None = None) -> str:
        """Build a command from the table and send it."""
        letter = letter.upper()
        answer = self.command(
            proto.build_command(letter, ch, value, self.series_or_default))
        if value is None:
            self._note_series(letter, answer)
        return answer

    # -- typed reads -------------------------------------------------------
    def identify(self) -> Identity:
        """``#`` -- unit number, software release, Vmax, Imax."""
        return proto.parse_identity(self.send("#"))

    def break_time(self) -> int:
        """``W`` -- gap in ms the unit leaves between output characters."""
        return int(self.send("W"))

    def status(self, ch: int) -> str:
        """``S<n>`` -- channel status word, e.g. ``ON``, ``L2H``, ``MAN``.

        **This read changes the state of the unit.**  It releases the
        permanent shut-off latch after a trip or an ERR / INH shut-off,
        and if autostart is set (``A<n>=8``) the unit restores the
        previous set voltage with a software ramp as soon as it has been
        read (NHQ x2xx manual p.8).  Call :meth:`autostart` first and
        decide whether that is wanted; the CLI does so in ``dump``,
        ``watch`` and ``mon S``.

        The 208L answers ``S2=ON `` where the manual promises a bare
        ``ON `` (bench, 2026-09-21), so the prefix is stripped when it is
        there -- and the channel in it has to match, or the answers have
        slipped and this status word belongs to the other output.
        """
        return proto.parse_status_reply(self.send("S", ch), ch)

    def autostart_is_armed(self, ch: int) -> int:
        """``A<n>`` masked to the autostart bit: 8 if reading ``S`` ramps.

        Cheap, does not touch the output, and the thing to call before
        anything reads the status word.
        """
        return self.autostart(ch) & 8

    def module_status(self, ch: int) -> tuple[int, list[str]]:
        """``T<n>`` -- module status byte and the names of its set bits."""
        value = int(self.send("T", ch))
        return value, proto.decode_module_status(value)

    def read_voltage(self, ch: int) -> float:
        """``U<n>`` -- measured output voltage in volts, signed."""
        return proto.parse_number(self.send("U", ch))

    def read_current(self, ch: int) -> float:
        """``I<n>`` -- measured output current in amperes."""
        return proto.parse_current(self.send("I", ch))

    def read_set_voltage(self, ch: int) -> float:
        """``D<n>`` -- the voltage set point in volts."""
        return proto.parse_number(self.send("D", ch))

    def ramp_speed(self, ch: int) -> int:
        """``V<n>`` -- software ramp speed in V/s."""
        return int(self.send("V", ch))

    def current_trip(self, ch: int) -> CurrentTrip:
        """``L<n>`` -- current trip; 0 means no trip.

        The number alone is ambiguous, so it comes back with its unit:
        amperes on the precision series (which answers mantissa and
        exponent), counts of the first current range on the standard
        series (which answers a plain integer).  The series is whatever
        :meth:`detect_series` found, and this reply settles it if nothing
        has yet.
        """
        answer = self.send("L", ch)
        value = proto.parse_number(answer)
        return CurrentTrip(value=value,
                           unit=proto.SERIES_TRIP_UNIT[self.series_or_default])

    def autostart(self, ch: int) -> int:
        """``A<n>`` -- autostart / EEPROM flag byte (8 = autostart active)."""
        return int(self.send("A", ch))

    def voltage_limit_pct(self, ch: int) -> int:
        """``M<n>`` -- Vmax rotary switch position, in % of Vout max."""
        return int(self.send("M", ch))

    def current_limit_pct(self, ch: int) -> int:
        """``N<n>`` -- Imax rotary switch position, in % of Iout max."""
        return int(self.send("N", ch))

    # -- typed writes ------------------------------------------------------
    def set_voltage(self, ch: int, volts: float) -> None:
        """``D<n>=v`` -- set point only; the output follows on ``G``.

        Unless autostart is set, in which case the output follows at once.
        This method does not check that; :func:`set_voltage_guarded` is
        the checked path the CLI uses.  On the standard series a
        fractional voltage is rounded to whole volts (round-half-even),
        because the unit would answer ``????`` to anything else.
        """
        self.send("D", ch, volts)

    def set_ramp(self, ch: int, volts_per_s: float) -> None:
        """``V<n>=nnn`` -- software ramp speed, 2 to 255 V/s."""
        self.send("V", ch, volts_per_s)

    def set_current_trip(self, ch: int, counts: float) -> None:
        """``L<n>=n`` -- current trip, **in counts**; 0 disables it.

        Both series take an integer here, in the resolution of the first
        current measurement range -- not in amperes, even on the precision
        series, which nevertheless *answers* ``L`` in amperes.  What one
        count is worth depends on the model and its current-range option
        (100 nA, 1 nA or 100 pA on the x2xx series, manual p.2), so the
        conversion is left to the caller, who knows the module.
        """
        self.send("L", ch, counts)

    def set_autostart(self, ch: int, value: int) -> None:
        """``A<n>=nn`` -- autostart and EEPROM save flags."""
        self.send("A", ch, value)

    def start(self, ch: int) -> str:
        """``G<n>`` -- start the voltage change; returns the status word."""
        return proto.parse_start_reply(self.send("G", ch), ch)


# --------------------------------------------------------------------------
# subcommands -- return text so the tests can use them as an API
# --------------------------------------------------------------------------
#: software ceiling on every set point, in volts.  The Vmax rotary switch
#: only goes in 10 % steps of the module maximum -- 800 V or 1600 V on the
#: 8 kV unit here -- so the voltage the detector actually wants cannot be
#: fenced off in hardware.  0 disables it.
DEFAULT_MAX_V = 1300.0

#: how close ``U`` has to be to ``D`` before ``watch`` calls a channel
#: settled, in volts.  The bench 208L reports ``S=ON`` as soon as its
#: internal set point is reached, while the measured voltage is still a
#: few volts away and takes a few more seconds to catch up (2026-09-21:
#: ``S=ON`` at U = -46 V for D = 50, and at U = -25 V for D = 0).  0
#: disables the read-back requirement and stops on the status word alone.
DEFAULT_SETTLE_V = 2.0


def info_text(dev: IsegNHQ, max_v: float = DEFAULT_MAX_V) -> str:
    """Identity, break time, series and the limit that actually applies."""
    ident = dev.identify()
    lines = [
        f"unit        {ident.unit}",
        f"software    {ident.software}",
        f"Vmax        {ident.vmax_v:g} V",
        f"Imax        {ident.imax_a * 1e3:g} mA",
    ]
    try:
        lines.append(f"break time  {dev.break_time()} ms")
    except (IsegNHQError, ValueError) as err:
        lines.append(f"break time  <{err}>")
    lines.append(f"series      {dev.series or 'unknown (assuming precision)'}")
    lines.append(f"max-v       {max_v:g} V (software ceiling)" if max_v
                 else "max-v       off (software ceiling disabled)")
    for ch in proto.CHANNELS:
        try:
            pct = dev.voltage_limit_pct(ch)
        except (IsegNHQError, IsegNHQTimeout, IsegEchoError, ValueError):
            lines.append(f"limit ch{ch}    <no answer>")
            continue
        hardware = ident.vmax_v * pct / 100.0
        effective = min(hardware, max_v) if max_v else hardware
        lines.append(f"limit ch{ch}    {effective:g} V "
                     f"(hardware {hardware:g} V = Vmax x {pct} %)")
    return "\n".join(lines)


#: the loud line printed when a dump finds autostart armed
AUTOSTART_WARNING = (
    "!! WARNING ch{ch}: autostart is armed (A{ch}={value}). Reading the "
    "status word S{ch} restores the previous set voltage with a software "
    "ramp if the channel was shut off; this dump reads S{ch}.")


def channel_row(dev: IsegNHQ, ch: int) -> tuple[dict[str, str], list[str]]:
    """Send every dump command to one channel, keeping the raw answers.

    Returns the answers and any warnings.  ``A`` and ``T`` are read before
    ``S`` (see :data:`iseg_nhq_protocol.DUMP_COMMANDS`) so that the
    autostart flags are known before the status word is touched; if
    autostart is armed the warning is printed to stderr the moment it is
    found, as well as being returned for the report.
    """
    row: dict[str, str] = {}
    warnings: list[str] = []
    for letter in proto.DUMP_COMMANDS:
        try:
            row[letter] = dev.send(letter, ch)
        except IsegNHQError as err:
            row[letter] = err.reply
        except (IsegNHQTimeout, IsegEchoError):
            # command() has already resynchronised the link
            row[letter] = "<no answer>"
            continue
        if letter == "A":
            try:
                armed = int(row["A"]) & 8
            except ValueError:
                continue
            if armed:
                warning = AUTOSTART_WARNING.format(ch=ch, value=row["A"])
                warnings.append(warning)
                print(warning, file=sys.stderr, flush=True)
    return row, warnings


def dump_text(dev: IsegNHQ, channels: list[int] | None = None) -> str:
    """Aligned table of the raw answers, plus a decoded line per channel."""
    channels = list(proto.CHANNELS) if channels is None else list(channels)
    for ch in channels:
        proto.check_channel(ch)
    gathered = {ch: channel_row(dev, ch) for ch in channels}
    rows = {ch: gathered[ch][0] for ch in channels}
    warnings = [line for ch in channels for line in gathered[ch][1]]
    columns = ("CH",) + proto.DUMP_COMMANDS

    def cell(ch: int, col: str) -> str:
        return str(ch) if col == "CH" else rows[ch].get(col, "")

    widths = {col: max([len(col)] + [len(cell(ch, col)) for ch in channels])
              for col in columns}
    header = "  ".join(f"{col:<{widths[col]}}" for col in columns)
    lines = [header, "-" * len(header)]
    for ch in channels:
        lines.append("  ".join(f"{cell(ch, col):<{widths[col]}}"
                               for col in columns))
    lines.append("")
    for ch in channels:
        lines.append(f"ch{ch}: {decode_row(rows[ch], ch)}")
    if warnings:
        lines.append("")
        lines.extend(warnings)
    lines.append("")
    lines.append("A autostart flags, T module status, S status word, "
                 "M/N hardware limits in %, D set voltage,")
    lines.append("V ramp V/s, L current trip, U voltage, I current; "
                 "values as the unit sends them")
    lines.append("A and T are read before S on purpose: S can restart a "
                 "ramp when autostart is armed")
    return "\n".join(lines)


def decode_row(row: dict[str, str], ch: int | None = None) -> str:
    """One readable line out of a raw dump row."""
    parts: list[str] = []
    status = row.get("S", "")
    if status:
        word = status
        if ch is not None:
            try:
                word = proto.parse_status_reply(status, ch)
            except ProtocolError:
                word = status
        parts.append(f"S={word} ({proto.STATUS_WORDS.get(word, 'unknown')})")
    try:
        value = int(row.get("T", ""))
        names = proto.decode_module_status(value) or ["-"]
        parts.append(f"T={value} [{','.join(names)}]")
    except ValueError:
        pass
    try:
        parts.append(f"U={proto.parse_number(row['U']):+.2f} V")
    except (KeyError, ValueError):
        pass
    try:
        parts.append(f"I={proto.parse_current(row['I']) * 1e6:.4g} uA")
    except (KeyError, ValueError):
        pass
    try:
        value = int(row.get("A", ""))
        names = proto.decode_autostart(value) or ["-"]
        parts.append(f"A={value} [{','.join(names)}]")
    except ValueError:
        pass
    return ", ".join(parts) if parts else "nothing decodable"


def mon_text(dev: IsegNHQ, letter: str, ch: int | None,
             yes: bool = False) -> str:
    """Send one read command and print the raw answer plus its meaning.

    Only genuine reads are allowed here.  ``G`` answers like a read and
    starts a ramp, so it is refused and the operator is sent to ``ramp``.
    ``S`` is allowed but checked first: with autostart armed, reading the
    status word restores the previous set voltage.
    """
    letter = letter.upper()
    spec = proto.COMMANDS.get(letter)
    if spec is None:
        raise ValueError(f"unknown command {letter!r}, known: "
                         f"{' '.join(sorted(proto.COMMANDS))}")
    if not spec.readable:
        raise IsegRefused(
            f"{letter} is not a read: {spec.desc}. 'mon' is for looking at "
            f"the unit without changing it -- use the 'ramp' subcommand, "
            f"which checks the state of the channel first.")
    if letter in proto.STATE_CHANGING_READS and not yes:
        guard_state_changing_read(dev, letter, ch)
    raw = dev.send(letter, ch if spec.per_channel else None)
    return f"{raw}{_mon_note(letter, raw, ch)}"


def guard_state_changing_read(dev: IsegNHQ, letter: str,
                              ch: int | None) -> int:
    """Refuse a read that would restart a ramp; return the ``A`` value.

    ``S`` is the one such read.  Reading it releases the permanent
    shut-off latch, and with autostart armed the unit ramps straight back
    to its old set point without anyone asking (NHQ x2xx manual p.8).
    """
    if ch is None:
        return 0
    armed = dev.autostart(ch)
    if armed & 8:
        raise IsegRefused(
            f"refusing to read {letter}{ch}: autostart is armed "
            f"(A{ch}={armed}, {','.join(proto.decode_autostart(armed))}) and "
            f"{proto.STATE_CHANGING_READS[letter]}. Clear it with "
            f"'set A 0 --ch {ch}' or pass --yes to read it anyway.")
    return armed


def _mon_note(letter: str, raw: str, ch: int | None) -> str:
    """The parenthesised interpretation printed after a raw ``mon`` answer."""
    try:
        if letter == "#":
            return f"   ({proto.parse_identity(raw)})"
        if letter == "U":
            return f"   ({proto.parse_number(raw):+.2f} V)"
        if letter == "I":
            return f"   ({proto.parse_current(raw) * 1e6:.4g} uA)"
        if letter == "D":
            return f"   ({proto.parse_number(raw):+.2f} V)"
        if letter in ("M", "N"):
            return f"   ({int(raw)} % of the module maximum)"
        if letter == "V":
            return f"   ({int(raw)} V/s)"
        if letter == "W":
            return f"   ({int(raw)} ms)"
        if letter == "T":
            names = proto.decode_module_status(int(raw)) or ["-"]
            return f"   ({','.join(names)})"
        if letter == "A":
            names = proto.decode_autostart(int(raw)) or ["-"]
            return f"   ({','.join(names)})"
        if letter in ("S", "G"):
            word = (proto.parse_status_reply(raw, ch) if ch is not None
                    else raw.strip())
            return f"   ({proto.STATUS_WORDS.get(word, 'unknown')})"
    except ValueError:
        return ""
    return ""


def check_autostart(dev: IsegNHQ, ch: int, yes: bool) -> int:
    """Refuse when autostart is set; return the ``A`` value.

    With any autostart flag set the output follows ``D`` immediately and
    there is no ``G`` gate left to hold it back, so every path that writes
    ``D`` goes through here.
    """
    auto = dev.autostart(ch)
    if auto and not yes:
        raise IsegRefused(
            f"channel {ch} has autostart flags set (A{ch}={auto}, "
            f"{','.join(proto.decode_autostart(auto))}). With autostart the "
            f"output follows D immediately, so this tool cannot gate the "
            f"ramp on G. Clear it with 'set A 0 --ch {ch}' or pass --yes.")
    return auto


def check_voltage_limit(dev: IsegNHQ, ch: int, to_volts: float, yes: bool,
                        max_v: float = DEFAULT_MAX_V) -> str:
    """Refuse a set point above either limit; describe both.

    Two ceilings apply and the lower one wins.  The hardware one is Vmax
    times the rotary switch and can be forced with ``--yes`` (the unit
    would answer ``? UMAX=`` and clamp).  The software one, ``--max-v``,
    is the number the operator asked this tool to enforce because the
    switch cannot express it; ``--yes`` does **not** lift it, only
    ``--max-v`` itself does.
    """
    ident = dev.identify()
    limit_pct = dev.voltage_limit_pct(ch)
    hardware = ident.vmax_v * limit_pct / 100.0
    if to_volts > hardware and not yes:
        raise IsegRefused(
            f"{to_volts:g} V is above the hardware limit {hardware:g} V "
            f"(Vmax {ident.vmax_v:g} V, rotary switch at {limit_pct} %). Turn "
            f"the Vmax switch up or ask for less; the unit would answer "
            f"'? UMAX=', and it clamps the set point when it does.")
    line = f"limit = {ident.vmax_v:g} V x {limit_pct} % = {hardware:g} V"
    if not max_v:
        return line
    if to_volts > max_v:
        raise IsegRefused(
            f"{to_volts:g} V is above the software ceiling {max_v:g} V "
            f"(--max-v), with the hardware limit at {hardware:g} V "
            f"(Vmax {ident.vmax_v:g} V x {limit_pct} %). The effective limit "
            f"is the smaller of the two, {min(max_v, hardware):g} V. --yes "
            f"does not lift the software ceiling: pass --max-v with a "
            f"higher number, or --max-v 0 to switch it off.")
    return (f"{line}\nceiling = {max_v:g} V (software, --max-v); "
            f"effective limit {min(max_v, hardware):g} V")


def set_voltage_guarded(dev: IsegNHQ, ch: int, to_volts: float,
                        yes: bool = False,
                        max_v: float = DEFAULT_MAX_V) -> list[str]:
    """Write ``D`` with the same guards as ``ramp``, and read it back.

    Writing ``D`` is never just bookkeeping: with autostart armed it moves
    the output there and then, and above the Vmax switch it is refused by
    the unit.  So this is the only way ``D`` is written -- ``ramp`` and
    the ``set D`` subcommand both come through here.

    The read-back is the last check, and it is not theoretical: asked for
    9999 V with a 5600 V limit the bench unit answered ``? UMAX@=5600``
    and **stored 5600 V** (2026-09-21).  The error alone would leave the
    caller believing nothing happened, so the set point is read back
    either way and ``G`` is never sent on a number nobody asked for.
    """
    proto.check_channel(ch)
    if to_volts < 0:
        raise IsegRefused("the set voltage is a magnitude, give a positive "
                          "value; polarity is a switch on the side cover")
    lines = [f"A{ch} = {check_autostart(dev, ch, yes)}",
             check_voltage_limit(dev, ch, to_volts, yes, max_v)]
    note = proto.rounding_note("D", to_volts, dev.series_or_default)
    if note:
        lines.append(note)
    try:
        dev.set_voltage(ch, to_volts)
    except IsegNHQError as err:
        if not proto.is_umax_error(err.reply):
            raise
        stored = dev.read_set_voltage(ch)
        raise IsegNHQError(
            err.command, err.reply,
            f"{err.meaning}: asked for {to_volts:g} V, and D{ch} now reads "
            f"back {stored:g} V. Nothing was started, but the set point has "
            f"changed -- run 'off --ch {ch}' to put it back to 0 V before "
            f"trying again") from None
    readback = dev.read_set_voltage(ch)
    lines.append(f"D{ch} = {readback:g} V")
    resolution = proto.SERIES_RESOLUTION_V[dev.series_or_default]
    if abs(readback - to_volts) > resolution + 1e-9:
        raise IsegRefused(
            f"asked for D{ch}={to_volts:g} V and the unit reads back "
            f"{readback:g} V, more than the {resolution:g} V resolution of "
            f"the {dev.series_or_default} series apart. Nothing was started, "
            f"but the set point has changed -- run 'off --ch {ch}' to put it "
            f"back to 0 V, then find out why it did not take (Vmax switch, a "
            f"model with a coarser DAC) before sending G.")
    return lines


def ramp_text(dev: IsegNHQ, ch: int, to_volts: float,
              speed: float | None = None, yes: bool = False,
              max_v: float = DEFAULT_MAX_V) -> str:
    """Check the unit is in a state to ramp, then set the point and start.

    Five checks, all of them cheap.  ``--yes`` skips the first three; the
    software ceiling and the read-back it does not skip.  Autostart off (otherwise ``D`` alone moves the output and
    the operator loses the ``G`` gate), the channel not in ``MAN`` or
    ``OFF`` (the front panel wins and nothing would happen), the target
    below the Vmax rotary switch setting (the unit would answer
    ``? UMAX=`` -- refusing here says which two numbers disagree), and the
    set point reading back as what was asked for before ``G`` goes out.

    Note that the ``S`` read below is itself a state change when autostart
    is armed -- which is why the autostart check happens first and refuses.
    """
    proto.check_channel(ch)
    if to_volts < 0:
        raise IsegRefused("the set voltage is a magnitude, give a positive "
                          "value; polarity is a switch on the side cover")
    lines: list[str] = [f"A{ch} = {check_autostart(dev, ch, yes)}"]

    status = dev.status(ch)
    if status in ("MAN", "OFF") and not yes:
        raise IsegRefused(
            f"channel {ch} is {status} ({proto.STATUS_WORDS[status]}). Set "
            f"the front panel CONTROL switch to its lower (DAC) position and "
            f"switch HV-ON on, then try again, or pass --yes to send the "
            f"commands anyway (they will be accepted and do nothing).")
    lines.append(f"S{ch} = {status}")

    if speed is not None:
        dev.set_ramp(ch, speed)
        lines.append(f"V{ch} = {dev.ramp_speed(ch)} V/s")
    # the autostart line from set_voltage_guarded repeats the one above,
    # so drop it and keep the limit, the rounding note and the read-back
    lines.extend(set_voltage_guarded(dev, ch, to_volts, yes, max_v)[1:])
    word = dev.start(ch)
    lines.append(f"G{ch} -> {word} "
                 f"({proto.STATUS_WORDS.get(word, 'unknown')})")
    return "\n".join(lines)


def set_text(dev: IsegNHQ, letter: str, ch: int | None, value: str,
             yes: bool = False, max_v: float = DEFAULT_MAX_V) -> str:
    """Write one command letter, with the guards the letter deserves.

    ``set`` used to be the back door around every check in ``ramp``:
    ``set D`` moves the output the moment autostart is armed, and
    ``set A 8`` is what arms it -- after which the next ``S`` anywhere in
    this tool restores the voltage.  Both now go through the same
    refusals, and both can still be forced with ``--yes``.  ``V``, ``L``
    and ``W`` cannot move the output on their own and are sent as they
    always were.
    """
    letter = letter.upper()
    number = float(value)
    if letter == "D":
        if ch is None:
            raise ValueError("D needs --ch")
        lines = set_voltage_guarded(dev, ch, number, yes, max_v)
        lines.append(f"D{ch} written; the output follows on G "
                     f"(or at once if autostart is armed)")
        return "\n".join(lines)
    if letter == "A" and int(number) & 8 and not yes:
        raise IsegRefused(
            f"A{ch}={int(number)} arms autostart. After this the output "
            f"follows D with no G to gate it, and reading the status word "
            f"restores the set voltage of a channel that was shut off. "
            f"Pass --yes if that is what you want.")
    dev.send(letter, ch, number)
    sent = proto.build_command(letter, ch, number, dev.series_or_default)
    note = proto.rounding_note(letter, number, dev.series_or_default)
    return f"{sent} OK" + (f"\n{note}" if note else "")


def off_text(dev: IsegNHQ, ch: int) -> str:
    """Set the channel's set point to zero and start the ramp down."""
    proto.check_channel(ch)
    dev.set_voltage(ch, 0)
    word = dev.start(ch)
    return (f"D{ch} = 0 V\n"
            f"G{ch} -> {word} ({proto.STATUS_WORDS.get(word, 'unknown')})")


def _watch_set_point(dev: IsegNHQ, ch: int) -> float | None:
    """``D<n>`` for the settle check, or ``None`` if it cannot be read.

    A set point that does not come back is not a reason to stop watching:
    the caller then falls back to the status word alone, which is what
    the watch did before the read-back requirement existed.
    """
    try:
        return dev.read_set_voltage(ch)
    except (ValueError, IsegNHQError, ProtocolError):
        return None


def watch_lines(dev: IsegNHQ, ch: int, interval: float = 1.0,
                until: str = "any", max_s: float = 120.0,
                yes: bool = False, settle_v: float = DEFAULT_SETTLE_V):
    """Yield one ``time S U I`` line per poll until the channel settles.

    ``until`` is ``ON``, ``OFF`` or ``any`` (stop on either).  The generator
    always ends, at the latest after ``max_s`` seconds.

    **The status word alone does not mean the output has arrived.**  The
    bench 208L answers ``S=ON`` the moment its internal set point is
    reached, while the measured voltage is still several volts away and
    goes on moving for a few seconds (2026-09-21: the watch stopped with
    ``S=ON`` at U = -46 V for D = 50, and again at U = -25 V for D = 0).
    So reaching a target status is only half of the condition: the watch
    also wants ``|U - D| <= settle_v`` before it returns, and keeps
    polling until then or until ``max_s``.  ``settle_v=0`` drops that
    second half.  ``D`` is read once before the first poll and again the
    first time the status turns into a target, because the set point may
    have been written between the two.  ``S=OFF`` ends the watch straight
    away whatever ``U`` says: with the front panel switch off the output
    is dead and the measured voltage means nothing.

    Watching means reading ``S`` once a second, and reading ``S`` is not
    free: with autostart armed it restores the set voltage of a channel
    that has been shut off, so a watch left running would silently bring
    a tripped channel back up -- once a second, for as long as it takes.
    The autostart flags are therefore checked once before the first poll
    and the watch is refused unless ``yes``.

    A single bad answer does not end the watch: an error reply is printed
    in place of the values and the next poll goes ahead, because the point
    of watching is to keep looking while the channel does something.
    """
    proto.check_channel(ch)
    if not yes:
        guard_state_changing_read(dev, "S", ch)
    targets = ("ON", "OFF") if until == "any" else (until.upper(),)
    set_point = _watch_set_point(dev, ch) if settle_v > 0 else None
    reread_set_point = True
    start = time.monotonic()
    deadline = start + max_s
    while True:
        now = time.monotonic()
        try:
            status = dev.status(ch)
        except (IsegNHQError, ProtocolError) as err:
            reply = err.reply if isinstance(err, IsegNHQError) else str(err)
            yield f"{now - start:7.1f} s  S=?     <{reply}>"
            if time.monotonic() >= deadline:
                yield f"stopped after {max_s:g} s without a status word"
                return
            time.sleep(max(0.0, interval - (time.monotonic() - now)))
            continue
        measured: float | None
        try:
            measured = dev.read_voltage(ch)
            volts = f"{measured:+9.2f} V"
        except (ValueError, IsegNHQError):
            measured = None
            volts = "        ? V"
        try:
            micro = f"{dev.read_current(ch) * 1e6:10.4g} uA"
        except (ValueError, IsegNHQError):
            micro = "         ? uA"
        yield f"{now - start:7.1f} s  S={status:<4s}  U={volts}  I={micro}"
        settled = False
        if status in targets:
            if settle_v <= 0 or status == "OFF":
                settled = True
            else:
                if reread_set_point:
                    reread_set_point = False
                    again = _watch_set_point(dev, ch)
                    if again is not None:
                        set_point = again
                settled = set_point is None or (
                    measured is not None
                    and abs(abs(measured) - abs(set_point)) <= settle_v)
        else:
            reread_set_point = True
        if settled:
            if set_point is not None:
                target = f"{set_point:+9.2f} V"
            elif settle_v <= 0:
                target = " not read"
            else:
                target = "        ? V"
            yield f"settled: S={status} U={volts} D={target}"
            return
        if time.monotonic() >= deadline:
            tail = "" if status not in targets else " (U never settled)"
            yield f"stopped after {max_s:g} s with S={status}{tail}"
            return
        time.sleep(max(0.0, interval - (time.monotonic() - now)))


def raw_text(dev: IsegNHQ, text: str) -> str:
    """Send a hand-written command line, print what came back."""
    command = text.strip()
    answer = dev.command(command)
    return f">>> {command}\n<<< {answer!r}"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="/dev/ttyUSB0", help="serial device")
    parser.add_argument("--timeout", type=float, default=1.0,
                        help="inter-character deadline for an answer, in "
                             "seconds; raised to 4 x break time + 0.2 s if "
                             "the unit asks for more")
    parser.add_argument("--echo-timeout", type=float, default=0.3,
                        help="per-byte echo timeout in seconds")
    parser.add_argument("--max-v", type=float, default=DEFAULT_MAX_V,
                        dest="max_v", metavar="VOLTS",
                        help="software ceiling on every set point "
                             "(default %(default)g V, 0 disables it). The "
                             "Vmax rotary switch only steps in 10 %% of the "
                             "module maximum, so it cannot express this. "
                             "--yes does not lift it.")
    parser.add_argument("--verbose", action="store_true",
                        help="print every byte in both directions")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="identity and break time")

    p_dump = sub.add_parser("dump", help="all readable values per channel")
    p_dump.add_argument("--ch", type=int, nargs="+", default=None,
                        choices=list(proto.CHANNELS),
                        help="channels to read (default: both)")

    readable = " ".join(sorted(letter for letter, spec
                               in proto.COMMANDS.items() if spec.readable))
    p_mon = sub.add_parser("mon", help="read one command letter")
    p_mon.add_argument("letter", help="one of " + readable +
                       " (G is not a read: use 'ramp')")
    p_mon.add_argument("--ch", type=int, default=None,
                       help="channel, for the per-channel commands")
    p_mon.add_argument("--yes", action="store_true",
                       help="read S even with autostart armed, when it "
                            "would restore the set voltage")

    p_set = sub.add_parser("set", help="write one command letter")
    p_set.add_argument("letter", help="one of D V L A W")
    p_set.add_argument("value")
    p_set.add_argument("--ch", type=int, default=None,
                       help="channel, for the per-channel commands")
    p_set.add_argument("--yes", action="store_true",
                       help="skip the refusals guarding D and A=8")

    p_ramp = sub.add_parser("ramp", help="set a voltage and start the ramp")
    p_ramp.add_argument("--ch", type=int, required=True)
    p_ramp.add_argument("--to", type=float, required=True, dest="to_volts",
                        help="set voltage in V (magnitude)")
    p_ramp.add_argument("--speed", type=float, default=None,
                        help="ramp speed in V/s, 2 to 255")
    p_ramp.add_argument("--yes", action="store_true",
                        help="skip the autostart / MAN / limit refusals")

    p_off = sub.add_parser("off", help="set the channel to 0 V and ramp down")
    p_off.add_argument("--ch", type=int, required=True)

    p_watch = sub.add_parser(
        "watch", help="poll S, U and I until the status word reaches its "
                      "target AND the measured voltage has caught up with D")
    p_watch.add_argument("--ch", type=int, required=True)
    p_watch.add_argument("--interval", type=float, default=1.0,
                         help="seconds between polls")
    p_watch.add_argument("--until", default="any", choices=("ON", "OFF", "any"),
                         help="status word to stop on (default: ON or OFF)")
    p_watch.add_argument("--max-s", type=float, default=120.0,
                         dest="max_s", help="give up after this many seconds")
    p_watch.add_argument("--settle-v", type=float,
                         default=DEFAULT_SETTLE_V, dest="settle_v",
                         metavar="VOLTS",
                         help="how close U has to be to D before the "
                              "channel counts as settled (default "
                              "%(default)g V, 0 stops on the status word "
                              "alone). The 208L says S=ON as soon as its "
                              "internal set point is reached, while the "
                              "measured voltage is still a few volts out "
                              "and needs a few more seconds; S=OFF still "
                              "ends the watch at once.")
    p_watch.add_argument("--yes", action="store_true",
                         help="poll S even with autostart armed, when each "
                              "poll would restore the set voltage")

    p_raw = sub.add_parser(
        "raw", help="send a hand-written command line (UNGUARDED: none of "
                    "the autostart, limit or read-back checks apply)")
    p_raw.add_argument("text")
    return parser


def _require_channel(letter: str, ch: int | None) -> int | None:
    """Channel argument check done here so the message names the command."""
    spec = proto.COMMANDS.get(letter.upper())
    if spec is None:
        raise ValueError(f"unknown command {letter!r}, known: "
                         f"{' '.join(sorted(proto.COMMANDS))}")
    if spec.per_channel:
        if ch is None:
            raise ValueError(f"{letter.upper()} needs --ch")
        return proto.check_channel(ch)
    if ch is not None:
        raise ValueError(f"{letter.upper()} takes no --ch")
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dev = IsegNHQ(args.port, timeout=args.timeout,
                  echo_timeout=args.echo_timeout, verbose=args.verbose)
    try:
        with dev:
            if args.cmd == "info":
                print(info_text(dev, args.max_v))
            elif args.cmd == "dump":
                print(dump_text(dev, args.ch))
            elif args.cmd == "mon":
                ch = _require_channel(args.letter, args.ch)
                print(mon_text(dev, args.letter, ch, args.yes))
            elif args.cmd == "set":
                letter = args.letter.upper()
                spec = proto.COMMANDS.get(letter)
                if spec is None:
                    raise ValueError(f"unknown command {letter!r}")
                if not spec.settable:
                    print(f"{letter} is read-only", file=sys.stderr)
                    return 2
                ch = _require_channel(letter, args.ch)
                print(set_text(dev, letter, ch, args.value, args.yes,
                               args.max_v))
            elif args.cmd == "ramp":
                print(ramp_text(dev, args.ch, args.to_volts, args.speed,
                                args.yes, args.max_v))
            elif args.cmd == "off":
                print(off_text(dev, args.ch))
            elif args.cmd == "watch":
                for line in watch_lines(dev, args.ch, args.interval,
                                        args.until, args.max_s, args.yes,
                                        args.settle_v):
                    print(line, flush=True)
            elif args.cmd == "raw":
                print(raw_text(dev, args.text))
    except PortBusy as err:
        print(err, file=sys.stderr)
        return 3
    except IsegNHQError as err:
        print(f"error: the unit answered {err.reply!r} ({err.meaning})",
              file=sys.stderr)
        print(f"  command: {err.command}", file=sys.stderr)
        return 1
    except ProtocolError as err:
        # the unit answered something we could not read: its fault, not
        # the operator's, so it exits 1 with the error replies and not 2
        # with the refusals.  ProtocolError is a ValueError, so this has
        # to come first.
        print(f"error: unit answered something we could not parse: {err}",
              file=sys.stderr)
        return 1
    except (IsegRefused, ValueError) as err:
        print(f"refused: {err}", file=sys.stderr)
        return 2
    except (IsegNHQTimeout, IsegEchoError, IsegLinkError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
