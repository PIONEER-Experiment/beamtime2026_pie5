"""MuPix PLL recovery (pioneer.sequencer.mupix_recovery) against a fake ODB.

No MIDAS needed. The PCLS fixture is a real pair of snapshots read from pinky, 3 s apart,
with the DAQ stopped: chips 2 and 6 had dropped their PLL (READY = 0, ~1.2e8 8b10b errors/s,
chip 6's counters wrapping between the reads), chip 5 link 16 was noisy but READY, FEB 0
links 24-35 (unused) showed junk and the FEB 1 (SMA) block read 0xCCCCCCCC.

FakeSeq stands in for the SequenceClient: it records every ODB write, plays the Quads
frontend for MupixConfig (configures the chips in ASICMask[0] after a few polls and writes
the key back to false), and synthesises PCLS from the live snapshot so that a chip comes
back once it has had a full EnPLL 1 -> 0 pulse (or never). The last part runs the sequence
loop of sequencer/sequencer_operator.py with stubbed midas and run-DB modules.
"""

import copy
import json
import re
import sys
import types
from collections import Counter
from pathlib import Path

import pytest

from pioneer.sequencer import mupix_recovery as mr

DATA = Path(__file__).resolve().parent / "data" / "pcls_chip2_broken.json"
SEQUENCER = Path(__file__).resolve().parents[2] / "sequencer" / "sequencer_operator.py"
LIVE = json.loads(DATA.read_text())

ASIC0 = f"{mr.ASIC_MASK_PATH}[0]"
CFG = mr.MUPIX_CONFIG_PATH


def en(c):
    return f"{mr.ENPLL_PATH}[{c}]"


class FakeSeq:
    def __init__(self, broken=(), fix_after=None, lvds=0xFFFFFF, febs_quads=(True, False, False, False),
                 febs_sma=(False, True, False, False), febs_active=(True, True, False, False),
                 asic_mask0=0x28, config_polls=2, config_hangs_on=None, wait_hook=None,
                 write_hook=None, stale=False, junk=False, pending_config=False, unreadable=False,
                 run_state=1):
        self.t = 1000.0
        self.odb = {
            mr.ASIC_MASK_PATH: [asic_mask0, 0, 0, 255],
            mr.ENPLL_PATH: [0] * 32,
            mr.LVDS_MASK_PATH: [lvds] * 4,
            mr.FEBS_ACTIVE_PATH: list(febs_active),
            mr.FEBS_QUADS_PATH: list(febs_quads),
            mr.FEBS_SMA_PATH: list(febs_sma),
            CFG: pending_config,
            "/Runinfo/State": run_state,
            "/Runinfo/Req number events": 10,
        }
        self.writes, self.msgs, self.configs = [], [], []
        self.broken = set(broken)
        self.fix_after = dict(fix_after or {})     # chip -> pulses it needs; absent = never recovers
        self.pulses, self.pll_high = Counter(), set()
        self.config_polls, self.config_hangs_on = config_polls, config_hangs_on
        self.n_config, self._left = 0, None
        self.wait_hook, self.write_hook, self.stale, self.junk = wait_hook, write_hook, stale, junk
        self.unreadable = unreadable
        self.pcls_reads = 0

    # ---- the four calls mupix_recovery uses
    def odb_get(self, path, include_key_metadata=False):
        if path == mr.PCLS_PATH:
            self.pcls_reads += 1
            vals = self.pcls()
            lw = 0 if self.stale else int(self.t)
            return {"PCLS": vals, "PCLS/key": {"last_written": lw}} if include_key_metadata else vals
        if path == CFG and self.odb[CFG] and self._left is not None:
            if self._left == 0:
                self._frontend_config()
            else:
                self._left -= 1
        return copy.deepcopy(self.odb[path])

    def odb_set(self, path, value, create_if_needed=True, resize_arrays=True):
        assert create_if_needed is False and resize_arrays is False
        self.writes.append((path, value))
        m = re.match(r"(.*)\[(\d+)\]$", path)
        if m:
            arr = self.odb[m.group(1)]
            assert int(m.group(2)) < len(arr)
            arr[int(m.group(2))] = value
        else:
            assert not isinstance(self.odb[path], list)
            self.odb[path] = value
        if path == CFG and value:
            self.n_config += 1
            self._left = None if self.config_hangs_on == self.n_config else self.config_polls
        if self.write_hook:
            self.write_hook(self, path, value)

    def msg(self, text, is_error=False):
        self.msgs.append(text)

    def wait_seconds(self, s):
        self.t += s
        if self.wait_hook:
            self.wait_hook(self, s)

    # ---- the Quads frontend and the chips
    def _frontend_config(self):
        mask, enpll = self.odb[mr.ASIC_MASK_PATH][0], self.odb[mr.ENPLL_PATH]
        self.configs.append((mask, tuple(enpll[:8])))
        for c in range(8):
            if not (mask >> c) & 1:
                continue
            if enpll[c]:
                self.pll_high.add(c)
            elif c in self.pll_high:
                self.pll_high.discard(c)
                self.pulses[c] += 1
                if c in self.fix_after and self.pulses[c] >= self.fix_after[c]:
                    self.broken.discard(c)
        self.odb[CFG] = False
        self._left = None

    def pcls(self):
        t = 0 if self.stale else self.t
        v = list(LIVE["snap_b"])
        for link in range(36):
            o = 2 + 4 * link
            if link < 24:
                bad = link // 3 in self.broken
                st = (v[o] | mr.READY_BIT | mr.FPGA_PLL_BIT) & ~(mr.READY_BIT if bad else 0)
                rate = 1.24e8 if bad else (3.9e5 if link == 16 else 0)
            else:                                   # unused FEB 0 links: junk, as seen live
                st, rate = v[o] & ~mr.READY_BIT, 1.24e8
            v[o], v[o + 2] = st, int(v[o + 2] + rate * t) % 2**32
        if self.junk:                               # SMA board block: READY=0 and wild counters
            for link in range(36):
                o = mr.PCLS_FEB_BLOCK + 2 + 4 * link
                v[o], v[o + 2] = 0x0CCCCCCC, int(0xCCCCCCCC + 5e8 * t) % 2**32
        if self.unreadable:                         # FEB 0 not read out: its block reads 0
            v[2:mr.PCLS_FEB_BLOCK] = [0] * (mr.PCLS_FEB_BLOCK - 2)
        return v


