"""Put the beamline back the way it was in an earlier run, from the ConfigDB page.

The page has a "restore beamline settings of run N" line below the beamline
table.  Like "go to" (goto.py) it asks for a preview first, shows it in a
confirm dialog, and only then asks for the write.  Both go through here.

The planning and the write are `restore_epics`'s own: the same dump lookup,
the same choice of value (`--source auto`: the run's Demand unless it is off
its Measured by more than the Warning Threshold, else the Measured), the same
Demand re-read just before writing.  On the online machines the RunDBView
python has no lz4 and the data directory holds only the end-of-run
runNNNNN.json, so in practice the values are the ones at the end of that run;
the dialog says which dump was used.

Channels come in groups.  The magnets (device type 1) are always restored,
except the SEP41 coil.  The slits (type 5), the SEP41 coil and the separator
high voltage (type 4) are each restored only when the page's box for them is
ticked.  The beam blocker (2) and the read-only types are never touched.
Unticked groups are still planned, so the dialog can show what is being left
behind, but they are never written.  With no box ticked this is
`restore_epics --magnets-only --exclude SEP41`.

As with go to, a run in progress or a running sequencer is a warning in the
dialog, not a refusal.  `Allow write access` off is a refusal: the frontend
would not pass the new Demand on to EPICS.

The same run can also be scheduled instead of applied now: the beamline table's
"settings from run N" row turns into an ordinary configuration at schedule
time (config_values, used by midas_commands.schedule_configuration).

The same thing from a shell, for when the page is not available:

    python -m pioneer.rundb.restore <run> [--include slits,sep41,sep41_hv] [--yes]

previews (the dialog's table, as text), asks, writes, and waits for the
read-backs, as the page does.  The page's "settings from run N" row, without
scheduling anything and without touching the ODB:

    python -m pioneer.rundb.restore <run> [--include ...] --store-config \
        [--table pie5_epics] --write-dsn "host=... dbname=pioneer user=shifter"

stores the configuration and prints its id, to be scheduled like any other.
(`python -m pioneer.sequencer.restore_epics` is the older tool underneath; with
`--magnets-only --exclude SEP41` it restores what this does with no group.)
"""

import re
import sys

from pioneer.rundb.goto import GotoError, warnings as goto_warnings
from pioneer.sequencer import restore_epics as rx

EPICS = rx.EPICS_PATH

SOURCE = "auto"

# The groups a request may add, and how a channel is recognised as one of
# them.  Everything else that is writeable is a magnet, and always restored.
GROUPS = {
    "slits": ("type", 5),
    "sep41": ("name", "SEP41"),
    "sep41_hv": ("type", 4),
}

# What the page calls each group: the box is "restore <label>".
LABELS = {"magnets": "magnets", "slits": "slits", "sep41": "SEP41", "sep41_hv": "SEP41-HV"}

# How long the page watches the read-backs, and how long they must hold.
STABLE_FOR = 5
TIMEOUT = 120


def group_of(row: dict) -> str:
    """The group a planned row belongs to: a GROUPS key, or "magnets" for any
    other magnet (device type 1).  Any other channel is an error: a writeable
    type nobody has decided about must not end up restored by default."""
    for group, (field, value) in GROUPS.items():
        if row[field] == value:
            return group
    if row["type"] == 1:
        return "magnets"
    raise GotoError("usage", f"{row['name']}: device type {row['type']} is in no restore group")


_PLAIN_INT = re.compile(r"^\s*[0-9]+\s*$")


def parse_run(value) -> int:
    """A run number from a request: a positive int, an integral float, or a
    string of digits.  Anything else (a bool, 1569.5, "1569abc", "") is refused."""
    if isinstance(value, bool):
        run = None
    elif isinstance(value, int):
        run = value
    elif isinstance(value, float) and value.is_integer():
        run = int(value)
    elif isinstance(value, str) and _PLAIN_INT.match(value):
        run = int(value)
    else:
        run = None
    if run is None or run <= 0:
        raise GotoError("usage", f"not a run number: {value!r}")
    return run


def _include(include) -> tuple:
    """The requested extra groups, checked, in GROUPS order."""
    if include is None:
        include = ()
    if isinstance(include, str) or not isinstance(include, (list, tuple)):
        raise GotoError("usage", "include must be a list of group names")
    unknown = [g for g in include if g not in GROUPS]
    if unknown:
        raise GotoError("usage", f"unknown group(s) {', '.join(map(str, unknown))}; "
                                 f"known: {', '.join(GROUPS)}")
    return tuple(g for g in GROUPS if g in include)


def _plan(client, run: int, include: tuple):
    """restore_epics' plan over every writeable channel, each row tagged with its group."""
    odb = rx.LiveODB(client)
    try:
        rows, only_old, only_cur, dump = rx.prepare(odb, run, source=SOURCE)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise GotoError("usage", f"run {run}: {exc}") from exc
    for r in rows:
        r["group"] = group_of(r)
        r["included"] = r["group"] == "magnets" or r["group"] in include   # group_of may refuse
        r["changes"] = rx.changed(r)
    rows.sort(key=lambda r: r["index"])
    return odb, rows, only_old, only_cur, dump


