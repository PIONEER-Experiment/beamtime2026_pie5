#!/usr/bin/env python3
"""Drive ``fake_iseg_nhq.py`` with the ``iseg_nhq_probe.py`` module API.

    python3 -m unittest discover -s drivers/iseg_nhq/tests

Every test starts its own emulator on a fresh pty, so the cases are
independent and the front panel flags (``--manual``, ``--hv-off``,
``--ch1-dead``) can differ per test.  The whole suite is meant to stay
under half a minute: the ramps use small voltages and fast ramp speeds.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER_DIR = os.path.dirname(HERE)
sys.path.insert(0, DRIVER_DIR)

import iseg_nhq_probe as probe        # noqa: E402
import iseg_nhq_protocol as proto     # noqa: E402

FAKE = os.path.join(DRIVER_DIR, "fake_iseg_nhq.py")
START_TIMEOUT = 10.0


class Fake:
    """The emulator as a subprocess; ``path`` is its pty slave."""

    #: unless a test asks otherwise the answers dribble out at 1 ms per
    #: character instead of the factory 3 ms, so the suite stays quick
    DEFAULT_ARGS = ("--break-time", "1")

    def __init__(self, *args: str) -> None:
        self.args = args if "--break-time" in args else (*self.DEFAULT_ARGS,
                                                         *args)
        self.proc: subprocess.Popen[str] | None = None
        self.path = ""

    def __enter__(self) -> "Fake":
        env = dict(os.environ, PYTHONPATH=DRIVER_DIR, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, FAKE, *self.args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env)
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        if not line:
            raise AssertionError(
                "fake printed no pty path; stderr:\n"
                + (self.proc.stderr.read() if self.proc.stderr else ""))
        self.path = line
        deadline = time.monotonic() + START_TIMEOUT
        while not os.path.exists(self.path) and time.monotonic() < deadline:
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    stream.close()

    def client(self, timeout: float = 2.0,
               probe_on_open: bool = True) -> probe.IsegNHQ:
        return probe.IsegNHQ(self.path, timeout=timeout,
                             probe_on_open=probe_on_open)

    def stderr_log(self) -> str:
        """Stop the fake and return everything it logged."""
        assert self.proc is not None and self.proc.stderr is not None
        self.proc.terminate()
        self.proc.wait(timeout=5)
        return self.proc.stderr.read()


def run_cli(fake: Fake, *args: str, timeout: str = "1.0") -> tuple[int, str]:
    """Call ``iseg_nhq_probe.main`` against the fake, capturing its output."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = probe.main(["--port", fake.path, "--timeout", timeout, *args])
    return code, out.getvalue() + err.getvalue()


def settle(dev: probe.IsegNHQ, ch: int, seconds: float = 3.0) -> str:
    """Poll until the channel stops moving, then return its status word."""
    deadline = time.monotonic() + seconds
    status = dev.status(ch)
    while status in ("L2H", "H2L") and time.monotonic() < deadline:
        status = dev.status(ch)
    return status


class IdentityAndSettingsTest(unittest.TestCase):
    def test_identify(self) -> None:
        # the factory break time, dribbled out for real
        with Fake("--break-time", "3") as fake, fake.client() as dev:
            ident = dev.identify()
            self.assertEqual(ident.unit, "483216")
            self.assertEqual(ident.software, "2.05")
            self.assertEqual(ident.vmax_v, 3000.0)
            self.assertAlmostEqual(ident.imax_a, 4e-3)
            text = probe.info_text(dev)
            self.assertIn("unit        483216", text)
            self.assertIn("Vmax        3000 V", text)
            self.assertIn("break time  3 ms", text)

    def test_identify_other_module(self) -> None:
        with Fake("--id", "111222", "--sw", "3.06",
                  "--vmax", "6kV", "--imax", "500uA") as fake, \
                fake.client() as dev:
            ident = dev.identify()
            self.assertEqual(ident.unit, "111222")
            self.assertEqual(ident.vmax_v, 6000.0)
            self.assertAlmostEqual(ident.imax_a, 500e-6)

    def test_break_time_read_and_write(self) -> None:
        with Fake("--break-time", "3") as fake, fake.client() as dev:
            self.assertEqual(dev.break_time(), 3)
            self.assertEqual(dev.send("W", None, 10), "")   # write: empty line
            self.assertEqual(dev.break_time(), 10)

    def test_dump_both_channels(self) -> None:
        with Fake() as fake, fake.client() as dev:
            text = probe.dump_text(dev)
            lines = text.splitlines()
            self.assertTrue(lines[0].startswith("CH"))
            for letter in proto.DUMP_COMMANDS:
                self.assertIn(letter, lines[0].split())
            self.assertEqual(lines[2].split()[0], "1")
            self.assertEqual(lines[3].split()[0], "2")
            self.assertIn("ch1: S=ON", text)
            self.assertIn("ch2: S=ON", text)
            self.assertNotIn("<no answer>", text)


