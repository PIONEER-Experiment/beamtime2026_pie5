#!/usr/bin/env python3
"""
Find MuPix chips whose PLL has dropped out and reset their PLL, before a run starts.

    python3 -m pioneer.sequencer.mupix_recovery --check                 # read-only report
    python3 -m pioneer.sequencer.mupix_recovery --recover               # detect, reset, verify
    python3 -m pioneer.sequencer.mupix_recovery --recover --chips 2,6   # reset only these, verify

The sequencer (sequencer/sequencer_operator.py) calls recover() after the run config has
loaded; this command line runs the same functions by hand.

Check: two snapshots of /Equipment/Quads/Variables/PCLS, CHECK_INTERVAL_S apart. A chip is bad
when one of its 3 LVDS links has lost READY (status bit 30) while the FPGA receiver PLL
(bit 31) is locked, or gains more than ERR_RATE_LIMIT 8b10b errors/s. Only FEB 0 links 0-23
are read: they are the 8 MuPix chips x 3 links. FEB 1 is the SMA board and FEB 0 links 24-35
are unused; neither is ever looked at. A status word of 0 or an unlocked FPGA receiver PLL
means the FEB itself is not readable, and an unchanged PCLS means it is not updating: then
there is no verdict and nothing is written.

Reset (the Quads web page's "Reset PLL?" + Configure, for the bad chips only):
ASICMask[0] = bad chips, EnPLL[c] = 1, wait ENPLL_SETTLE_S, MupixConfig, wait CONFIG_SETTLE_S,
EnPLL[c] = 0, wait ENPLL_SETTLE_S, MupixConfig, wait CONFIG_SETTLE_S. ASICMask[0] and EnPLL are put back afterwards on every path (success, timeout,
error, sequence stopped), and a MupixConfig of ours still pending is cancelled. Nothing is
written unless the run is stopped.

Exit codes: 0 all checked chips OK, 1 chip(s) bad, 3 no verdict (PCLS stale or FEB 0
unreadable), 4 refused or aborted (FEB layout, run not stopped, frontend did not answer,
no requested chip enabled). 2 is argparse's usage error.

The detection idea (growth of the 8b10b counters in PCLS) comes from Thomas's draft
pinky:/home/pinky/online/userfiles/sequencer/runStart_thomas.py. That draft assumes one header
per 48 words, which is right for FEB 0 only; here PCLS is read as 4 FEB blocks of
2 header + 36 links x 4 words.
"""
import argparse
import sys
import time
from dataclasses import dataclass, field

MUPIX_FEB = 0
MUPIX_LINKS = range(0, 24)
MUPIX_CHIPS = range(0, 8)
LINKS_PER_CHIP = 3
PCLS_FEB_BLOCK = 2 + 36 * 4        # header (FEB, n links) + 36 links x 4 words
WORDS_PER_LINK = 4                 # Status, Disparity errors, 8b10b errors, Hits
READY_BIT = 1 << 30
FPGA_PLL_BIT = 1 << 31             # FPGA receiver PLL locked: stays 1 on a chip that lost its PLL
COUNTER_RESET = 2**31              # an 8b10b delta above this (mod 2**32) is a counter reset
ERR_RATE_LIMIT = 1e7               # 8b10b errors/s per link; healthy < 1e3, noisy-but-fine ~1e5, broken ~1e8
CHECK_INTERVAL_S = 3
CONFIG_TIMEOUT_S = 15              # MupixConfig is only handled between the (slow) periodic events
ENPLL_SETTLE_S = 1                 # lets the frontend pick up the EnPLL writes before the configure
CONFIG_SETTLE_S = 1                # chip settles after a configure; the Quads page's ResetPLL waits 1 s after the first
MAX_RETRIES_CAP = 3
POLL_S = 0.3
STATE_STOPPED = 1                  # midas.STATE_STOPPED, without importing midas
STATE_NAMES = {1: "stopped", 2: "paused", 3: "running"}

EXIT_OK, EXIT_BAD, EXIT_NO_VERDICT, EXIT_REFUSED = 0, 1, 3, 4

