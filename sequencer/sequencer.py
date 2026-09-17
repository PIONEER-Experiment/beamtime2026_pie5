from midas.sequencer import SequenceClient
import midas

from pioneer.rundb.interface import interface
from pioneer.sequencer.config_loader import load_config

db_interface = interface(user = "bot", password = "bot")

def load_config_to_odb(seq : SequenceClient):
    aConfig = db_interface.find_next_run_config()
    if aConfig is None:
        return False
    load_config(seq, aConfig, sequential = True)
    return True

def execute_run(seq : SequenceClient):
    seq.start_run()
    run_id = seq.odb_get("/Runinfo/Run DB PK")
    run_nr = seq.odb_get("/Runinfo/Run number")
    num_ev = seq.odb_get("/Runinfo/Req number events")
    # call start of midas run here as fail save.
    # The nearline daemon should have registered during transition
    db_interface.start_of_midas_run(run_id, run_nr)

    # This is where the actual run happens.
    # The wait_seconds needs to be replaced by a more reasonable
    # wait until completion logic, e.g. total number of events
    # sent by a specific frontend or some integrated beam quantity.
    seq.wait_seconds(numOfEvents)
    seq.stop_run()
    # Again, fail save as the nearline daemon should have scheduled
    # picked up everything during transition.
    db_interface.end_of_midas_run(run_id)
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