@pytest.fixture
def make_seq(monkeypatch):
    def make(**kw):
        seq = FakeSeq(**kw)
        monkeypatch.setattr(mr, "_monotonic", lambda: seq.t)
        return seq
    return make


def recipe(chips, saved):
    mask = sum(1 << c for c in chips)
    return ([(ASIC0, mask)] + [(en(c), 1) for c in chips] + [(CFG, True)]
            + [(en(c), 0) for c in chips] + [(CFG, True)]
            + [(en(c), 0) for c in chips] + [(ASIC0, saved)])


def assert_restored(seq, saved=0x28):
    assert seq.odb[mr.ASIC_MASK_PATH] == [saved, 0, 0, 255]
    assert seq.odb[mr.ENPLL_PATH] == [0] * 32


# ---------------------------------------------------------------- pure functions

def test_live_snapshot_finds_chips_2_and_6():
    a, b = mr.parse_pcls(LIVE["snap_a"]), mr.parse_pcls(LIVE["snap_b"])
    assert sorted(a) == list(range(24))
    statuses, nv = mr.classify(a, b, LIVE["dt_s"], mr.enabled_chips(0xFFFFFF))
    assert nv is None
    assert [c for c, s in statuses.items() if s.bad] == [2, 6]
    assert statuses[2].ready == (0, 0, 0) and min(statuses[2].rates) > 1e8
    # chip 6's counters wrapped between the two reads: still a positive ~1.2e8/s
    assert all(1e8 < r < 2e8 for r in statuses[6].rates)
    # chip 5 link 16: noisy (1e5-1e6/s) but READY, not bad
    assert statuses[5].ready == (1, 1, 1) and 1e5 < statuses[5].rates[1] < 1e6
    assert not statuses[5].bad


def test_hex_strings_parse_like_ints():
    hexed = ["0x%08x" % x for x in LIVE["snap_b"]]
    assert mr.parse_pcls(hexed) == mr.parse_pcls(LIVE["snap_b"])
    assert mr.as_int("0x0028") == 40 and mr.as_int(40) == 40 and mr.as_int("40") == 40