PCLS_PATH = "/Equipment/Quads/Variables/PCLS"
LINKS_DIR = "/Equipment/Quads/Settings/DAQ/Links"
ASIC_MASK_PATH = LINKS_DIR + "/ASICMask"                 # UINT16[4], bit = chip on that FEB
LVDS_MASK_PATH = LINKS_DIR + "/LVDSLinkMask"             # UINT64[4], bit = link enabled
FEBS_ACTIVE_PATH = LINKS_DIR + "/FEBsActive"
FEBS_QUADS_PATH = LINKS_DIR + "/FEBsQuads"
FEBS_SMA_PATH = LINKS_DIR + "/FEBsSMA"
ENPLL_PATH = "/Equipment/Quads/Settings/Config/CONFDACS/EnPLL"   # UINT32[32], idx = feb*8 + chip
MUPIX_CONFIG_PATH = "/Equipment/Quads/Settings/DAQ/Commands/MupixConfig"
RUN_STATE_PATH = "/Runinfo/State"

PREFIX = "MuPix PLL recovery: "

# The only ODB keys this module may write.
_ALLOWED_WRITES = {f"{ASIC_MASK_PATH}[{MUPIX_FEB}]", MUPIX_CONFIG_PATH} | \
                  {f"{ENPLL_PATH}[{MUPIX_FEB * 8 + c}]" for c in MUPIX_CHIPS}

_monotonic = time.monotonic   # replaced by the tests


class RecoveryAborted(RuntimeError):
    """The recovery stopped before or during its writes; the message says why."""


class MupixConfigTimeout(RecoveryAborted):
    """MupixConfig did not go back to false within CONFIG_TIMEOUT_S."""


class RunNotStopped(RecoveryAborted):
    """The run is not stopped, so nothing may be written."""


# ---------------------------------------------------------------- pure functions

def as_int(x):
    """ODB integer as int: accepts ints, bools and strings like '0x0028' (MIDAS JSON) or '40'."""
    if isinstance(x, str):
        return int(x.strip(), 0)
    return int(x)


def as_bool(x):
    if isinstance(x, str):
        return x.strip().lower() in ("y", "yes", "true", "1")
    return bool(x)


def _as_list(x):
    return list(x) if isinstance(x, (list, tuple)) else [x]


def parse_pcls(values):
    """{link: (status, disparity errors, 8b10b errors, hits)} for FEB 0 links 0-23 only."""
    values = _as_list(values)
    base = MUPIX_FEB * PCLS_FEB_BLOCK + 2
    need = base + WORDS_PER_LINK * len(MUPIX_LINKS)
    if len(values) < need:
        raise ValueError(f"PCLS has {len(values)} words, need at least {need}")
    out = {}
    for link in MUPIX_LINKS:
        o = base + WORDS_PER_LINK * link
        out[link] = tuple(as_int(v) for v in values[o:o + WORDS_PER_LINK])
    return out


def chip_links(chip):
    return [LINKS_PER_CHIP * chip + i for i in range(LINKS_PER_CHIP)]


def enabled_chips(lvds_mask):
    """Chips 0-7 whose 3 links are all enabled in LVDSLinkMask[0]."""
    m = as_int(lvds_mask)
    return [c for c in MUPIX_CHIPS if all((m >> l) & 1 for l in chip_links(c))]


@dataclass
class ChipStatus:
    chip: int
    ready: tuple            # READY bit per link
    rates: tuple            # 8b10b errors/s per link, None after a counter reset
    reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def bad(self):
        return bool(self.reasons)

    def describe(self):
        links = chip_links(self.chip)
        return (f"chip {self.chip} (links {links[0]}-{links[-1]}): READY "
                + "/".join(str(r) for r in self.ready) + ", 8b10b "
                + "/".join("reset" if r is None else f"{r:.2g}" for r in self.rates) + " /s"
                + (" -> " + "; ".join(self.reasons) if self.reasons else "")
                + (" (" + "; ".join(self.notes) + ")" if self.notes else ""))