class NumberFormatTest(unittest.TestCase):
    """The two series disagree about how numbers look on the wire."""

    def _check_precision(self, dev: probe.IsegNHQ) -> None:
        """Set 50 V, ramp there, and read every number back."""
        self.assertEqual(dev.send("U", 2), "+00000-01")
        dev.set_voltage(2, 50)
        self.assertEqual(dev.send("D", 2), "+00500-01")
        self.assertEqual(dev.read_set_voltage(2), 50.0)
        dev.start(2)
        self.assertEqual(settle(dev, 2), "ON")
        self.assertEqual(dev.send("U", 2), "+00500-01")
        self.assertEqual(dev.read_voltage(2), 50.0)
        # current is mantissa + exponent in amperes on both series
        self.assertEqual(dev.send("I", 2), "50000-12")
        self.assertAlmostEqual(dev.read_current(2), 50e-9)

    def _check_standard(self, dev: probe.IsegNHQ) -> None:
        self.assertEqual(dev.send("U", 2), "+0000")
        dev.set_voltage(2, 50)
        self.assertEqual(dev.send("D", 2), "0050")   # no polarity on D
        self.assertEqual(dev.read_set_voltage(2), 50.0)
        dev.start(2)
        self.assertEqual(settle(dev, 2), "ON")
        self.assertEqual(dev.send("U", 2), "+0050")
        self.assertEqual(dev.read_voltage(2), 50.0)
        self.assertEqual(dev.send("I", 2), "50000-12")

    def test_precision_format(self) -> None:
        with Fake("--break-time", "3", "--ramp", "255") as fake, \
                fake.client() as dev:
            self._check_precision(dev)

    def test_standard_format(self) -> None:
        with Fake("--fmt", "standard", "--ramp", "255") as fake, \
                fake.client() as dev:
            self._check_standard(dev)

    def test_precision_format_with_an_extra_crlf(self) -> None:
        """Same again on a unit that puts a blank line before every answer.

        The vendor example reads one character more than a standard-series
        answer is long (manual x2xx p.9), so some firmware probably does
        this; the bench 208L does not.  Either way the client has to cope.
        """
        with Fake("--extra-crlf", "--ramp", "255") as fake, \
                fake.client() as dev:
            self._check_precision(dev)

    def test_standard_format_with_an_extra_crlf(self) -> None:
        with Fake("--extra-crlf", "--fmt", "standard", "--ramp", "255") \
                as fake, fake.client() as dev:
            self._check_standard(dev)

    def test_ramp_with_an_extra_crlf(self) -> None:
        """The write commands of a ramp also survive the extra blank line."""
        with Fake("--extra-crlf") as fake, fake.client() as dev:
            report = probe.ramp_text(dev, 2, 50.0, speed=200)
            self.assertIn("D2 = 50 V", report)
            self.assertIn("G2 -> L2H", report)
            self.assertEqual(settle(dev, 2), "ON")

    def test_negative_polarity_sign(self) -> None:
        with Fake("--pol", "-,-", "--ramp", "255") as fake, \
                fake.client() as dev:
            dev.set_voltage(2, 50)
            dev.start(2)
            self.assertEqual(settle(dev, 2), "ON")
            self.assertEqual(dev.read_voltage(2), -50.0)
            value, names = dev.module_status(2)
            self.assertNotIn("POL_POS", names)
            self.assertEqual(value & 4, 0)


class RampTest(unittest.TestCase):
    def test_ramp_up_and_down_through_watch(self) -> None:
        """50 V up and back: L2H -> ON, then H2L -> ON, seen by ``watch``."""
        with Fake() as fake, fake.client() as dev:
            report = probe.ramp_text(dev, 2, 50.0, speed=100)
            self.assertIn("A2 = 0", report)
            self.assertIn("S2 = ON", report)
            self.assertIn("limit = 3000 V x 100 % = 3000 V", report)
            self.assertIn("D2 = 50 V", report)
            self.assertIn("G2 -> L2H", report)

            up = list(probe.watch_lines(dev, 2, interval=0.05, max_s=10.0))
            self.assertIn("S=L2H", up[0])
            self.assertIn("S=ON", up[-1])
            self.assertGreater(len(up), 2)
            self.assertNotIn("stopped after", up[-1])
            self.assertAlmostEqual(dev.read_voltage(2), 50.0, places=3)

            self.assertIn("D2 = 0 V", probe.off_text(dev, 2))
            down = list(probe.watch_lines(dev, 2, interval=0.05, max_s=10.0))
            self.assertIn("S=H2L", down[0])
            self.assertIn("S=ON", down[-1])
            self.assertAlmostEqual(dev.read_voltage(2), 0.0, places=3)

    def test_ramp_speed_is_honoured(self) -> None:
        with Fake() as fake, fake.client() as dev:
            dev.set_ramp(2, 25)
            self.assertEqual(dev.ramp_speed(2), 25)
            dev.set_voltage(2, 15)
            dev.start(2)
            time.sleep(0.3)
            middle = dev.read_voltage(2)
            self.assertGreater(middle, 1.0)
            self.assertLess(middle, 14.0)
            self.assertEqual(dev.status(2), "L2H")
            self.assertEqual(settle(dev, 2), "ON")

    def test_no_movement_without_g(self) -> None:
        """``D`` alone stores the set point; the output waits for ``G``."""
        with Fake("--ramp", "255") as fake, fake.client() as dev:
            dev.set_voltage(2, 40)
            time.sleep(0.5)
            self.assertEqual(dev.read_voltage(2), 0.0)
            self.assertEqual(dev.status(2), "ON")

    def test_autostart_moves_without_g(self) -> None:
        with Fake("--ramp", "255") as fake, fake.client() as dev:
            dev.set_autostart(2, 8)
            dev.set_voltage(2, 40)
            self.assertEqual(settle(dev, 2), "ON")
            self.assertAlmostEqual(dev.read_voltage(2), 40.0, places=3)


