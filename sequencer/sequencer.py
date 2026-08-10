from midas.sequencer import SequenceClient
import midas

from pioneer.rundb.interface import interface

# Constructed lazily: building it at import time would make a DB problem kill
# script *loading* in the PySequencer, where the operator cannot see why.
_db_interface = None

def db_interface() -> interface:
    global _db_interface
    if _db_interface is None:
        _db_interface = interface(user = "bot", password = "bot")
    return _db_interface

def load_config_to_odb(seq : SequenceClient):
    try:
        aConfig = db_interface().find_next_run_config()
    except Exception as e:
        # A transient DB outage must not kill the sequence loop; the caller
        # treats False as "nothing to do, retry shortly".
        seq.sequencer_msg(f"Run DB unavailable: {e!r}; retrying")
        return False
    if aConfig is None:
        return False

    # Push each configuration into the ODB under /PSM/Demand/<table>/<column>.
    # On the bench these are dummy stand-ins for the eventual EPICS writes;
    # keeping the table structure means the real actuation layer can replace
    # this loop without changing what the database hands us.
    job_id = aConfig["job_id"]
    for table, row in aConfig.items():
        if table == "job_id" or not isinstance(row, dict):
            continue
        for column, value in row.items():
            seq.odb_set(f"/PSM/Demand/{table}/{column}", value)

    summary = ", ".join(
        f"{t}({', '.join(f'{c}={v}' for c, v in r.items())})"
        for t, r in aConfig.items() if isinstance(r, dict))
    seq.sequencer_msg(f"Loaded configuration for run DB job {job_id}: {summary}")
    seq.odb_set("/Experiment/Run Parameters/Run Description", f"PSM job {job_id}: {summary}")
    seq.odb_set("/Runinfo/Run DB PK", int(job_id))
    return True

def execute_run(seq : SequenceClient):
    run_secs = seq.get_param("run_secs")
    seq.start_run()
    # Fixed-length runs: the nearline payload is rate-normalised over the
    # recorded span, so run length only affects statistics. If a
    # completion criterion is ever needed instead, replace this with e.g.
    # seq.wait_odb on a frontend's event counter.
    seq.wait_seconds(run_secs)
    seq.stop_run()
    return True

def define_params(seq : SequenceClient):
    seq.register_param("run_secs", "Run length (seconds)", 20)

def sequence(seq: SequenceClient):
    while True:
        seq.wait_odb("/Runinfo/State", "==", midas.STATE_STOPPED)
        loaded = load_config_to_odb(seq)
        if not loaded:
            seq.wait_seconds(5)
            continue
        run_successful = execute_run(seq)
        if not run_successful:
            break

def at_exit(seq: SequenceClient):
    seq.sequencer_msg("End of Sequencer has been reached.", wait = True)