@dataclass
class NoVerdict:
    kind: str               # "stale" or "unreadable"
    text: str


def _links_text(links):
    return ",".join(str(l) for l in links)


def classify(snap_a, snap_b, dt, chips, key_updated=None):
    """Verdict for each chip in `chips` from two parse_pcls() snapshots taken dt seconds apart.

    Returns (statuses, no_verdict). no_verdict is None, or a NoVerdict when no chip verdict
    can be trusted:
      unreadable  a status word of 0, or the FPGA receiver PLL (bit 31) unlocked, on a
                  checked link: the FEB is not read out; resetting a chip would not help
      stale       the words did not change and the ODB key was not seen to be rewritten
                  (key_updated not True). A quiet, healthy detector (no hits, no errors)
                  gives identical words too, which is why the key's write time counts.
    """
    dt = max(float(dt), 1e-3)
    links = [l for c in chips for l in chip_links(c)]
    no_verdict = None
    zero = [l for l in links if snap_a[l][0] == 0 or snap_b[l][0] == 0]
    unlocked = [l for l in links if l not in zero and not snap_b[l][0] & FPGA_PLL_BIT]
    if zero:
        no_verdict = NoVerdict("unreadable", f"FEB {MUPIX_FEB} not readable: status word 0 on "
                                             f"link(s) {_links_text(zero)}")
    elif unlocked:
        no_verdict = NoVerdict("unreadable", f"FPGA receiver PLL unlocked on link(s) "
                                             f"{_links_text(unlocked)}: FEB {MUPIX_FEB} problem, "
                                             "not a chip PLL")
    elif snap_a == snap_b and not key_updated:
        no_verdict = NoVerdict("stale", f"PCLS did not change in {CHECK_INTERVAL_S} s and was not "
                                        "rewritten: not updating")
    statuses = {}
    for c in chips:
        cl = chip_links(c)
        ready = tuple(1 if snap_b[l][0] & READY_BIT else 0 for l in cl)
        rates = []
        for l in cl:
            delta = (snap_b[l][2] - snap_a[l][2]) % 2**32
            rates.append(None if delta > COUNTER_RESET else delta / dt)
        lost = [l for l, r in zip(cl, ready) if not r and snap_b[l][0] & FPGA_PLL_BIT]
        noisy = [l for l, r in zip(cl, rates) if r is not None and r > ERR_RATE_LIMIT]
        reset = [l for l, r in zip(cl, rates) if r is None]
        reasons, notes = [], []
        if lost:
            reasons.append("not READY on link " + _links_text(lost))
        if noisy:
            reasons.append(f"8b10b > {ERR_RATE_LIMIT:.0e}/s on link " + _links_text(noisy))
        if reset:
            notes.append("8b10b counter went down on link " + _links_text(reset) + ", rate unknown")
        statuses[c] = ChipStatus(c, ready, tuple(rates), reasons, notes)
    return statuses, no_verdict


# ---------------------------------------------------------------- ODB access

def _log(seq, text, result=None, is_error=False):
    line = PREFIX + text
    if result is not None:
        result.log.append(line)
    seq.msg(line, is_error=is_error)


def _guarded_set(seq, path, value):
    """The only way this module writes to the ODB: ASICMask[0], EnPLL[0..7], MupixConfig."""
    if path not in _ALLOWED_WRITES:
        raise ValueError(f"mupix_recovery refuses to write {path}")
    seq.odb_set(path, value, create_if_needed=False, resize_arrays=False)


def check_layout(seq):
    """None if the ODB agrees that FEB 0 is the only active Quads board and not the SMA board,
    else why not. MupixConfig configures every active Quads FEB, so another one would be
    touched too."""
    active = [as_bool(x) for x in _as_list(seq.odb_get(FEBS_ACTIVE_PATH))]
    quads = [as_bool(x) for x in _as_list(seq.odb_get(FEBS_QUADS_PATH))]
    sma = [as_bool(x) for x in _as_list(seq.odb_get(FEBS_SMA_PATH))]
    f = MUPIX_FEB
    problems = []
    if not active[f]:
        problems.append(f"FEBsActive[{f}]=n")
    if not quads[f]:
        problems.append(f"FEBsQuads[{f}]=n")
    if sma[f]:
        problems.append(f"FEBsSMA[{f}]=y")
    others = [i for i in range(min(len(active), len(quads))) if i != f and active[i] and quads[i]]
    if others:
        problems.append(f"FEB(s) {', '.join(map(str, others))} also Active and Quads")
    if not problems:
        return None
    return "FEB layout not as expected (" + "; ".join(problems) + "); nothing checked or written"


