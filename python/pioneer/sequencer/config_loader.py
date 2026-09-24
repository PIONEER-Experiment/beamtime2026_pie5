from midas.sequencer import SequenceClient
import pioneer.sequencer.config_validate as cfg_val

"""
Load a single value directly to the specified ODB path
This does not include a validation stage and we immediately
return an empty list of validation requirements.
"""
def load_value(seq : SequenceClient, cfg_key : str,  value : int):
    seq.odb_set(config_odb_paths[cfg_key], value)
    return None


"""
Load the arcus stage configuration. This is currently a single value.
Note that the run db configuration specifies a position in mm,
the midas FE will assume a mm value and convert it internally to motor steps.
"""
def load_arcus_config(seq : SequenceClient, cfg_key : str, aConfig : dict):
    xpos = aConfig['xpos']

    # Set Demand Value
    seq.odb_set(config_odb_paths[cfg_key] + "/Variables/Demand", xpos)

    return cfg_val.ODBRequirement(
                seq = seq,
                path = config_odb_paths[cfg_key] + "/Variables/Measured",
                op = "between",
                target =  xpos - 0.01,
                upper =   xpos + 0.01,
                timeout = 60
            )

def load_isel_config(seq : SequenceClient, cfg_key : str,   aConfig : dict):
    odb_path    = config_odb_paths[cfg_key]
    xpos        = aConfig['xpos']
    ypos        = aConfig['ypos']
    pos = (xpos, ypos)
    seq.odb_set(config_odb_paths[cfg_key] + "/Variables/Demand", pos)
    return cfg_val.ODBRequirementCollection(
        seq = seq,
        name = f"ISEL ({cfg_key})",
        requirements= [
            cfg_val.ODBRequirement(
                seq = seq,
                path = odb_path + f"/Variables/Measured[{i}]",
                op = "==",
                target = pos[i],
            )
            for i in range(2)
        ],
        timeout = 60
    )

def load_beam_config(seq : SequenceClient, cfg_key : str,  aConfig : dict):
    writeable_device_types = [
        1, # Magnets
        4, # Separator
        5, # Slits
    ]
    # 2 (Beam Blocker) is a security feature we are not writing to. Open/Close of beam blocker
    # is a shifter responsibility and should not be automated.
    # 3 (PSA) and 6 (Value) are not considerd writable

    odb_path    = config_odb_paths[cfg_key]
    ca_names    = seq.odb_get(odb_path + "/Settings/CA Name")
    ca_demand   = seq.odb_get(odb_path + "/Settings/CA Demand")
    dev_type    = seq.odb_get(odb_path + "/Settings/Device type")
    thresholds  = seq.odb_get(odb_path + "/Settings/Warning Threshold")
    demand_vals = seq.odb_get(odb_path + "/Variables/Demand")

    ch_names = [f"{cn}{cd}" for cn, cd in zip(ca_names, ca_demand)]

    if len(ch_names) != len(dev_type):
        raise RuntimeError("ODB Corrupted: EPICS names and device type arrays have different dimension")
    elif len(ch_names) != len(thresholds):
        raise RuntimeError("ODB Corrupted: EPICS names and Update Threshold Measured arrays have different dimension")
    elif len(ch_names) != len(demand_vals):
        raise RuntimeError("ODB Corrupted: EPCIS names and demand values have different dimensions")

    missing = set(aConfig.keys()) - set(ch_names)
    if missing:
        raise ValueError(f"Requested config keys have no ODB counterpart: {missing}")

    for ch_index, this_name in enumerate(ch_names):
        if dev_type[ch_index] not in writeable_device_types:
            # informative channel we can't write.
            # Those should not participate in configuration writing.
            continue
        this_val  = aConfig.get(this_name, None)
        if this_val is None:
            raise ValueError(f"Missing configuration entry for {this_name}")
        demand_vals[ch_index] = this_val

    seq.odb_set(odb_path + "/Variables/Demand", demand_vals)
    return cfg_val.ODBRequirementCollection(
        seq = seq,
        name = f"Beamline ({cfg_key})",
        requirements= [
            cfg_val.ODBRequirement(
                seq = seq,
                path = odb_path + f"/Variables/Measured[{i}]",
                op = "between",
                target = demand_vals[i] - thresholds[i],
                upper =  demand_vals[i] + thresholds[i],
            )
            for i, dt in enumerate(dev_type) if dt in writeable_device_types
        ],
        stable_for = 5,
        timeout = 60
    )

def non_exist_warn(seq : SequenceClient, cfg_key : str, aConfig : dict):
    seq.sequencer_msg(f"No method to load configuration {cfg_key} is available", wait = True)
    raise ValueError(f"No method to load configuration {cfg_key} is available")

# this is the main entrance routine for loading configurations
config_dispatch = {
    "job_id"            : load_value,
    "num_ev"            : load_value,
    "degrader_position" : load_arcus_config,
    "target_position"   : load_isel_config,
    "pie5_epics"        : load_beam_config,
    "pim1_epics"        : load_beam_config
}

config_odb_paths = {
    "job_id"            : "/Runinfo/Run DB PK",
    "num_ev"            : "/Runinfo/Req number events",
    "degrader_position" : "/Equipment/Degrader",
    "target_position"   : "/Equipment/XYTable",
    "pie5_epics"        : "/Equipment/EPICS",
    "pim1_epics"        : "/Equipment/EPICS"
}

def load_config(seq : SequenceClient, config : dict, sequential = True):
    end_require = cfg_val.ODBRequirementCollection(
        seq = seq,
        name = "Configuration",
        requirements = []
    )
    for cfg_key in config.keys():
        wait_conditions = config_dispatch.get(cfg_key, non_exist_warn)(seq, cfg_key, config[cfg_key])
        if wait_conditions:
            if sequential:
                wait_conditions.wait()
            else:
                end_require.requirements.append(wait_conditions)

    end_require.wait()
