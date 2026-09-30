"""iseg NHQ RS232 protocol tables and helpers.

Single source of truth for the command letters, value formats, status words
and module-status bits of the iseg NHQ NIM high voltage supplies, shared by
``iseg_nhq_probe.py`` (client) and the pty emulator.  Sources: the two
manuals in ``doc/`` -- NHQ x2xx (precision series) v3.06 and NHQ x0x
(standard series) v2.04.  The two differ only in the value formats, see
"Value formats" below; everything else in the command table is identical.

Line discipline
---------------
9600 bit/s, 8 data bits, no parity, 1 stop bit, no flow control.  A 1:1
extension cord, **not** a null modem cable: RxD is pin 2, TxD pin 3, GND
pin 5 on both ends.

Framing and the echo handshake
------------------------------
Every command is ASCII text terminated by ``<CR><LF>`` (``\\r\\n``).

Input to the unit is synchronised **byte by byte using echo**: the host
writes one byte and must wait until the unit has echoed that same byte
before writing the next one.  This applies to the terminator too, so once
the last byte of ``\\r\\n`` has come back the whole command line has already
been echoed.

**Bench-confirmed** on an NHQ 208L (unit 481198, software 2.06) over a
Keyspan USB adapter, 2026-09-21: there is no second copy of the echoed
line and no blank line in front of the answer.  The answer follows the
echoed ``\\n`` immediately, in the same read chunk -- the trace of an
``S2`` reads ``rx b'\\nS2=ON \\r\\n'``.  The vendor's own example (NHQ x2xx
manual p.9) nevertheless reads *eight* characters after the echo of
``U1\\r\\n`` where a standard-series ``+1234\\r\\n`` is seven, so some
firmware may well prepend a ``\\n``; the client skips empty lines before
the answer rather than depend on either behaviour
(:meth:`iseg_nhq_probe.IsegNHQ._read_answer`), and it must tolerate the
answer arriving in the same chunk as the last echoed byte
(:meth:`iseg_nhq_probe.IsegNHQ._send_echoed`, which keeps the surplus).

A mismatched echo means the two sides are out of step; recover by sending a
bare ``\\r\\n`` with the handshake disabled and draining whatever comes
back (:func:`iseg_nhq_probe.IsegNHQ.sync`).  The manual only says that this
bare ``\\r\\n`` "assures synchronisation" between computer and supply after
opening the port; it does not promise that it resets the unit's parser or
that anything is sent back.  On the bench the 208L answered that first
sync with an unsolicited ``S2=ON \\r\\n``, apparently left over from an
earlier session -- so the drain after the sync has to be long enough to
swallow a whole line (at least ``4 x W + 50 ms``), and every command
flushes the input before it sends.

Output from the unit is asynchronous, with a programmable gap between
characters (the "break time", command ``W``, 0...255 ms, 3 ms from the
factory) so that a slow host can keep up.  After the echo the unit sends::

    read command    answer text  \\r\\n          e.g.  "U2"   -> "+1234-01"
    write command   \\r\\n  (an empty line)      e.g.  "D2=50" -> ""
    G<n>            "S<n>=xxx"  \\r\\n            e.g.  "G2"   -> "S2=ON "
    S<n>            "S<n>=xxx"  \\r\\n            e.g.  "S2"   -> "S2=ON "

The last line is bench-confirmed and differs from the manual, which shows
``S1 -> xxx`` (a bare status word) and reserves the ``S<n>=`` prefix for
the ``G`` reply.  The 208L prefixes both, trailing space included, so
:func:`parse_status_reply` accepts either shape -- and insists that the
channel in the prefix is the channel that was asked for.

Value formats
-------------
Leading zeroes may be omitted on input; output is fixed format.

* Voltage, precision series: ``{polarity}{mantissa}{exponent with sign}``,
  ``+12345-01`` = +1234.5 V.
* Voltage, standard series: ``{polarity}{voltage}``, ``+1234`` = 1234 V.
* Current, both series: ``{mantissa}{exponent with sign}`` in **amperes**,
  ``12345-06`` = 12345e-6 A.

:func:`parse_number` accepts both voltage shapes (the exponent group is
optional); :func:`parse_current` requires the exponent.

The two series also disagree about ``L``, the current trip: the precision
series answers amperes (mantissa and exponent), the standard series a
plain count in the resolution of the first current range, and ``L<n>=``
takes that same count.  Which series is on the other end is not in the
``#`` identity, so it is worked out from the shape of the first ``U``,
``D`` or ``L`` answer -- see :func:`series_of_reply`.

Reads that change the state of the unit
---------------------------------------
``S`` is **not** a harmless read.  After a permanent shut-off (current
trip, or ERR / INH with KILL enabled) the status word has to be read once
before the voltage can be restored, and with autostart active (``A<n>=8``)
reading it is enough: "the previous voltage setting will be restored with
software ramp after 'Read status word'" (NHQ x2xx manual p.8).  Anything
that polls ``S`` therefore has to look at ``A`` first; the letters listed
in :data:`STATE_CHANGING_READS` are the ones that can move the output.

Error replies
-------------
The unit answers a bad command with a line starting with ``?``::

    ????            syntax error
    ?WCN            wrong channel number
    ?TOT            timeout error, the unit re-initialises itself
    ? UMAX=nnnn     set voltage above the Vmax hardware limit

Note the space in the last one -- and that the bench 208L actually
answered ``? UMAX@=5600``, with a stray ``@``, so :func:`is_error` matches
that reply with a regular expression rather than a fixed prefix.  Anything
else starting with ``?`` is reported verbatim as an unknown error.

**The UMAX reply is not a refusal.** The 208L clamps the set point to the
hardware limit and *stores* it: after ``D2=9999`` on a 5600 V limit,
``D2`` reads back ``5600``.  So a caller that gets this error has already
changed the set point and has to deal with it -- see
:func:`parse_umax_limit` and ``iseg_nhq_probe.set_voltage_guarded``,
which reads ``D`` back and refuses to send ``G``.

Front panel interaction
-----------------------
The CONTROL switch must be in its lower (DAC) position for the interface to
change the output voltage; in the upper (manual) position commands are
accepted and answered but have no effect on the output.  The HV-ON switch
must be on.  After a permanent shut-off (ERR or INH with KILL enabled, or a
current trip) the status word has to be read once before ``G`` can restore
the voltage.  With autostart active (``A<n>=8``) the unit ramps on its own
after ``D``, at power-on, and on OFF -> ON, and ``G`` is not needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TERMINATOR = "\r\n"

#: the only channel numbers an NHQ accepts
CHANNELS = (1, 2)


# --------------------------------------------------------------------------
# command table
# --------------------------------------------------------------------------
class ProtocolError(ValueError):
    """The unit answered something this module cannot parse.

    A subclass of ``ValueError`` so the existing ``except ValueError``
    sites keep working, but distinguishable from an operator mistake: the
    CLI exits 1 for this (the unit said something unexpected) and 2 for a
    plain ``ValueError`` (we refused locally / the arguments were wrong).
    """


@dataclass(frozen=True)
class Cmd:
    """One command letter of the RS232 set.

    ``answers`` and ``readable`` are not the same thing.  ``answers`` means
    the bare ``<letter><ch>`` form is accepted and comes back with a line.
    ``readable`` means sending it is a genuine read that does not touch the
    output.  ``G`` answers but is not readable -- it starts a ramp.  ``S``
    answers and is nominally a read, but see :data:`STATE_CHANGING_READS`.
    """

    letter: str
    name: str
    per_channel: bool
    readable: bool          # a genuine read: sending it changes nothing
    settable: bool          # accepts "<letter><ch>=<value>"
    lo: float | None = None
    hi: float | None = None
    unit: str = ""
    desc: str = ""
    answers: bool = True    # the bare form is accepted and returns a line


def _c(*args, **kwargs) -> tuple[str, Cmd]:
    cmd = Cmd(*args, **kwargs)
    return cmd.letter, cmd


COMMANDS: dict[str, Cmd] = dict(
    [
        _c("#", "identify", False, True, False, unit="",
           desc="module identifier: unit;software;Vmax;Imax"),
        _c("W", "break_time", False, True, True, lo=0, hi=255, unit="ms",
           desc="gap the unit leaves between output characters"),
        _c("U", "voltage", True, True, False, unit="V",
           desc="measured output voltage"),
        _c("I", "current", True, True, False, unit="A",
           desc="measured output current"),
        _c("M", "voltage_limit", True, True, False, lo=0, hi=100, unit="%",
           desc="Vmax hardware limit, rotary switch, in % of Vout max"),
        _c("N", "current_limit", True, True, False, lo=0, hi=100, unit="%",
           desc="Imax hardware limit, rotary switch, in % of Iout max"),
        _c("D", "set_voltage", True, True, True, lo=0, unit="V",
           desc="voltage set point; takes effect on G, or at once with autostart"),
        _c("V", "ramp_speed", True, True, True, lo=2, hi=255, unit="V/s",
           desc="software ramp speed"),
        _c("G", "start", True, False, False, unit="", answers=True,
           desc="START THE RAMP; answers S<n>=xxx -- not a read"),
        _c("L", "current_trip", True, True, True, lo=0, unit="",
           desc="current trip in the resolution of the first range; 0 disables it"),
        _c("S", "status", True, True, False, unit="",
           desc="channel status word; re-arms a tripped channel, see "
                "STATE_CHANGING_READS"),
        _c("T", "module_status", True, True, False, lo=0, hi=255, unit="",
           desc="module status byte, see T_BITS"),
        _c("A", "autostart", True, True, True, lo=0, hi=15, unit="",
           desc="autostart and EEPROM save flags, see AUTOSTART_BITS"),
    ]
)

#: commands ``iseg_nhq_probe.py dump`` reads for every channel, in order.
#: ``A`` and ``T`` come first on purpose: reading ``S`` restores the
#: voltage of a tripped channel when autostart is set, so the dump has to
#: know the autostart flags and the module status before it gets there.
DUMP_COMMANDS = ("A", "T", "S", "M", "N", "D", "V", "L", "U", "I")

#: reads that can move the output, letter -> why.  ``S`` releases the
#: permanent shut-off latch, and with ``A<n>=8`` that alone restarts the
#: software ramp to the old set point (NHQ x2xx manual p.8).
STATE_CHANGING_READS: dict[str, str] = {
    "S": "reading the status word releases a permanent shut-off; with "
         "autostart (A=8) the previous set voltage is restored at once",
}

#: how fine a set point the two series accept, in volts
SERIES_RESOLUTION_V: dict[str, float] = {"precision": 0.1, "standard": 1.0}

#: what the two series mean by ``L``
SERIES_TRIP_UNIT: dict[str, str] = {"precision": "A", "standard": "counts"}


# --------------------------------------------------------------------------
# status words and bit fields
# --------------------------------------------------------------------------
#: channel status word (reply to ``S<n>`` and to ``G<n>``)
STATUS_WORDS: dict[str, str] = {
    "ON": "output voltage has reached the set voltage",
    "OFF": "channel switched off at the front panel",
    "MAN": "channel is on but set to manual control",
    "ERR": "Vmax or Imax is or was exceeded",
    "INH": "INHIBIT signal is or was active",
    "QUA": "quality of the output voltage not given at present",
    "L2H": "output voltage increasing",
    "H2L": "output voltage decreasing",
    "LAS": "look at status, only after a G command",
    "TRP": "current trip was active",
}

#: module status byte (reply to ``T<n>``), value -> name
T_BITS: dict[int, str] = {
    128: "QUA",
    64: "ERR",
    32: "INH",
    16: "KILL_ENA",
    8: "OFF",
    4: "POL_POS",
    2: "MAN",
    1: "DISPLAY",
}

#: what each module-status bit means
T_BIT_DESC: dict[str, str] = {
    "QUA": "quality of the output voltage not given at present",
    "ERR": "Vmax or Imax is or was exceeded",
    "INH": "INHIBIT signal is or was active",
    "KILL_ENA": "KILL switch is in the ENABLE position",
    "OFF": "front panel HV-ON switch is in the OFF position",
    "POL_POS": "polarity set to positive (clear = negative)",
    "MAN": "control is manual (clear = via RS232)",
    "DISPLAY": "T1: display shows voltage (clear = current); "
               "T2: channel A selected (clear = channel B)",
}

#: autostart / EEPROM flags (value written with ``A<n>=``)
AUTOSTART_BITS: dict[int, str] = {
    8: "AUTOSTART",
    4: "SAVE_TRIP",
    2: "SAVE_VSET",
    1: "SAVE_RAMP",
}

#: the error replies of the unit, prefix -> meaning
ERROR_REPLIES: dict[str, str] = {
    "????": "syntax error",
    "?WCN": "wrong channel number",
    "?TOT": "timeout error, the unit re-initialises itself",
    "? UMAX=": "set voltage exceeds the Vmax hardware limit, "
               "and the unit has clamped the set point to it",
}

#: the UMAX reply, matched loosely.  The manual writes ``?<SP>UMAX=nnnn``;
#: the bench 208L answered ``? UMAX@=5600`` (2026-09-21), so whatever the
#: firmware puts between UMAX and the ``=`` is ignored.
_UMAX_RE = re.compile(r"^\?\s*UMAX[^=]*=\s*(\d+)")


def decode_module_status(value: int) -> list[str]:
    """Names of the bits set in a ``T`` reply, most significant first."""
    return [name for bit, name in sorted(T_BITS.items(), reverse=True)
            if value & bit]


def decode_autostart(value: int) -> list[str]:
    """Names of the flags set in an ``A`` reply, most significant first."""
    return [name for bit, name in sorted(AUTOSTART_BITS.items(), reverse=True)
            if value & bit]


def is_error(reply: str) -> str | None:
    """Description of the error reply ``reply``, or ``None`` if it is not one.

    Any answer line starting with ``?`` is an error; the four documented
    ones are named, anything else is reported verbatim.
    """
    text = reply.strip()
    if not text.startswith("?"):
        return None
    if _UMAX_RE.match(text):
        return ERROR_REPLIES["? UMAX="]
    for prefix, meaning in ERROR_REPLIES.items():
        if text.startswith(prefix):
            return meaning
    return "unknown error reply"


def parse_umax_limit(reply: str) -> float | None:
    """The limit out of a ``? UMAX=nnnn`` reply, or ``None``.

    Tolerates the junk the bench unit puts in the middle
    (``? UMAX@=5600``).  The number is the hardware limit the unit
    applied -- and, since it clamps and stores rather than ignoring the
    command, also the set point it is now holding.
    """
    match = _UMAX_RE.match(reply.strip())
    return float(match.group(1)) if match else None


def is_umax_error(reply: str) -> bool:
    """Is this the "set voltage exceeds the limit" reply, in any spelling?"""
    return _UMAX_RE.match(reply.strip()) is not None


def check_channel(ch: int) -> int:
    """Return ``ch`` if the unit has it, otherwise raise ``ValueError``.

    Checking locally keeps the unit from answering ``?WCN`` and gives the
    operator a message that names the valid channels.
    """
    if ch not in CHANNELS:
        raise ValueError(f"channel {ch} does not exist, use "
                         f"{' or '.join(str(c) for c in CHANNELS)}")
    return ch


# --------------------------------------------------------------------------
# value parsing
# --------------------------------------------------------------------------
#: voltage: polarity, mantissa (a decimal point is tolerated, the display
#: format has none but the manual writes set points as ``nnnn.nn``),
#: optional exponent with sign
_NUMBER_RE = re.compile(r"^([+-]?)(\d+(?:\.\d*)?)([+-]\d+)?$")

#: current: optional sign, mantissa and exponent with sign, in amperes
_CURRENT_RE = re.compile(r"^([+-]?)(\d+(?:\.\d*)?)([+-]\d+)$")

#: identity: unit number ; software release ; Vout max ; Iout max
_IDENT_RE = re.compile(r"^\s*([^;]*);([^;]*);([^;]*);([^;]*?)\s*$")

#: a quantity with a unit suffix, e.g. "3000V", "6kV", "1mA", "500uA"
_QUANTITY_RE = re.compile(r"^\s*([0-9.]+)\s*([a-zA-Zµ]*)\s*$")

#: multipliers for the unit suffixes the ``#`` reply uses
UNIT_SCALE: dict[str, float] = {
    "": 1.0,
    "v": 1.0,
    "kv": 1e3,
    "mv": 1e-3,
    "a": 1.0,
    "ma": 1e-3,
    "ua": 1e-6,
    "µa": 1e-6,
    "na": 1e-9,
}


def parse_number(text: str) -> float:
    """Parse a voltage reply of either series.

    ``+12345-01`` (precision series) is 1234.5, ``+1234`` (standard series)
    is 1234.0, ``-500`` is -500.0.  The value is mantissa times ten to the
    exponent; without an exponent group it is plain volts.  A decimal point
    in the mantissa is accepted (the manual writes set points as
    ``nnnn.nn``, so a unit may well echo one back).
    """
    match = _NUMBER_RE.match(text.strip())
    if match is None:
        raise ProtocolError(f"cannot parse number {text!r}")
    sign, mantissa, exponent = match.groups()
    value = float(mantissa)
    if exponent:
        value *= 10.0 ** int(exponent)
    return -value if sign == "-" else value


def parse_current(text: str) -> float:
    """Parse a current reply in amperes.

    The unit always sends mantissa and signed exponent here, e.g.
    ``12345-06`` = 12345e-6 A.  A leading sign is accepted: the current is
    a magnitude on both series, but the polarity switch makes a signed
    reply plausible and it costs nothing to take one.
    """
    match = _CURRENT_RE.match(text.strip())
    if match is None:
        raise ProtocolError(f"cannot parse current {text!r}")
    sign, mantissa, exponent = match.groups()
    value = float(mantissa) * 10.0 ** int(exponent)
    return -value if sign == "-" else value


def series_of_reply(text: str) -> str | None:
    """Which series sent this ``U`` / ``D`` / ``L`` reply, if it can tell.

    The precision series always appends a signed exponent
    (``+00500-01``), the standard series never does (``+0050``).  The
    identity does not say which unit is on the other end, so this is how
    the client finds out.  ``None`` means the text is neither shape and
    nothing should be concluded from it.
    """
    match = _NUMBER_RE.match(text.strip())
    if match is None:
        return None
    return "precision" if match.group(3) else "standard"


@dataclass(frozen=True)
class CurrentTrip:
    """An ``L`` reading together with what its number means.

    The two series answer ``L`` in different quantities and nothing in the
    reply says which, so the value never travels on its own.
    """

    value: float
    unit: str            # "A" on the precision series, "counts" on the other

    def __str__(self) -> str:
        if self.value == 0:
            return "0 (no current trip)"
        if self.unit == "A":
            return f"{self.value * 1e6:.4g} uA"
        return f"{self.value:g} counts of the first current range"


def parse_quantity(text: str) -> float:
    """Parse ``3000V`` / ``6kV`` / ``4mA`` into volts or amperes."""
    match = _QUANTITY_RE.match(text)
    if match is None:
        raise ProtocolError(f"cannot parse quantity {text!r}")
    number, suffix = match.groups()
    scale = UNIT_SCALE.get(suffix.lower())
    if scale is None:
        raise ProtocolError(f"unknown unit {suffix!r} in {text!r}")
    return float(number) * scale


@dataclass(frozen=True)
class Identity:
    """The reply to ``#``: ``nnnnnn;n.nn;xxxxV;xmA``."""

    unit: str
    software: str
    vmax_v: float
    imax_a: float

    def __str__(self) -> str:
        return (f"unit {self.unit}, software {self.software}, "
                f"Vmax {self.vmax_v:g} V, Imax {self.imax_a * 1e3:g} mA")