def test_counter_wraparound():
    a, b = mr.parse_pcls(LIVE["snap_b"]), mr.parse_pcls(LIVE["snap_b"])
    st = a[0][0]
    a[0] = (st, 0, 2**32 - 100, 0)
    b[0] = (st, 0, 50, 0)
    statuses, nv = mr.classify(a, b, 1.0, [0])
    assert statuses[0].rates[0] == 150 and not statuses[0].bad and nv is None


def test_counter_going_down_is_a_reset_not_a_rate():
    a, b = mr.parse_pcls(LIVE["snap_b"]), mr.parse_pcls(LIVE["snap_b"])
    st = a[0][0]                               # READY, FPGA PLL locked
    a[0] = (st, 0, 5_000_000, 0)
    b[0] = (st, 0, 1_000, 0)                   # counters cleared between the reads
    statuses, nv = mr.classify(a, b, 3.0, [0])
    assert nv is None and statuses[0].rates[0] is None and not statuses[0].bad
    assert "counter went down" in statuses[0].describe()
    # a reset does not hide a lost PLL: READY=0 with the FPGA PLL locked is still bad
    b[0] = (st & ~mr.READY_BIT, 0, 1_000, 0)
    statuses, _ = mr.classify(a, b, 3.0, [0])
    assert statuses[0].bad and statuses[0].rates[0] is None


def test_unreadable_feb_gives_no_verdict():
    a = mr.parse_pcls(LIVE["snap_a"])
    zero = {l: (0, 0, 0, 0) for l in a}
    statuses, nv = mr.classify(a, zero, 3.0, range(8))
    assert nv.kind == "unreadable" and "status word 0" in nv.text
    # FPGA receiver PLL (bit 31) unlocked: a FEB problem, not a chip PLL
    b = dict(a)
    b[4] = (a[4][0] & ~mr.FPGA_PLL_BIT, *a[4][1:])
    statuses, nv = mr.classify(a, b, 3.0, range(8))
    assert nv.kind == "unreadable" and "FPGA receiver PLL" in nv.text


def test_stale_snapshot_trusts_no_verdict():
    a = mr.parse_pcls(LIVE["snap_b"])
    statuses, nv = mr.classify(a, dict(a), 3.0, range(8))
    assert nv.kind == "stale"
    # identical words but the key was rewritten: a quiet detector, verdict from READY
    statuses, nv = mr.classify(a, dict(a), 3.0, range(8), key_updated=True)
    assert nv is None
    assert [c for c, s in statuses.items() if s.bad] == [2, 6]


def test_chip_links_and_enabled_chips():
    assert mr.chip_links(2) == [6, 7, 8]
    assert mr.enabled_chips(0xFFFFFF) == list(range(8))
    assert mr.enabled_chips(0xFFFFFF & ~(1 << 7)) == [0, 1, 3, 4, 5, 6, 7]
    assert mr.enabled_chips("0xffffff") == list(range(8))
    assert mr.enabled_chips(0) == []


def test_short_pcls_is_refused():
    with pytest.raises(ValueError):
        mr.parse_pcls([0] * 50)


@pytest.mark.parametrize("path", [
    f"{mr.ASIC_MASK_PATH}[1]", f"{mr.ASIC_MASK_PATH}[3]", mr.ASIC_MASK_PATH,
    f"{mr.ENPLL_PATH}[8]", f"{mr.ENPLL_PATH}[-1]", f"{mr.ENPLL_PATH}[31]", mr.ENPLL_PATH,
    mr.LVDS_MASK_PATH, "/Equipment/Quads/Settings/DAQ/Commands/ResetASICs",
    "/Equipment/Quads/Settings/DAQ/Commands/MupixTDACConfig", "/Runinfo/State",
])
def test_guard_refuses(make_seq, path):
    seq = make_seq()
    with pytest.raises(ValueError):
        mr._guarded_set(seq, path, 0)
    assert seq.writes == []


def test_guard_allows_the_three_keys(make_seq):
    seq = make_seq()
    for path, value in [(ASIC0, 4), (en(0), 0), (en(7), 0), (CFG, False)]:
        mr._guarded_set(seq, path, value)
    assert len(seq.writes) == 4


# ---------------------------------------------------------------- the check

