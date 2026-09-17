#!/usr/bin/env python3
"""Emulator for a CAEN DT1470ET HV supply on a pty.

Prints the slave device path on the first line of stdout (flushed) and then
serves the N1470-family ASCII protocol for ever.  Standard library only.

    ./fake_caen_hv.py                      # 4 channels, 1 MOhm load
    ./fake_caen_hv.py --fault-current 5    # force IMON = 5 uA -> OVC/TRIP
    ./fake_caen_hv.py --local              # every SET answers LOC:ERR
    ./fake_caen_hv.py --pol -,+,+,- --stat-bits 8

Every SET received is logged to stderr with a timestamp.
"""

from __future__ import annotations

import argparse
import errno
import os
import pty
import select
import sys
import termios
import threading
import time
from dataclasses import dataclass, field

import caen_hv_protocol as proto

UPDATE_HZ = 10.0


@dataclass
class Channel:
    """Mutable per-channel state."""

    pol: str = "+"
    vset: float = 0.0
    iset: float = 3000.0
    maxv: float = 8000.0
    rup: float = 50.0
    rdw: float = 50.0
    trip: float = 10.0
    pdwn: str = "KILL"
    on: bool = False
    vmon: float = 0.0
    imon: float = 0.0
    ramping: int = 0  # 0 none, 1 up, 2 down
    clipped: bool = False  # last SET VSET was clipped to MAXV -> MAXV bit
    tripped: bool = False
    ovc_since: float | None = None
    extra_bits: int = 0

    def stat(self) -> int:
        """Assemble the STAT word from the current state."""
        word = self.extra_bits
        if self.on:
            word |= proto.stat_bit("ON")
        if self.ramping == 1:
            word |= proto.stat_bit("RUP")
        elif self.ramping == 2:
            word |= proto.stat_bit("RDW")
        if self.ovc_since is not None:
            word |= proto.stat_bit("OVC")
        if self.tripped:
            word |= proto.stat_bit("TRIP")
        if self.clipped:
            word |= proto.stat_bit("MAXV")
        return word