def parse_identity(text: str) -> Identity:
    """Parse the ``#`` reply.

    The unit and software fields are kept as text (the unit number has
    leading zeroes that mean something to iseg).  Vmax and Imax carry their
    unit letters, which vary by model, so they are parsed generically and
    returned in volts and amperes.
    """
    match = _IDENT_RE.match(text)
    if match is None:
        raise ProtocolError(f"cannot parse identity {text!r}")
    unit, software, vmax, imax = (part.strip() for part in match.groups())
    return Identity(unit=unit, software=software,
                    vmax_v=parse_quantity(vmax), imax_a=parse_quantity(imax))


def parse_status_reply(text: str, ch: int) -> str:
    """Pull the status word out of an ``S<n>`` or ``G<n>`` reply.

    Both come back as ``S2=ON `` on the 208L (bench, 2026-09-21); the
    manual shows a bare ``ON `` for ``S<n>`` and only prefixes the ``G``
    reply, so both shapes are accepted.

    The channel number in the prefix has to be the one that was asked for:
    an ``S1=`` coming back from ``G2`` or ``S2`` means the answers have
    slipped by one command and the status word belongs to the other
    channel, which is exactly the mistake that would hide a ramp on the
    wrong output.
    """
    body = text.strip()
    for other in CHANNELS:
        prefix = f"S{other}="
        if body.startswith(prefix):
            if other != ch:
                raise ProtocolError(
                    f"asked about channel {ch} and got {body!r}, which is "
                    f"channel {other}: the link is out of step")
            return body[len(prefix):].strip()
    return body


