"""Load one configuration into the ODB without starting a run.

The ConfigDB page has a "go to" button next to every configuration.  It asks
for a preview first (what would change), shows that in a confirm dialog, and
only then asks for the load.  Both go through here.

The load is the sequencer's own: the same `config_loader` setters write the
same Demand values the sequencer would write before a run, so a configuration
reached by hand and one reached by a run end up identical in the ODB.  The
difference is that nothing waits here.  The setters hand back the conditions
the sequencer would wait for, and they go back to the page as a plain list,
which the page then watches until the hardware has arrived.  This process
answers every page from one thread, so blocking it for the minute a magnet
can take to settle would freeze the page for everybody.

A run in progress or a running sequencer does not stop a load.  It turns into
a warning in the confirm dialog: the shifter may have a good reason, and the
decision is theirs.

The same thing from a shell, for when the page is not available:

    python -m pioneer.rundb.goto <config_id> [--yes]
"""

from pioneer.sequencer import config_loader

# midas.h
STATE_STOPPED = 1
STATE_PAUSED = 2
STATE_RUNNING = 3

# The tables a configuration can be loaded from.  `job_id` and `num_ev` are in
# the sequencer's dispatch too, but they describe a run, not a place.
LOADABLE = ("target_position", "degrader_position", "pie5_epics", "pim1_epics")

# Columns of the config tables that are bookkeeping, not settings.  The
# sequencer drops the same two (interface.load_run_config).
BOOKKEEPING = ("id", "seq_id")


