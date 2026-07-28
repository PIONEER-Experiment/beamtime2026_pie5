

import argparse
import datetime
import math

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
        return f"{self.variable}[{self.index}]"

    # Physics-oriented label
    @property
    def label(self) -> str:
        return f"{self.equipment}[{self.variable}]:{self.name}"

    @property
    def odb_path(self) -> str:
        return f"/Equipment/{self.equipment}/Variables/{self.variable}"
@dataclass
class ChannelCache:
    last_val : float
    last_upd : float

@dataclass
class Equipment:
    # Name of equipment
    # ODB path shall be /Equipment/<name>
    name : str

    # time at which it was last seen in the ODB
    last_read_time: datetime.datetime

    # channels, grouped by their variable odb path.
    channels : dict[str : set[Channel]]

    # cached values
    channel_cache : dict[Channel : ChannelCache]

    # other odb_paths this equipment is watching
    # this should not include the odb paths watched
    # for channel variables
    odb_paths_watched : list[str]

    enabled : bool

    @property
    def channel_list(self) -> set[Channel]:
        return set().union(*self.channels.values())

    @property
    def odb_path(self) -> str:
        return f"/Equipment/{self.name}"

def assert_list(obj) -> list:
    if isinstance(obj, list):
        return obj
    elif isinstance(obj, (str, bytes)):
        return [obj]
    try:
        return list(obj)
    except TypeError:
        return [obj]

def is_valid(obj):
    if obj is None:
        return False
    elif isinstance(obj, float):
        # This marks nan and +- inf as invalid values for update purpose.
        return math.isfinite(obj)

    return True