def test_find_bad_chips_live_like(make_seq):
    seq = make_seq(broken={2, 6})
    bad, nv, statuses = mr.find_bad_chips(seq)
    assert bad == [2, 6] and nv is None and sorted(statuses) == list(range(8))
    assert seq.writes == []


def test_junk_outside_mupix_links_is_ignored(make_seq):
    seq = make_seq(junk=True)          # FEB 1 READY=0 + huge rates, FEB 0 links 24-35 junk
    bad, nv, _ = mr.find_bad_chips(seq)
    assert bad == [] and nv is None
    result = mr.recover(seq, 3)
    assert result.ok and result.rounds == 0 and seq.writes == []


def test_masked_chip_is_skipped(make_seq):
    seq = make_seq(broken={2, 6}, fix_after={6: 1},
                   lvds=0xFFFFFF & ~(0b111 << 6))   # chip 2's links masked
    result = mr.recover(seq, 3)
    assert result.ok and result.rounds == 1
    assert seq.writes == recipe([6], 0x28)


# ---------------------------------------------------------------- the reset

def test_reset_write_order_and_restore(make_seq):
    seq = make_seq(broken={2, 6}, fix_after={2: 1, 6: 1})
    mr.reset_pll(seq, [6, 2])
    assert seq.writes == recipe([2, 6], 0x28)
    # the frontend configured chips 2 and 6 with EnPLL 1, then 0
    en_on = tuple(1 if c in (2, 6) else 0 for c in range(8))
    assert seq.configs == [(0x44, en_on), (0x44, (0,) * 8)]
    assert_restored(seq)
    assert any(m.startswith(mr.PREFIX + "starting ODB writes") for m in seq.msgs)


@pytest.mark.parametrize("hang", [1, 2])
def test_restore_after_config_timeout(make_seq, hang):
    seq = make_seq(broken={2}, config_hangs_on=hang)
    with pytest.raises(mr.MupixConfigTimeout, match="did not answer"):
        mr.reset_pll(seq, [2])
    assert_restored(seq)
    # our pending MupixConfig is cancelled first, then EnPLL and ASICMask are restored
    assert seq.writes[-3:] == [(CFG, False), (en(2), 0), (ASIC0, 0x28)]
    assert seq.odb[CFG] is False
    assert sum(1 for p, v in seq.writes if p == CFG and v) == hang     # no extra MupixConfig
    assert any("still pending: set back to false" in m for m in seq.msgs)
    assert any("may still hold EnPLL = 1" in m for m in seq.msgs)


def test_restore_after_exception_in_wait(make_seq):
    def boom(seq, s):
        if s == mr.PLL_PULSE_S:
            raise RuntimeError("sequence aborted")
    seq = make_seq(broken={2}, wait_hook=boom)
    with pytest.raises(RuntimeError, match="sequence aborted"):
        mr.reset_pll(seq, [2])
    assert_restored(seq)
    assert seq.writes[-2:] == [(en(2), 0), (ASIC0, 0x28)]
    assert (CFG, False) not in seq.writes          # the first MupixConfig had been answered
    assert any("may still hold EnPLL = 1" in m for m in seq.msgs)


def test_exception_while_our_config_is_pending_cancels_it(make_seq):
    def boom(seq, s):
        if s == mr.POLL_S:
            raise RuntimeError("sequence aborted")
    seq = make_seq(broken={2}, wait_hook=boom, config_polls=100)
    with pytest.raises(RuntimeError, match="sequence aborted"):
        mr.reset_pll(seq, [2])
    assert seq.writes == [(ASIC0, 0x04), (en(2), 1), (CFG, True),
                          (CFG, False), (en(2), 0), (ASIC0, 0x28)]
    assert seq.configs == [] and seq.odb[CFG] is False
    assert any("may still hold EnPLL = 1" in m for m in seq.msgs)


def test_exception_before_any_config_has_no_enpll_warning(make_seq):
    def boom(seq, path, value):
        if path == en(2) and value == 1:
            raise RuntimeError("odb error")
    seq = make_seq(broken={2}, write_hook=boom)
    with pytest.raises(RuntimeError):
        mr.reset_pll(seq, [2])
    assert_restored(seq)
    assert not any("may still hold" in m for m in seq.msgs)


class FakeStop(Exception):
    pass