def _require_stopped(seq, allow_running=False):
    state = as_int(seq.odb_get(RUN_STATE_PATH))
    if state != STATE_STOPPED and not allow_running:
        raise RunNotStopped(f"run is {STATE_NAMES.get(state, state)}, not stopped; nothing written")


def _read_pcls(seq):
    """(values, last_written or None)."""
    raw = seq.odb_get(PCLS_PATH, include_key_metadata=True)
    if isinstance(raw, dict):
        return raw["PCLS"], raw.get("PCLS/key", {}).get("last_written")
    return raw, None


def find_bad_chips(seq):
    """Two PCLS snapshots CHECK_INTERVAL_S apart. Returns (bad chips, NoVerdict or None,
    {chip: ChipStatus}) for the chips enabled in LVDSLinkMask[0]. No chip is bad when there
    is no verdict."""
    chips = enabled_chips(_as_list(seq.odb_get(LVDS_MASK_PATH))[MUPIX_FEB])
    raw_a, lw_a = _read_pcls(seq)
    t0 = _monotonic()
    seq.wait_seconds(CHECK_INTERVAL_S)
    raw_b, lw_b = _read_pcls(seq)
    dt = _monotonic() - t0
    updated = lw_a is not None and lw_b is not None and lw_b != lw_a
    statuses, no_verdict = classify(parse_pcls(raw_a), parse_pcls(raw_b), dt, chips,
                                    key_updated=updated)
    bad = [] if no_verdict else [c for c in chips if statuses[c].bad]
    return bad, no_verdict, statuses


def _wait_config_done(seq, message):
    t0 = _monotonic()
    while as_bool(seq.odb_get(MUPIX_CONFIG_PATH)):
        if _monotonic() - t0 > CONFIG_TIMEOUT_S:
            raise MupixConfigTimeout(message)
        seq.wait_seconds(POLL_S)


