from __future__ import annotations

import json
import argparse
from pathlib import Path

from pioneer.nearline.miniTwinInterface import miniTwin_histograms

header_paths = [
    "beamline"
]

# The proton current the histograms are normalised by: the beam delivered while
# the SMA was live. Two sources, in order of preference; each is optional, see
# normalise, and one join never mixes them.
#  - "wd": the WaveDREAM scaler of the input the run's wd_channel_map calls
#    "current" (PIWDScalerMonitor: counts and the board seconds they cover),
#    whose ratio is the mean proton-current rate, times the SMA live time
#    (PIPSMSMACalibration: the summed SMA frame spans);
#  - "sma": the SMA channel the run's mutrig_channel_map marks as the proton
#    current (PITMidasMusip: one count per pulse the FEB recorded, so only
#    while the SMA was live).
# Both measure rate x SMA live time of the same ~220 kHz signal, so factors of
# the two sources mean the same thing.
wd_current_path = "histograms/PIWDScalerMonitor/proton_current_counts"
wd_seconds_path = "histograms/PIWDScalerMonitor/proton_current_seconds"
sma_live_path = "histograms/PIPSMSMACalibration/sma_live_seconds"
sma_current_path = "histograms/musip/current"
source_paths = {"wd": (wd_current_path, wd_seconds_path, sma_live_path),
                "sma": (sma_current_path,)}
source_names = {"wd": "WaveDREAM scaler proton-current rate x SMA live time",
                "sma": "SMA proton-current pulses"}
current_paths = tuple(dict.fromkeys(p for paths in source_paths.values() for p in paths))

histo_paths = [
    *current_paths,
    *miniTwin_histograms # all histograms the miniTwin is asking for
]

def load_json_config(config_file : Path) -> dict:
    """
    Load the merge configuration form the json file.
    """
    with open(config_file, 'r') as f:
        config = json.load(f)
    return config


def merge_sub_runs(input_files : list[str]):
    """
    Combine histograms from the same run that got split into subruns, and
    normalise them by the run's proton current (see normalise). Only the
    current histograms of the source used are kept, as in combine_runs.
    """
    headers, histos = sum_sub_runs(input_files)
    used = normalise([histos], [input_files[0]])
    keep_only_source(histos, used)
    return headers, histos


def integral(histos : dict, path : str) -> float:
    """Integral of one run's summed histogram under `path`; 0 without it."""
    h = histos.get(path)
    return h.Integral() if h is not None else 0.0


def normaliser(histos : dict, source : str) -> float | None:
    """One run's normalisation from `source`, None when the run lacks it:
    "wd" = WD counts / WD seconds x SMA live seconds (each present and > 0),
    "sma" = the SMA current pulses (> 0)."""
    if source == "wd":
        counts, seconds, live = (integral(histos, p) for p in source_paths["wd"])
        if counts > 0 and seconds > 0 and live > 0:
            return counts / seconds * live
        return None
    count = integral(histos, sma_current_path)
    return count if count > 0 else None


def current_source(runs : list[dict]) -> str | None:
    """The source every run of a join has ("wd" before "sma"); None when
    neither covers every run."""
    for source in source_paths:
        if all(normaliser(h, source) is not None for h in runs):
            return source
    return None


def keep_only_source(histos : dict, used : str | None) -> None:
    """Drop every current histogram that is not one of the source used (all
    of them for a join left as raw counts)."""
    keep = source_paths.get(used, ())
    for path in current_paths:
        if path not in keep:
            histos.pop(path, None)


def normalise(runs : list[dict], names : list[str]) -> str | None:
    """
    Divide each run's histograms by its own proton-current normalisation,
    from one source for the whole join (current_source): the WaveDREAM rate x
    SMA live time when every run has the three histograms non-empty, else the
    SMA current pulses when every run has those, else nothing. Without a
    source every run is left as raw counts (factor 1), with one warning line,
    so the runs stay comparable with each other. The two sources are never
    mixed. Returns the source used ("wd", "sma"), or None.
    """
    source = current_source(runs)
    if source is None:
        missing = []
        for src, paths in source_paths.items():
            lacking = [name for name, h in zip(names, runs) if normaliser(h, src) is None]
            missing.append(f"{' + '.join(paths)} in {', '.join(lacking)}")
        print(f"warning: no proton-current source covers every run of this join "
              f"({'; '.join(missing)} missing or empty); "
              "histograms are summed but not normalised (factor 1)")
        return None
    factors = [normaliser(h, source) for h in runs]
    if source == "wd":
        detail = ", ".join(
            f"{name} {integral(h, wd_current_path):.6g} counts / {integral(h, wd_seconds_path):.6g} s"
            f" x {integral(h, sma_live_path):.6g} s live = {f:.6g}"
            for name, h, f in zip(names, runs, factors))
    else:
        detail = ", ".join(f"{name} {f:.6g}" for name, f in zip(names, factors))
    print(f"normalised by the {source_names[source]} ({' + '.join(source_paths[source])}): {detail}")
    for histos, factor in zip(runs, factors):
        for h in histos.values():
            h.Scale ( 1. / factor)
    return source


