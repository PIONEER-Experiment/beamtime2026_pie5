"""CAEN N1470-family ASCII protocol tables and helpers.

Single source of truth for parameter names, units, ranges and STAT bit
layout, shared by ``caen_hv_probe.py`` (client) and ``fake_caen_hv.py``
(emulator).  The tables below are the *documented* N1470 / DT1470ET set and
must be corrected after the first run against real hardware -- that is the
whole reason they live in one place.

Wire format (terminator: ``\\r\\n``, but accept ``\\r``, ``\\n`` or ``\\r\\n``)::

    $BD:00,CMD:MON,CH:0,PAR:VMON            -> #BD:00,CMD:OK,VAL:1234.5
    $BD:00,CMD:SET,CH:0,PAR:VSET,VAL:1000   -> #BD:00,CMD:OK
    $BD:00,CMD:MON,PAR:BDNAME               -> #BD:00,CMD:OK,VAL:DT1470ET

The unit at hand zero-pads every numeric reply (confirmed on hardware
2026-09-17, DT1470ET fw 1.08): ``VAL:1100.0`` comes back as ``VAL:1100.0``
but ``VAL:0`` as ``VAL:0000.0`` and STAT as ``VAL:02048``.  The width is
per-parameter (``Param.fmt``), and every integer must be parsed base 10 --
``VAL:02048`` read as octal is 1064.

Error replies carry ``ERR`` as the value of the offending field::

    #BD:00,CMD:ERR   unknown/malformed command
    #BD:00,CH:ERR    bad channel
    #BD:00,PAR:ERR   bad parameter
    #BD:00,VAL:ERR   bad value
    #BD:00,LOC:ERR   board is in LOCAL mode, SET refused
"""

from __future__ import annotations

from dataclasses import dataclass, field

TERMINATOR = "\r\n"
TERM_CHARS = "\r\n"

#: error kinds, in the order a board checks them
ERROR_KINDS = ("CMD", "CH", "PAR", "VAL", "LOC")


@dataclass(frozen=True)
class Param:
    """One MON/SET parameter of the ASCII protocol."""

    name: str
    scope: str  # "ch" or "bd"
    kind: str  # "float" | "int" | "enum" | "str" | "none"
    mon: bool = True
    settable: bool = False
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""
    desc: str = ""
    fmt: str = "%s"  # how the board renders a MON reply (zero-padded!)


def _p(*args, **kwargs) -> tuple[str, Param]:
    p = Param(*args, **kwargs)
    return p.name, p


# --------------------------------------------------------------------------
# channel parameters
# --------------------------------------------------------------------------
CH_PARAMS: dict[str, Param] = dict(
    [
        _p("VSET", "ch", "float", settable=True, lo=0.0, hi=8100.0, unit="V",
           fmt="%06.1f", desc="voltage set point (magnitude)"),
        _p("ISET", "ch", "float", settable=True, lo=0.0, hi=3000.0, unit="uA",
           fmt="%07.2f", desc="current limit"),
        _p("VMON", "ch", "float", unit="V", fmt="%06.1f",
           desc="measured voltage"),
        _p("IMON", "ch", "float", unit="uA", fmt="%07.2f",
           desc="measured current"),
        _p("MAXV", "ch", "float", settable=True, lo=0.0, hi=8100.0, unit="V",
           fmt="%04d", desc="hardware voltage limit"),
        _p("RUP", "ch", "float", settable=True, lo=1.0, hi=500.0, unit="V/s",
           fmt="%03d", desc="ramp-up speed"),
        _p("RDW", "ch", "float", settable=True, lo=1.0, hi=500.0, unit="V/s",
           fmt="%03d", desc="ramp-down speed"),
        _p("TRIP", "ch", "float", settable=True, lo=0.0, hi=1000.0, unit="s",
           fmt="%06.1f", desc="over-current trip time"),
        _p("PDWN", "ch", "enum", settable=True, choices=("KILL", "RAMP"),
           desc="power-down mode"),
        _p("POL", "ch", "enum", choices=("+", "-"),
           desc="polarity (read-only, physical switch)"),
        _p("STAT", "ch", "int", fmt="%05d", desc="channel status bitmask"),
        _p("ON", "ch", "none", mon=False, settable=True, desc="switch channel on"),
        _p("OFF", "ch", "none", mon=False, settable=True, desc="switch channel off"),
    ]
)