@dataclass
class Board:
    """Board-level state and behaviour."""

    bdname: str = "DT1470ET"
    nch: int = 4
    bd: int = 0
    bdfrel: str = "00.07"
    bdsnum: str = "00042"
    local: bool = False
    bdilkm: str = "CLOSED"
    bdterm: str = "ON"
    bdalarm: int = 0
    load_ohm: float = 1e6
    fault_current: float | None = None
    clip_vset: bool = False
    channels: list[Channel] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # -- ramping ----------------------------------------------------------
    def step(self, dt: float) -> None:
        """One 10 Hz update of VMON / IMON / OVC / TRIP."""
        now = time.monotonic()
        with self.lock:
            for ch in self.channels:
                target = ch.vset if ch.on else 0.0
                rate = ch.rup if target > ch.vmon else ch.rdw
                delta = target - ch.vmon
                move = rate * dt
                if abs(delta) <= move or abs(delta) < 1e-9:
                    ch.vmon = target
                    ch.ramping = 0
                else:
                    ch.vmon += move if delta > 0 else -move
                    ch.ramping = 1 if delta > 0 else 2
                if self.fault_current is not None:
                    ch.imon = self.fault_current
                else:
                    ch.imon = ch.vmon / self.load_ohm * 1e6
                if ch.imon > ch.iset:
                    if ch.ovc_since is None:
                        ch.ovc_since = now
                    elif now - ch.ovc_since >= ch.trip:
                        ch.on = False
                        ch.tripped = True
                else:
                    ch.ovc_since = None

    # -- request handling -------------------------------------------------
    def handle(self, line: str) -> str | None:
        """Turn one request line into one reply line (None: ignore silently)."""
        text = line.strip()
        if not text:
            return None
        if not text.startswith("$"):
            return proto.error_reply(self.bd, "CMD")
        fields = proto.parse_fields(text)
        try:
            if int(fields.get("BD", "-1")) != self.bd:
                return None  # not for us, like a board on a shared bus
        except ValueError:
            return proto.error_reply(self.bd, "CMD")

        cmd = fields.get("CMD", "").upper()
        if cmd not in ("MON", "SET"):
            return proto.error_reply(self.bd, "CMD")
        if "PAR" not in fields:
            return proto.error_reply(self.bd, "CMD")
        par = fields["PAR"].upper()

        spec = proto.PARAMS.get(par)
        if spec is None:
            return proto.error_reply(self.bd, "PAR")
        if cmd == "MON" and not spec.mon:
            return proto.error_reply(self.bd, "PAR")
        if cmd == "SET" and not spec.settable:
            return proto.error_reply(self.bd, "PAR")

        ch_index: int | None = None
        if spec.scope == "ch":
            if "CH" not in fields:
                return proto.error_reply(self.bd, "CH")
            try:
                ch_index = int(fields["CH"])
            except ValueError:
                return proto.error_reply(self.bd, "CH")
            if not 0 <= ch_index < self.nch:
                return proto.error_reply(self.bd, "CH")

        if cmd == "SET" and self.local:
            self._log_set(fields, refused="LOC:ERR")
            return proto.error_reply(self.bd, "LOC")

        if cmd == "MON":
            return self._do_mon(par, spec, ch_index)
        return self._do_set(fields, par, spec, ch_index)

    # -- MON ---------------------------------------------------------------
    def _do_mon(self, par: str, spec: proto.Param, ch_index: int | None) -> str:
        with self.lock:
            if spec.scope == "bd":
                board_values = {
                    "BDNAME": self.bdname,
                    "BDNCH": str(self.nch),
                    "BDFREL": self.bdfrel,
                    "BDSNUM": self.bdsnum,
                    "BDCTR": "LOCAL" if self.local else "REMOTE",
                    "BDTERM": self.bdterm,
                    "BDILK": "NO",
                    "BDILKM": self.bdilkm,
                    "BDALARM": str(self.bdalarm),
                }
                return proto.ok_reply(self.bd, board_values[par])
            ch = self.channels[ch_index or 0]
            channel_values = {
                "VSET": proto.format_value(ch.vset, "float"),
                "ISET": proto.format_value(ch.iset, "float"),
                "VMON": proto.format_value(ch.vmon, "float"),
                "IMON": proto.format_value(ch.imon, "float"),
                "MAXV": proto.format_value(ch.maxv, "float"),
                "RUP": proto.format_value(ch.rup, "float"),
                "RDW": proto.format_value(ch.rdw, "float"),
                "TRIP": proto.format_value(ch.trip, "float"),
                "PDWN": ch.pdwn,
                "POL": ch.pol,
                "STAT": str(ch.stat()),
            }
            return proto.ok_reply(self.bd, channel_values[par])

    # -- SET ---------------------------------------------------------------
    def _do_set(self, fields: dict[str, str], par: str, spec: proto.Param,
                ch_index: int | None) -> str:
        raw_val = fields.get("VAL")
        value = proto.check_value(par, raw_val)
        if value is None:
            self._log_set(fields, refused="VAL:ERR")
            return proto.error_reply(self.bd, "VAL")

        with self.lock:
            if spec.scope == "bd":
                if par == "BDILKM":
                    self.bdilkm = str(value)
                elif par == "BDCLR":
                    self.bdalarm = 0
                    for ch in self.channels:
                        ch.tripped = False
                        ch.clipped = False
            else:
                ch = self.channels[ch_index or 0]
                if par == "VSET":
                    number = float(value)
                    if number > ch.maxv:
                        if not self.clip_vset:
                            self._log_set(fields, refused="VAL:ERR (> MAXV)")
                            return proto.error_reply(self.bd, "VAL")
                        number = ch.maxv
                        ch.clipped = True
                    else:
                        ch.clipped = False
                    ch.vset = number
                elif par == "ISET":
                    ch.iset = float(value)
                elif par == "MAXV":
                    ch.maxv = float(value)
                    if ch.vset > ch.maxv:
                        ch.vset = ch.maxv
                        ch.clipped = True
                elif par == "RUP":
                    ch.rup = float(value)
                elif par == "RDW":
                    ch.rdw = float(value)
                elif par == "TRIP":
                    ch.trip = float(value)
                elif par == "PDWN":
                    ch.pdwn = str(value)
                elif par == "ON":
                    ch.on = True
                    ch.tripped = False
                elif par == "OFF":
                    ch.on = False
        self._log_set(fields)
        return proto.ok_reply(self.bd)

    # -- logging -----------------------------------------------------------
    def _log_set(self, fields: dict[str, str], refused: str = "") -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"{k}:{v}" for k, v in fields.items() if k != "BD"]
        tail = f" -> {refused}" if refused else ""
        print(f"{stamp} SET {','.join(parts)}{tail}", file=sys.stderr, flush=True)


