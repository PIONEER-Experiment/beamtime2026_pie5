from midas.sequencer import SequenceClient

from  pioneer.sequencer.config_writer import write_epics

def define_params(seq : SequenceClient):
    seq.register_param("table", "table", "pim1_epics")
    seq.register_param("src", "source", "epics")

def sequence(seq: SequenceClient):
    source = seq.get_param("src")
    if source == "epics":
        write_epics(seq)
    # more configs can be added here

def at_exit(seq: SequenceClient):
    pass