# --------------------------------------------------------------------------
# board parameters (no CH field)
# --------------------------------------------------------------------------
BD_PARAMS: dict[str, Param] = dict(
    [
        _p("BDNAME", "bd", "str", desc="board model name"),
        _p("BDNCH", "bd", "int", fmt="%d", desc="number of channels"),
        _p("BDFREL", "bd", "str", desc="firmware release"),
        _p("BDSNUM", "bd", "str", desc="serial number"),
        _p("BDCTR", "bd", "enum", choices=("LOCAL", "REMOTE"), desc="control mode"),
        _p("BDTERM", "bd", "enum", choices=("ON", "OFF"), desc="RS232 termination"),
        _p("BDILK", "bd", "enum", choices=("YES", "NO"), desc="interlock status"),
        _p("BDILKM", "bd", "enum", settable=True, choices=("OPEN", "CLOSED"),
           desc="interlock mode"),
        _p("BDALARM", "bd", "int", fmt="%d", desc="board alarm bitmask"),
        _p("BDCLR", "bd", "none", mon=False, settable=True, desc="clear board alarm"),
    ]
)

PARAMS: dict[str, Param] = {**CH_PARAMS, **BD_PARAMS}

#: parameters ``caen_hv_probe.py info`` reads
INFO_PARAMS = ("BDNAME", "BDNCH", "BDFREL", "BDSNUM", "BDCTR", "BDILK",
               "BDILKM", "BDALARM")

#: parameters ``caen_hv_probe.py dump`` reads for every channel
DUMP_PARAMS = ("VSET", "VMON", "ISET", "IMON", "MAXV", "RUP", "RDW", "TRIP",
               "PDWN", "POL", "STAT")

# --------------------------------------------------------------------------
# STAT bitmask
# --------------------------------------------------------------------------
STAT_BITS: tuple[str, ...] = (
    "ON",      # 0  channel is on
    "RUP",     # 1  ramping up
    "RDW",     # 2  ramping down
    "OVC",     # 3  over current
    "OVV",     # 4  over voltage
    "UNV",     # 5  under voltage
    "MAXV",    # 6  MAXV reached / set point clipped
    "TRIP",    # 7  tripped
    "OVP",     # 8  over power
    "OVT",     # 9  over temperature
    "DIS",     # 10 disabled
    "KILL",    # 11 killed by external signal
    "ILK",     # 12 interlocked
    "NOCAL",   # 13 calibration error
)

STAT_BIT_INDEX: dict[str, int] = {name: i for i, name in enumerate(STAT_BITS)}


def stat_bit(name: str) -> int:
    """Mask with the single STAT bit ``name`` set."""
    return 1 << STAT_BIT_INDEX[name]


def decode_stat(value: int) -> list[str]:
    """Names of the bits set in a STAT word; unknown bits become ``BIT<n>``."""
    names: list[str] = []
    for bit in range(value.bit_length()):
        if value & (1 << bit):
            names.append(STAT_BITS[bit] if bit < len(STAT_BITS) else f"BIT{bit}")
    return names


# --------------------------------------------------------------------------
# framing
# --------------------------------------------------------------------------
def build_request(bd: int, cmd: str, par: str, ch: int | None = None,
                  val: str | None = None) -> str:
    """Build a request line (without terminator)."""
    parts = [f"$BD:{bd:02d}", f"CMD:{cmd}"]
    if ch is not None:
        parts.append(f"CH:{ch}")
    parts.append(f"PAR:{par}")
    if val is not None:
        parts.append(f"VAL:{val}")
    return ",".join(parts)


