#!/usr/bin/env python3
"""Probe / debug CLI for a CAEN DT1470ET (N1470-family ASCII protocol).

Standard library only (``os.open`` + ``termios``, no pyserial) so it runs
unchanged on the PSI DAQ machine and inside the pioneer-midas container.

    ./caen_hv_probe.py --port /dev/ttyACM0 info
    ./caen_hv_probe.py --port /dev/ttyACM0 dump
    ./caen_hv_probe.py --port /dev/ttyACM0 mon 0 VMON
    ./caen_hv_probe.py --port /dev/ttyACM0 --yes set 0 VSET 500
    ./caen_hv_probe.py --port /dev/ttyACM0 raw 'CMD:MON,PAR:BDNAME'

Exit code is non-zero when the board answers ``*:ERR``; the raw reply is
printed in that case.  ``--verbose`` echoes every request and reply.
"""

from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import time

import caen_hv_protocol as proto
from caen_hv_protocol import Param, Reply


class CaenHVError(RuntimeError):
    """A ``*:ERR`` reply from the board."""

    def __init__(self, request: str, reply: Reply) -> None:
        self.request = request
        self.reply = reply
        self.kind = reply.error or "CMD"
        super().__init__(f"{self.kind}:ERR for {request!r} (reply: {reply.raw!r})")


class CaenHVTimeout(TimeoutError):
    """No terminated reply arrived within the timeout."""


class CaenHV:
    """Blocking ASCII client on a serial / pty character device."""

    def __init__(self, port: str = "/dev/ttyACM0", bd: int = 0,
                 timeout: float = 1.0, verbose: bool = False) -> None:
        self.port = port
        self.bd = bd
        self.timeout = timeout
        self.verbose = verbose
        self.fd = -1
        self._buf = ""

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self.fd >= 0:
            return
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self._configure(self.fd)
        except termios.error:
            pass  # not a tty (e.g. a plain file in a test) -- keep going
        self._drain()

    @staticmethod
    def _configure(fd: int) -> None:
        """Raw 9600 8N1.  CDC-ACM ignores the baud rate, an FTDI N1470 does not."""
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

    def __enter__(self) -> "CaenHV":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- raw exchange ------------------------------------------------------
    def _drain(self) -> None:
        """Throw away anything already buffered or in the input queue."""
        self._buf = ""
        while select.select([self.fd], [], [], 0.0)[0]:
            try:
                if not os.read(self.fd, 4096):
                    break
            except (BlockingIOError, OSError):
                break

    def exchange(self, request: str) -> Reply:
        """Send one request line, return the parsed reply."""
        if self.fd < 0:
            self.open()
        self._drain()
        wire = request + proto.TERMINATOR
        if self.verbose:
            print(f">>> {request!r}", file=sys.stderr)
        os.write(self.fd, wire.encode("ascii"))
        raw = self._read_line()
        if self.verbose:
            print(f"<<< {raw!r}", file=sys.stderr)
        return proto.parse_reply(raw)

    def _take_line(self) -> str | None:
        """Pop one terminated, non-empty line out of the read buffer."""
        while True:
            index = min((self._buf.find(c) for c in proto.TERM_CHARS
                         if c in self._buf), default=-1)
            if index < 0:
                return None
            line = self._buf[:index]
            self._buf = self._buf[index + 1:].lstrip(proto.TERM_CHARS)
            if line.strip():
                return line.strip()

    def _read_line(self) -> str:
        """Read until any of CR / LF / CRLF, with a deadline.

        Keeps a buffer, so several replies arriving in one read are handed
        out one by one.
        """
        line = self._take_line()
        if line is not None:
            return line
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaenHVTimeout(
                    f"no reply from {self.port} within {self.timeout} s "
                    f"(got {self._buf!r})")
            if not select.select([self.fd], [], [], remaining)[0]:
                continue
            try:
                data = os.read(self.fd, 256)
            except BlockingIOError:
                continue
            if not data:
                continue
            self._buf += data.decode("ascii", "replace")
            line = self._take_line()
            if line is not None:
                return line

    # -- typed access ------------------------------------------------------
    def command(self, cmd: str, par: str, ch: int | None = None,
                val: str | None = None) -> Reply:
        request = proto.build_request(self.bd, cmd, par, ch=ch, val=val)
        reply = self.exchange(request)
        if not reply.ok:
            raise CaenHVError(request, reply)
        return reply

    def mon(self, par: str, ch: int | None = None) -> str:
        """MON one parameter.  ``ch`` is omitted for board parameters."""
        par = par.upper()
        if ch is None and par in proto.CH_PARAMS:
            raise ValueError(f"{par} is a channel parameter, give a channel")
        return self.command("MON", par, ch=ch).value or ""

    def set(self, par: str, ch: int | None = None, val: str | None = None) -> Reply:
        """SET one parameter.  ON/OFF/BDCLR take no value."""
        return self.command("SET", par.upper(), ch=ch, val=val)

    def mon_float(self, par: str, ch: int | None = None) -> float:
        """MON a numeric parameter; the board's zero padding is harmless here."""
        return proto.parse_float(self.mon(par, ch))

    def mon_int(self, par: str, ch: int | None = None) -> int:
        """MON an integer parameter, always base 10 (``VAL:02048`` is 2048)."""
        return proto.parse_int(self.mon(par, ch))

    def n_channels(self) -> int:
        return self.mon_int("BDNCH")


