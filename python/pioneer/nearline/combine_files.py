import json
import argparse
from pathlib import Path

from pioneer.nearline.miniTwinInterface import miniTwin_histograms

header_paths = [
    "beamline"
]

# the current pulses the histograms are normalised by; optional, see below
current_path = "histograms/musip/current"

histo_paths = [
    current_path,
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
    normalise them by the run's current pulses (see normalise).
    """
    headers, histos = sum_sub_runs(input_files)
    normalise([histos], [input_files[0]])
    return headers, histos


def current_count(histos : dict) -> float:
    """Current pulses of one run's summed histograms; 0 without any."""
    current = histos.get(current_path)
    return current.Integral() if current is not None else 0.0


def normalise(runs : list[dict], names : list[str]) -> bool:
    """
    Divide each run's histograms by its own number of current pulses -- all
    of them or none: if any run has no current histogram, or an empty one,
    every run is left as raw counts (factor 1), with one warning line, so
    runs stay comparable with each other. Returns True when normalised.
    """
    counts = [current_count(h) for h in runs]
    missing = [name for name, count in zip(names, counts) if count <= 0]
    if missing:
        print(f"warning: {current_path} is missing or empty in {', '.join(missing)}; "
              "histograms are summed but not normalised (factor 1)")
        return False
    for histos, count in zip(runs, counts):
        for h in histos.values():
            h.Scale ( 1. / count)
    return True


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
        if not obj and path == current_path:
            continue    # no current histogram: not normalised, see below
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
        for name, histo in histos.items():
            next_hist = next_file.Get(name)
            if next_hist:
                histo.Add(next_hist)
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
    if not normalise([h for _, h in summed], [files[0] for files in runs]):
        # raw counts: the current histogram is not part of the result
        for _, histos in summed:
            histos.pop(current_path, None)

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