def reset_pll(seq, chips, result=None, allow_running=False):
    """The ResetPLL recipe on FEB 0 for `chips`, all in one pass. ASICMask[0] and EnPLL of
    those chips are put back on every path, and a MupixConfig of ours still pending is
    cancelled. Raises RecoveryAborted (MupixConfigTimeout, RunNotStopped)."""
    chips = sorted(set(chips))
    for c in chips:
        if not isinstance(c, int) or c not in MUPIX_CHIPS:
            raise ValueError(f"chip {c!r} is not a MuPix chip (0-7)")
    if not chips:
        return
    # A MupixConfig already pending (shifter, frontend busy) must finish first; no writes yet.
    _wait_config_done(seq, f"MupixConfig was already pending before the recovery started and "
                           f"did not clear in {CONFIG_TIMEOUT_S} s; nothing written")
    _require_stopped(seq, allow_running)

    saved_mask = as_int(_as_list(seq.odb_get(ASIC_MASK_PATH))[MUPIX_FEB])
    enpll = _as_list(seq.odb_get(ENPLL_PATH))
    idx = {c: MUPIX_FEB * 8 + c for c in chips}
    for c in chips:
        if as_int(enpll[idx[c]]) != 0:
            _log(seq, f"chip {c}: EnPLL was {as_int(enpll[idx[c]])} before the reset, "
                      "it is left at 0 afterwards", result)
    mask = 0
    for c in chips:
        mask |= 1 << c
    _log(seq, f"starting ODB writes for chip(s) {_chip_list(chips)}: ASICMask[0] "
              f"0x{saved_mask:02x} -> 0x{mask:02x}, EnPLL 1 then 0, waits {ENPLL_SETTLE_S} s after "
              f"EnPLL and {CONFIG_SETTLE_S} s after each MupixConfig", result)

    timeout_msg = f"MupixConfig still true after {CONFIG_TIMEOUT_S} s, the Quads frontend did not answer"
    attempted = sent = answered = 0     # MupixConfig writes started / done / answered by the frontend
    try:
        _guarded_set(seq, f"{ASIC_MASK_PATH}[{MUPIX_FEB}]", mask)
        for c in chips:
            _guarded_set(seq, f"{ENPLL_PATH}[{idx[c]}]", 1)
        seq.wait_seconds(ENPLL_SETTLE_S)
        attempted += 1
        _guarded_set(seq, MUPIX_CONFIG_PATH, True)
        sent += 1
        _wait_config_done(seq, timeout_msg)
        answered += 1
        seq.wait_seconds(CONFIG_SETTLE_S)          # the PLL pulse: EnPLL = 1 is now on the chip
        for c in chips:
            _guarded_set(seq, f"{ENPLL_PATH}[{idx[c]}]", 0)
        seq.wait_seconds(ENPLL_SETTLE_S)
        attempted += 1
        _guarded_set(seq, MUPIX_CONFIG_PATH, True)
        sent += 1
        _wait_config_done(seq, timeout_msg)
        answered += 1
        seq.wait_seconds(CONFIG_SETTLE_S)
    finally:
        # The MIDAS sequencer stops a script by raising StopSequencerException from its trace
        # function on the next Python call once Stop is pressed. The thread's trace function
        # (sys.settrace) is switched off while restoring, so a Stop pressed now cannot cut the
        # restore short; the stop takes effect right after.
        tracer = sys.gettrace()
        sys.settrace(None)
        try:
            failed = []
            if attempted > answered:
                # Ours, not answered yet: the frontend would run it late with whatever
                # ASICMask/EnPLL are then in the ODB. Cancel it before restoring the mask.
                try:
                    if as_bool(seq.odb_get(MUPIX_CONFIG_PATH)):
                        _guarded_set(seq, MUPIX_CONFIG_PATH, False)
                        _log(seq, "our MupixConfig was still pending: set back to false so the "
                                  "frontend does not run it late", result, is_error=True)
                except Exception as e:
                    failed.append(f"{MUPIX_CONFIG_PATH} ({e})")
            for path, value in [(f"{ENPLL_PATH}[{idx[c]}]", 0) for c in chips] + \
                               [(f"{ASIC_MASK_PATH}[{MUPIX_FEB}]", saved_mask)]:
                try:
                    _guarded_set(seq, path, value)
                except Exception as e:   # keep going: every key gets its chance to be restored
                    failed.append(f"{path} ({e})")
            if failed:
                _log(seq, "could not restore " + ", ".join(failed), result, is_error=True)
            else:
                _log(seq, f"restored ASICMask[0] = 0x{saved_mask:02x}, EnPLL = 0 for chip(s) "
                          f"{_chip_list(chips)}", result)
            if sent and answered < 2:          # EnPLL = 0 never reached the chip
                _log(seq, f"interrupted after a MupixConfig was sent: chip(s) {_chip_list(chips)} "
                          "may still hold EnPLL = 1 on the chip until the next MupixConfig "
                          "(EnPLL is 0 in the ODB)", result, is_error=True)
        finally:
            sys.settrace(tracer)


# ---------------------------------------------------------------- the retry loop

