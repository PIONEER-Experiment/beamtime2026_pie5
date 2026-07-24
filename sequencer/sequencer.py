from midas.sequencer import SequenceClient
import midas

from pioneer.rundb.interface import interface
db_interface = interface(user = "bot", password = "bot")

def load_config_to_odb(seq : SequenceClient):
    aConfig = db_interface.find_next_run_config()
    if aConfig is None:
        return False
    seq.sequencer_msg(f"Loaded configuration {aConfig.__repr__()}")
    seq.odb_set("/Runinfo/Run DB PK", int(aConfig['job_id']))
    return True

def execute_run(seq : SequenceClient):
    numOfEvents = seq.get_param("nEv")
    seq.start_run()
    seq.wait_seconds(numOfEvents)
    seq.stop_run()
    return True

def define_params(seq : SequenceClient):
    seq.register_param("nEv", "Number of Events", 15)

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
