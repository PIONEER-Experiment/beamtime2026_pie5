

import itertools
from pioneer.rundb.interface import interface as db_interface

class midas_run_sequence:
    def __init__(self, iface : db_interface, num_ev : int | None = None):
        self.this_sequence = dict()
        self.the_sub_sequence = None
        self.on_complete = None
        self.iface = iface
        self.num_ev = num_ev

    def add_config_list(self, name : str, config : list[dict]):
        if name in self.this_sequence.keys():
            self.this_sequence[name] += config
        else:
            self.this_sequence[name] = config

    def add_config_seq(self, name :str, seq_id : int):
        self.add_config_list(name, self.iface.load_config_sequence(name, seq_id))

    def add_config_id(self, name : str, config_id : int):
        cfg = self.iface.load_config(name, config_id)
        if cfg is not None:
            self.add_config_list(name, [cfg])

    def set_config_list(self, name : str, config : list[dict] | dict):
        self.this_sequence[name] = []
        self.add_config_list(name, config)

    def set_config_seq(self, name : str, seq_id : int):
        self.this_sequence[name] = []
        self.add_config_seq(name, seq_id)

    def set_config_id(self, name : str, config_id : int):
        self.this_sequence[name] = []
        self.add_config_id(name, config_id)

    def set_subsequence(self, sub):
        self.the_sub_sequence = sub

    def set_on_complete(self, on_complete : str):
        self.on_complete = on_complete

    def schedule(self):
        key_list = self.this_sequence.keys()
        run_list = list()
        for it in itertools.product(*[self.this_sequence[k] for k in key_list]):
            if self.the_sub_sequence is None:
                this_run = midas_run(self.iface, cfg = dict(zip(key_list, it)), num_ev = self.num_ev)
                run_list.append(this_run.schedule())
            else:
                for k, v in zip(key_list, it):
                    if (self.the_sub_sequence.num_ev is None):
                        self.the_sub_sequence.num_ev = self.num_ev;
                    self.the_sub_sequence.set_config_list(k, [v])
                run_list.extend(self.the_sub_sequence.schedule())
        self.iface.register_sequence(run_list, self.on_complete)
        return run_list

class midas_run:
    def __init__(self, iface : db_interface, from_existing_run  : None | int = None, cfg : None | dict = None, num_ev : int = 1e6):
        self.this_configuration = dict()
        self.num_ev = num_ev
        self.iface = iface

        if (from_existing_run is not None):
            self.load_from_db(from_existing_run)

        if cfg is not None:
            self.apply(cfg)

    def load_from_db(self, run_id : int):
        cfg = self.iface.load_run_config(run_id)
        self.apply(cfg)

    def apply(self, cfg : dict):
        for k,v in cfg.items():
            self.this_configuration[k] = v

    def write_config(self):
        for key, config in self.this_configuration.items():
            print(key, config)
            if 'id' in config.keys():
                # an ID has already been assigned. This implies that it
                # was likely loaded from the database or it was already
                # written. Either way, it already exists
                continue
            cfg_id = self.iface.add_new_configuration(key, config)
            config['id'] = cfg_id


    def schedule(self):
        # Call write config first - this will guarantee that each configuration has
        # an ID assigned and exists in the config tables.
        self.write_config()
        print(self.this_configuration)
        return self.iface.schedule_new_run([c['id'] for c in self.this_configuration.values()])


def five_point_sequence(iface : db_interface):
    mrs = midas_run_sequence(iface)
    mrs.set_config_seq("target_position", 2) # Default 5 point sequence is marked with sequence number 2 in the runDB
    mrs.set_on_complete("merge mt_add")
    return mrs

def degrader_scan(iface : db_interface):
    mrs = midas_run_sequence(iface)
    mrs.set_config_seq("degrader_position", 1) # Default degrader scan is marked with sequence number 1 in the runDB
    return mrs

if __name__ == "__main__":
    iface = db_interface("bot", "bot")

    dscan = degrader_scan(iface)
    dscan.set_subsequence(five_point_sequence(iface))

    mrs2 = midas_run_sequence(iface)
    mrs2.set_config_list("dummy", [{"p1" : "test1", "p2" : "test2"}, {"p1" : "test3", "p2" : "test4"}])
    mrs2.set_subsequence(dscan)

    mrs2.schedule()