def _write_allowed(client) -> bool:
    try:
        return bool(client.odb_get(EPICS + "/Settings/Allow write access"))
    except Exception:  # noqa: BLE001 - an unreadable flag is not a yes
        return False


WRITE_OFF = (f"{EPICS}/Settings/Allow write access is off: the frontend would not pass "
             "the new Demand on to EPICS, so nothing can be restored")


def _warnings(client) -> list:
    out = goto_warnings(client)
    if not _write_allowed(client):
        out.append(WRITE_OFF)
    return out


def _groups_text(include: tuple) -> str:
    return ", ".join(LABELS[g] for g in ("magnets",) + include)


def _row_out(r: dict) -> dict:
    return {k: r[k] for k in ("name", "type", "group", "included", "unit", "old_demand",
                              "old_measured", "cur_demand", "target", "overruled", "changes")} | \
        {"type_name": rx.TYPE_NAMES.get(r["type"], str(r["type"]))}


def preview(client, run: int, include=()) -> dict:
    """What a restore would write, and what it would leave alone, without writing."""
    include = _include(include)
    _, rows, only_old, only_cur, dump = _plan(client, run, include)
    return {"run": run, "dump": dump, "source": SOURCE, "include": list(include),
            "rows": [_row_out(r) for r in rows],
            "only_old": only_old, "only_cur": only_cur,
            "n_changed": sum(1 for r in rows if r["included"] and r["changes"]),
            "n_included": sum(1 for r in rows if r["included"]),
            "warnings": _warnings(client)}


def load(client, run: int, include=(), who: str = "ConfigDB page") -> dict:
    """Write the Demand of the included, changed channels and return without waiting."""
    include = _include(include)
    odb, rows, only_old, only_cur, dump = _plan(client, run, include)
    if not _write_allowed(client):
        raise GotoError("denied", WRITE_OFF)
    warn = goto_warnings(client)
    todo = [r for r in rows if r["included"] and r["changes"]]
    label = f"the beamline settings of run {run}"
    if todo:
        odb.write_demand({r["index"]: r["target"] for r in todo})
        n = len(todo)
        line = (f"{who}: Demand of {n} EPICS channel{'' if n == 1 else 's'} set from run {run} "
                f"({dump}, {SOURCE}); restored {_groups_text(include)}")
        if warn:
            line += " (" + "; ".join(warn) + ")"
        try:
            client.msg(line, is_error=False)
        except Exception as exc:  # noqa: BLE001 - the Demand is written; say so, whatever the log does
            print(f"restore: Demand written, but the message log refused the line: {exc!r}: {line}",
                  file=sys.stderr)
    return {"run": run, "dump": dump, "source": SOURCE, "include": list(include),
            "label": label, "written": [r["name"] for r in todo],
            "n_changed": len(todo), "warnings": warn,
            "arrival": [{"path": f"{EPICS}/Variables/Measured[{r['index']}]", "op": "range",
                         "target": r["target"] - r["threshold"],
                         "upper": r["target"] + r["threshold"]} for r in todo],
            "stable_for": STABLE_FOR, "timeout": TIMEOUT}


def config_values(client, run: int, include=()) -> tuple:
    """A complete beamline configuration for the run DB: (values, comment).

    The sequencer's beamline loader (config_loader.load_beam_config) wants a
    value for every writeable channel, keyed `CA Name` + `CA Demand` as
    config_writer.write_epics stores them.  Included groups get run `run`'s
    planned value; everything else gets the live Demand as it is now, so an
    unticked group is frozen at its value at schedule time, not at the moment
    the run starts.
    """
    include = _include(include)
    _, rows, _, _, dump = _plan(client, run, include)
    target = {r["index"]: r["target"] for r in rows if r["included"]}
    names = rx._as_list(client.odb_get(EPICS + "/Settings/CA Name"))
    suffix = rx._as_list(client.odb_get(EPICS + "/Settings/CA Demand"))
    types = rx._as_list(client.odb_get(EPICS + "/Settings/Device type"))
    demand = rx._as_list(client.odb_get(EPICS + "/Variables/Demand"))
    values = {}
    for i, (name, sfx) in enumerate(zip(names, suffix)):
        if int(types[i]) not in rx.WRITEABLE_TYPES:
            continue
        values[f"{name}{sfx}"] = float(target.get(i, demand[i]))
    kept = [LABELS[g] for g in GROUPS if g not in include]
    # The ConfigDB page hides these rows by this prefix: FROM_RUN_COMMENT in custom/js/cfgdb.js.
    comment = f"from run {run} ({dump}, {SOURCE}" + (f"; {'/'.join(kept)} kept)" if kept else ")")
    return values, comment


# ---------------------------------------------------------------- the manual path