def sum_sub_runs(input_files : list[str]):
    """
    Sum the histograms of one run's subrun files; no normalisation.
    """

    import ROOT     # here, not at module level, so the module imports without ROOT

    if not input_files:
        raise ValueError("Received invalid list of subruns")

    # Load first file.
    first_file = ROOT.TFile.Open(input_files[0])
    if not first_file or first_file.IsZombie():
        raise OSError(f"Unable to read input file {input_files[0]}")

    # Load everything of interest:
    histos = dict()
    absent_first = set()
    for path in histo_paths:
        obj = first_file.Get(path)
        if not obj and path in current_paths:
            absent_first.add(path)
            continue    # no current histogram from this source: see normalise
        if not obj:
            raise ValueError(f"File {input_files[0]} does not contain {path}")
        histos[path] = obj.Clone()
        histos[path].SetDirectory(0)

    headers = dict()
    for path in header_paths:
        obj = first_file.Get(path)
        if not obj:
            raise ValueError(f"File {input_files[0]} does not contain {path}")
        headers[path] = obj.Clone()

    first_file.Close()


    # add all subsequent files.
    for next_file_path in input_files[1:]:
        next_file = ROOT.TFile.Open(next_file_path)
        if not next_file or next_file.IsZombie():
            raise OSError(f"Unable to read input file {next_file_path}")
        for name in sorted(absent_first):
            if next_file.Get(name):
                # present here but not in the first subrun: the run has no
                # complete count from it either
                print(f"note: {name} not present in {input_files[0]}; the run is not "
                      "normalised by it")
                absent_first.discard(name)
        for name, histo in list(histos.items()):
            next_hist = next_file.Get(name)
            if next_hist:
                histo.Add(next_hist)
            elif name in current_paths:
                # a subrun without this current source: the run has no complete
                # count from it, so it is not offered for normalisation
                print(f"note: {name} not present in {next_file_path}; the run is not "
                      "normalised by it")
                del histos[name]
            else:
                raise ValueError(f"Histogram {name} not present in file {next_file_path}.")
        for name, header in headers.items():
            next_header = next_file.Get(name)
            if not next_header:
                raise ValueError(f"Header {name} not present in file {next_file_path}.")
            elif not header.MergeHeader(next_header):
                raise ValueError(f"Header {name} is incompatible between {input_files[0]} and {next_file_path}.")
        next_file.Close()


    return headers, histos


def combine_runs(runs : list[list[str]]):
    """
    Sum each run's subruns, normalise all runs or none (see normalise), and
    add the runs together. Returns (headers, histos).
    """
    summed = [sum_sub_runs(files) for files in runs]
    used = normalise([h for _, h in summed], [files[0] for files in runs])
    # Only the source used stays in the result (normalised, one per run); the
    # other, which not every run may have, and every current histogram of a
    # join left as raw counts, are dropped.
    for _, histos in summed:
        keep_only_source(histos, used)

    combined_headers = {}
    combined_histos = {}
    for headers, histos in summed:
        if not combined_headers:
            combined_headers = headers
        else:
            if set(combined_headers.keys()) != set(headers.keys()):
                raise ValueError(f"Inconsistent header sets between runs")
            for name, header in headers.items():
                if not combined_headers[name].MergeHeader(header):
                    raise ValueError(f"Incompatible header {name} detected")

        if not combined_histos:
            combined_histos = histos
        else:
            if set(combined_histos.keys()) != set(histos.keys()):
                raise ValueError(f"Inconsistent histogram sets between runs")
            for name, histo in histos.items():
                combined_histos[name].Add(histo)
    return combined_headers, combined_histos


def main():
    import ROOT

    parser = argparse.ArgumentParser(description="Combine nearline ROOT files.")
    parser.add_argument("config", type=Path, help="JSON merge configuration file")
    args = parser.parse_args()

    config = load_json_config(args.config)
    combined_headers, combined_histos = combine_runs(list(config["runs"].values()))

    output = ROOT.TFile.Open(config["output"], "RECREATE")
    if not output or output.IsZombie():
        raise OSError(f"Unable to create output file {config['output']}")

    for objects in (combined_headers, combined_histos):
        for path, obj in objects.items():
            directory = output
            parts = path.split("/")
            for part in parts[:-1]:
                directory = directory.GetDirectory(part) or directory.mkdir(part)
            directory.cd()
            obj.Write(parts[-1])
    output.Close()

    import subprocess

    subprocess.run(
            ["rsync", config["output"], "analysis:~/nearline/combined"],
            check=True,
    )


if __name__ == "__main__":
    main()