def _run_with_stop_tracer(seq, state):
    """reset_pll under a trace function that, like the MIDAS sequencer's, raises on every
    Python call once state['stop'] is set. Returns whether the stop reached the caller."""
    def tracer(frame, event, arg):
        if event == "call" and state["stop"]:
            raise FakeStop()
        return None

    caught = False
    old = sys.gettrace()
    sys.settrace(tracer)
    try:
        mr.reset_pll(seq, [2])
    except FakeStop:
        caught = True
    finally:
        sys.settrace(old)
    return caught


def test_restore_after_sequencer_stop_during_pulse(make_seq):
    state = {"stop": False}

    def press_stop(seq, s):
        if s == mr.PLL_PULSE_S:
            state["stop"] = True

    seq = make_seq(broken={2}, wait_hook=press_stop)
    assert _run_with_stop_tracer(seq, state)
    assert_restored(seq)
    assert seq.writes[-2:] == [(en(2), 0), (ASIC0, 0x28)]


def test_restore_finishes_when_stop_pressed_during_restore(make_seq):
    """Stop pressed between the restore writes must not cut the restore short."""
    state = {"stop": False}

    def press_stop(seq, path, value):
        if (path, value) == (en(2), 0) and seq.writes.count((en(2), 0)) == 2:   # first restore write
            state["stop"] = True

    seq = make_seq(broken={2}, write_hook=press_stop)
    _run_with_stop_tracer(seq, state)
    assert_restored(seq)
    assert seq.writes == recipe([2], 0x28)


def test_nonzero_enpll_is_logged_and_left_at_zero(make_seq):
    seq = make_seq(broken={2}, fix_after={2: 1})
    seq.odb[mr.ENPLL_PATH][2] = 1
    mr.reset_pll(seq, [2])
    assert seq.odb[mr.ENPLL_PATH][2] == 0
    assert any("EnPLL was 1" in m for m in seq.msgs)


def test_pending_config_times_out_without_writes(make_seq):
    seq = make_seq(broken={2}, pending_config=True)   # someone else's MupixConfig never ends
    with pytest.raises(mr.MupixConfigTimeout, match="already pending before the recovery started"):
        mr.reset_pll(seq, [2])
    assert seq.writes == []                           # not ours: not cancelled either


@pytest.mark.parametrize("state", [2, 3])
def test_reset_refuses_unless_stopped(make_seq, state):
    seq = make_seq(broken={2}, run_state=state)
    with pytest.raises(mr.RunNotStopped):
        mr.reset_pll(seq, [2])
    assert seq.writes == []
    result = mr.recover(seq, 3)
    assert "not stopped" in result.aborted_reason and seq.writes == []
    assert result.exit_code == mr.EXIT_REFUSED
    assert "aborted" in result.operator_message()
    result = mr.recover(seq, 1, allow_running=True)
    assert seq.writes == recipe([2], 0x28)


def test_run_started_between_rounds_stops_the_writes(make_seq):
    def start_run(seq, path, value):
        if (path, value) == (ASIC0, 0x28):           # end of the first round's restore
            seq.odb["/Runinfo/State"] = 3
    seq = make_seq(broken={2}, write_hook=start_run)
    result = mr.recover(seq, 3)
    assert seq.writes == recipe([2], 0x28)
    assert result.rounds == 2 and "not stopped" in result.aborted_reason and result.still_bad == [2]


@pytest.mark.parametrize("chips", [[8], [-1], ["2"], [2.0]])
def test_reset_refuses_non_mupix_chips(make_seq, chips):
    seq = make_seq()
    with pytest.raises(ValueError):
        mr.reset_pll(seq, chips)
    assert seq.writes == []


# ---------------------------------------------------------------- the retry loop

def test_clean_detector_writes_nothing(make_seq):
    seq = make_seq()
    result = mr.recover(seq, 3)
    assert result.ok and result.rounds == 0 and seq.writes == []
    assert any("all 8 enabled chip(s) OK" in m for m in seq.msgs)


def test_only_bad_chips_reset_then_fixed(make_seq):
    seq = make_seq(broken={2, 6}, fix_after={2: 1, 6: 2})
    result = mr.recover(seq, 3)
    assert result.ok and result.rounds == 2 and result.still_bad == []
    assert seq.writes == recipe([2, 6], 0x28) + recipe([6], 0x28)
    assert all(m.startswith(mr.PREFIX) for m in result.log)


