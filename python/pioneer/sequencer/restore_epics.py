#!/usr/bin/env python3
"""
Re-apply the EPICS beamline settings of an earlier run to the current ODB.

    python3 restore_epics.py 510                  # show the change table, ask, write, wait
    python3 restore_epics.py 510 --dry-run        # show only
    python3 restore_epics.py 510 --magnets-only   # device type 1 only, slits untouched
    python3 restore_epics.py 510 --channels 'ASM12,QSL1*'
    python3 restore_epics.py 510 --against run00520.json   # compare two runs, no MIDAS needed

The settings of the old run come from its ODB dump: the start-of-run dump inside
run%05d.mid[.lz4|.gz] (default), or the end-of-run run%05d.json the logger writes. Both are
looked up in /Logger/Data dir (~/online without a live ODB) unless --data-dir or --file
is given.

Channels are matched by CA name, not by index, so a channel list that changed between the
two runs is handled; channels missing on either side are reported and left alone. Only the
writeable device types are touched (1 magnets, 4 separator, 5 slits, as in config_loader);
the beam blocker (2) is never written.

--source picks which number of the old run becomes the new Demand:
    auto      Demand where the old Demand and Measured agree within the channel's
              Warning Threshold, Measured where they do not (default)
    demand    the old Demand as it was in the ODB
    measured  the old Measured read-back
The ODB Demand has been seen far from what the machine ran at (every magnet at 1.4x its
Measured), which is why the default does not trust Demand blindly.
"""
import argparse
import fnmatch
import glob
import json
import os
import sys
import time

EPICS_PATH = "/Equipment/EPICS"

WRITEABLE_TYPES = (1, 4, 5)   # magnets, separator, slits; 2 (beam blocker) is a shifter decision
TYPE_NAMES = {1: "magnet", 2: "blocker", 3: "PSA", 4: "separator", 5: "slit", 6: "value"}

STATE_RUNNING = 3


# ---------------------------------------------------------------- reading the old run

def _find_dump(run, data_dir, at):
    # a run is one file or subruns run%05d_%05d; the start-of-run dump is in the first subrun,
    # the end-of-run dump in the last
    exts = [".mid.lz4", ".mid", ".mid.gz"]
    try:
        import lz4.frame  # noqa: F401  (the midas reader needs it for .lz4)
    except ImportError:
        exts.remove(".mid.lz4")
    mids = []
    for ext in exts:
        subs = sorted(glob.glob(os.path.join(data_dir, f"run{run:05d}_[0-9][0-9][0-9][0-9][0-9]{ext}")))
        if subs:
            mids.append(subs[0] if at == "bor" else subs[-1])
        mids.append(os.path.join(data_dir, f"run{run:05d}{ext}"))
    js = os.path.join(data_dir, f"run{run:05d}.json")
    order = mids + [js] if at == "bor" else [js] + mids
    for path in order:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(f"no ODB dump for run {run} in {data_dir} (looked for {', '.join(os.path.basename(p) for p in order)})")


def _load_dump(path, at):
    """Return (odb dict, description of which dump was used)."""
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f), f"{os.path.basename(path)} (end of run)"
    import midas.file_reader
    mfile = midas.file_reader.MidasFile(path)
    dump = mfile.get_bor_odb_dump() if at == "bor" else mfile.get_eor_odb_dump()
    if dump is None:
        raise RuntimeError(f"{path} has no {at.upper()} ODB dump")
    which = "start of run" if at == "bor" else "end of run"
    return dump.data, f"{os.path.basename(path)} ({which})"


def _as_list(x):
    return list(x) if isinstance(x, (list, tuple)) else [x]


def epics_table(odb):
    """Channel name -> dict(index, type, demand, measured, threshold, unit) from an ODB dict."""
    try:
        eq = odb["Equipment"]["EPICS"]
    except KeyError:
        raise RuntimeError("ODB dump has no /Equipment/EPICS")
    return _table(eq["Settings"]["CA Name"], eq["Settings"]["Device type"],
                  eq["Settings"]["Warning Threshold"], eq["Settings"].get("Unit"),
                  eq["Variables"]["Demand"], eq["Variables"]["Measured"])


def _table(names, types, thresholds, units, demand, measured):
    names, types, thresholds = _as_list(names), _as_list(types), _as_list(thresholds)
    demand, measured = _as_list(demand), _as_list(measured)
    units = _as_list(units) if units is not None else [""] * len(names)
    n = len(names)
    if not all(len(a) == n for a in (types, thresholds, demand, measured)):
        raise RuntimeError("EPICS arrays in the ODB have different lengths")
    table = {}
    for i in range(n):
        name = str(names[i]).strip()
        if int(types[i]) not in WRITEABLE_TYPES:
            continue   # read-only channels are never written and may share a CA name
        if name in table:
            raise RuntimeError(f"CA name {name} appears twice in /Equipment/EPICS")
        table[name] = dict(index=i, type=int(types[i]), demand=float(demand[i]),
                           measured=float(measured[i]), threshold=float(thresholds[i]),
                           unit=str(units[i]).strip() if i < len(units) else "")
    return table