class RefusalTest(unittest.TestCase):
    def test_manual_refused_then_allowed_with_yes(self) -> None:
        with Fake("--manual") as fake, fake.client() as dev:
            self.assertEqual(dev.status(2), "MAN")
            with self.assertRaises(probe.IsegRefused) as caught:
                probe.ramp_text(dev, 2, 50.0)
            self.assertIn("MAN", str(caught.exception))
            self.assertIn("CONTROL switch", str(caught.exception))
            report = probe.ramp_text(dev, 2, 50.0, yes=True)
            self.assertIn("G2 -> MAN", report)
            # commands were accepted, the output did not move
            time.sleep(0.3)
            self.assertEqual(dev.read_voltage(2), 0.0)
            self.assertEqual(dev.read_set_voltage(2), 50.0)

    def test_hv_off_refused_then_allowed_with_yes(self) -> None:
        with Fake("--hv-off") as fake, fake.client() as dev:
            self.assertEqual(dev.status(2), "OFF")
            with self.assertRaises(probe.IsegRefused) as caught:
                probe.ramp_text(dev, 2, 50.0)
            self.assertIn("OFF", str(caught.exception))
            report = probe.ramp_text(dev, 2, 50.0, yes=True)
            self.assertIn("G2 -> OFF", report)
            time.sleep(0.3)
            self.assertEqual(dev.read_voltage(2), 0.0)
            value, names = dev.module_status(2)
            self.assertIn("OFF", names)
            self.assertEqual(value & 8, 8)

    def test_autostart_guard(self) -> None:
        with Fake() as fake, fake.client() as dev:
            dev.set_autostart(2, 8)
            self.assertEqual(dev.autostart(2), 8)
            with self.assertRaises(probe.IsegRefused) as caught:
                probe.ramp_text(dev, 2, 10.0)
            self.assertIn("autostart", str(caught.exception))
            self.assertIn("A2=8", str(caught.exception))
            self.assertIn("A2 = 8", probe.ramp_text(dev, 2, 10.0, yes=True))

    def test_negative_set_voltage_refused_locally(self) -> None:
        with Fake() as fake, fake.client() as dev:
            with self.assertRaises(probe.IsegRefused):
                probe.ramp_text(dev, 2, -10.0)

    def test_above_hardware_limit_refused_locally(self) -> None:
        with Fake() as fake, fake.client() as dev:
            with self.assertRaises(probe.IsegRefused) as caught:
                probe.ramp_text(dev, 2, 4000.0)
            self.assertIn("3000 V", str(caught.exception))


class ErrorReplyTest(unittest.TestCase):
    def test_umax_from_the_unit(self) -> None:
        """The unit itself answers ``? UMAX=`` when ``D`` is over the limit."""
        with Fake() as fake, fake.client() as dev:
            with self.assertRaises(probe.IsegNHQError) as caught:
                dev.command("D2=99999")
            self.assertEqual(caught.exception.reply, "? UMAX=3000")
            self.assertIn("set voltage exceeds the Vmax hardware limit",
                          caught.exception.meaning)
            # the set point did NOT stay put: the unit clamps and stores
            self.assertEqual(dev.read_set_voltage(2), 3000.0)
            # and the same through the typed writer
            with self.assertRaises(probe.IsegNHQError):
                dev.set_voltage(2, 99999)

    def test_wrong_channel_number(self) -> None:
        with Fake() as fake, fake.client() as dev:
            with self.assertRaises(probe.IsegNHQError) as caught:
                dev.command("U3")
            self.assertEqual(caught.exception.reply, "?WCN")
            self.assertEqual(caught.exception.meaning, "wrong channel number")
            with self.assertRaises(probe.IsegNHQError) as caught:
                probe.raw_text(dev, "D3=10")
            self.assertEqual(caught.exception.reply, "?WCN")
            self.assertEqual(dev.status(2), "ON")   # link still in step

    def test_syntax_error(self) -> None:
        with Fake() as fake, fake.client() as dev:
            for garbage in ("X9", "U", "HELLO", "U2=5", "V2=1"):
                with self.subTest(garbage=garbage):
                    with self.assertRaises(probe.IsegNHQError) as caught:
                        dev.command(garbage)
                    self.assertEqual(caught.exception.reply, "????")
                    self.assertEqual(caught.exception.meaning, "syntax error")
            self.assertEqual(dev.status(2), "ON")


class EchoTest(unittest.TestCase):
    def test_echo_mismatch_then_sync_recovers(self) -> None:
        """A wrong echo is fatal to the command and survivable for the link."""
        # bytes 1 and 2 are the sync <CR><LF> sent by open(), so the third
        # byte -- the first byte of the first command -- comes back wrong.
        # probe_on_open=False keeps open() to those two bytes: with the
        # W and U probing it does by default the count would shift.
        with Fake("--corrupt-echo-every", "3",
                  "--corrupt-echo-count", "1") as fake, \
                fake.client(probe_on_open=False) as dev:
            with self.assertRaises(probe.IsegEchoError) as caught:
                dev.identify()
            self.assertEqual(caught.exception.sent, b"#")
            self.assertNotEqual(caught.exception.received, b"#")
            dev.sync()
            self.assertEqual(dev.identify().unit, "483216")
            self.assertEqual(dev.status(2), "ON")

    def test_echo_timeout_is_an_echo_error(self) -> None:
        """No echo at all (nothing listening) is the same failure mode."""
        with Fake("--corrupt-echo-every", "3",
                  "--corrupt-echo-count", "-1") as fake:
            dev = probe.IsegNHQ(fake.path, timeout=0.5, echo_timeout=0.2)
            with dev:
                with self.assertRaises(probe.IsegEchoError):
                    dev.identify()

    def test_slow_echo_still_works(self) -> None:
        with Fake("--echo-delay", "0.01") as fake, fake.client() as dev:
            self.assertEqual(dev.identify().unit, "483216")


class DeadChannelTest(unittest.TestCase):
    def test_ch1_dead_ch2_alive(self) -> None:
        with Fake("--ch1-dead") as fake, \
                fake.client(timeout=0.25, probe_on_open=False) as dev:
            with self.assertRaises(probe.IsegNHQTimeout):
                dev.status(1)
            self.assertEqual(dev.status(2), "ON")
            with self.assertRaises(probe.IsegNHQTimeout):
                dev.read_voltage(1)
            self.assertEqual(dev.read_voltage(2), 0.0)

    def test_dump_survives_the_dead_channel(self) -> None:
        with Fake("--ch1-dead") as fake, \
                fake.client(timeout=0.1, probe_on_open=False) as dev:
            text = probe.dump_text(dev)
            self.assertIn("<no answer>", text)
            self.assertIn("ch1: S=<no answer>", text)
            self.assertIn("ch2: S=ON", text)