def test_stops_after_n_rounds(make_seq):
    seq = make_seq(broken={2})
    result = mr.recover(seq, 2)
    assert result.still_bad == [2] and result.rounds == 2 and not result.ok
    assert seq.writes == recipe([2], 0x28) * 2
    assert "chip(s) 2 still bad after 2" in result.operator_message()


def test_retries_clamped_to_three(make_seq):
    seq = make_seq(broken={2})
    result = mr.recover(seq, 10)
    assert result.rounds == 3 and result.still_bad == [2]
    assert sum(1 for p, v in seq.writes if p == CFG) == 6
    assert_restored(seq)


def test_zero_retries_only_checks(make_seq):
    seq = make_seq(broken={2})
    result = mr.recover(seq, 0)
    assert result.still_bad == [2] and result.rounds == 0 and seq.writes == []


@pytest.mark.parametrize("active,quads,sma,why", [
    ((True, True, False, False), (False, False, False, False), (False, True, False, False), "FEBsQuads[0]=n"),
    ((True, True, False, False), (True, False, False, False), (True, True, False, False), "FEBsSMA[0]=y"),
    ((False, True, False, False), (True, False, False, False), (False, True, False, False), "FEBsActive[0]=n"),
    # FEB 3 has ASICMask 255: Active + Quads there would make MupixConfig configure it too
    ((True, True, False, True), (True, False, False, True), (False, True, False, False), "FEB(s) 3 also"),
])
def test_preflight_failure_writes_nothing(make_seq, active, quads, sma, why):
    seq = make_seq(broken={2}, febs_active=active, febs_quads=quads, febs_sma=sma)
    result = mr.recover(seq, 3)
    assert why in result.aborted_reason and not result.ok
    assert seq.writes == [] and seq.pcls_reads == 0 and result.exit_code == mr.EXIT_REFUSED


def test_stale_pcls_writes_nothing(make_seq):
    seq = make_seq(broken={2}, stale=True)
    result = mr.recover(seq, 3)
    assert result.stale and not result.ok and seq.writes == []
    assert "not updating" in result.operator_message()
    assert result.exit_code == mr.EXIT_NO_VERDICT


def test_unreadable_feb_writes_nothing(make_seq):
    seq = make_seq(broken={2}, unreadable=True)
    result = mr.recover(seq, 3)
    assert result.unreadable and not result.ok and seq.writes == [] and result.still_bad == []
    assert "not readable" in result.operator_message()
    assert result.exit_code == mr.EXIT_NO_VERDICT


def test_timeout_aborts_recovery(make_seq):
    seq = make_seq(broken={2}, config_hangs_on=1)
    result = mr.recover(seq, 3)
    assert result.aborted_reason and result.still_bad == [2] and result.rounds == 1
    assert_restored(seq)


def test_forced_chips_skip_first_check(make_seq):
    seq = make_seq(fix_after={2: 1})          # chip 2 looks fine: reset it anyway
    result = mr.recover(seq, 0, chips=[2])
    assert result.ok and result.rounds == 1
    assert seq.writes == recipe([2], 0x28)
    assert seq.pcls_reads == 2                # only the check after the reset


def test_forced_chips_never_spread_to_other_bad_chips(make_seq):
    seq = make_seq(broken={2, 6})             # neither recovers
    result = mr.recover(seq, 3, chips=[2])
    assert seq.writes == recipe([2], 0x28) * 3
    assert {v for p, v in seq.writes if p == ASIC0} == {0x04, 0x28}
    assert result.still_bad == [2] and result.rounds == 3
    assert any("6 also bad, left alone" in m for m in seq.msgs)


def test_forced_chips_respect_lvds_mask(make_seq):
    seq = make_seq(fix_after={2: 1}, lvds=0xFFFFFF & ~(0b111 << 18))   # chip 6 masked
    result = mr.recover(seq, 1, chips=[2, 6])
    assert seq.writes == recipe([2], 0x28) and result.ok
    assert any("chip(s) 6: links masked" in m for m in seq.msgs)
    seq = make_seq(lvds=0xFFFFFF & ~(0b111 << 18))
    result = mr.recover(seq, 1, chips=[6])
    assert seq.writes == [] and "none of the requested" in result.aborted_reason


# ---------------------------------------------------------------- the sequencer loop

