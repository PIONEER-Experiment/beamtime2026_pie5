from midas.sequencer import SequenceClient

import pioneer.rundb.interface as interface
db_interface = interface(user = "bot", password = "bot")

def load_config_to_odb(seq : SequenceClient):
    aConfig = db_interface.load_config()
    if aConfig is None:
        return None
    seq.sequencer_msg(f"Loaded configuration {aConfig.__repr__()}")
    return aConfig['job_id']

def execute_run(seq : SequenceClient, job_id : int):
    numOfEvents = seq.get_param("nEv")
    seq.start_run()
    seq.wait_seconds(numOfEvents)
    seq.stop_run()
    run_id = seq.odb_get("/Runinfo/Run number")
    ret_val = db_interface.end_of_midas_run(job_id, run_id)
    return ret_val

def define_params(seq : SequenceClient):
    seq.register_param("nEv", "Number of Events", 15)

def sequence(seq: SequenceClient):
    while True:
        try:
            job_id = load_config_to_odb(seq)
            if job_id is None:
                seq.wait_seconds(5)
                continue
        except Exception as e:
            seq.msg(f"An exception occured: {e}")
            break
        run_successful = execute_run(seq, job_id)
        if not run_successful:
            break

def at_exit(seq: SequenceClient):
    seq.sequencer_msg("End of Sequencer has been reached.", wait = True)
