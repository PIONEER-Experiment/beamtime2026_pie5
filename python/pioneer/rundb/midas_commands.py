
from pioneer.nearline.run import midas_run_sequence
from pioneer.rundb.interface import interface as db_iface
from pioneer.rundb.commands import CommandError
from pioneer.rundb import commands, goto
from pioneer.sequencer.config_writer import write_config
from midas.client import MidasClient
import json

MidasCommands = [
    "generate_sequence",
    "add_config_from_odb",
    "goto_preview",
    "goto_config",
]

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
    if table in ('pim1_epics', 'pie5_epics'):
        return "beamline"
    raise CommandError("usage", f"Unknown table {table} in config")


def _split_selection(config) -> tuple[dict, set]:
    """{level: [(table, id), ...]} and the set of levels kept at their current setting.

    Every level must be either selected or marked current, never both and never
    neither, so a forgotten selection cannot turn silently into "leave it".
    """
    current = set(config.get("current") or [])
    unknown = current - set(SEQUENCE_LEVELS)
    if unknown:
        raise CommandError("usage", f"Unknown level(s) marked current: {', '.join(sorted(unknown))}")
    selected = {level: [] for level in SEQUENCE_LEVELS}
    for row in config.get("config") or []:
        table, id = row.split(":")
        selected[_level_of(table)].append((table, id))
    for level in SEQUENCE_LEVELS:
        if level in current and selected[level]:
            raise CommandError("usage", f"{level}: configurations selected and marked current setting; pick one")
        if level not in current and not selected[level]:
            raise CommandError("usage", f"{level}: select at least one configuration or mark it current setting")
    if current == set(SEQUENCE_LEVELS):
        raise CommandError("usage", "Every level is marked current setting, so there is nothing to schedule")
    return selected, current


def schedule_configuration(config, iface = None):
    selected, current = _split_selection(config)
    if iface is None:
        iface = db_iface(user = "shifter", password = config.get("password"))
    num_ev = config.get("events", 10000)
    author = config.get("operator", "RPC Callback")
    desc   = config.get("description", "RPC Callback")
    quality = config.get("quality", "")

    def label(name, level):
        return name + " (current)" if level in current else name

    mrs_target = midas_run_sequence(iface, num_ev = num_ev, author = author, description = label("XY", "target"), quality = quality)
    mrs_degrad = midas_run_sequence(iface, num_ev = num_ev, author = author, description = label("Degrader", "degrader"), quality = quality)
    mrs_beam = midas_run_sequence(iface, num_ev = num_ev, author = author, description = desc + "\nSequence: " + label("Beam", "beamline"), quality = quality)

    # A level with no configuration iterates once and adds nothing to the run,
    # so the chain stays beam -> degrader -> target whatever is kept current
    # (and the description, which lives on the beam level, is never lost).
    sequence_of = {"target": mrs_target, "degrader": mrs_degrad, "beamline": mrs_beam}
    for level, rows in selected.items():
        for table, id in rows:
            sequence_of[level].add_config_id(table, id)

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

def _generate_sequence(args):
    """generate_sequence in the standard envelope, so the page can show why it refused."""
    cmd = "generate_sequence"
    try:
        data = schedule_configuration(json.loads(args) if isinstance(args, str) else (args or {}))
    except CommandError as exc:
        return json.dumps(commands.error_envelope(cmd, exc.kind, exc.message))
    except Exception as exc:  # noqa: BLE001 - the page must get JSON whatever happens
        return json.dumps(commands.error_envelope(cmd, "internal", f"{exc.__class__.__name__}: {exc}"))
    return json.dumps(commands.ok_envelope(cmd, data, 0), default=str)

def call(client : MidasClient, cmd : str, args, view = None):
    if cmd in ("goto_preview", "goto_config"):
        return _goto(client, view, cmd, args)
    if cmd == "generate_sequence":
        return _generate_sequence(args)
    elif cmd == "add_config_from_odb":
        table = args.get("table")
        comment = args.get("comment")
        if not table or not comment:
            raise CommandError("Syntax error, add_config_from_odb requires 'table' and 'comment'")
        data = write_config(client, table, comment)

    return json.dumps({"data" : data}, default=str, ensure_ascii=True)
