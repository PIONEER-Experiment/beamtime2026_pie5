
from pioneer.nearline.run import midas_run_sequence
from pioneer.rundb.interface import interface as db_iface
from pioneer.rundb.commands import CommandError
from pioneer.rundb import commands, goto, restore
from pioneer.sequencer.config_writer import write_config
from midas.client import MidasClient
import json

MidasCommands = [
    "generate_sequence",
    "add_config_from_odb",
    "goto_preview",
    "goto_config",
    "restore_preview",
    "restore_run",
]

# The tables a beamline configuration lives in, one per beamline.
BEAMLINE_TABLES = ("pim1_epics", "pie5_epics")

# The three levels of a ConfigDB sequence, outermost first: beamline, then
# degrader, then target. A level the page marks "current" gets no configuration
# at all, so its runs carry no row for that table and the sequencer leaves the
# equipment where it is (config_loader.load_config only loads what a run has).
SEQUENCE_LEVELS = ("beamline", "degrader", "target")


def _level_of(table : str) -> str:
    if table == "target_position":
        return "target"
    if table == "degrader_position":
        return "degrader"
    if table in BEAMLINE_TABLES:
        return "beamline"
    raise CommandError("usage", f"Unknown table {table} in config")


def _from_run(config) -> dict | None:
    """The beamline table's "settings from run N" row, checked: {run, include, table} or None."""
    fr = config.get("from_run")
    if not fr:
        return None
    try:
        run = restore.parse_run(fr.get("run") if isinstance(fr, dict) else None)
    except goto.GotoError:
        raise CommandError("usage", "settings from run: give the run number in the restore line below the beamline table")
    table = fr.get("table")
    if table not in BEAMLINE_TABLES:
        raise CommandError("usage", f"settings from run {run}: unknown beamline table {table}")
    return {"run": run, "include": fr.get("include") or [], "table": table}


def _split_selection(config) -> tuple[dict, set, dict | None]:
    """{level: [(table, id), ...]}, the set of levels kept at their current setting,
    and the "settings from run N" row if it is ticked.

    Every level must be either selected or marked current, never both and never
    neither, so a forgotten selection cannot turn silently into "leave it".  The
    "settings from run N" row counts as a beamline selection.
    """
    from_run = _from_run(config)
    current = set(config.get("current") or [])
    unknown = current - set(SEQUENCE_LEVELS)
    if unknown:
        raise CommandError("usage", f"Unknown level(s) marked current: {', '.join(sorted(unknown))}")
    selected = {level: [] for level in SEQUENCE_LEVELS}
    for row in config.get("config") or []:
        table, id = row.split(":")
        selected[_level_of(table)].append((table, id))
    if from_run and "beamline" in current:
        raise CommandError("usage", f"beamline: settings from run {from_run['run']} ticked and marked current setting; pick one")
    for level in SEQUENCE_LEVELS:
        has = selected[level] or (level == "beamline" and from_run)
        if level in current and has:
            raise CommandError("usage", f"{level}: configurations selected and marked current setting; pick one")
        if level not in current and not has:
            raise CommandError("usage", f"{level}: select at least one configuration or mark it current setting")
    if current == set(SEQUENCE_LEVELS):
        raise CommandError("usage", "Every level is marked current setting, so there is nothing to schedule")
    return selected, current, from_run


def _add_from_run(client, iface, from_run) -> tuple[str, int]:
    """Store run N's settings as a new beamline configuration: (table, config id).

    The row is complete, as config_writer.write_epics would write it, so the
    sequencer loads it like any other (restore.config_values).  Everything that
    can refuse runs before the insert, so a refusal leaves no configuration behind.
    """
    if client is None:
        raise CommandError("internal", "settings from run: no MIDAS client to read the ODB with")
    try:
        values, comment = restore.config_values(client, from_run["run"], from_run["include"])
    except goto.GotoError as exc:
        raise CommandError(exc.kind, f"settings from run {from_run['run']}: {exc}") from exc
    config_id = iface.add_new_configuration(table = from_run["table"], values = values, comment = comment)
    if config_id is None:
        raise CommandError("db", f"settings from run {from_run['run']}: the configuration was not stored")
    return from_run["table"], config_id


class _CountingIface:
    """The run-DB interface, noting the runs it scheduled, so that a failure
    half-way through a sequence can say whether any run exists."""

    def __init__(self, iface):
        self._iface = iface
        self.scheduled = []

    def schedule_new_run(self, *args, **kwargs):
        run_id = self._iface.schedule_new_run(*args, **kwargs)
        self.scheduled.append(run_id)
        return run_id

    def __getattr__(self, name):
        return getattr(self._iface, name)


def schedule_configuration(config, iface = None, client = None):
    selected, current, from_run = _split_selection(config)
    if iface is None:
        iface = db_iface(user = "shifter", password = config.get("password"))
    if not from_run:
        return _schedule(config, iface, selected, current)

    table, config_id = _add_from_run(client, iface, from_run)
    counting = _CountingIface(iface)
    try:
        data = _schedule(config, counting, selected, current, extra_beamline = (table, config_id))
    except Exception as exc:  # noqa: BLE001 - the stored row has to be named, whatever went wrong
        # The configuration is in the database now. Say which one, and if no
        # run uses it, mark it do_not_use so nobody picks it up by mistake.
        what = f"settings from run {from_run['run']} stored as configuration {config_id} but not scheduled"
        if counting.scheduled:
            what += f" ({len(counting.scheduled)} run(s) were scheduled before the failure)"
        else:
            try:
                iface.set_do_not_use(config_id)
                what += f"; configuration {config_id} is marked do_not_use"
            except Exception as mark_exc:  # noqa: BLE001
                what += f"; marking it do_not_use failed too ({mark_exc.__class__.__name__}: {mark_exc})"
        message = exc.message if isinstance(exc, CommandError) else f"{exc.__class__.__name__}: {exc}"
        kind = exc.kind if isinstance(exc, CommandError) else "internal"
        raise CommandError(kind, f"{what}: {message}") from exc
    data["from_run_config"] = config_id
    return data


