from __future__ import annotations

import json
import argparse
from pathlib import Path

from pioneer.nearline.miniTwinInterface import miniTwin_histograms

header_paths = [
    "beamline"
]

# The proton current the histograms are normalised by, in order of preference;
# each is optional, see normalise. Both count the same ~220 kHz signal, but with
# different live times, so one join never mixes them.
#  - the WaveDREAM scaler of the input the run's wd_channel_map calls "current"
#    (PIWDScalerMonitor: rate x interval per reading, in scaler counts);
#  - the SMA channel the run's mutrig_channel_map marks as the proton current
#    (PITMidasMusip: one count per pulse the FEB recorded).
wd_current_path = "histograms/PIWDScalerMonitor/proton_current_counts"
sma_current_path = "histograms/musip/current"
current_paths = (wd_current_path, sma_current_path)
current_sources = {wd_current_path: "WaveDREAM scaler proton current",
                   sma_current_path: "SMA proton-current pulses"}

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
    normalise them by the run's proton current (see normalise).
    """
    headers, histos = sum_sub_runs(input_files)
    normalise([histos], [input_files[0]])
    return headers, histos


def current_count(histos : dict, path : str = wd_current_path) -> float:
    """Proton-current counts of one run's summed histograms under `path`; 0
    without any."""
    current = histos.get(path)
    return current.Integral() if current is not None else 0.0


def current_source(runs : list[dict]) -> str | None:
    """The current histogram every run of a join has non-empty, the WaveDREAM
    scaler counts before the SMA pulses; None when neither covers every run."""
    for path in current_paths:
        if all(current_count(h, path) > 0 for h in runs):
            return path
    return None


def normalise(runs : list[dict], names : list[str]) -> str | None:
    """
    Divide each run's histograms by its own proton-current count, from one
    source for the whole join (current_source): the WaveDREAM scaler counts
    when every run has them non-empty, else the SMA current pulses when every
    run has those, else nothing. Without a source every run is left as raw
    counts (factor 1), with one warning line, so the runs stay comparable with
    each other. The two sources are never mixed. Returns the path of the
    source used, or None.
    """
    path = current_source(runs)
    if path is None:
        missing = []
        for p in current_paths:
            lacking = [name for name, h in zip(names, runs) if current_count(h, p) <= 0]
            missing.append(f"{p} in {', '.join(lacking)}")
        print(f"warning: no proton-current source covers every run of this join "
              f"({'; '.join(missing)} missing or empty); "
              "histograms are summed but not normalised (factor 1)")
        return None
    counts = [current_count(h, path) for h in runs]
    print(f"normalised by the {current_sources[path]} ({path}): "
          + ", ".join(f"{name} {count:.6g}" for name, count in zip(names, counts)))
    for histos, count in zip(runs, counts):
        for h in histos.values():
            h.Scale ( 1. / count)
    return path


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
    for path in histo_paths:
        obj = first_file.Get(path)
        if not obj and path in current_paths:
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
        for path in current_paths:
            if path != used:
                histos.pop(path, None)

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



if __name__ == "__main__":
    main()
