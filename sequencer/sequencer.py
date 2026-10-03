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
    requested_events = seq.odb_get("/Runinfo/Req number events")
    # The nearline daemon marks the run RUNNING (start_of_run_callback) and DONE
    # (end_of_run_callback) during the transitions, keyed on
    # /Nearline/Info/Run DB PK. A second start_of_midas_run here would raise,
    # since the run is no longer PENDING/CLAIMED.

    # This is where the actual run happens.
    # The wait_seconds needs to be replaced by a more reasonable
    # wait until completion logic, e.g. total number of events
    # sent by a specific frontend or some integrated beam quantity.
    while seq.odb_get("/Runinfo/State") == midas.STATE_RUNNING:
        current_events = seq.odb_get("/Equipment/WDWaveforms/Statistics/Events sent")
        if current_events > requested_events:
            seq.stop_run()
        else:
            seq.wait_seconds(1)
    return True

def define_params(seq : SequenceClient):
    pass

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