@dataclass
class RecoveryResult:
    still_bad: list = field(default_factory=list)
    rounds: int = 0
    no_verdict: NoVerdict = None
    aborted_reason: str = ""
    log: list = field(default_factory=list)

    @property
    def stale(self):
        return bool(self.no_verdict) and self.no_verdict.kind == "stale"

    @property
    def unreadable(self):
        return bool(self.no_verdict) and self.no_verdict.kind == "unreadable"

    @property
    def ok(self):
        return not (self.still_bad or self.no_verdict or self.aborted_reason)

    @property
    def exit_code(self):
        if self.aborted_reason:
            return EXIT_REFUSED
        if self.no_verdict:
            return EXIT_NO_VERDICT
        return EXIT_BAD if self.still_bad else EXIT_OK

    def operator_message(self):
        if self.aborted_reason:
            what = f"MuPix PLL recovery aborted: {self.aborted_reason}."
        elif self.no_verdict:
            what = f"MuPix PLL check: {self.no_verdict.text}; chip state unknown, nothing written."
        else:
            what = (f"MuPix chip(s) {_chip_list(self.still_bad)} still bad after "
                    f"{self.rounds} automatic PLL reset round(s).")
        return what + " Fix by hand (Quads page, Reset PLL? + Configure) or accept it, then press OK to continue."


def _chip_list(chips):
    return ", ".join(str(c) for c in chips)


def _report(seq, statuses, result):
    chips = sorted(statuses)
    bad = [c for c in chips if statuses[c].bad]
    for c in chips:
        if statuses[c].bad or statuses[c].notes:
            _log(seq, statuses[c].describe(), result)
    if not bad:
        rates = [(r, c) for c in chips for r in statuses[c].rates if r is not None]
        worst = max(rates, default=(0.0, None))
        _log(seq, f"all {len(chips)} enabled chip(s) OK (no PLL loss, highest 8b10b "
                  f"rate {worst[0]:.2g}/s" + (f" on chip {worst[1]})" if worst[1] is not None else ")"),
             result)


def recover(seq, max_retries=MAX_RETRIES_CAP, chips=None, allow_running=False):
    """Check, then up to max_retries (at most MAX_RETRIES_CAP) rounds of reset + re-check on the
    chips that are still bad. With `chips`, only those chips are ever reset (the ones enabled
    in LVDSLinkMask[0]), the first time without a check first. Writes nothing when the FEB
    layout is unexpected, there is no verdict, or the run is not stopped."""
    result = RecoveryResult()
    retries = max(0, min(int(max_retries), MAX_RETRIES_CAP))
    reason = check_layout(seq)
    if reason:
        result.aborted_reason = reason
        _log(seq, reason, result, is_error=True)
        return result

    requested = None
    if chips is not None:
        for c in chips:
            if not isinstance(c, int) or c not in MUPIX_CHIPS:
                raise ValueError(f"chip {c!r} is not a MuPix chip (0-7)")
        enabled = enabled_chips(_as_list(seq.odb_get(LVDS_MASK_PATH))[MUPIX_FEB])
        skipped = sorted(c for c in set(chips) if c not in enabled)
        if skipped:
            _log(seq, f"chip(s) {_chip_list(skipped)}: links masked in LVDSLinkMask[0], skipped",
                 result, is_error=True)
        requested = set(chips) - set(skipped)
        bad = sorted(requested)
        if not bad:
            result.aborted_reason = "none of the requested chips is enabled in LVDSLinkMask[0]; nothing written"
            _log(seq, result.aborted_reason, result, is_error=True)
            return result
        retries = max(retries, 1)
        _log(seq, f"chip(s) {_chip_list(bad)} requested by hand, no check before the first reset",
             result)
    else:
        bad, no_verdict, statuses = find_bad_chips(seq)
        if no_verdict:
            result.no_verdict = no_verdict
            _log(seq, f"{no_verdict.text}; chip state unknown, nothing written", result, is_error=True)
            return result
        _report(seq, statuses, result)

    for rnd in range(1, retries + 1):
        if not bad:
            break
        result.rounds = rnd
        _log(seq, f"round {rnd}/{retries}: resetting PLL of chip(s) {_chip_list(bad)}", result)
        try:
            _require_stopped(seq, allow_running)
            reset_pll(seq, bad, result, allow_running=allow_running)
        except RecoveryAborted as e:
            result.aborted_reason = str(e)
            result.still_bad = bad
            _log(seq, f"round {rnd}: {e}; giving up", result, is_error=True)
            return result
        seq.wait_seconds(CHECK_INTERVAL_S)   # let the links realign before judging them
        prev = bad
        bad, no_verdict, statuses = find_bad_chips(seq)
        if no_verdict:
            result.no_verdict = no_verdict
            result.still_bad = prev
            _log(seq, f"round {rnd}: {no_verdict.text} after the reset; chip state unknown",
                 result, is_error=True)
            return result
        _report(seq, statuses, result)
        if requested is not None:
            others = [c for c in bad if c not in requested]
            if others:
                _log(seq, f"chip(s) {_chip_list(others)} also bad, left alone (not requested)", result)
            bad = [c for c in bad if c in requested]

    result.still_bad = bad
    if bad:
        _log(seq, f"chip(s) {_chip_list(bad)} still bad after {result.rounds} round(s)", result,
             is_error=True)
    elif result.rounds:
        _log(seq, f"all chips OK after {result.rounds} round(s)", result)
    return result