# ---------------------------------------------------------------- the live ODB

class LiveODB:
    def __init__(self):
        import midas.client
        self.client = midas.client.MidasClient("restore_epics")

    def get(self, path):
        return self.client.odb_get(path)

    def epics_table(self):
        s, v = EPICS_PATH + "/Settings", EPICS_PATH + "/Variables"
        units = None
        try:
            units = self.get(s + "/Unit")
        except Exception:
            pass
        return _table(self.get(s + "/CA Name"), self.get(s + "/Device type"),
                      self.get(s + "/Warning Threshold"), units,
                      self.get(v + "/Demand"), self.get(v + "/Measured"))

    def write_demand(self, targets):
        """targets: index -> value. Re-reads Demand right before writing so that a channel
        changed by someone else in the meantime is not reset."""
        demand = [float(x) for x in _as_list(self.get(EPICS_PATH + "/Variables/Demand"))]
        for i, val in targets.items():
            demand[i] = val
        self.client.odb_set(EPICS_PATH + "/Variables/Demand", demand)

    def measured(self):
        return [float(x) for x in _as_list(self.get(EPICS_PATH + "/Variables/Measured"))]

    def msg(self, text):
        self.client.msg(text)

    def disconnect(self):
        self.client.disconnect()


# ---------------------------------------------------------------- planning the change

def pick_value(old, source):
    """The value of the old run to use as the new Demand, and whether Demand was overruled."""
    if source == "demand":
        return old["demand"], False
    if source == "measured":
        return old["measured"], False
    if abs(old["demand"] - old["measured"]) <= old["threshold"]:
        return old["demand"], False
    return old["measured"], True


def plan(old, cur, args):
    """Rows for every selected channel, plus the names skipped for being absent on one side."""
    types = (1,) if args.magnets_only else WRITEABLE_TYPES
    pats = [p.strip() for p in args.channels.split(",")] if args.channels else None
    excl = [p.strip() for p in args.exclude.split(",")] if args.exclude else []

    def selected(name, t):
        if t not in types:
            return False
        if pats and not any(fnmatch.fnmatchcase(name, p) for p in pats):
            return False
        return not any(fnmatch.fnmatchcase(name, p) for p in excl)

    rows, only_old, only_cur = [], [], []
    for name, o in old.items():
        if not selected(name, o["type"]):
            continue
        c = cur.get(name)
        if c is None:
            only_old.append(name)
            continue
        if c["type"] != o["type"]:
            raise RuntimeError(f"{name}: device type {o['type']} in the old run, {c['type']} now")
        target, overruled = pick_value(o, args.source)
        rows.append(dict(name=name, index=c["index"], type=o["type"], unit=c["unit"],
                         old_demand=o["demand"], old_measured=o["measured"],
                         cur_demand=c["demand"], cur_measured=c["measured"],
                         threshold=c["threshold"], target=target, overruled=overruled))
    for name, c in cur.items():
        if selected(name, c["type"]) and name not in old:
            only_cur.append(name)
    if pats:
        for p in pats:
            if not any(fnmatch.fnmatchcase(n, p) for n in list(old) + list(cur)):
                raise RuntimeError(f"--channels pattern {p!r} matches no channel")
    return rows, only_old, only_cur


def changed(r):
    return abs(r["target"] - r["cur_demand"]) > 1e-6 * max(1.0, abs(r["target"]))


def print_plan(rows, only_old, only_cur, run, dump_desc, source, cur_desc):
    print(f"Run {run}: {dump_desc}; new Demand from old {source}.")
    print(f"Compared against {cur_desc}.\n")
    hdr = f"{'channel':<10} {'type':<9} {'run Demand':>11} {'run Meas.':>11} {'now Demand':>11} {'now Meas.':>11} {'-> new':>11}  "
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(rows, key=lambda r: r["index"]):
        flag = ""
        if r["overruled"]:
            flag = "Demand off in that run, using Measured"
        elif not changed(r):
            flag = "unchanged"
        print(f"{r['name']:<10} {TYPE_NAMES.get(r['type'], r['type']):<9} "
              f"{r['old_demand']:>11.4f} {r['old_measured']:>11.4f} "
              f"{r['cur_demand']:>11.4f} {r['cur_measured']:>11.4f} {r['target']:>11.4f}  {flag}")
    n = sum(changed(r) for r in rows)
    print(f"\n{n} of {len(rows)} selected channel(s) change.")
    if only_old:
        print(f"In run {run} but not in the current ODB, skipped: {', '.join(only_old)}")
    if only_cur:
        print(f"In the current ODB but not in run {run}, left alone: {', '.join(only_cur)}")
    return n


# ---------------------------------------------------------------- waiting for read-backs

