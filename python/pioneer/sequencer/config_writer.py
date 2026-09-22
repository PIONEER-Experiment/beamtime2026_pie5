from midas.sequencer import SequenceClient
from pioneer.rundb.interface import interface


def write_epics(seq : SequenceClient):
    writeable_device_types = [
        1, # Magnets
        4, # Separator
        5, # Slits
    ]
    # While 2 (Beam blocker) is a writeable device type, we don't configure it automatically.
    # It has to be a user decision to block the beam or not.
    # Therefore, we don't write it to the run db.
    # 3 (PSA) and 6 (Value) are not considerd writable

    odb_path    = "/Equipment/EPICS"
    ca_names    = seq.odb_get(odb_path + "/Settings/CA Name")
    ca_demand   = seq.odb_get(odb_path + "/Settings/CA Demand")
    dev_type    = seq.odb_get(odb_path + "/Settings/Device type")
    demand_vals = seq.odb_get(odb_path + "/Variables/Demand")
    ch_names = [f"{cn}{cd}" for cn, cd in zip(ca_names, ca_demand)]
    aConfig = {}

    for ch_index in range(len(ch_names)):
        this_name = ch_names[ch_index]
        if dev_type[ch_index] not in writeable_device_types:
            # informative channel we can't write.
            # Those should not participate in configuration writing.
            continue
        aConfig[this_name] = demand_vals[ch_index]

    iface = interface(user = "bot", password = "bot")
    return iface.add_new_configuration(table = seq.get_param("table"), values = aConfig)
