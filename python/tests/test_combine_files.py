"""combine_files: which proton-current normalisation a join uses.

The WaveDREAM source, rate x SMA live time = proton_current_counts /
proton_current_seconds x PIPSMSMACalibration/sma_live_seconds, when every run
being joined has the three histograms non-empty; else the SMA current pulses
(musip/current) when every run has those; else factor 1 with a warning. One
join never mixes the two. Runs on stand-in ROOT objects (tuning_fakes).
"""

import sys

import pytest

from tuning_fakes import FakeTH2, fake_root

WD = "histograms/PIWDScalerMonitor/proton_current_counts"
WD_S = "histograms/PIWDScalerMonitor/proton_current_seconds"
LIVE = "histograms/PIPSMSMACalibration/sma_live_seconds"
SMA = "histograms/musip/current"
XXP, YYP, XY = ("histograms/PIPSMMuPixMonitor/xxp_mt", "histograms/PIPSMMuPixMonitor/yyp_mt",
                "histograms/PIPSMMuPixMonitor/xy_mt")


class FakeHeader:
    def __bool__(self):
        return True

    def Clone(self):
        return self

    def MergeHeader(self, other):
        return True


def _maps(counts=10.0):
    return {XXP: FakeTH2.blob(64, 64, (-37, 37), (-950, 950), (0, 0), counts),
            YYP: FakeTH2.blob(64, 64, (-37, 37), (-950, 950), (0, 0), counts),
            XY: FakeTH2.blob(64, 64, (-37, 37), (-37, 37), (0, 0), counts)}


def _one(value):
    return FakeTH2([[value]], (0, 1), (0, 1))


def _run(prefix, wd=None, sma=None, subruns=("0", "1"), wd_subruns=None, live_subruns=None):
    """Subrun files of one run, each with 10 counts per map and, per subrun,
    `wd` = (WD scaler counts, WD seconds, SMA live seconds), any of them None
    for an absent histogram, and `sma` current pulses (None: absent).
    `wd_subruns` / `live_subruns` limit the WD / live histograms to those
    subruns."""
    files = {}
    for sub in subruns:
        objects = dict(_maps(), beamline=FakeHeader())
        if wd is not None:
            counts, seconds, live = wd
            if wd_subruns is None or sub in wd_subruns:
                if counts is not None:
                    objects[WD] = _one(counts)
                if seconds is not None:
                    objects[WD_S] = _one(seconds)
            if live is not None and (live_subruns is None or sub in live_subruns):
                objects[LIVE] = _one(live)
        if sma is not None:
            objects[SMA] = _one(sma)
        files[prefix + sub] = objects
    return files


def _combine(monkeypatch, *runs):
    files = {}
    for r in runs:
        files.update(r)
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(files))
    from pioneer.nearline import combine_files
    prefixes = sorted({name[:-1] for name in files})
    return combine_files.combine_runs([[p + "0", p + "1"] for p in prefixes])


def test_wd_only(monkeypatch, capsys):
    # run a: 1000 counts / 10 s x 0.1 s live per subrun -> 2000 / 20 x 0.2 = 20
    # run b: 2000 / 10 x 0.1 per subrun -> 40
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, 10.0, 0.1)),
                               _run("b", wd=(2000.0, 10.0, 0.1)))
    # each run 20 map counts: 20 / 20 + 20 / 40
    assert histos[XXP].Integral() == pytest.approx(1.0 + 0.5)
    assert WD in histos and WD_S in histos and LIVE in histos and SMA not in histos
    out = capsys.readouterr().out
    assert "warning" not in out and "WaveDREAM scaler" in out and WD in out and LIVE in out
    # the three numbers of each run and its factor
    assert "a0 2000 counts / 20 s x 0.2 s live = 20" in out
    assert "b0 4000 counts / 20 s x 0.2 s live = 40" in out


def test_wd_is_rate_times_live_time(monkeypatch):
    # The same beam with half the SMA live time gives half the factor: the
    # WaveDREAM seconds do not enter, only the rate does.
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, 5.0, 0.2)),
                               _run("b", wd=(5000.0, 25.0, 0.1)))
    # a: 200 Hz x 0.4 s = 80, b: 200 Hz x 0.2 s = 40
    assert histos[XXP].Integral() == pytest.approx(20 / 80 + 20 / 40)


def test_sma_only(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a", sma=5.0), _run("b", sma=10.0))
    assert histos[XXP].Integral() == pytest.approx(3.0)
    assert SMA in histos and WD not in histos and LIVE not in histos
    out = capsys.readouterr().out
    assert "warning" not in out and "SMA proton-current pulses" in out and SMA in out


def test_both_everywhere_prefers_wd(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, 10.0, 0.1), sma=1000.0),
                               _run("b", wd=(2000.0, 10.0, 0.1), sma=1000.0))
    assert histos[XXP].Integral() == pytest.approx(1.5)
    assert WD in histos and SMA not in histos


