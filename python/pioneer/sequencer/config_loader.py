from midas.sequencer import SequenceClient
import midas

"""
Load a single value directly to the specified ODB path
This does not include a validation stage and we immediately
return an empty list of validation requirements.
"""
def load_value(seq : SequenceClient, cfg_key : str,  value : int):
    seq.odb_set(config_odb_paths[cfg_key], value)
    return []


"""
Load the arcus stage configuration. This is currently a single value.
Note that the run db configuration specifies a position in mm,
the midas FE expects this in motor steps.
"""
def load_arcus_config(seq : SequenceClient, cfg_key : str, aConfig : dict):
    num_steps_per_mm = 800 # This value requires validation
    xpos_in_steps = int(aConfig['xpos'] * num_steps_per_mm)

    # Set Demand Value
    seq.odb_set(config_odb_paths[cfg_key] + "/Variables/Demand", xpos_in_steps)

    return [
        {
            "path"         : config_odb_paths[cfg_key] + "/Variables/Measured",
            "op"           : "==",
            "target"       : xpos_in_steps,
            "timeout_secs" : 60
        }
    ]

def load_isel_config(seq : SequenceClient, cfg_key : str,   aConfig : dict):
    return []

def load_beam_config(seq : SequenceClient, cfg_key : str,  aConfig : dict):
    return []

def non_exist_warn(seq : SequenceClient, cfg_key : str, aConfig : dict):
    seq.sequencer_msg(f"No method to load configuration {cfg_key} is available", wait = True)
    raise ValueError(f"No method to load configuration {cfg_key} is available")

# this is the main entrance routine for loading configurations
config_dispatch = {
    "job_id"            : load_value,
    "degrader_position" : load_arcus_config,
    "target_position"   : load_isel_config,
    "pie5_epics"        : load_beam_config,
    "pim1_epics"        : load_beam_config
}

config_odb_paths = {
    "job_id"            : "/Runinfo/Run DB PK",
    "degrader_position" : "/Equipment/Degrader",
    "target_position"   : "",
    "pie5_epics"        : "",
    "pim1_epics"        : ""
}

def load_config(seq : SequenceClient, config : dict, sequential = True):
    end_waits = []
    for cfg_key in config.keys():
        wait_conditions = config_dispatch.get(cfg_key, non_exist_warn)(seq, cfg_key, config[cfg_key])
        if sequential:
            for wc in wait_conditions:
                seq.wait_odb(**wc)
        else:
            end_waits.extend(wait_conditions)

    # We iterate the end_waits in sequential and parallel mode
    # While not envisioned now, we might at some point add things
    # to it, even in sequential mode.
    for wc in end_waits:
        seq.wait_odb(**wc)
