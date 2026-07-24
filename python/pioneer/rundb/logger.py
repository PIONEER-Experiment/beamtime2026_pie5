

import argparse
import logging
import time

from dataclasses import dataclass

import midas.client
from pioneer.rundb.interface import interface as db_iface

kMidasClientName = "pioneer_logger"
kMidasHostName = "localhost"
kMidasExptName = "test"

@dataclass(frozen=True)
class Channel:
    # SC equipment it belongs to
    equipment: str

    # Variable type it belongs to, e.g. DEMAND, OBSERVED
    variable: str

    # For array variables, the index of the channel
    index: int

    # Human-readable name, e.g. Beam Current instead of EPICS/DEMAND[2]
    name: str

    # Channel as observed by the SCFE
    @property
    def channel(self) -> str:
        return f"{self.equipment}/{self.variable}[{self.index}]"

    # Physics-oriented label
    @property
    def label(self) -> str:
        return f"{self.equipment}_{self.name}_{self.variable}"


class Logger:
    def __init__(self, args):
        self.db_iface = db_iface("bot", "bot")
        self.client = midas.client.MidasClient(
            client_name=args.midas_client,
            host_name=args.midas_host,
            expt_name=args.midas_expt,
        )
        self.sleep_time = 1000

        self.known_equipment     = dict()
        self.last_equipment_read = dict()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.client.disconnect()

    def log_channel_state_change(self, channel_list : set[Channel], this_run : int, change_time : float, state : str):
        log_entries = list()
        for it in channel_list:
            log_entries.append(
                {
                    "upd_time" : change_time,
                    "channel"  : it.channel,
                    "label"    : it.label,
                    "reading"  : None
                }
            )
        self.db_iface.log_sc_values(this_run, state, log_entries)

    def log_vanished_equipment(self, eq_name : str, this_run : int):
        # Someone erased the ODB entry for this equipment.
        # As this was not properly logged, assign the last
        # time communicated with it.
        self.log_channel_state_change(
            channel_list = self.known_equipment[eq_name],
            this_run = this_run,
            change_time = self.last_equipment_read.get(eq_name, time.time()),
            state = "VANISHED"
        )
        self.known_equipment.pop(eq_name)
        self.last_equipment_read.pop(eq_name)

    def log_disable_equipment(self, eq_name : str, this_run : int):
        disable_time = self.client.odb_last_update_time(f"/Equipment/{eq_name}/Common/Enabled").timestamp()
        self.log_channel_state_change(
            channel_list = self.known_equipment[eq_name],
            this_run = this_run,
            change_time = disable_time,
            state = "DISABLE"
        )
        self.known_equipment.pop(eq_name)

    def log_enable_equipment(self, eq_name : str, channel_list : set[Channel], this_run : int):
        enable_time = self.client.odb_last_update_time(f"/Equipment/{eq_name}/Common/Enabled").timestamp()
        self.log_channel_state_change(channel_list, this_run, enable_time, "ENABLE")

    def iter_channels(self, equips):
        for eq_name, val in equips.items():
            if not val["Common"]["Enabled"]:
                continue
            names = val.get("Settings", {}).get("Names", [])
            for var_name, values in val.get("Variables", {}).items():
                last_update = self.client.odb_last_update_time(f"/Equipment/{eq_name}/Variables/{var_name}").timestamp()
                for index, value in enumerate(values):
                    yield (
                        Channel(
                            equipment=eq_name,
                            variable=var_name,
                            index=index,
                            name=names[index] if index < len(names) else f"var{index}",
                        ),
                        value,
                        last_update
                    )

    def initialise_inventory(self, equips):
        for channel, _, _ in self.iter_channels(equips = equips):
            self.known_equipment.setdefault(channel.equipment, set()).add(channel)
        self.last_equipment_read = { eq: time.time() for eq in self.known_equipment.keys()}

    def update_inventory(self, equips):
        this_run = self.client.odb_get("/Runinfo/Run number")

        # check for vanished equipment first.
        # copy of key list such that we are not iterating
        # the dictionary directly and can remove obsolete
        # entries as we go.
        known_equipment_keys = list(self.known_equipment.keys())
        for eq_name in known_equipment_keys:
            if eq_name not in equips.keys():
                self.log_vanished_equipment(eq_name)

        equipment = dict()
        for channel, _, _ in self.iter_channels(equips = equips):
            equipment.setdefault(channel.equipment, set()).add(channel)
        for eq_name, val in equips.items():
            enabled = val['Common']['Enabled']
            if not enabled:
                if eq_name in self.known_equipment.keys():
                    # it was known in the past but now is disabled.
                    self.log_disable_equipment(eq_name, this_run)
                continue

            these_channels = equipment[eq_name]
            # check these_channels against known_equipment
            if eq_name not in self.known_equipment.keys():
                self.log_enable_equipment(eq_name, these_channels, this_run)
            else:
                if "Settings" in val.keys():
                    if "Names" in val['Settings'].keys():
                        change_time = self.client.odb_last_update_time(f"/Equipment/{eq_name}/Settings/Names")
                    else:
                        change_time = self.client.odb_last_update_time(f"/Equipment/{eq_name}/Settings")
                elif "Variables" in val.keys():
                    change_time = self.client.odb_last_update_time(f"/Equipment/{eq_name}/Variables")
                else:
                    change_time = self.client.odb_last_update_time(f"/Equipment/{eq_name}")

                added   = these_channels - self.known_equipment[eq_name]
                self.log_channel_state_change(added, this_run, change_time, "ENABLE")

                removed = self.known_equipment[eq_name] - these_channels
                self.log_channel_state_change(removed, this_run, change_time, "DISABLE")

            # store current time when we saw this equipment enabled.
            self.last_equipment_read[eq_name] = time.time()
        self.known_equipment = equipment

    def log(self, reason : str, equips = None):
        log_entries = list()
        this_run = self.client.odb_get("/Runinfo/Run number")
        if equips is None:
            equips = self.client.odb_get("/Equipment")

        for aChannel, aValue, last_update in self.iter_channels(equips = equips):
            log_entries.append({
                    "upd_time" : last_update,
                    "channel"  : aChannel.channel,
                    "label"    : aChannel.label,
                    "reading"  : aValue
                })

        self.db_iface.log_sc_values(this_run, reason, log_entries)

    def log_bor_callback(self, client, run_number):
        self.log(reason = "BOR")

    def log_eor_callback(self, client, run_number):
        self.log(reason = "EOR")

    # ---------------------------------------------------------------------
    def mainloop(self):
        self.initialise_inventory(self.client.odb_get("/Equipment"))
        self.client.register_transition_callback(
            transition = midas.TR_START,
            sequence = 999,
            callback = self.log_bor_callback
        )
        self.client.register_transition_callback(
            transition = midas.TR_STOP,
            sequence = 1,
            callback = self.log_eor_callback
        )

        while True:
            try:
                self.client.communicate(self.sleep_time)
                equips = self.client.odb_get("/Equipment")
                self.update_inventory(equips)
                self.log(reason = "UPDATE", equips = equips)
            except Exception:
                logging.exception("Logger iteration failed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--midas-client", default=kMidasClientName)
    parser.add_argument("--midas-host", default=kMidasHostName)
    parser.add_argument("--midas-expt", default=kMidasExptName)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    with Logger(args) as logger:
        logger.mainloop()


if __name__ == "__main__":
    main()
