#!/usr/bin/env python3
"""Drive ``fake_caen_hv.py`` with the ``caen_hv_probe.py`` module API.

    python3 -m unittest discover -s drivers/caen_hv/tests
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER_DIR = os.path.dirname(HERE)
sys.path.insert(0, DRIVER_DIR)

import caen_hv_probe as probe          # noqa: E402
import caen_hv_protocol as proto       # noqa: E402

FAKE = os.path.join(DRIVER_DIR, "fake_caen_hv.py")
START_TIMEOUT = 10.0


class Fake:
    """The emulator as a subprocess; ``path`` is its pty slave."""

    def __init__(self, *args: str) -> None:
        self.args = args
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

    def client(self) -> probe.CaenHV:
        return probe.CaenHV(self.path, bd=0, timeout=2.0)


class FakeWithProbeTest(unittest.TestCase):
    def test_info(self) -> None:
        with Fake() as fake, fake.client() as dev:
            info = probe.info_dict(dev)
            self.assertEqual(info["BDNAME"], "DT1470ET")
            self.assertEqual(info["BDNCH"], "4")
            self.assertEqual(info["BDCTR"], "REMOTE")
            self.assertEqual(dev.n_channels(), 4)

    def test_set_then_mon_vset(self) -> None:
        with Fake() as fake, fake.client() as dev:
            dev.set("VSET", 0, "500")
            self.assertAlmostEqual(dev.mon_float("VSET", 0), 500.0, places=3)

    def test_ramp_after_on(self) -> None:
        with Fake() as fake, fake.client() as dev:
            dev.set("VSET", 0, "500")
            dev.set("RUP", 0, "50")
            self.assertAlmostEqual(dev.mon_float("VMON", 0), 0.0, places=3)
            dev.set("ON", 0)
            time.sleep(1.0)
            vmon = dev.mon_float("VMON", 0)
            self.assertGreater(vmon, 10.0)
            self.assertLess(vmon, 500.0)
            # IMON = VMON / 1 MOhm -> 1 uA per volt
            self.assertAlmostEqual(dev.mon_float("IMON", 0), vmon, delta=5.0)

    def test_stat_decodes_on_and_rup(self) -> None:
        with Fake() as fake, fake.client() as dev:
            dev.set("VSET", 0, "500")
            dev.set("RUP", 0, "10")
            dev.set("ON", 0)
            time.sleep(0.3)
            bits = proto.decode_stat(dev.mon_int("STAT", 0))
            self.assertIn("ON", bits)
            self.assertIn("RUP", bits)
            self.assertNotIn("RDW", bits)

    def test_local_mode_refuses_set(self) -> None:
        with Fake("--local") as fake, fake.client() as dev:
            self.assertEqual(probe.info_dict(dev)["BDCTR"], "LOCAL")
            with self.assertRaises(probe.CaenHVError) as caught:
                dev.set("VSET", 0, "100")
            self.assertEqual(caught.exception.kind, "LOC")
            self.assertIn("LOC:ERR", caught.exception.reply.raw)
            # MON still works in LOCAL mode
            self.assertAlmostEqual(dev.mon_float("VSET", 0), 0.0, places=3)

    def test_vset_above_maxv_is_val_err(self) -> None:
        with Fake() as fake, fake.client() as dev:
            dev.set("MAXV", 0, "100")
            with self.assertRaises(probe.CaenHVError) as caught:
                dev.set("VSET", 0, "200")
            self.assertEqual(caught.exception.kind, "VAL")
            self.assertAlmostEqual(dev.mon_float("VSET", 0), 0.0, places=3)

    def test_clip_vset_flag(self) -> None:
        with Fake("--clip-vset") as fake, fake.client() as dev:
            dev.set("MAXV", 0, "100")
            dev.set("VSET", 0, "200")
            self.assertAlmostEqual(dev.mon_float("VSET", 0), 100.0, places=3)
            self.assertIn("MAXV", proto.decode_stat(dev.mon_int("STAT", 0)))

    def test_numeric_replies_are_zero_padded(self) -> None:
        """The board zero-pads every numeric reply (fw 1.08)."""
        with Fake() as fake, fake.client() as dev:
            dev.set("VSET", 0, "1100")
            dev.set("ISET", 0, "400")
            dev.set("MAXV", 0, "8100")
            dev.set("RUP", 0, "50")
            dev.set("TRIP", 0, "10")
            self.assertEqual(dev.mon("VSET", 0), "1100.0")
            self.assertEqual(dev.mon("VMON", 0), "0000.0")
            self.assertEqual(dev.mon("ISET", 0), "0400.00")
            self.assertEqual(dev.mon("IMON", 0), "0000.00")
            self.assertEqual(dev.mon("MAXV", 0), "8100")
            self.assertEqual(dev.mon("RUP", 0), "050")
            self.assertEqual(dev.mon("RDW", 0), "050")
            self.assertEqual(dev.mon("TRIP", 0), "0010.0")
            self.assertEqual(dev.mon("STAT", 0), "00000")
            self.assertEqual(dev.mon("BDNCH"), "4")
            self.assertEqual(dev.mon("BDALARM"), "0")
            self.assertEqual(dev.mon("PDWN", 0), "KILL")
            # padding must not survive into the typed accessors
            self.assertEqual(dev.mon_float("VSET", 0), 1100.0)
            self.assertEqual(dev.mon_int("MAXV", 0), 8100)

    def test_raw_stat_reply_is_five_digits(self) -> None:
        """A raw MON STAT reply below 10000 is 'VAL:0' + a 5-digit field."""
        with Fake("--stat-bits", "2048") as fake, fake.client() as dev:
            reply = dev.exchange(
                proto.build_request(0, "MON", "STAT", ch=0))
            self.assertIn("VAL:0", reply.raw)
            value = reply.value or ""
            self.assertEqual(len(value), 5)
            self.assertTrue(value.isdigit())
            self.assertLess(int(value, 10), 10000)
            self.assertEqual(value, "02048")
            # decimal, not octal: octal 02048 would not even parse
            self.assertEqual(reply.as_int(), 2048)
            self.assertEqual(proto.parse_int(value), 2048)
            self.assertIn("KILL", proto.decode_stat(reply.as_int()))

    def test_parse_reply_zero_padded_is_decimal(self) -> None:
        reply = proto.parse_reply("#BD:00,CMD:OK,VAL:02048\r\n")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.value, "02048")
        self.assertEqual(reply.as_int(), 2048)
        self.assertEqual(proto.parse_int("02048"), 2048)
        self.assertEqual(proto.parse_float("0000.00"), 0.0)
        self.assertEqual(proto.parse_int("00000"), 0)
        # what the C++ driver did wrong
        with self.assertRaises(ValueError):
            int("02048", 0)

    def test_format_value_matches_hardware(self) -> None:
        cases = [
            ("VSET", 1100.0, "1100.0"), ("VSET", 0.0, "0000.0"),
            ("VMON", 0.0, "0000.0"), ("ISET", 400.0, "0400.00"),
            ("IMON", 0.0, "0000.00"), ("MAXV", 8100.0, "8100"),
            ("RUP", 50.0, "050"), ("RDW", 50.0, "050"),
            ("TRIP", 10.0, "0010.0"), ("STAT", 2048, "02048"),
            ("BDNCH", 4, "4"), ("BDALARM", 0, "0"),
            ("POL", "-", "-"), ("PDWN", "RAMP", "RAMP"),
            ("BDFREL", "1.08", "1.08"), ("BDSNUM", "33997", "33997"),
        ]
        for par, value, expected in cases:
            with self.subTest(par=par, value=value):
                self.assertEqual(proto.format_value(value, par), expected)

    def test_observed_board_identity(self) -> None:
        with Fake() as fake, fake.client() as dev:
            info = probe.info_dict(dev)
            self.assertEqual(info["BDFREL"], "1.08")
            self.assertEqual(info["BDSNUM"], "33997")
            self.assertEqual(info["BDILK"], "NO")
            self.assertEqual(info["BDILKM"], "CLOSED")

    def test_wrong_board_address_is_silence(self) -> None:
        with Fake() as fake:
            dev = probe.CaenHV(fake.path, bd=1, timeout=0.5)
            with dev, self.assertRaises(probe.CaenHVTimeout):
                dev.mon("BDNAME")

    def test_stat_bits_flag_shows_ovc(self) -> None:
        with Fake("--stat-bits", "8") as fake, fake.client() as dev:
            self.assertIn("OVC", proto.decode_stat(dev.mon_int("STAT", 0)))

    def test_bad_channel_and_parameter(self) -> None:
        with Fake() as fake, fake.client() as dev:
            with self.assertRaises(probe.CaenHVError) as caught:
                dev.mon("VMON", 9)
            self.assertEqual(caught.exception.kind, "CH")
            with self.assertRaises(probe.CaenHVError) as caught:
                dev.mon("NOSUCH", 0)
            self.assertEqual(caught.exception.kind, "PAR")

    def test_polarity_and_dump(self) -> None:
        with Fake("--pol", "-,+,+,-") as fake, fake.client() as dev:
            self.assertEqual([dev.mon("POL", ch) for ch in range(4)],
                             ["-", "+", "+", "-"])
            table = probe.dump_text(dev)
            self.assertIn("VMON", table.splitlines()[0])
            self.assertEqual(len(table.splitlines()[2:6]), 4)

    def test_fault_current_trips_channel(self) -> None:
        with Fake("--fault-current", "50") as fake, fake.client() as dev:
            dev.set("ISET", 0, "10")
            dev.set("TRIP", 0, "0")
            dev.set("VSET", 0, "100")
            dev.set("ON", 0)
            time.sleep(0.5)
            bits = proto.decode_stat(dev.mon_int("STAT", 0))
            self.assertIn("TRIP", bits)
            self.assertNotIn("ON", bits)

    def test_reconnect_after_client_close(self) -> None:
        with Fake() as fake:
            with fake.client() as dev:
                dev.set("VSET", 1, "250")
            with fake.client() as dev:
                self.assertAlmostEqual(dev.mon_float("VSET", 1), 250.0, places=3)

    def test_multiple_commands_in_one_write(self) -> None:
        with Fake() as fake, fake.client() as dev:
            dev.open()
            os.write(dev.fd, b"$BD:00,CMD:SET,CH:2,PAR:VSET,VAL:321\r\n"
                             b"$BD:00,CMD:MON,CH:2,PAR:VSET\r\n")
            first = proto.parse_reply(dev._read_line())
            second = proto.parse_reply(dev._read_line())
            self.assertTrue(first.ok)
            self.assertEqual(float(second.value or "nan"), 321.0)

    def test_set_log_on_stderr(self) -> None:
        with Fake() as fake:
            with fake.client() as dev:
                dev.set("VSET", 0, "42")
                dev.set("ON", 0)
            assert fake.proc is not None
            fake.proc.terminate()
            fake.proc.wait(timeout=5)
            assert fake.proc.stderr is not None
            log = fake.proc.stderr.read()
            self.assertEqual(log.count(" SET "), 2)
            self.assertIn("PAR:VSET,VAL:42", log)
            self.assertIn("PAR:ON", log)


if __name__ == "__main__":
    unittest.main()
