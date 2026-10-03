from midas.sequencer import SequenceClient
import midas

from pioneer.rundb.interface import interface
from pioneer.sequencer.config_loader import load_config
from pioneer.sequencer.mupix_recovery import recover as recover_mupix_pll

db_interface = interface(user = "bot", password = "bot")

def wait_for_operator(seq : SequenceClient, text, name = "Seq operator"):
    # Warning class: yellow banner on every MIDAS page plus the alarm sound,
    # and no execute command (the Alarm class posts to Slack every 120 s).
    seq.trigger_internal_alarm(name, text, default_alarm_class = "Warning")
    try:
        # Blocks until the operator presses OK on the Sequencer page
        # (or the sequence is stopped).
        seq.sequencer_msg(text, wait = True)
    finally:
        seq.reset_alarm(name)

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
    seq.register_param("waitBeforeRun", "Wait for operator OK before each run", False)
    seq.register_param("mupixRecovery", "Check MuPix chips before each run, reset the PLL of bad ones", True)
    seq.register_param("mupixMaxRetries", "PLL reset rounds before asking the operator (max 3)", 3)

def sequence(seq: SequenceClient):
    while True:
        seq.wait_odb("/Runinfo/State", "==", midas.STATE_STOPPED)
        loaded = load_config_to_odb(seq)
        if not loaded:
            seq.wait_seconds(5)
            continue
        if seq.get_param("mupixRecovery"):
            # Resets only chips that fail the check; alarms if any is still bad, there is
            # no verdict (PCLS stale/unreadable) or it was aborted. The run goes ahead after OK.
            try:
                result = recover_mupix_pll(seq, seq.get_param("mupixMaxRetries"))
                text = None if result.ok else result.operator_message()
            except Exception as e:
                # Matched by name: the sequencer runs as `python -m midas.sequencer`, so the
                # Stop it raises is __main__.StopSequencerException, not the importable class.
                if type(e).__name__ == "StopSequencerException":
                    raise
                seq.msg(f"MuPix PLL recovery: failed: {e!r}", is_error = True)
                text = f"MuPix PLL recovery failed ({e}). Check the chips by hand, then press OK to continue."
            if text:
                wait_for_operator(seq, text)
        if seq.get_param("waitBeforeRun"):
            run_id = seq.odb_get("/Nearline/Info/Run DB PK")
            wait_for_operator(seq, f"Run DB config {run_id} loaded. Press OK to start the run.")
        wait_for_operator(seq, "Please check the PLL Lock is ok and click ok")
        run_successful = execute_run(seq)
        if not run_successful:
            break

def at_exit(seq: SequenceClient):
    seq.sequencer_msg("End of Sequencer has been reached.", wait = True)