class Logger:
    def __init__(self, args):
        self.db_iface = db_iface("bot", "bot")
        self.client = midas.client.MidasClient(
            client_name=args.midas_client,
            host_name=args.midas_host,
            expt_name=args.midas_expt,
        )
        self.sleep_time = 1000

        self.known_equipment : dict[str : Equipment] = dict()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.client.disconnect()


    def enable_callback(self, client, path, value):
        # path will be /Equipment/<eq_name>/Common/Enabled
        if value:
            self.enable_equipment(
                self.known_equipment[path.split("/")[2]]
            )
        else:
            self.disable_equipment(
                self.known_equipment[path.split("/")[2]]
            )

    def name_update_callback(self, client, path, value):
        this_run = self.client.odb_get("/Runinfo/Run number")
        equip : Equipment = self.known_equipment[path.split("/")[2]]
        upd_time = self.client.odb_last_update_time(path)
        old_channels = equip.channel_list
        new_channel_cache = {ch: ChannelCache(v, t) for ch, v, t in self.iter_channels(equip.name)}
        new_channels = set(new_channel_cache.keys())

        added   = new_channels - old_channels
        removed = old_channels - new_channels

        self.db_iface.log_sc_values(this_run, "ENABLE", [{
            "upd_time"  : upd_time,
            "equipment" : it.equipment,
            "channel"   : it.channel,
            "label"     : it.label,
            "reading"   : new_channel_cache[it].last_val}
            for it in added])

        self.db_iface.log_sc_values(this_run, "DISABLE", [{
            "upd_time"  : upd_time,
            "equipment" : it.equipment,
            "channel"   : it.channel,
            "label"     : it.label,
            "reading"   : None}
            for it in removed])

        for it in removed:
            equip.channels[it.odb_path].discard(it)
            equip.channel_cache.pop(it)
        for it in added:
            equip.channels[it.odb_path].add(it)
            equip.channel_cache[it] = new_channel_cache[it]


    def value_update_callback(self, client, path, value):
        value_list = assert_list(value)
        equip : Equipment = self.known_equipment[path.split("/")[2]]
        upd_time = self.client.odb_last_update_time(path)
        channels = equip.channels.get(path, [])
        if (len(channels)) == 0:
            # There are no channels here, hence nothing to log
            return
        # The value path should be
        #  /Equipment/<eq_name>/Variables/<var_name>
        var_name = path.split("/")[-1]
        if (self.client.odb_exists(f"/Equipment/{equip.name}/Settings/Update Threshold {var_name}")):
            upd_thr = self.client.odb_get(f"/Equipment/{equip.name}/Settings/Update Threshold {var_name}")
        elif (self.client.odb_exists(f"/Equipment/{equip.name}/Settings/Update Threshold")):
            upd_thr = self.client.odb_get(f"/Equipment/{equip.name}/Settings/Update Threshold")
        else:
            # No update threshold provided. This is typical for demand/request values.
            # We assume that any change is user-driven and thereby should be logged.
            upd_thr = [ 0 for _ in value_list ]

        if len(upd_thr) < len(value_list):
            print("Size mismatch detected between update thresholds and value dimension for equipment", equip.name)
            print("padding with 0 in update threshold list.")
            upd_thr.extend([0] * (len(value_list) - len(upd_thr)))
        elif len(upd_thr) > len(value_list):
            print("Size mismatch detected between update thresholds and value dimension for equipment", equip.name)
            print("ignoring excess values.")

        log_entries = []
        for channel in channels:
            new_val = value_list[channel.index]
            if not is_valid(new_val):
                continue
            old_val = equip.channel_cache[channel].last_val
            thr     = upd_thr[channel.index]
            try:
                # Try a numeric difference
                diff = abs(old_val - new_val)
            except TypeError:
                # No numeric difference can be extracted,
                # any deviation shall be considered above threshold.
                above_thr = (old_val != new_val)
            else:
                # If you have valid numeric values to compute a difference
                # but you can't compare to the thr type, your ODB is in
                # places it should not be, which rightfully deserves a TypeError
                above_thr = diff > thr

            if above_thr or not is_valid(old_val):
                log_entries.append({
                    "upd_time"  : upd_time,
                    "equipment" : channel.equipment,
                    "channel"   : channel.channel,
                    "label"     : channel.label,
                    "reading"   : new_val
                })
                equip.channel_cache[channel].last_val = new_val
                equip.channel_cache[channel].last_upd = upd_time

        this_run = self.client.odb_get("/Runinfo/Run number")
        self.db_iface.log_sc_values(this_run, "UPDATE", log_entries)
        equip.last_read_time = datetime.datetime.now(datetime.timezone.utc)

    def create_equipment(self, eq_name):
        if not self.client.odb_exists(f"/Equipment/{eq_name}"):
            return

        # This creates a bare-bone equipment entry that will just suffice for a disabled
        # one. If the equipment is enabled, it will be properly filled down below.
        new_equip = Equipment(
            name = eq_name,
            last_read_time = datetime.datetime.now(datetime.timezone.utc),
            channels = dict(),
            channel_cache = dict(),
            odb_paths_watched = [],
            enabled = False
        )
        self.known_equipment[eq_name] = new_equip

        # enable hook
        self.client.odb_watch(
            path = f"/Equipment/{eq_name}/Common/Enabled",
            callback = self.enable_callback
        )

        if self.client.odb_get(f"/Equipment/{eq_name}/Common/Enabled"):
            self.enable_equipment(new_equip)

    def log_channel_state_change(self, channel_list : set[Channel], this_run : int, change_time : float, state : str, values : dict[Channel : ChannelCache] | None = None):
        log_entries = list()
        for it in channel_list:
            aValue = None
            if values is not None:
                aCache = values.get(it, None)
                aValue = aCache.last_val if aCache is not None else None
            log_entries.append(
                {
                    "upd_time"  : change_time,
                    "equipment" : it.equipment,
                    "channel"   : it.channel,
                    "label"     : it.label,
                    "reading"   : aValue
                }
            )
        self.db_iface.log_sc_values(this_run, state, log_entries)

    def log_vanished_equipment(self, eq_name : str):
        # Someone erased the ODB entry for this equipment.
        # As this was not properly logged, assign the last
        # time communicated with it.
        equip : Equipment = self.known_equipment[eq_name]
        this_run = self.client.odb_get("/Runinfo/Run number")
        self.log_channel_state_change(
            channel_list = equip.channel_list,
            this_run = this_run,
            change_time = equip.last_read_time,
            state = "VANISHED"
        )
        self.known_equipment.pop(eq_name)

    def enable_equipment(self, equip : Equipment):
        equip.enabled = True
        this_run = self.client.odb_get("/Runinfo/Run number")
        enable_time = self.client.odb_last_update_time(f"/Equipment/{equip.name}/Common/Enabled")

        # Build channel map and cache
        for aChannel, val, upd_time in self.iter_channels(equips = equip.name):
            equip.channels.setdefault(aChannel.odb_path, set()).add(aChannel)
            equip.channel_cache[aChannel] = ChannelCache(last_val = val, last_upd = upd_time)

        # Watch ODB paths
        name_paths = [f"{equip.odb_path}/Settings/Names"]
        for aPath in equip.channels.keys():
            self.client.odb_watch(aPath, self.value_update_callback)
            var_name = aPath.split("/")[-1]
            name_paths.append(f"{equip.odb_path}/Settings/Names {var_name}")

        for aNamePath in name_paths:
            if self.client.odb_exists(aNamePath):
                self.client.odb_watch(aNamePath, self.name_update_callback)
                equip.odb_paths_watched.append(aNamePath)

        # Create enable log entry, which contains the first read of the values.
        self.log_channel_state_change(
            channel_list = equip.channel_list,
            this_run     = this_run,
            change_time  = enable_time,
            state        = "ENABLE",
            values       = equip.channel_cache
        )

    def disable_equipment(self, equip : Equipment):
        equip.enabled = False
        this_run = self.client.odb_get("/Runinfo/Run number")
        disable_time = self.client.odb_last_update_time(f"/Equipment/{equip.name}/Common/Enabled")
        for odb_path, channels in equip.channels.items():
            self.client.odb_stop_watching(odb_path)
            self.log_channel_state_change(
                channel_list = channels,
                this_run = this_run,
                change_time = disable_time,
                state = "DISABLE"
            )
        for odb_path in equip.odb_paths_watched:
            self.client.odb_stop_watching(odb_path)

        # we'll rebuild the channel list upon enabling.
        # not the most efficient thing to do, but one
        # that guarantees a clean state and as enabling
        # disabling should remain a rare occurence, it
        # will not significantly contribute to the overall
        # load.
        # PS: If it becomes a regular occurence of concern,
        # we'll have much larger problems to deal with.
        equip.channels.clear()
        equip.channel_cache.clear()


    # iterate all channels associated with a given equipment or a
    # list of equipments. If None is provided, it will load the
    # list of all available equipments from the ODB instead.
    def iter_channels(self, equips : str | list[str] | None = None):
        if equips == None:
            equips = list(self.client.odb_get("/Equipment", just_key_list = True))
        if isinstance(equips, str):
            equips = [equips]

        for eq_name in equips:
            eq_entry = self.client.odb_get(f"/Equipment/{eq_name}", recurse_dir = True )
            if not eq_entry["Common"]["Enabled"]:
                # Only iterate enabled channels
                continue
            gloabl_names = eq_entry.get("Settings", {}).get("Names", [])
            for var_name, values in eq_entry.get("Variables", {}).items():
                last_update = self.client.odb_last_update_time(f"/Equipment/{eq_name}/Variables/{var_name}")
                names = eq_entry.get("Settings", {}).get(f"Names {var_name}", gloabl_names)
                name_list = assert_list(names)
                value_list = assert_list(values)
                for index, value in enumerate(value_list):
                    yield (
                        Channel(
                            equipment=eq_name,
                            variable=var_name,
                            index=index,
                            name=name_list[index] if index < len(name_list) else f"var{index}",
                        ),
                        value,
                        last_update
                    )

    def initialise_inventory(self):
        # load equipment names
        equip_list = self.client.odb_get("/Equipment", just_key_list = True)
        for it in equip_list:
            self.create_equipment(it)

    def check_inventory(self):
        equips_in_odb = set(self.client.odb_get("/Equipment", just_key_list = True))
        equips_known  = set(self.known_equipment.keys())

        equips_vanished = equips_known - equips_in_odb
        equips_added    = equips_in_odb - equips_known

        for vanished in equips_vanished:
            self.log_vanished_equipment(vanished)

        for added in equips_added:
            self.create_equipment(added)

    def log(self, reason : str):
        log_entries = list()
        this_run = self.client.odb_get("/Runinfo/Run number")

        for aChannel, aValue, last_update in self.iter_channels():
            log_entries.append({
                    "upd_time"  : last_update,
                    "equipment" : aChannel.equipment,
                    "channel"   : aChannel.channel,
                    "label"     : aChannel.label,
                    "reading"   : aValue
                })

        self.db_iface.log_sc_values(this_run, reason, log_entries)

    # client and run_number are used to match transition callback signature
    def log_bor_callback(self, client, run_number):
        self.log(reason = "BOR")

    # client and run_number are used to match transition callback signature
    def log_eor_callback(self, client, run_number):
        self.log(reason = "EOR")

    # ---------------------------------------------------------------------
    def mainloop(self):
        self.initialise_inventory()

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
            self.client.communicate(self.sleep_time)

            # We manually iterate the existing registered equipments at the end of each cycle.
            # This should detect newly added or unexpectedly removed equipment as we don't want
            # to add a callback on the entire `/Equipment` ODB tree. Such a callback would fire
            # on every statistics update, potential heartbeats and similar events, which would
            # likely result in an excessive amount of callbacks for otherwise exceptionally rare
            # events.
            self.check_inventory()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--midas-client", default=kMidasClientName)
    parser.add_argument("--midas-host", default=kMidasHostName)
    parser.add_argument("--midas-expt", default=kMidasExptName)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    with Logger(args) as logger:
        logger.mainloop()


if __name__ == "__main__":
    main()