#: the ``G`` reply has the same shape; one parser does both
parse_start_reply = parse_status_reply


# --------------------------------------------------------------------------
# building command lines
# --------------------------------------------------------------------------
def format_value(letter: str, value: float | int | str,
                 series: str = "precision") -> str:
    """Render a value the way the unit expects it after ``=``.

    ``D`` is the only command whose two series disagree: the precision
    series takes ``nnnn.nn``, the standard series only whole volts.  An
    integral set point is written without a decimal point, which both
    series accept.  A fractional one gets two decimals on the precision
    series and is **rounded to whole volts on the standard series**, which
    would otherwise answer ``????`` -- see :func:`rounding_note`, which the
    callers print so the operator sees the number that actually went out.
    Rounding is Python's round-half-even, so 50.5 V is sent as 50 V and
    51.5 V as 52 V.

    ``series`` defaults to ``"precision"`` because that is the format that
    carries the most information; the client overrides it once it knows
    what is on the other end.  Everything else -- ``W``, ``V``, ``L``,
    ``A`` -- is an integer on both series.
    """
    letter = letter.upper()
    if isinstance(value, str):
        return value.strip()
    if letter == "D":
        number = float(value)
        if series == "standard" or number == int(number):
            return str(int(round(number)))
        return f"{number:.2f}"
    return str(int(round(float(value))))