def preview_text(p: dict) -> str:
    """A restore_preview reply as the text the shell shows: the dialog's tables."""
    def fmt(x):
        return f"{x:>11.4f}"
    head = (f"{'channel':<10} {'type':<9} {'run Demand':>11} {'run Meas.':>11} "
            f"{'now Demand':>11} {'-> new':>11}  ")
    out = [f"Run {p['run']}: {p['dump']}; new Demand from the run's {p['source']} value.",
           f"Restoring: {_groups_text(tuple(p['include']))}.", ""]
    for w in p["warnings"]:
        out.append(f"WARNING: {w}")
    if p["warnings"]:
        out.append("")
    out += [head, "-" * len(head)]
    for r in p["rows"]:
        if not r["included"]:
            continue
        note = ("run Demand off, using Measured" if r["overruled"]
                else "" if r["changes"] else "unchanged")
        out.append(f"{r['name']:<10} {r['type_name']:<9} {fmt(r['old_demand'])} "
                   f"{fmt(r['old_measured'])} {fmt(r['cur_demand'])} {fmt(r['target'])}  {note}")
    out.append(f"\n{p['n_changed']} of {p['n_included']} restored channel(s) change.")
    left = [r for r in p["rows"] if not r["included"]]
    if left:
        out.append("\nNot restored (--include " + ",".join(GROUPS) + " to add):")
        for r in left:
            note = "differs, kept as now" if r["changes"] else "same as run"
            out.append(f"{r['name']:<10} {r['type_name']:<9} {fmt(r['old_demand'])} "
                       f"{fmt(r['old_measured'])} {fmt(r['cur_demand'])} {'-':>11}  "
                       f"[{LABELS[r['group']]}] {note}")
    if p["only_old"]:
        out.append(f"In run {p['run']} but not in the ODB now, skipped: {', '.join(p['only_old'])}")
    if p["only_cur"]:
        out.append(f"In the ODB now but not in run {p['run']}, left alone: {', '.join(p['only_cur'])}")
    return "\n".join(out)


def _parse_args(argv):
    import argparse

    ap = argparse.ArgumentParser(
        description="Restore the beamline settings of an earlier run, as the ConfigDB page does.")
    ap.add_argument("run", help="run whose EPICS settings to restore")
    ap.add_argument("--include", default="",
                    help="comma-separated extra groups to restore: " + ", ".join(GROUPS)
                         + " (default: the magnets only, without SEP41)")
    ap.add_argument("--yes", action="store_true", help="do not ask before writing")
    ap.add_argument("--store-config", action="store_true",
                    help="only store the settings as a new beamline configuration in the run DB "
                         "and print its id; nothing is written to the ODB and nothing is scheduled")
    ap.add_argument("--table", default="pie5_epics", choices=("pie5_epics", "pim1_epics"),
                    help="the beamline table for --store-config (default pie5_epics)")
    ap.add_argument("--write-dsn", help="run DB connection string for --store-config")
    a = ap.parse_args(argv)
    try:
        a.run = parse_run(a.run)
        a.include = _include([g.strip() for g in a.include.split(",") if g.strip()])
    except GotoError as exc:
        ap.error(str(exc))
    if a.store_config and not a.write_dsn:
        ap.error("--store-config needs --write-dsn")
    if a.write_dsn and not a.store_config:
        ap.error("--write-dsn is only used with --store-config")
    return a


def store_config(write_dsn: str, table: str, values: dict, comment: str) -> int:
    """Insert one beamline configuration through `write_dsn`; its id."""
    import psycopg

    from pioneer.rundb.interface import insert_configuration

    with psycopg.connect(write_dsn, connect_timeout=10) as conn:
        config_id = insert_configuration(conn, table, dict(values), comment)
        conn.commit()
    return config_id


def main(argv=None) -> int:
    """The manual path: preview, confirm, write, wait (or --store-config)."""
    import time

    import midas.client

    from pioneer.rundb.goto import _met

    a = _parse_args(argv)
    client = midas.client.MidasClient("RunDBRestore")
    try:
        if a.store_config:
            values, comment = config_values(client, a.run, a.include)
            config_id = store_config(a.write_dsn, a.table, values, comment)
            print(f"stored {a.table} configuration {config_id}: {comment}")
            return 0
        pre = preview(client, a.run, a.include)
        print(preview_text(pre))
        if not pre["n_changed"]:
            print("Nothing to write.")
            return 0
        if not a.yes and input(f"\nWrite {pre['n_changed']} channel(s)? [y/N] ").strip().lower() != "y":
            print("Nothing written.")
            return 1
        done = load(client, a.run, a.include, who="RunDBRestore (command line)")
        print(f"Written: {', '.join(done['written'])}. Waiting up to {done['timeout']} s ...")
        t0 = time.monotonic()
        since = None
        while time.monotonic() - t0 < done["timeout"]:
            meas = client.odb_get(EPICS + "/Variables/Measured")
            ok = all(_met(meas[int(r["path"].rsplit("[", 1)[1][:-1])], r) for r in done["arrival"])
            since = (since or time.monotonic()) if ok else None
            if since is not None and time.monotonic() - since >= done["stable_for"]:
                print(f"arrived after {time.monotonic() - t0:.0f} s")
                return 0
            time.sleep(1)
        print(f"not there after {done['timeout']} s; check the EPICS page")
        return 2
    except GotoError as exc:
        print(f"refused: {exc}")
        return 1
    finally:
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