def parse_fields(line: str) -> dict[str, str]:
    """Split ``KEY:VALUE,KEY:VALUE`` into a dict, keys upper-cased.

    The leading ``$``/``#`` is stripped.  Malformed chunks (no colon) are
    ignored, which is what a board does with trailing rubbish.
    """
    body = line.strip().strip(TERM_CHARS)
    if body[:1] in ("$", "#"):
        body = body[1:]
    fields: dict[str, str] = {}
    for chunk in body.split(","):
        if not chunk:
            continue
        key, sep, value = chunk.partition(":")
        if sep:
            fields[key.strip().upper()] = value.strip()
    return fields


@dataclass
class Reply:
    """A parsed ``#BD:..`` reply."""

    raw: str
    fields: dict[str, str] = field(default_factory=dict)
    error: str | None = None  # one of ERROR_KINDS
    value: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_int(self) -> int:
        """VAL as a decimal integer, tolerating the board's zero padding."""
        return parse_int(self.value or "")

    def as_float(self) -> float:
        """VAL as a float, tolerating the board's zero padding."""
        return parse_float(self.value or "")


def parse_reply(raw: str) -> Reply:
    """Parse a reply line.  A reply that is not ``CMD:OK`` is an error."""
    fields = parse_fields(raw)
    for kind in ERROR_KINDS:
        if fields.get(kind) == "ERR":
            return Reply(raw=raw.strip(), fields=fields, error=kind)
    if fields.get("CMD") != "OK":
        return Reply(raw=raw.strip(), fields=fields, error="CMD")
    return Reply(raw=raw.strip(), fields=fields, value=fields.get("VAL"))


def error_reply(bd: int, kind: str) -> str:
    """Build an error reply line (without terminator)."""
    if kind not in ERROR_KINDS:
        raise ValueError(f"unknown error kind {kind!r}")
    return f"#BD:{bd:02d},{kind}:ERR"


def ok_reply(bd: int, val: str | None = None) -> str:
    """Build a success reply line (without terminator)."""
    if val is None:
        return f"#BD:{bd:02d},CMD:OK"
    return f"#BD:{bd:02d},CMD:OK,VAL:{val}"


def format_value(value: object, par: str) -> str:
    """Render a parameter exactly the way the board does.

    The DT1470ET zero-pads every numeric reply (``VAL:02048``,
    ``VAL:0000.00``), so the width lives with the parameter, not with its
    kind.  Confirmed on hardware 2026-09-17 (fw 1.08).
    """
    spec = PARAMS.get(par.upper())
    fmt = spec.fmt if spec is not None else "%s"
    if fmt == "%s":
        return str(value)
    if fmt.endswith("d"):
        return fmt % int(float(value))  # type: ignore[arg-type]
    return fmt % float(value)  # type: ignore[arg-type]


def parse_int(text: str) -> int:
    """Parse a possibly zero-padded integer reply.

    Always base 10: ``int("02048", 0)`` would raise and ``strtol(.., 0)``
    in C would read it as octal 1064 -- the bug this padding caused in the
    C++ driver.
    """
    return int(text.strip(), 10)


def parse_float(text: str) -> float:
    """Parse a possibly zero-padded float reply (``0000.00``)."""
    return float(text.strip())


def check_value(par: str, text: str) -> float | str | None:
    """Validate a SET value against the table.

    Returns the coerced value, or ``None`` if the value is invalid (the
    caller answers ``VAL:ERR``).  Parameters of kind ``none`` accept no
    value and return ``""``.
    """
    p = PARAMS.get(par.upper())
    if p is None:
        return None
    if p.kind == "none":
        return "" if text in (None, "") else None
    if text is None:
        return None
    if p.kind == "enum":
        upper = text.strip().upper()
        return upper if upper in p.choices else None
    if p.kind in ("float", "int"):
        try:
            number = float(text)
        except ValueError:
            return None
        if p.lo is not None and number < p.lo:
            return None
        if p.hi is not None and number > p.hi:
            return None
        return int(number) if p.kind == "int" else number
    return text