class LoopDone(Exception):
    pass


class StopSeq(Exception):
    """Stands in for midas.sequencer.StopSequencerException, the class a script can import."""


# What the running sequencer actually raises on Stop: it runs as `python -m midas.sequencer`,
# so its class is __main__.StopSequencerException, same name, a different class object.
MainStopSequencerException = type("StopSequencerException", (Exception,), {"__module__": "__main__"})


class SeqFake(FakeSeq):
    """FakeSeq plus the SequenceClient calls sequencer_operator.py makes."""

    def __init__(self, params, runs=1, **kw):
        super().__init__(**kw)
        self.params, self.runs, self.events = params, runs, []

    def get_param(self, name):
        return self.params[name]

    def wait_odb(self, path, op, value):
        if sum(1 for e in self.events if e[0] == "start_run") >= self.runs:
            raise LoopDone()

    def trigger_internal_alarm(self, name, text, default_alarm_class="Alarm"):
        self.events.append(("alarm", text))

    def sequencer_msg(self, text, wait=False):
        self.events.append(("ok", text))

    def reset_alarm(self, name):
        pass

    def start_run(self):
        self.events.append(("start_run", len(self.writes)))

    def stop_run(self):
        pass


def run_sequence(monkeypatch, seq):
    midas_mod = types.ModuleType("midas")
    midas_mod.STATE_STOPPED, midas_mod.STATE_RUNNING = 1, 3
    seq_mod = types.ModuleType("midas.sequencer")
    seq_mod.SequenceClient, seq_mod.StopSequencerException = object, StopSeq
    midas_mod.sequencer = seq_mod
    iface_mod = types.ModuleType("pioneer.rundb.interface")

    class Interface:
        def __init__(self, **kw):
            pass

        def find_next_run_config(self):
            return {"pk": 1}

    iface_mod.interface = Interface
    loader_mod = types.ModuleType("pioneer.sequencer.config_loader")
    loader_mod.load_config = lambda s, cfg, sequential=False: s.events.append(("load", cfg["pk"]))
    for name, mod in [("midas", midas_mod), ("midas.sequencer", seq_mod),
                      ("pioneer.rundb.interface", iface_mod),
                      ("pioneer.sequencer.config_loader", loader_mod)]:
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(mr, "_monotonic", lambda: seq.t)
    ns = {"__name__": "sequencer_operator"}
    exec(compile(SEQUENCER.read_text(), str(SEQUENCER), "exec"), ns)
    with pytest.raises(LoopDone):
        ns["sequence"](seq)
    return ns


PARAMS = {"waitBeforeRun": False, "mupixRecovery": True, "mupixMaxRetries": 3}
PLL_PAUSE = "Please check the PLL Lock is ok and click ok"


def test_define_params_registers_recovery(monkeypatch):
    seq = SeqFake(PARAMS)
    reg = []
    seq.register_param = lambda name, comment, default, options=[]: reg.append((name, default))
    ns = run_sequence(monkeypatch, seq)
    ns["define_params"](seq)
    assert ("mupixRecovery", True) in reg and ("mupixMaxRetries", 3) in reg


def test_sequence_clean_no_writes(monkeypatch):
    seq = SeqFake(PARAMS)
    run_sequence(monkeypatch, seq)
    assert seq.writes == []
    assert seq.events == [("load", 1), ("alarm", PLL_PAUSE), ("ok", PLL_PAUSE), ("start_run", 0)]


def test_sequence_chip_fixed_in_one_round(monkeypatch):
    seq = SeqFake(PARAMS, broken={2}, fix_after={2: 1})
    run_sequence(monkeypatch, seq)
    assert seq.writes == recipe([2], 0x28)
    assert seq.events == [("load", 1), ("alarm", PLL_PAUSE), ("ok", PLL_PAUSE),
                          ("start_run", len(seq.writes))]


def test_sequence_bad_forever_alarms_then_runs(monkeypatch):
    seq = SeqFake(PARAMS, broken={2, 6})
    run_sequence(monkeypatch, seq)
    assert seq.writes == recipe([2, 6], 0x28) * 3
    kinds = [e[0] for e in seq.events]
    assert kinds == ["load", "alarm", "ok", "alarm", "ok", "start_run"]
    assert "chip(s) 2, 6 still bad after 3" in seq.events[1][1]
    assert seq.events[3][1] == PLL_PAUSE
    assert_restored(seq)


