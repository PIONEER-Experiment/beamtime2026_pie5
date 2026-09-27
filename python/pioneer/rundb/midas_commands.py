
from pioneer.nearline.run import midas_run_sequence
from pioneer.rundb.interface import interface as db_iface
from pioneer.rundb.commands import CommandError
from pioneer.sequencer.config_writer import write_config
from midas.client import MidasClient
import json

MidasCommands = [
    "generate_sequence",
    "add_config_from_odb"
]

def schedule_configuration(config):
    iface = db_iface(user = "shifter", password = config.get("password"))
    num_ev = config.get("events", 10000)
    mrs_target = midas_run_sequence(iface, num_ev = num_ev)
    mrs_degrad = midas_run_sequence(iface, num_ev = num_ev)
    mrs_beam = midas_run_sequence(iface, num_ev = num_ev)

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

def call(client : MidasClient, cmd : str, args):
    if cmd == "generate_sequence":
        data = schedule_configuration(json.loads(args))
    elif cmd == "add_config_from_odb":
        table = args.get("table")
        comment = args.get("comment")
        if not table or not comment:
            raise CommandError("Syntax error, add_config_from_odb requires 'table' and 'comment'")
        data = write_config(client, table, comment)

    return json.dumps({"data" : data}, default=str, ensure_ascii=True)