def _schedule(config, iface, selected, current, extra_beamline = None):
    """The sequence of runs for the selected configurations: {"runs": [...]}.

    `extra_beamline` is the (table, id) of a configuration made just now (the
    "settings from run N" row), added by id: like a selected row, only its id
    goes into the run (midas_run.schedule takes `c['id']`), so there is nothing
    to read back."""
    num_ev = config.get("events", 10000)
    author = config.get("operator", "RPC Callback")
    desc   = config.get("description", "RPC Callback")
    quality = config.get("quality", "")
    merge = config.get("merge", False)

    def label(name, level):
        return name + " (current)" if level in current else name

    mrs_target = midas_run_sequence(iface, num_ev = num_ev, author = author, description = label("XY", "target"), quality = quality)
    if merge:
        mrs_target.set_on_complete("merge")
    mrs_degrad = midas_run_sequence(iface, num_ev = num_ev, author = author, description = label("Degrader", "degrader"), quality = quality)
    mrs_beam = midas_run_sequence(iface, num_ev = num_ev, author = author, description = desc + "\nSequence: " + label("Beam", "beamline"), quality = quality)

    # A level with no configuration iterates once and adds nothing to the run,
    # so the chain stays beam -> degrader -> target whatever is kept current
    # (and the description, which lives on the beam level, is never lost).
    sequence_of = {"target": mrs_target, "degrader": mrs_degrad, "beamline": mrs_beam}
    for level, rows in selected.items():
        for table, id in rows:
            sequence_of[level].add_config_id(table, id)
    if extra_beamline is not None:
        mrs_beam.add_config_list(extra_beamline[0], [{"id": extra_beamline[1]}])

    mrs_degrad.set_subsequence(mrs_target)
    mrs_beam.set_subsequence(mrs_degrad)

    return {"runs": mrs_beam.schedule()}

def _goto(client : MidasClient, view, cmd : str, args):
    """goto_preview / goto_config, answered in the page's standard envelope."""
    try:
        parsed = json.loads(args) if isinstance(args, str) else (args or {})
        config_id = int(parsed.get("config_id"))
    except (TypeError, ValueError):
        return json.dumps(commands.error_envelope(cmd, "usage", f"{cmd} needs a numeric config_id"))
    try:
        if cmd == "goto_preview":
            data = goto.preview(client, view, config_id)
        else:
            data = goto.load(client, view, config_id)
    except goto.GotoError as exc:
        return json.dumps(commands.error_envelope(cmd, exc.kind, str(exc)))
    except Exception as exc:  # noqa: BLE001 - the page must get JSON whatever happens
        return json.dumps(commands.error_envelope(cmd, "internal", f"{exc.__class__.__name__}: {exc}"))
    return json.dumps(commands.ok_envelope(cmd, data, 0), default=str)

def _restore(client : MidasClient, cmd : str, args):
    """restore_preview / restore_run, answered in the page's standard envelope."""
    try:
        parsed = json.loads(args) if isinstance(args, str) else (args or {})
        run = restore.parse_run(parsed.get("run"))
        include = parsed.get("include") or []
    except (AttributeError, TypeError, ValueError, goto.GotoError):
        return json.dumps(commands.error_envelope(cmd, "usage", f"{cmd} needs a run number (a positive integer)"))
    try:
        if cmd == "restore_preview":
            data = restore.preview(client, run, include)
        else:
            data = restore.load(client, run, include)
    except goto.GotoError as exc:
        return json.dumps(commands.error_envelope(cmd, exc.kind, str(exc)))
    except Exception as exc:  # noqa: BLE001 - the page must get JSON whatever happens
        return json.dumps(commands.error_envelope(cmd, "internal", f"{exc.__class__.__name__}: {exc}"))
    return json.dumps(commands.ok_envelope(cmd, data, 0), default=str)

def _generate_sequence(client, args):
    """generate_sequence in the standard envelope, so the page can show why it refused."""
    cmd = "generate_sequence"
    try:
        data = schedule_configuration(json.loads(args) if isinstance(args, str) else (args or {}),
                                      client = client)
    except CommandError as exc:
        return json.dumps(commands.error_envelope(cmd, exc.kind, exc.message))
    except Exception as exc:  # noqa: BLE001 - the page must get JSON whatever happens
        return json.dumps(commands.error_envelope(cmd, "internal", f"{exc.__class__.__name__}: {exc}"))
    return json.dumps(commands.ok_envelope(cmd, data, 0), default=str)

def call(client : MidasClient, cmd : str, args, view = None):
    if cmd in ("goto_preview", "goto_config"):
        return _goto(client, view, cmd, args)
    if cmd in ("restore_preview", "restore_run"):
        return _restore(client, cmd, args)
    if cmd == "generate_sequence":
        return _generate_sequence(client, args)
    elif cmd == "add_config_from_odb":
        table = args.get("table")
        comment = args.get("comment")
        if not table or not comment:
            raise CommandError("Syntax error, add_config_from_odb requires 'table' and 'comment'")
        data = write_config(client, table, comment)

    return json.dumps({"data" : data}, default=str, ensure_ascii=True)