def _make_pty() -> tuple[int, int, str]:
    """Open a pty pair in raw mode and return (master, slave, slave path)."""
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[0] = 0  # iflag: no CR/NL translation, no flow control
    attrs[1] = 0  # oflag: no post-processing
    attrs[3] = 0  # lflag: no echo, no canonical mode
    cc = list(attrs[6])
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    attrs[6] = cc
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    return master, slave, os.ttyname(slave)


def serve(board: Board, master: int) -> None:
    """Read requests, write replies; tolerate partial reads and reconnects."""
    buffer = ""
    while True:
        if not select.select([master], [], [], 0.2)[0]:
            continue
        try:
            data = os.read(master, 4096)
        except OSError as err:
            if err.errno in (errno.EIO, errno.EAGAIN):
                # no client attached to the slave side: wait and retry
                time.sleep(0.05)
                continue
            raise
        if not data:
            time.sleep(0.05)
            continue
        buffer += data.decode("ascii", "replace")
        while True:
            index = min((buffer.find(c) for c in proto.TERM_CHARS
                         if c in buffer), default=-1)
            if index < 0:
                break
            line, buffer = buffer[:index], buffer[index + 1:].lstrip("\r\n")
            reply = board.handle(line)
            if reply is None:
                continue
            try:
                os.write(master, (reply + proto.TERMINATOR).encode("ascii"))
            except OSError as err:
                if err.errno in (errno.EIO, errno.EAGAIN):
                    break  # client went away mid-reply
                raise


def ramp_loop(board: Board) -> None:
    dt = 1.0 / UPDATE_HZ
    last = time.monotonic()
    while True:
        time.sleep(dt)
        now = time.monotonic()
        board.step(now - last)
        last = now


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bdname", default="DT1470ET", help="reported BDNAME")
    parser.add_argument("--nch", type=int, default=4, help="channel count")
    parser.add_argument("--bd", type=int, default=0, help="board address")
    parser.add_argument("--load-ohm", type=float, default=1e6,
                        help="load resistance; IMON = VMON / R (default 1e6)")
    parser.add_argument("--fault-current", type=float, default=None,
                        metavar="UA", help="force IMON to this value in uA")
    parser.add_argument("--stat-bits", type=lambda s: int(s, 0), default=0,
                        metavar="MASK", help="extra STAT bits OR'ed in")
    parser.add_argument("--pol", default=None, metavar="+,-,...",
                        help="per-channel polarity, e.g. '-,+,+,-'")
    parser.add_argument("--local", action="store_true",
                        help="board in LOCAL mode: every SET answers LOC:ERR")
    parser.add_argument("--clip-vset", action="store_true",
                        help="clip SET VSET to MAXV instead of answering VAL:ERR")
    return parser


def board_from_args(args: argparse.Namespace) -> Board:
    board = Board(bdname=args.bdname, nch=args.nch, bd=args.bd,
                  local=args.local, load_ohm=args.load_ohm,
                  fault_current=args.fault_current, clip_vset=args.clip_vset)
    pols = [p.strip() for p in args.pol.split(",")] if args.pol else []
    for i in range(args.nch):
        pol = pols[i] if i < len(pols) and pols[i] in ("+", "-") else "+"
        board.channels.append(Channel(pol=pol, extra_bits=args.stat_bits))
    return board


def _fix_negative_values(argv: list[str]) -> list[str]:
    """Let ``--pol -,+,+,-`` work: argparse would read the value as a flag."""
    out: list[str] = []
    it = iter(range(len(argv)))
    skip = False
    for i in it:
        if skip:
            skip = False
            continue
        token = argv[i]
        if token == "--pol" and i + 1 < len(argv):
            out.append(f"--pol={argv[i + 1]}")
            skip = True
        else:
            out.append(token)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = _fix_negative_values(list(argv) if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(argv)
    board = board_from_args(args)
    master, slave, path = _make_pty()
    print(path, flush=True)
    print(f"fake {board.bdname}: {board.nch} channels on {path}"
          f"{' (LOCAL)' if board.local else ''}", file=sys.stderr, flush=True)
    threading.Thread(target=ramp_loop, args=(board,), daemon=True).start()
    try:
        serve(board, master)
    except KeyboardInterrupt:
        pass
    finally:
        os.close(master)
        os.close(slave)
    return 0


if __name__ == "__main__":
    sys.exit(main())