def test_sequence_recovery_off(monkeypatch):
    seq = SeqFake(dict(PARAMS, mupixRecovery=False), broken={2})
    run_sequence(monkeypatch, seq)
    assert seq.writes == [] and seq.pcls_reads == 0
    assert [e[0] for e in seq.events] == ["load", "alarm", "ok", "start_run"]


def test_sequence_unexpected_error_alarms_then_runs(monkeypatch):
    seq = SeqFake(PARAMS)
    del seq.odb[mr.LVDS_MASK_PATH]           # e.g. a key renamed by a frontend update
    run_sequence(monkeypatch, seq)
    assert seq.writes == []
    assert "MuPix PLL recovery failed" in seq.events[1][1]
    assert [e[0] for e in seq.events] == ["load", "alarm", "ok", "alarm", "ok", "start_run"]


def test_sequence_stop_is_not_swallowed(monkeypatch):
    def stop(seq, s):
        raise MainStopSequencerException()
    seq = SeqFake(PARAMS, broken={2}, wait_hook=stop)
    assert MainStopSequencerException is not StopSeq
    with pytest.raises(MainStopSequencerException):
        run_sequence(monkeypatch, seq)
    assert not any(e[0] == "start_run" for e in seq.events)


# ---------------------------------------------------------------- the command line

def run_cli(monkeypatch, argv, **kw):
    """main() against a FakeSeq standing in for midas.client.MidasClient."""
    client = FakeSeq(**kw)
    client.disconnect = lambda: None
    midas_mod = types.ModuleType("midas")
    client_mod = types.ModuleType("midas.client")
    client_mod.MidasClient = lambda name: client
    midas_mod.client = client_mod
    monkeypatch.setitem(sys.modules, "midas", midas_mod)
    monkeypatch.setitem(sys.modules, "midas.client", client_mod)
    monkeypatch.setattr(mr._ClientSeq, "wait_seconds", lambda self, s: client.wait_seconds(s))
    monkeypatch.setattr(mr, "_monotonic", lambda: client.t)
    return mr.main(argv), client


@pytest.mark.parametrize("kw,code", [
    ({}, mr.EXIT_OK),
    ({"broken": {2, 6}}, mr.EXIT_BAD),
    ({"stale": True}, mr.EXIT_NO_VERDICT),
    ({"unreadable": True}, mr.EXIT_NO_VERDICT),
    ({"febs_sma": (True, True, False, False)}, mr.EXIT_REFUSED),
])
def test_cli_check_exit_codes(monkeypatch, capsys, kw, code):
    rc, client = run_cli(monkeypatch, ["--check"], **kw)
    assert rc == code and client.writes == []


@pytest.mark.parametrize("state", [2, 3])
def test_cli_recover_refuses_unless_stopped(monkeypatch, capsys, state):
    rc, client = run_cli(monkeypatch, ["--recover"], broken={2}, run_state=state)
    assert rc == mr.EXIT_REFUSED and client.writes == []
    assert "not stopped" in capsys.readouterr().out
    rc, client = run_cli(monkeypatch, ["--recover", "--allow-running", "--retries", "1"],
                         broken={2}, fix_after={2: 1}, run_state=state)
    assert rc == mr.EXIT_OK and client.writes == recipe([2], 0x28)


def test_cli_recover_exit_codes(monkeypatch, capsys):
    rc, client = run_cli(monkeypatch, ["--recover", "--chips", "2"], broken={2, 6}, fix_after={2: 1})
    assert rc == mr.EXIT_OK and client.writes == recipe([2], 0x28)
    rc, client = run_cli(monkeypatch, ["--recover"], broken={2})
    assert rc == mr.EXIT_BAD and len(client.writes) == 3 * len(recipe([2], 0x28))
    rc, client = run_cli(monkeypatch, ["--recover"], broken={2}, config_hangs_on=1)
    assert rc == mr.EXIT_REFUSED


def test_cli_rejects_bad_arguments(monkeypatch):
    for argv in (["--recover", "--chips", "8"], ["--check", "--chips", "2"], []):
        with pytest.raises(SystemExit) as e:
            mr.main(argv)
        assert e.value.code == 2