def test_old_files_without_live_time_use_the_sma(monkeypatch, capsys):
    # files written before sma_live_seconds existed: the WaveDREAM counts alone
    # are not a source, so the SMA pulses normalise as before
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, 10.0, None), sma=5.0),
                               _run("b", wd=(2000.0, 10.0, None), sma=10.0))
    assert histos[XXP].Integral() == pytest.approx(3.0)
    assert SMA in histos and WD not in histos and WD_S not in histos
    assert "SMA proton-current pulses" in capsys.readouterr().out


def test_wd_without_seconds_is_no_source(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, None, 0.1)),
                               _run("b", wd=(2000.0, 10.0, 0.1)))
    assert histos[XXP].Integral() == pytest.approx(40.0)
    out = capsys.readouterr().out
    assert out.count("warning") == 1 and "not normalised" in out


def test_mixed_falls_back_to_the_source_every_run_has(monkeypatch, capsys):
    # run a has both, run b only the SMA pulses: the SMA for both, never one each
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, 10.0, 0.1), sma=50.0),
                               _run("b", sma=100.0))
    # run a: 20 / 100, run b: 20 / 200 -- not a's WD factor
    assert histos[XXP].Integral() == pytest.approx(0.2 + 0.1)
    assert SMA in histos and WD not in histos and LIVE not in histos
    assert "SMA proton-current pulses" in capsys.readouterr().out


def test_mixed_without_a_common_source_is_not_normalised(monkeypatch, capsys):
    # run a has only the WD source, run b only the SMA pulses
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, 10.0, 0.1)), _run("b", sma=100.0))
    assert histos[XXP].Integral() == pytest.approx(40.0)
    assert WD not in histos and SMA not in histos and LIVE not in histos
    out = capsys.readouterr().out
    assert out.count("warning") == 1 and "not normalised" in out
    assert "b0" in out and "a0" in out


def test_an_empty_wd_histogram_does_not_count(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, 10.0, 0.1), sma=50.0),
                               _run("b", wd=(0.0, 10.0, 0.1), sma=100.0))
    assert histos[XXP].Integral() == pytest.approx(0.3)
    assert SMA in histos and WD not in histos


def test_a_subrun_without_the_wd_histogram_drops_it_for_the_run(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, 10.0, 0.1), sma=50.0, wd_subruns=("0",)),
                               _run("b", wd=(2000.0, 10.0, 0.1), sma=100.0))
    assert histos[XXP].Integral() == pytest.approx(0.3)
    assert SMA in histos and WD not in histos
    out = capsys.readouterr().out
    assert "a1" in out and "SMA proton-current pulses" in out


def test_a_first_subrun_without_the_live_time_is_noted(monkeypatch, capsys):
    # absent in the first subrun, present in the second: the run cannot use it,
    # and the merge says so (as it does for the reverse case)
    headers, histos = _combine(monkeypatch, _run("a", wd=(1000.0, 10.0, 0.1), sma=50.0, live_subruns=("1",)),
                               _run("b", wd=(2000.0, 10.0, 0.1), sma=100.0))
    assert histos[XXP].Integral() == pytest.approx(0.3)
    out = capsys.readouterr().out
    assert f"note: {LIVE} not present in a0" in out and "SMA proton-current pulses" in out


def test_none(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a"), _run("b"))
    assert histos[XXP].Integral() == pytest.approx(40.0)
    assert WD not in histos and SMA not in histos
    out = capsys.readouterr().out
    assert out.count("warning") == 1 and "not normalised" in out


def test_a_single_run_merge_uses_the_wd_source_and_drops_the_other(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(_run("a", wd=(1000.0, 10.0, 0.1), sma=1000.0)))
    from pioneer.nearline import combine_files
    headers, histos = combine_files.merge_sub_runs(["a0", "a1"])
    # factor 2000 / 20 x 0.2 = 20
    assert histos[XXP].scaled == pytest.approx(1 / 20)
    # as combine_runs: only the source used stays
    assert WD in histos and LIVE in histos and SMA not in histos
    assert "WaveDREAM scaler" in capsys.readouterr().out


def test_a_single_run_merge_without_a_source_drops_every_current(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(_run("a", wd=(0.0, 10.0, 0.1), sma=0.0)))
    from pioneer.nearline import combine_files
    headers, histos = combine_files.merge_sub_runs(["a0", "a1"])
    assert histos[XXP].scaled is None
    assert not any(p in histos for p in (WD, WD_S, LIVE, SMA))
