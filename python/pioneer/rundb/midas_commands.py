
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

def schedule_configuration(config):
    iface = db_iface(user = "shifter", password = config.get("password"))
    num_ev = config.get("events", 10000)
    author = config.get("operator", "RPC Callback")
    desc   = config.get("description", "RPC Callback")
    quality = config.get("quality", "")
    mrs_target = midas_run_sequence(iface, num_ev = num_ev, author = author, description= "XY", quality = quality)
    mrs_degrad = midas_run_sequence(iface, num_ev = num_ev, author = author, description= "Degrader", quality = quality)
    mrs_beam = midas_run_sequence(iface, num_ev = num_ev, author = author, description = desc + "\nSequence: Beam", quality = quality)

    for row in config['config']:
        table, id = row.split(":")
        print(row, table, id)
        if table == "target_position":
            mrs_target.add_config_id(table, id)
        elif table == "degrader_position":
            mrs_degrad.add_config_id(table, id)
        elif table in ('pim1_epics', 'pie5_epics'):
            mrs_beam.add_config_id(table, id)
        else:
            raise CommandError("unknown_table", f"Unknown table {table} in config, skipping.")

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

def call(client : MidasClient, cmd : str, args, view = None):
    if cmd in ("goto_preview", "goto_config"):
        return _goto(client, view, cmd, args)
    if cmd == "generate_sequence":
        data = schedule_configuration(json.loads(args))
    elif cmd == "add_config_from_odb":
        table = args.get("table")
        comment = args.get("comment")
        if not table or not comment:
            raise CommandError("Syntax error, add_config_from_odb requires 'table' and 'comment'")
        data = write_config(client, table, comment)

    return json.dumps({"data" : data}, default=str, ensure_ascii=True)
