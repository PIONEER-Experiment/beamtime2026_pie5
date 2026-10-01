"""combine_files: which proton-current source normalises a join.

The WaveDREAM scaler counts (PIWDScalerMonitor/proton_current_counts) when
every run being joined has them non-empty, else the SMA current pulses
(musip/current) when every run has those, else factor 1 with a warning. One
join never mixes the two. Runs on stand-in ROOT objects (tuning_fakes).
"""

import sys

import pytest

from tuning_fakes import FakeTH2, fake_root

WD = "histograms/PIWDScalerMonitor/proton_current_counts"
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


def _run(prefix, wd=None, sma=None, subruns=("0", "1"), wd_subruns=None):
    """Subrun files of one run, each with 10 counts per map and, per subrun,
    `wd` scaler counts and `sma` current pulses (None: histogram absent).
    `wd_subruns` limits the WD histogram to those subruns."""
    files = {}
    for sub in subruns:
        objects = dict(_maps(), beamline=FakeHeader())
        if wd is not None and (wd_subruns is None or sub in wd_subruns):
            objects[WD] = FakeTH2([[wd]], (0, 1), (0, 1))
        if sma is not None:
            objects[SMA] = FakeTH2([[sma]], (0, 1), (0, 1))
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
    headers, histos = _combine(monkeypatch, _run("a", wd=5.0), _run("b", wd=10.0))
    # run a: 20 counts / 10 scaler counts, run b: 20 / 20
    assert histos[XXP].Integral() == pytest.approx(2.0 + 1.0)
    assert WD in histos and SMA not in histos
    out = capsys.readouterr().out
    assert "warning" not in out and "WaveDREAM scaler" in out and WD in out


def test_sma_only(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a", sma=5.0), _run("b", sma=10.0))
    assert histos[XXP].Integral() == pytest.approx(3.0)
    assert SMA in histos and WD not in histos
    out = capsys.readouterr().out
    assert "warning" not in out and "SMA proton-current pulses" in out and SMA in out


def test_both_everywhere_prefers_wd(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a", wd=5.0, sma=1000.0),
                               _run("b", wd=10.0, sma=1000.0))
    assert histos[XXP].Integral() == pytest.approx(3.0)
    assert WD in histos and SMA not in histos


def test_mixed_falls_back_to_the_source_every_run_has(monkeypatch, capsys):
    # run a has both, run b only the SMA pulses: the SMA for both, never one each
    headers, histos = _combine(monkeypatch, _run("a", wd=5.0, sma=50.0), _run("b", sma=100.0))
    # run a: 20 / 100, run b: 20 / 200 -- not 20 / 10 from a's WD counts
    assert histos[XXP].Integral() == pytest.approx(0.2 + 0.1)
    assert SMA in histos and WD not in histos
    assert "SMA proton-current pulses" in capsys.readouterr().out


def test_mixed_without_a_common_source_is_not_normalised(monkeypatch, capsys):
    # run a has only the WD counts, run b only the SMA pulses
    headers, histos = _combine(monkeypatch, _run("a", wd=5.0), _run("b", sma=100.0))
    assert histos[XXP].Integral() == pytest.approx(40.0)
    assert WD not in histos and SMA not in histos
    out = capsys.readouterr().out
    assert out.count("warning") == 1 and "not normalised" in out
    assert "b0" in out and "a0" in out


def test_an_empty_wd_histogram_does_not_count(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a", wd=5.0, sma=50.0),
                               _run("b", wd=0.0, sma=100.0))
    assert histos[XXP].Integral() == pytest.approx(0.3)
    assert SMA in histos and WD not in histos


def test_a_subrun_without_the_wd_histogram_drops_it_for_the_run(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a", wd=5.0, sma=50.0, wd_subruns=("0",)),
                               _run("b", wd=10.0, sma=100.0))
    assert histos[XXP].Integral() == pytest.approx(0.3)
    assert SMA in histos and WD not in histos
    out = capsys.readouterr().out
    assert "a1" in out and "SMA proton-current pulses" in out


def test_none(monkeypatch, capsys):
    headers, histos = _combine(monkeypatch, _run("a"), _run("b"))
    assert histos[XXP].Integral() == pytest.approx(40.0)
    assert WD not in histos and SMA not in histos
    out = capsys.readouterr().out
    assert out.count("warning") == 1 and "not normalised" in out


def test_a_single_run_merge_uses_the_wd_counts(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ROOT", fake_root(_run("a", wd=5.0, sma=1000.0)))
    from pioneer.nearline import combine_files
    headers, histos = combine_files.merge_sub_runs(["a0", "a1"])
    assert histos[XXP].scaled == pytest.approx(0.1)
    assert "WaveDREAM scaler" in capsys.readouterr().out
