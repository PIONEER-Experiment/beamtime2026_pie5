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
    odb_path = config_odb_paths[cfg_key]
    ch_names = seq.odb_get(odb_path + "/Settings/Names")
    ca_demand = seq.odb_get(odb_path + "/Settings/CA Demand")
    thresholds = seq.odb_get(odb_path + "/Settings/Update Threshold Measured")
    demand_vals = seq.odb_get(odb_path + "/Variables/Demand")

    if len(ch_names) != len(ca_demand):
        raise RuntimeError("ODB Corrupted: EPICS names and CA Demand arrays have different dimension")
    elif len(ch_names) != len(thresholds):
        raise RuntimeError("ODB Corrupted: EPICS names and Update Threshold Measured arrays have different dimension")
    elif len(ch_names) != len(demand_vals):
        raise RuntimeError("ODB Currupted: EPCIS names and demand values have different dimensions")

    missing = set(aConfig.keys()) - set(ch_names)
    if missing:
        raise ValueError(f"Requested config keys have no ODB counterpart: {missing}")

    for ch_index, this_name in enumerate(ch_names):
        if not ca_demand[ch_index]:
            # informative channel we can't write.
            # Those should not participate in configuration writing.
            continue
        this_val  = aConfig.get(this_name, None)
        if this_val is None:
            raise ValueError(f"Missing configuration entry for {this_name}")
        demand_vals[ch_index] = this_val

    seq.odb_set(odb_path + "/Variables/Demand", demand_vals)
    return [
        {
            "path"                : odb_path + f"/Variables/Measured[{i}]",
            "op"                  : "between",
            "target"              : demand_vals[i] - thresholds[i],
            "between_uper_target" : demand_vals[i] + thresholds[i],
            # Require the value to be stable in case at some point,
            # we decide to use some cycling sequence and we'll be
            # moving past the value first to approach from the other side.
            "stable_for_n_secs"   : 5,
            "timeout_secs"        : 60
        }
        for i, requestable in enumerate(ca_demand) if requestable
    ]

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
    "pie5_epics"        : "/Equipment/EPICS",
    "pim1_epics"        : "/Equipment/EPICS"
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