# --------------------------------------------------------------------------
# subcommands -- return text so the tests can use them as an API
# --------------------------------------------------------------------------
def info_text(dev: CaenHV) -> str:
    """BDNAME/BDNCH/... as ``PAR value`` lines; missing params are reported."""
    lines = []
    width = max(len(p) for p in proto.INFO_PARAMS)
    for par in proto.INFO_PARAMS:
        try:
            value = dev.mon(par)
        except CaenHVError as err:
            value = f"<{err.kind}:ERR>"
        lines.append(f"{par:<{width}}  {value}")
    return "\n".join(lines)


def info_dict(dev: CaenHV) -> dict[str, str]:
    """Same as :func:`info_text` but as a dict (used by the tests)."""
    out: dict[str, str] = {}
    for par in proto.INFO_PARAMS:
        try:
            out[par] = dev.mon(par)
        except CaenHVError as err:
            out[par] = f"<{err.kind}:ERR>"
    return out


def channel_row(dev: CaenHV, ch: int) -> dict[str, str]:
    """MON every dump parameter of one channel."""
    row: dict[str, str] = {}
    for par in proto.DUMP_PARAMS:
        try:
            row[par] = dev.mon(par, ch)
        except CaenHVError as err:
            row[par] = f"<{err.kind}:ERR>"
    return row


def dump_text(dev: CaenHV, nch: int | None = None) -> str:
    """Aligned per-channel table with STAT decoded into bit names."""
    if nch is None:
        nch = dev.n_channels()
    if nch <= 0:
        return "no channels (BDNCH = 0)"
    rows = [channel_row(dev, ch) for ch in range(nch)]
    for row in rows:
        try:
            # keep the wire text (zero-padded) but decode it base 10
            row["BITS"] = ",".join(proto.decode_stat(
                proto.parse_int(row["STAT"]))) or "-"
        except ValueError:
            row["BITS"] = "-"
    columns = ("CH",) + proto.DUMP_PARAMS + ("BITS",)

    def cell(row: dict[str, str], ch: int, col: str) -> str:
        return str(ch) if col == "CH" else row.get(col, "")

    widths = {
        col: max(len(col), *(len(cell(r, i, col)) for i, r in enumerate(rows)))
        for col in columns
    }
    header = "  ".join(f"{col:<{widths[col]}}" for col in columns)
    lines = [header, "-" * len(header)]
    for ch, row in enumerate(rows):
        lines.append("  ".join(f"{cell(row, ch, col):<{widths[col]}}"
                               for col in columns))
    lines.append("")
    lines.append("units: V, uA, RUP/RDW V/s, TRIP s; values as the board sends "
                 "them (zero-padded, fw 1.08)")
    return "\n".join(lines)


def raw_text(dev: CaenHV, text: str) -> str:
    """Send a hand-written command; ``$BD:nn,`` is added when missing."""
    request = text.strip()
    if not request.startswith("$"):
        request = f"$BD:{dev.bd:02d}," + request.lstrip(",")
    reply = dev.exchange(request)
    out = [f">>> {request}", f"<<< {reply.raw}"]
    if not reply.ok:
        raise CaenHVError(request, reply)
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _known(par: str) -> Param | None:
    return proto.PARAMS.get(par.upper())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="/dev/ttyACM0", help="serial device")
    parser.add_argument("--bd", type=int, default=0, help="board address")
    parser.add_argument("--timeout", type=float, default=1.0,
                        help="reply timeout in seconds")
    parser.add_argument("--verbose", action="store_true",
                        help="print every raw request and reply")
    parser.add_argument("--yes", action="store_true",
                        help="really perform SET commands")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="board identity and interlock state")

    p_dump = sub.add_parser("dump", help="all parameters of all channels")
    p_dump.add_argument("--nch", type=int, default=None,
                        help="channel count (default: read BDNCH)")

    p_mon = sub.add_parser("mon", help="MON one parameter")
    p_mon.add_argument("ch", help="channel number, or 'bd' for board params")
    p_mon.add_argument("par")

    p_set = sub.add_parser("set", help="SET one parameter (needs --yes)")
    p_set.add_argument("ch", help="channel number, or 'bd' for board params")
    p_set.add_argument("par")
    p_set.add_argument("val", nargs="?", default=None)

    p_raw = sub.add_parser("raw", help="send a hand-written command")
    p_raw.add_argument("text")
    return parser


def _channel_arg(text: str) -> int | None:
    return None if text.lower() in ("bd", "board", "-") else int(text)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dev = CaenHV(args.port, bd=args.bd, timeout=args.timeout,
                 verbose=args.verbose)
    try:
        with dev:
            if args.cmd == "info":
                print(info_text(dev))
            elif args.cmd == "dump":
                print(dump_text(dev, args.nch))
            elif args.cmd == "mon":
                print(dev.mon(args.par, _channel_arg(args.ch)))
            elif args.cmd == "set":
                par = _known(args.par)
                if par is not None and not par.settable:
                    print(f"{args.par.upper()} is read-only", file=sys.stderr)
                    return 2
                if not args.yes:
                    print(f"refusing to SET {args.par.upper()} without --yes",
                          file=sys.stderr)
                    return 2
                dev.set(args.par, _channel_arg(args.ch), args.val)
                print("OK")
            elif args.cmd == "raw":
                print(raw_text(dev, args.text))
    except CaenHVError as err:
        print(f"error: {err.kind}:ERR", file=sys.stderr)
        print(f"  request: {err.request}", file=sys.stderr)
        print(f"  reply:   {err.reply.raw}", file=sys.stderr)
        return 1
    except (CaenHVTimeout, OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