def rounding_note(letter: str, value: float | int | str,
                  series: str = "precision") -> str | None:
    """Say so when :func:`format_value` had to change the number.

    Returns ``None`` when the value goes out as given.
    """
    if isinstance(value, str) or letter.upper() != "D":
        return None
    number = float(value)
    sent = float(format_value("D", number, series))
    if sent == number:
        return None
    return (f"note: the {series} series takes whole volts, so "
            f"D={number:g} goes out as D={sent:g} "
            f"({abs(sent - number):g} V of rounding)")


def build_command(letter: str, ch: int | None = None,
                  value: float | int | str | None = None,
                  series: str = "precision") -> str:
    """Build a command line without the ``\\r\\n`` terminator.

    ``build_command("U", 2)`` is ``"U2"``, ``build_command("D", 2, 50)`` is
    ``"D2=50"``, ``build_command("#")`` is ``"#"``.
    """
    letter = letter.upper()
    spec = COMMANDS.get(letter)
    if spec is None:
        raise ValueError(f"unknown command {letter!r}, known: "
                         f"{' '.join(sorted(COMMANDS))}")
    if spec.per_channel:
        if ch is None:
            raise ValueError(f"{letter} needs a channel")
        check_channel(ch)
    elif ch is not None:
        raise ValueError(f"{letter} takes no channel")
    text = letter if ch is None else f"{letter}{ch}"
    if value is None:
        if not spec.answers:
            raise ValueError(f"{letter} cannot be sent on its own")
        return text
    if not spec.settable:
        raise ValueError(f"{letter} is read-only")
    check_range(letter, value)
    return f"{text}={format_value(letter, value, series)}"


def check_range(letter: str, value: float | int | str) -> None:
    """Raise ``ValueError`` if ``value`` is outside the documented range."""
    spec = COMMANDS.get(letter.upper())
    if spec is None or isinstance(value, str):
        return
    number = float(value)
    if spec.lo is not None and number < spec.lo:
        raise ValueError(f"{letter}={value} below the minimum "
                         f"{spec.lo:g} {spec.unit}".rstrip())
    if spec.hi is not None and number > spec.hi:
        raise ValueError(f"{letter}={value} above the maximum "
                         f"{spec.hi:g} {spec.unit}".rstrip())