# ---------------------------------------------------------------- command line

class _ClientSeq:
    """MidasClient with the four calls the functions above use."""

    def __init__(self, client):
        self.client = client

    def odb_get(self, path, **kw):
        return self.client.odb_get(path, **kw)

    def odb_set(self, path, value, **kw):
        self.client.odb_set(path, value, **kw)

    def msg(self, text, is_error=False):
        print(text)
        self.client.msg(text, is_error=is_error)

    def wait_seconds(self, s):
        time.sleep(s)


def _print_check(seq):
    reason = check_layout(seq)
    if reason:
        print(reason)
        return EXIT_REFUSED
    lvds = as_int(_as_list(seq.odb_get(LVDS_MASK_PATH))[MUPIX_FEB])
    print(f"Reading {PCLS_PATH} twice, {CHECK_INTERVAL_S} s apart (LVDSLinkMask[0] = 0x{lvds:x}) ...")
    bad, no_verdict, statuses = find_bad_chips(seq)
    for c in MUPIX_CHIPS:
        if c in statuses:
            print(("BAD  " if statuses[c].bad else "ok   ") + statuses[c].describe())
        else:
            print(f"-    chip {c}: links masked in LVDSLinkMask[0], not checked")
    if no_verdict:
        print(f"No verdict: {no_verdict.text}.")
        return EXIT_NO_VERDICT
    print(f"Bad chip(s): {_chip_list(bad)}" if bad else "All checked chips OK.")
    return EXIT_BAD if bad else EXIT_OK


def _parse_chips(text):
    chips = sorted({int(x) for x in text.split(",") if x.strip()})
    for c in chips:
        if c not in MUPIX_CHIPS:
            raise argparse.ArgumentTypeError(f"chip {c} is not a MuPix chip (0-7)")
    return chips


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report every chip, write nothing")
    mode.add_argument("--recover", action="store_true", help="reset the PLL of bad chips and verify")
    ap.add_argument("--retries", type=int, default=MAX_RETRIES_CAP,
                    help=f"reset rounds (default and maximum {MAX_RETRIES_CAP})")
    ap.add_argument("--chips", type=_parse_chips,
                    help="comma-separated chips 0-7: reset only these (the first time without a check), e.g. 2,6")
    ap.add_argument("--allow-running", action="store_true",
                    help="allow --recover while the run is not stopped (running or paused)")
    args = ap.parse_args(argv)
    if args.chips is not None and not args.recover:
        ap.error("--chips needs --recover")

    import midas.client
    client = midas.client.MidasClient("mupix_recovery")
    try:
        seq = _ClientSeq(client)
        if args.check:
            return _print_check(seq)
        state = as_int(seq.odb_get(RUN_STATE_PATH))
        if state != STATE_STOPPED and not args.allow_running:
            print(f"The run is {STATE_NAMES.get(state, state)}, not stopped. Stop it first, or pass "
                  "--allow-running. Nothing written.")
            return EXIT_REFUSED
        result = recover(seq, args.retries, chips=args.chips, allow_running=args.allow_running)
        print("Done: all checked chips OK." if result.ok else result.operator_message())
        return result.exit_code
    finally:
        client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