class GotoError(Exception):
    """A load that cannot go ahead, with the envelope error kind."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _read(view, config_id: int) -> dict:
    """`view.config()`, with an unknown id turned into a GotoError."""
    from pioneer.rundb.view import ViewError

    try:
        return view.config(config_id)
    except ViewError as exc:
        raise GotoError(exc.kind if exc.kind == "usage" else "db", exc.message) from exc


def settings_of(row: dict) -> tuple:
    """(config_type, settings) of a `view.config()` row, or a GotoError."""
    cfg = row.get("config") or {}
    config_type = cfg.get("config_type")
    config_id = cfg.get("config_id")
    if config_type not in LOADABLE:
        raise GotoError("usage", f"configuration {config_id} is a {config_type}, "
                                 "which cannot be loaded from this page")
    if cfg.get("do_not_use"):
        raise GotoError("denied", f"configuration {config_id} is marked do_not_use")
    values = cfg.get("values")
    if not values:
        raise GotoError("usage", f"configuration {config_id} has no values")
    settings = {k: v for k, v in values.items() if k not in BOOKKEEPING}
    return config_type, settings


def warnings(client) -> list:
    """Reasons to think twice, read from the ODB now."""
    out = []
    try:
        state = client.odb_get("/Runinfo/State")
    except Exception:  # noqa: BLE001 - an unreadable state is not a reason to refuse
        state = None
    if state == STATE_RUNNING:
        out.append("a run is in progress: the change lands in the middle of it")
    elif state == STATE_PAUSED:
        out.append("a run is paused: the change lands in the middle of it")
    try:
        seq_running = bool(client.odb_get("/PySequencer/State/Running"))
    except Exception:  # noqa: BLE001
        seq_running = False
    if seq_running:
        out.append("the sequencer is running: it loads the next queued run's "
                   "configuration itself, over this one")
    return out


def _same(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _change(name: str, now, new) -> dict:
    return {"name": name, "now": now, "new": new, "changes": not _same(now, new)}


def changes(client, config_type: str, settings: dict) -> list:
    """What a load would write, one line per setting, without writing."""
    root = config_loader.config_odb_paths[config_type]
    if config_type == "target_position":
        now = client.odb_get(root + "/Variables/Demand")
        return [_change("x [mm]", now[0], settings["xpos"]),
                _change("y [mm]", now[1], settings["ypos"])]
    if config_type == "degrader_position":
        now = client.odb_get(root + "/Variables/Demand")
        return [_change("x [mm]", now, settings["xpos"])]

    # Beamline: the channels config_loader.load_beam_config would write, by
    # the same name it matches on.
    names = client.odb_get(root + "/Settings/CA Name")
    demand_suffix = client.odb_get(root + "/Settings/CA Demand")
    dev_type = client.odb_get(root + "/Settings/Device type")
    demand = client.odb_get(root + "/Variables/Demand")
    out = []
    for i, (name, suffix) in enumerate(zip(names, demand_suffix)):
        if dev_type[i] not in config_loader.WRITEABLE_DEVICE_TYPES:
            continue
        channel = f"{name}{suffix}"
        out.append(_change(channel, demand[i], settings.get(channel)))
    return out


def _arrival(requirement) -> list:
    """The conditions a setter returned, as plain data for the page."""
    if requirement is None:
        return []
    if hasattr(requirement, "requirements"):
        out = []
        for r in requirement.requirements:
            out.extend(_arrival(r))
        return out
    return [{"path": requirement.path, "op": requirement.op,
             "target": requirement.target, "upper": requirement.upper}]


def preview(client, view, config_id: int) -> dict:
    config_type, settings = settings_of(_read(view, config_id))
    return {"config_id": config_id, "config_type": config_type,
            "changes": changes(client, config_type, settings),
            "warnings": warnings(client)}


def load(client, view, config_id: int, who: str = "ConfigDB page") -> dict:
    """Write the Demand values and return without waiting for them."""
    config_type, settings = settings_of(_read(view, config_id))
    warn = warnings(client)
    planned = changes(client, config_type, settings)
    setter = config_loader.config_dispatch[config_type]
    try:
        requirement = setter(client, config_type, settings)
    except (KeyError, ValueError, RuntimeError) as exc:
        client.msg(f"{who}: go to configuration {config_id} ({config_type}) "
                   f"failed: {exc}", is_error=True)
        raise GotoError("usage", str(exc)) from exc

    n = sum(1 for c in planned if c["changes"])
    line = (f"{who}: loaded configuration {config_id} ({config_type}) into the ODB, "
            f"{n} setting{'' if n == 1 else 's'} changed")
    if warn:
        line += " (" + "; ".join(warn) + ")"
    client.msg(line, is_error=False)

    timeout = getattr(requirement, "timeout", None)
    return {"config_id": config_id, "config_type": config_type,
            "changes": planned, "warnings": warn,
            "arrival": _arrival(requirement),
            "stable_for": getattr(requirement, "stable_for", None),
            "timeout": 60 if timeout is None else timeout}


def main(argv=None) -> int:
    """The manual path: preview, confirm, load, and wait for arrival."""
    import argparse
    import time

    import midas.client

    from pioneer.rundb.view import RunDbView

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("config_id", type=int)
    p.add_argument("--dsn", default=None, help="read-only run DB DSN "
                   "(default: $PIONEER_RUNDB_DSN)")
    p.add_argument("--yes", action="store_true", help="do not ask before loading")
    a = p.parse_args(argv)

    view = RunDbView(dsn=a.dsn)
    client = midas.client.MidasClient("RunDBGoto")
    try:
        pre = preview(client, view, a.config_id)
        print(f"configuration {a.config_id} ({pre['config_type']}):")
        for c in pre["changes"]:
            if c["changes"]:
                print(f"  {c['name']:<24} {c['now']} -> {c['new']}")
        print(f"  ({sum(not c['changes'] for c in pre['changes'])} settings already there)")
        for w in pre["warnings"]:
            print(f"WARNING: {w}")
        if not a.yes and input("load it? [y/N] ").strip().lower() != "y":
            return 1
        done = load(client, view, a.config_id, who="RunDBGoto (command line)")
        t0 = time.monotonic()
        while time.monotonic() - t0 < done["timeout"]:
            if all(_met(client.odb_get(r["path"]), r) for r in done["arrival"]):
                print(f"arrived after {time.monotonic() - t0:.0f} s")
                return 0
            time.sleep(1)
        print(f"not there after {done['timeout']} s; check the equipment pages")
        return 2
    except GotoError as exc:
        print(f"refused: {exc}")
        return 1
    finally:
        view.close()
        client.disconnect()


def _met(value, r) -> bool:
    if r["op"] == "==":
        return _same(value, r["target"])
    return r["target"] <= value <= r["upper"]


if __name__ == "__main__":
    raise SystemExit(main())