def wait_settled(odb, rows, timeout, stable_for=5.0):
    """Poll Measured until every written channel is within its Warning Threshold of the new
    Demand for stable_for seconds. Returns the rows still outside when the time runs out."""
    t0 = time.time()
    good_since = None
    outside = rows
    while True:
        meas = odb.measured()
        outside = [r for r in rows if abs(meas[r["index"]] - r["target"]) > r["threshold"]]
        now = time.time()
        if not outside:
            good_since = good_since or now
            if now - good_since >= stable_for:
                return []
        else:
            good_since = None
        if now - t0 > timeout:
            for r in outside:
                r["final_measured"] = meas[r["index"]]
            return outside
        names = ", ".join(r["name"] for r in outside[:6]) + (" ..." if len(outside) > 6 else "")
        print(f"\r  {now - t0:5.0f} s  waiting for {len(outside)}: {names:<60}", end="", flush=True)
        time.sleep(1.0)


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=int, help="run whose EPICS settings to re-apply")
    ap.add_argument("--file", help="ODB dump to read instead of looking it up (.json, .mid, .mid.lz4, .mid.gz)")
    ap.add_argument("--data-dir", help="where run files live (default: /Logger/Data dir of the current ODB, else ~/online)")
    ap.add_argument("--at", choices=("bor", "eor"), default="bor",
                    help="start-of-run (default) or end-of-run settings")
    ap.add_argument("--source", choices=("auto", "demand", "measured"), default="auto",
                    help="which value of the old run becomes the new Demand (default auto, see above)")
    ap.add_argument("--magnets-only", action="store_true", help="device type 1 only (no slits, no separator)")
    ap.add_argument("--channels", help="comma-separated CA names or glob patterns to restrict to, e.g. 'ASM12,QSL1*'")
    ap.add_argument("--exclude", help="comma-separated CA names or glob patterns to leave alone")
    ap.add_argument("--dry-run", action="store_true", help="show the change table and stop")
    ap.add_argument("--against", help="compare with this ODB dump instead of the live ODB (implies --dry-run)")
    ap.add_argument("-y", "--yes", action="store_true", help="do not ask before writing")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for the read-backs to follow")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for the read-backs (default 120)")
    ap.add_argument("--allow-running", action="store_true", help="allow writing while a run is in progress")
    args = ap.parse_args(argv)

    odb = None
    if args.against:
        args.dry_run = True
        cur_odb, _ = _load_dump(args.against, "bor")
        cur = epics_table(cur_odb)
        cur_desc = args.against
    else:
        odb = LiveODB()
        cur = odb.epics_table()
        cur_desc = "the live ODB"

    try:
        if args.file:
            path = args.file
        else:
            data_dir = args.data_dir or (odb.get("/Logger/Data dir") if odb else None) \
                or os.path.expanduser("~/online")
            path = _find_dump(args.run, data_dir, args.at)
        old_odb, dump_desc = _load_dump(path, args.at)
        dump_run = old_odb.get("Runinfo", {}).get("Run number")
        if dump_run is not None and int(dump_run) != args.run:
            raise RuntimeError(f"{path} belongs to run {dump_run}, not {args.run}")
        if args.at == "bor" and path.endswith(".json"):
            print("Note: no readable .mid file (or no lz4 module), using the end-of-run .json dump.\n")

        rows, only_old, only_cur = plan(epics_table(old_odb), cur, args)
        n = print_plan(rows, only_old, only_cur, args.run, dump_desc, args.source, cur_desc)
        if args.dry_run or n == 0:
            return 0

        if not odb.get(EPICS_PATH + "/Settings/Allow write access"):
            print(f"\n{EPICS_PATH}/Settings/Allow write access is off: the frontend would not pass "
                  "the new Demand on to EPICS. Nothing written.")
            return 1
        if odb.get("/Runinfo/State") == STATE_RUNNING and not args.allow_running:
            print("\nA run is in progress. Stop it first, or pass --allow-running. Nothing written.")
            return 1
        if not args.yes:
            if input(f"\nWrite {n} channel(s) to {EPICS_PATH}/Variables/Demand? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Nothing written.")
                return 1

        todo = [r for r in rows if changed(r)]
        odb.write_demand({r["index"]: r["target"] for r in todo})
        text = f"restore_epics: Demand of {n} EPICS channel(s) set from run {args.run} ({dump_desc}, {args.source})"
        odb.msg(text)
        print(text)

        if args.no_wait:
            return 0
        print(f"Waiting up to {args.timeout:.0f} s for Measured to follow ...")
        outside = wait_settled(odb, todo, args.timeout)
        print()
        if not outside:
            print("All written channels are within their Warning Threshold.")
            return 0
        print(f"{len(outside)} channel(s) did not reach the new Demand within {args.timeout:.0f} s:")
        for r in outside:
            print(f"  {r['name']:<10} new Demand {r['target']:.4f}  Measured {r['final_measured']:.4f}  "
                  f"(threshold {r['threshold']})")
        return 2
    finally:
        if odb:
            odb.disconnect()


if __name__ == "__main__":
    sys.exit(main())