class LogTest(unittest.TestCase):
    def test_writes_are_logged_with_a_timestamp(self) -> None:
        with Fake() as fake:
            with fake.client() as dev:
                dev.set_voltage(2, 12)
                dev.set_ramp(2, 30)
                dev.start(2)
                dev.status(2)          # a read, not logged
            log = fake.stderr_log()
            self.assertEqual(log.count(" SET "), 3)
            self.assertIn("SET D2=12", log)
            self.assertIn("SET V2=30", log)
            self.assertIn("SET G2", log)
            stamps = re.findall(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d SET ",
                                log, re.M)
            self.assertEqual(len(stamps), 3)


class CliExitCodeTest(unittest.TestCase):
    def test_zero_on_success(self) -> None:
        with Fake() as fake:
            code, text = run_cli(fake, "info")
            self.assertEqual(code, 0)
            self.assertIn("483216", text)
            self.assertEqual(run_cli(fake, "dump")[0], 0)
            self.assertEqual(run_cli(fake, "mon", "S", "--ch", "2")[0], 0)
            self.assertEqual(run_cli(fake, "set", "V", "30", "--ch", "2")[0], 0)

    def test_one_when_the_unit_answers_an_error(self) -> None:
        with Fake() as fake:
            # --yes to get past the local limit check and let the unit
            # answer for itself; without it this is a local refusal (2)
            code, text = run_cli(fake, "--max-v", "0", "set", "D", "99999",
                                 "--ch", "2", "--yes")
            self.assertEqual(code, 1)
            self.assertIn("? UMAX=3000", text)
            code, text = run_cli(fake, "raw", "X9")
            self.assertEqual(code, 1)
            self.assertIn("????", text)

    def test_two_when_the_tool_refuses(self) -> None:
        with Fake("--manual") as fake:
            code, text = run_cli(fake, "ramp", "--ch", "2", "--to", "50")
            self.assertEqual(code, 2)
            self.assertIn("refused:", text)
            code, text = run_cli(fake, "mon", "U")       # U needs --ch
            self.assertEqual(code, 2)
            self.assertIn("needs --ch", text)
            code, text = run_cli(fake, "set", "U", "5", "--ch", "2")
            self.assertEqual(code, 2)
            self.assertIn("read-only", text)

    def test_three_on_io_trouble(self) -> None:
        with Fake("--ch1-dead") as fake:
            code, text = run_cli(fake, "mon", "U", "--ch", "1", timeout="0.25")
            self.assertEqual(code, 3)
            self.assertIn("no answer", text)

    def test_three_when_the_port_is_missing(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = probe.main(["--port", "/nonexistent/tty", "info"])
        self.assertEqual(code, 3)


def wait_for_status(dev: probe.IsegNHQ, ch: int, word: str,
                    seconds: float = 3.0) -> str:
    """Poll until the channel reports ``word``, or give up and return."""
    deadline = time.monotonic() + seconds
    status = dev.status(ch)
    while status != word and time.monotonic() < deadline:
        status = dev.status(ch)
    return status


class PortLockTest(unittest.TestCase):
    def test_second_open_refused_until_first_closed(self) -> None:
        with Fake() as fake:
            first = fake.client()
            first.open()
            try:
                with self.assertRaises(probe.PortBusy):
                    fake.client().open()
                env = dict(os.environ, PYTHONPATH=DRIVER_DIR)
                res = subprocess.run(
                    [sys.executable, os.path.join(DRIVER_DIR,
                                                  "iseg_nhq_probe.py"),
                     "--port", fake.path, "--timeout", "1.0", "info"],
                    capture_output=True, text=True, env=env, timeout=20)
                self.assertEqual(res.returncode, 3)
                self.assertIn(f"port {fake.path} in use (MIDAS frontend "
                              "running? stop scfe first)", res.stderr)
            finally:
                first.close()
            with fake.client() as again:
                self.assertEqual(again.identify().unit, "483216")


class StatusReadIsAStateChangeTest(unittest.TestCase):
    """``S`` is a read that can put high voltage back on an output.

    Manual x2xx p.8: after a permanent shut-off "the previous voltage
    setting will be restored with software ramp after 'Read status word'"
    when autostart is active.  Everything here is about not doing that by
    accident.
    """

    #: 1 uA trip on a 1 MOhm load: anything above 1 V trips the channel
    TRIP_ARGS = ("--load-ohm", "1e6", "--ramp", "255")

    def _trip_channel(self, dev: probe.IsegNHQ) -> None:
        dev.set_current_trip(2, 1000)          # 1000 counts x 1 nA = 1 uA
        dev.set_voltage(2, 20)
        dev.start(2)
        self.assertEqual(wait_for_status(dev, 2, "TRP"), "TRP")
        self.assertEqual(dev.read_voltage(2), 0.0)

    def test_status_read_restarts_the_ramp_when_autostart_is_armed(self):
        with Fake(*self.TRIP_ARGS) as fake, fake.client() as dev:
            self._trip_channel(dev)
            dev.set_current_trip(2, 0)         # so it cannot trip again
            dev.set_autostart(2, 8)
            self.assertEqual(dev.autostart_is_armed(2), 8)
            self.assertEqual(dev.read_voltage(2), 0.0)

            # no G, no D -- just the status word, and the ramp restarts
            self.assertEqual(dev.status(2), "TRP")
            self.assertEqual(wait_for_status(dev, 2, "ON"), "ON")
            self.assertAlmostEqual(dev.read_voltage(2), 20.0, places=3)

    def test_status_read_does_not_restart_without_autostart(self) -> None:
        with Fake(*self.TRIP_ARGS) as fake, fake.client() as dev:
            self._trip_channel(dev)
            dev.set_current_trip(2, 0)
            self.assertEqual(dev.status(2), "TRP")
            time.sleep(0.3)
            self.assertEqual(dev.read_voltage(2), 0.0)

    def test_watch_refuses_when_autostart_is_armed(self) -> None:
        with Fake() as fake, fake.client() as dev:
            dev.set_autostart(2, 8)
            with self.assertRaises(probe.IsegRefused) as caught:
                next(probe.watch_lines(dev, 2, interval=0.05, max_s=1.0))
            self.assertIn("autostart", str(caught.exception))
            self.assertIn("A2=8", str(caught.exception))
            # and it still watches when the operator insists
            lines = list(probe.watch_lines(dev, 2, interval=0.05,
                                           max_s=1.0, yes=True))
            self.assertIn("S=ON", lines[-1])

    def test_mon_s_refuses_when_autostart_is_armed(self) -> None:
        with Fake() as fake:
            self.assertEqual(
                run_cli(fake, "set", "A", "8", "--ch", "2", "--yes")[0], 0)
            code, text = run_cli(fake, "mon", "S", "--ch", "2")
            self.assertEqual(code, 2)
            self.assertIn("autostart", text)
            code, text = run_cli(fake, "mon", "S", "--ch", "2", "--yes")
            self.assertEqual(code, 0)
            self.assertIn("ON", text)

    def test_dump_reads_a_before_s_and_warns(self) -> None:
        with Fake() as fake:
            self.assertEqual(
                run_cli(fake, "set", "A", "8", "--ch", "2", "--yes")[0], 0)
            code, text = run_cli(fake, "dump", "--ch", "2")
            self.assertEqual(code, 0)
            self.assertIn("!! WARNING ch2", text)
            self.assertIn("autostart is armed", text)
            columns = text.splitlines()[0].split()
            self.assertLess(columns.index("A"), columns.index("S"))
            self.assertLess(columns.index("T"), columns.index("S"))


class MonRefusesWhatIsNotAReadTest(unittest.TestCase):
    def test_mon_g_is_refused(self) -> None:
        with Fake() as fake:
            code, text = run_cli(fake, "mon", "G", "--ch", "2")
            self.assertEqual(code, 2)
            self.assertIn("not a read", text)
            self.assertIn("ramp", text)
            # and the output really did not move
            self.assertEqual(run_cli(fake, "set", "D", "30", "--ch", "2")[0], 0)
            code, text = run_cli(fake, "mon", "G", "--ch", "2")
            self.assertEqual(code, 2)
            code, text = run_cli(fake, "mon", "U", "--ch", "2")
            self.assertIn("+0.00 V", text)

    def test_g_is_still_reachable_through_ramp(self) -> None:
        with Fake("--ramp", "255") as fake, fake.client() as dev:
            self.assertIn("G2 ->", probe.ramp_text(dev, 2, 20.0))
            self.assertEqual(settle(dev, 2), "ON")


class SetGuardTest(unittest.TestCase):
    """``set D`` and ``set A`` are the two writes that can move the output."""

    def test_set_d_is_guarded_by_autostart(self) -> None:
        with Fake("--ramp", "255") as fake:
            self.assertEqual(
                run_cli(fake, "set", "A", "8", "--ch", "2", "--yes")[0], 0)
            code, text = run_cli(fake, "set", "D", "30", "--ch", "2")
            self.assertEqual(code, 2)
            self.assertIn("autostart", text)
            code, text = run_cli(fake, "mon", "D", "--ch", "2")
            self.assertIn("+0.00 V", text)          # nothing was written
            code, text = run_cli(fake, "set", "D", "30", "--ch", "2", "--yes")
            self.assertEqual(code, 0)
            self.assertIn("A2 = 8", text)

    def test_set_d_is_guarded_by_the_voltage_limit(self) -> None:
        with Fake() as fake:
            code, text = run_cli(fake, "set", "D", "4000", "--ch", "2")
            self.assertEqual(code, 2)
            self.assertIn("3000 V", text)

    def test_set_a_8_requires_yes(self) -> None:
        with Fake() as fake:
            code, text = run_cli(fake, "set", "A", "8", "--ch", "2")
            self.assertEqual(code, 2)
            self.assertIn("arms autostart", text)
            code, text = run_cli(fake, "mon", "A", "--ch", "2")
            self.assertEqual(code, 0)
            self.assertIn("-", text)                # nothing was armed
            # the flags that do not arm autostart go through untouched
            self.assertEqual(
                run_cli(fake, "set", "A", "4", "--ch", "2")[0], 0)


class FramingTest(unittest.TestCase):
    """Everything that can get the two sides one answer out of step."""

    def test_stray_line_does_not_become_the_next_answer(self) -> None:
        with Fake("--stray-line", "+09999-01") as fake, \
                fake.client(probe_on_open=False) as dev:
            self.assertEqual(dev.send("U", 2), "+00000-01")
            time.sleep(0.15)        # let the unsolicited line arrive
            self.assertEqual(dev.send("D", 2), "+00000-01")
            self.assertEqual(dev.identify().unit, "483216")

    def test_sync_answer_and_stale_line_are_drained(self) -> None:
        """The bench 208L answers the sync ???? and had a stale line too."""
        with Fake("--sync-answer", "????",
                  "--stale-answer", "S2=ON ") as fake, fake.client() as dev:
            self.assertEqual(dev.identify().unit, "483216")
            self.assertEqual(dev.status(2), "ON")

    def test_answer_in_the_same_chunk_as_the_echo(self) -> None:
        """Zero break time: the echoed \n and the answer arrive together."""
        with Fake("--break-time", "0") as fake, fake.client() as dev:
            self.assertEqual(dev.status(2), "ON")
            self.assertEqual(dev.identify().unit, "483216")
            self.assertEqual(dev.read_voltage(2), 0.0)

    def test_break_time_widens_the_timeouts(self) -> None:
        with Fake("--break-time", "60") as fake:
            dev = probe.IsegNHQ(fake.path, timeout=0.2, echo_timeout=0.1)
            with dev:
                self.assertEqual(dev.break_ms, 60)
                # 4 x W + 200 ms, and W + 200 ms for the echo
                self.assertAlmostEqual(dev.timeout, 0.44)
                self.assertAlmostEqual(dev.echo_timeout, 0.26)
                # an answer that takes far longer than the old whole-line
                # budget still arrives, because the deadline is per byte
                self.assertEqual(dev.status(2), "ON")

    def test_tot_resynchronises_the_link(self) -> None:
        with Fake("--tot-on-next", "1") as fake, \
                fake.client(probe_on_open=False) as dev:
            with self.assertRaises(probe.IsegNHQError) as caught:
                dev.identify()
            self.assertEqual(caught.exception.reply, "?TOT")
            self.assertEqual(caught.exception.meaning,
                             "timeout error, the unit re-initialises itself")
            # sync() has already run; the next command is in step again
            self.assertEqual(dev.identify().unit, "483216")

    def test_a_dead_link_is_not_a_timeout(self) -> None:
        with Fake("--die-after", "3") as fake, \
                fake.client(timeout=0.25, probe_on_open=False) as dev:
            with self.assertRaises(probe.IsegLinkError):
                for _ in range(6):
                    dev.status(2)

    def test_a_dead_link_exits_three(self) -> None:
        with Fake("--die-after", "1") as fake:
            code, text = run_cli(fake, "info", timeout="0.5")
            self.assertEqual(code, 3)


class SeriesTest(unittest.TestCase):
    """Which series is on the other end, and what ``L`` means on it."""

    def test_precision_series_is_detected_and_l_is_amperes(self) -> None:
        with Fake() as fake, fake.client() as dev:
            self.assertEqual(dev.series, "precision")
            dev.set_current_trip(2, 1500)       # 1500 counts x 1 nA
            trip = dev.current_trip(2)
            self.assertEqual(trip.unit, "A")
            self.assertAlmostEqual(trip.value, 1.5e-6)
            self.assertIn("uA", str(trip))
            self.assertIn("series      precision", probe.info_text(dev))

    def test_standard_series_is_detected_and_l_is_counts(self) -> None:
        with Fake("--fmt", "standard") as fake, fake.client() as dev:
            self.assertEqual(dev.series, "standard")
            dev.set_current_trip(2, 1500)
            trip = dev.current_trip(2)
            self.assertEqual(trip.unit, "counts")
            self.assertEqual(trip.value, 1500.0)
            self.assertIn("counts", str(trip))
            self.assertIn("series      standard", probe.info_text(dev))

    def test_fractional_set_point_on_the_standard_series(self) -> None:
        """50.5 V would be ???? on an x0x; it goes out as 50 V and says so."""
        with Fake("--fmt", "standard", "--ramp", "255") as fake:
            with fake.client() as dev:
                self.assertEqual(dev.series, "standard")
                report = probe.set_text(dev, "D", 2, "50.5")
                self.assertIn("rounding", report)
                self.assertIn("D2 = 50 V", report)
                self.assertEqual(dev.read_set_voltage(2), 50.0)
            log = fake.stderr_log()
            self.assertIn("SET D2=50", log)
            self.assertNotIn("????", log)

    def test_fractional_set_point_on_the_precision_series(self) -> None:
        with Fake("--ramp", "255") as fake, fake.client() as dev:
            report = probe.set_text(dev, "D", 2, "50.5")
            self.assertNotIn("rounding", report)
            self.assertAlmostEqual(dev.read_set_voltage(2), 50.5)

    def test_bench_208l_replica(self) -> None:
        """The unit on the bench: standard series, 8 kV, Vmax switch at 70 %."""
        with Fake("--id", "481198", "--sw", "2.06", "--vmax", "8000V",
                  "--imax", "1000uA", "--fmt", "standard", "--mlimit", "70",
                  "--nlimit", "50", "--ramp", "2",
                  "--stale-answer", "S2=ON ") as fake, fake.client() as dev:
            text = probe.info_text(dev)
            self.assertIn("unit        481198", text)
            self.assertIn("Vmax        8000 V", text)
            self.assertIn("Imax        1 mA", text)
            self.assertIn("series      standard", text)
            self.assertEqual(dev.status(2), "ON")
            self.assertEqual(dev.read_voltage(2), 0.0)
            with self.assertRaises(probe.IsegRefused) as caught:
                probe.ramp_text(dev, 2, 6000.0)
            self.assertIn("5600 V", str(caught.exception))


class StatusReplyShapeTest(unittest.TestCase):
    """The 208L prefixes the status word; the manual says it does not."""

    def test_prefixed_status_is_stripped(self) -> None:
        with Fake() as fake, fake.client() as dev:
            self.assertEqual(dev.send("S", 2), "S2=ON")
            self.assertEqual(dev.status(2), "ON")
            self.assertEqual(dev.status(1), "ON")

    def test_bare_status_still_works(self) -> None:
        with Fake("--bare-status") as fake, fake.client() as dev:
            self.assertEqual(dev.send("S", 2), "ON")
            self.assertEqual(dev.status(2), "ON")

    def test_dump_decodes_the_prefixed_status(self) -> None:
        with Fake() as fake, fake.client() as dev:
            self.assertIn("ch2: S=ON (output voltage has reached the set "
                          "voltage)", probe.dump_text(dev, [2]))


class ReadBackTest(unittest.TestCase):
    def test_a_clamped_set_point_aborts_before_g(self) -> None:
        """A unit that quietly stores less than it was asked for."""
        with Fake("--clamp-d", "10", "--ramp", "255") as fake:
            with fake.client() as dev:
                with self.assertRaises(probe.IsegRefused) as caught:
                    probe.ramp_text(dev, 2, 50.0)
                self.assertIn("reads back 10 V", str(caught.exception))
                time.sleep(0.3)
                self.assertEqual(dev.read_voltage(2), 0.0)   # no G was sent
            self.assertNotIn("SET G2", fake.stderr_log())

    def test_a_set_point_within_the_resolution_is_fine(self) -> None:
        with Fake("--fmt", "standard", "--ramp", "255") as fake, \
                fake.client() as dev:
            # 50.5 V is rounded to 50 V by the standard series, which is
            # inside its 1 V resolution and must not abort the ramp
            self.assertIn("G2 ->", probe.ramp_text(dev, 2, 50.5))


class WatchSettleTest(unittest.TestCase):
    """The read-back requirement added after the 2026-09-21 bench run.

    The 208L answered ``S=ON`` with U still several volts from D and kept
    moving for a few seconds; ``--meas-lag`` reproduces that in the fake.
    """

    def test_watch_waits_for_the_readback_to_settle(self) -> None:
        with Fake("--meas-lag", "1.5", "--ramp", "255") as fake, \
                fake.client() as dev:
            self.assertIn("G2 ->", probe.ramp_text(dev, 2, 40.0, speed=255))
            began = time.monotonic()
            lines = list(probe.watch_lines(dev, 2, interval=0.05,
                                           max_s=10.0))
            took = time.monotonic() - began
            self.assertTrue(lines[-1].startswith("settled: S=ON"), lines[-1])
            self.assertNotIn("stopped after", lines[-1])
            # the status word alone would have stopped it in well under a
            # second; waiting for U costs the whole measurement lag
            self.assertGreater(took, 1.0)
            self.assertLessEqual(abs(dev.read_voltage(2) - 40.0), 2.0)

    def test_watch_settle_zero_stops_on_status_alone(self) -> None:
        with Fake("--meas-lag", "1.5", "--ramp", "255") as fake, \
                fake.client() as dev:
            self.assertIn("G2 ->", probe.ramp_text(dev, 2, 40.0, speed=255))
            began = time.monotonic()
            lines = list(probe.watch_lines(dev, 2, interval=0.05,
                                           max_s=10.0, settle_v=0))
            took = time.monotonic() - began
            self.assertTrue(lines[-1].startswith("settled: S=ON"), lines[-1])
            self.assertLess(took, 1.0)
            # and this is the old behaviour's trap: it stopped with the
            # read-back still far from the set point
            self.assertLess(dev.read_voltage(2), 38.0)


class WatchSurvivesErrorsTest(unittest.TestCase):
    def test_an_error_reply_does_not_end_the_watch(self) -> None:
        # two error replies: the settle check reads D before the first
        # poll, so the first ?TOT lands there (and is shrugged off, the
        # watch falling back to the status word), the second on the S read
        with Fake("--tot-on-next", "2") as fake, \
                fake.client(probe_on_open=False) as dev:
            lines = list(probe.watch_lines(dev, 2, interval=0.05,
                                           max_s=3.0, yes=True))
            self.assertIn("?TOT", lines[0])
            self.assertIn("S=ON", lines[-1])


class ParsingTest(unittest.TestCase):
    """Pure parser cases, no emulator needed."""

    def test_status_reply_channel_must_match(self) -> None:
        self.assertEqual(proto.parse_status_reply("S2=ON ", 2), "ON")
        self.assertEqual(proto.parse_status_reply("OFF", 1), "OFF")
        with self.assertRaises(proto.ProtocolError) as caught:
            proto.parse_status_reply("S1=ON ", 2)
        self.assertIn("out of step", str(caught.exception))
        with self.assertRaises(proto.ProtocolError):
            proto.parse_start_reply("S2=L2H", 1)

    def test_number_accepts_a_decimal_point(self) -> None:
        self.assertEqual(proto.parse_number("+1234"), 1234.0)
        self.assertEqual(proto.parse_number("-0000"), 0.0)
        self.assertEqual(proto.parse_number("+12345-01"), 1234.5)
        self.assertEqual(proto.parse_number("1234.50"), 1234.5)
        self.assertEqual(proto.parse_number("-12.5"), -12.5)
        for bad in ("", "ON", "1,5", "++1", "1e5"):
            with self.subTest(bad=bad):
                with self.assertRaises(proto.ProtocolError) as caught:
                    proto.parse_number(bad)
                self.assertIn(repr(bad), str(caught.exception))

    def test_current_accepts_a_sign(self) -> None:
        self.assertAlmostEqual(proto.parse_current("12345-06"), 12345e-6)
        self.assertAlmostEqual(proto.parse_current("0000-6"), 0.0)
        self.assertAlmostEqual(proto.parse_current("-12345-06"), -12345e-6)
        self.assertAlmostEqual(proto.parse_current("+1234-09"), 1234e-9)
        for bad in ("12345", "abc-06", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(proto.ProtocolError):
                    proto.parse_current(bad)

    def test_g_answers_but_is_not_readable(self) -> None:
        self.assertTrue(proto.COMMANDS["G"].answers)
        self.assertFalse(proto.COMMANDS["G"].readable)
        self.assertEqual(proto.build_command("G", 2), "G2")
        self.assertTrue(proto.COMMANDS["S"].readable)
        self.assertIn("S", proto.STATE_CHANGING_READS)

    def test_d_formatting_per_series(self) -> None:
        self.assertEqual(proto.format_value("D", 50.5, "precision"), "50.50")
        self.assertEqual(proto.format_value("D", 50.5, "standard"), "50")
        self.assertEqual(proto.format_value("D", 51.5, "standard"), "52")
        self.assertEqual(proto.format_value("D", 50, "standard"), "50")
        self.assertIsNone(proto.rounding_note("D", 50, "standard"))
        self.assertIn("whole volts", proto.rounding_note("D", 50.5,
                                                         "standard"))


class ParserErrorExitCodeTest(unittest.TestCase):
    def test_unparsable_answer_exits_one(self) -> None:
        """A unit answering nonsense is the unit's fault: 1, not 2."""
        with Fake("--id", "not;an;identity") as fake:
            code, text = run_cli(fake, "info")
            self.assertEqual(code, 1)
            self.assertIn("could not parse", text)


class UmaxClampTest(unittest.TestCase):
    """``? UMAX=`` is not a refusal: the unit clamps and keeps the value.

    Bench, 2026-09-21: ``D2=9999`` against a 5600 V limit answered
    ``? UMAX@=5600`` -- note the stray ``@`` -- and ``D2`` then read back
    ``5600``.  Both halves of that matter: the reply has to be recognised
    despite the junk, and the set point has to be read back afterwards.
    """

    BENCH = ("--vmax", "8000V", "--imax", "1000uA", "--fmt", "standard",
             "--mlimit", "70", "--umax-junk", "@")

    def test_junk_in_the_umax_reply_is_still_the_limit_error(self) -> None:
        with Fake(*self.BENCH) as fake, fake.client() as dev:
            with self.assertRaises(probe.IsegNHQError) as caught:
                dev.command("D2=9999")
            self.assertEqual(caught.exception.reply, "? UMAX@=5600")
            self.assertIn("exceeds the Vmax hardware limit",
                          caught.exception.meaning)
            self.assertEqual(proto.parse_umax_limit(caught.exception.reply),
                             5600.0)
            self.assertEqual(dev.read_set_voltage(2), 5600.0)

    def test_ramp_reports_the_clamp_and_sends_no_g(self) -> None:
        with Fake(*self.BENCH, "--ramp", "255") as fake:
            with fake.client() as dev:
                with self.assertRaises(probe.IsegNHQError) as caught:
                    # --yes past the hardware check, --max-v 0 past the
                    # software ceiling: this is the unit's own refusal
                    probe.ramp_text(dev, 2, 9999.0, yes=True, max_v=0)
                self.assertIn("UMAX", caught.exception.reply)
                self.assertIn("reads back 5600 V", caught.exception.meaning)
                self.assertIn("off --ch 2", caught.exception.meaning)
                time.sleep(0.3)
                self.assertEqual(dev.read_voltage(2), 0.0)
            self.assertNotIn("SET G2", fake.stderr_log())

    def test_the_cli_exits_one_and_says_to_run_off(self) -> None:
        with Fake(*self.BENCH) as fake:
            code, text = run_cli(fake, "--max-v", "0", "set", "D", "9999",
                                 "--ch", "2", "--yes")
            self.assertEqual(code, 1)
            self.assertIn("clamped", text.lower() + "clamped")
            self.assertIn("reads back 5600 V", text)
            self.assertIn("off --ch 2", text)
            # and 'off' is a working recovery
            self.assertEqual(run_cli(fake, "off", "--ch", "2")[0], 0)
            code, text = run_cli(fake, "mon", "D", "--ch", "2")
            self.assertIn("+0.00 V", text)


class SoftwareCeilingTest(unittest.TestCase):
    """``--max-v``: the ceiling the 10 % Vmax switch cannot express."""

    def test_ramp_above_the_ceiling_is_refused(self) -> None:
        with Fake() as fake:
            code, text = run_cli(fake, "ramp", "--ch", "2", "--to", "1400")
            self.assertEqual(code, 2)
            self.assertIn("1300 V", text)          # the software ceiling
            self.assertIn("3000 V", text)          # the hardware limit
            # --yes does not lift it
            code, text = run_cli(fake, "ramp", "--ch", "2", "--to", "1400",
                                 "--yes")
            self.assertEqual(code, 2)
            self.assertIn("does not lift", text)

    def test_ramp_above_the_ceiling_is_allowed_when_it_is_off(self) -> None:
        with Fake("--ramp", "255") as fake:
            code, text = run_cli(fake, "--max-v", "0", "ramp", "--ch", "2",
                                 "--to", "1400")
            self.assertEqual(code, 0)
            self.assertIn("D2 = 1400 V", text)
            self.assertIn("G2 ->", text)

    def test_set_d_above_the_ceiling_is_refused(self) -> None:
        with Fake() as fake:
            code, text = run_cli(fake, "set", "D", "1400", "--ch", "2")
            self.assertEqual(code, 2)
            self.assertIn("software ceiling", text)
            code, text = run_cli(fake, "mon", "D", "--ch", "2")
            self.assertIn("+0.00 V", text)         # nothing was written
            self.assertEqual(
                run_cli(fake, "--max-v", "0", "set", "D", "1400",
                        "--ch", "2")[0], 0)

    def test_info_prints_the_effective_limit(self) -> None:
        with Fake() as fake:
            code, text = run_cli(fake, "info")
            self.assertEqual(code, 0)
            self.assertIn("max-v       1300 V", text)
            self.assertIn("limit ch2    1300 V (hardware 3000 V", text)
            code, text = run_cli(fake, "--max-v", "0", "info")
            self.assertIn("max-v       off", text)
            self.assertIn("limit ch2    3000 V", text)

    def test_the_ceiling_does_not_touch_smaller_set_points(self) -> None:
        with Fake("--ramp", "255") as fake, fake.client() as dev:
            self.assertIn("ceiling = 1300 V",
                          probe.ramp_text(dev, 2, 200.0))
            self.assertEqual(settle(dev, 2), "ON")


if __name__ == "__main__":
    unittest.main()