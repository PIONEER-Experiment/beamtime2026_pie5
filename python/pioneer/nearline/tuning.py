"""The tuning loop between the nearline daemon and the beam-tuning service.

One proposal from the service becomes one run in the run database: the
proposal's row in the config table named by `/Nearline/config/MiniTwin updates`
times one `target_position` config (the stage centre by default), in a
sequence with `on_complete = mt_add`.  When the run and every one of its
nearline jobs are done the run database marks that sequence RUNSDONE, the
daemon claims it and `post_sequence()` sends the run's histogram files to the
service as a context.

The daemon calls the functions here; nothing in this module imports midas or
ROOT at module level, so it also runs by hand (see the README, "Running it").
"""

from __future__ import annotations

import time

ODB_CONFIG = "/Nearline/config"

#: keys under /Nearline/config this module needs, with their defaults.  They
#: are created when missing and never overwritten.
CONFIG_DEFAULTS = {
    # config.target_position id the one run per proposal is taken at; id 2 is
    # the centre (0, 0) of the standard five-point sequence.
    "MiniTwin target config": 2,
    # the nearline output tree as pinky writes it, and where the service
    # reads the mirror of it; a posted file path has the first replaced by
    # the second.
    "MiniTwin local prefix": "/home/pinky/nearline/",
    "MiniTwin remote prefix": "/home/pioneer/nearline/histograms/",
}

#: The loop's memory, under /Nearline/MiniTwin: the newest proposal id seen
#: and the step whose run is being taken.  Restored at start-up, so a restart
#: never schedules the outstanding proposal again.  Proposal id 0 means no
#: active step; an empty string or attempt -1 means "not known".
ODB_STATE = "/Nearline/MiniTwin"
STATE_DEFAULTS = {
    "Last proposal id": 0,
    "Active step/Proposal id": 0,
    "Active step/Step id": "",
    "Active step/Attempt": -1,
    "Active step/Plan": "",
    "Active step/Seq id": 0,
}

#: the daemon's own /Nearline/config/Output path default
DEFAULT_OUTPUT_PATH = "/home/pinky/nearline"

#: requested WaveDREAM events per run
ITER_EVENTS = 1e6
FINAL_EVENTS = 1e7


def ensure_odb_keys(odb):
    """Create the keys this module reads, with their defaults, if missing."""
    for base, defaults in ((ODB_CONFIG, CONFIG_DEFAULTS), (ODB_STATE, STATE_DEFAULTS)):
        for key, default in defaults.items():
            path = base + "/" + key
            if not odb.odb_exists(path):
                odb.odb_set(path, default)


def load_state(odb):
    """``(last proposal id, active step or None)`` from /Nearline/MiniTwin."""
    def get(key):
        return odb_value(odb, ODB_STATE + "/" + key, STATE_DEFAULTS[key])
    last_id = int(get("Last proposal id") or 0)
    proposal_id = int(get("Active step/Proposal id") or 0)
    if proposal_id <= 0:
        return last_id, None
    attempt = int(get("Active step/Attempt"))
    step = {
        "proposal_id": proposal_id,
        "step_id": str(get("Active step/Step id")) or None,
        "attempt": attempt if attempt >= 0 else None,
        "plan": str(get("Active step/Plan")) or None,
        "seq_id": int(get("Active step/Seq id") or 0) or None,
    }
    return last_id, step


def save_last_id(odb, last_id):
    odb.odb_set(ODB_STATE + "/Last proposal id", int(last_id))


def save_step(odb, step):
    """Write the active step; None clears it."""
    step = step or {}
    attempt = step.get("attempt")
    odb.odb_set(ODB_STATE + "/Active step/Proposal id", int(step.get("proposal_id") or 0))
    odb.odb_set(ODB_STATE + "/Active step/Step id", str(step.get("step_id") or ""))
    odb.odb_set(ODB_STATE + "/Active step/Attempt", int(attempt) if attempt is not None else -1)
    odb.odb_set(ODB_STATE + "/Active step/Plan", str(step.get("plan") or ""))
    odb.odb_set(ODB_STATE + "/Active step/Seq id", int(step.get("seq_id") or 0))


def odb_value(odb, path, default):
    """`path` from the ODB, or `default` when there is no ODB or no key."""
    if odb is None:
        return default
    if not odb.odb_exists(path):
        return default
    return odb.odb_get(path)


def read_beamline_header(path):
    """The ``beamline`` header of a nearline histogram file, as plain lists.

    Needs ROOT with the PIONEER dictionaries (``PIODBBeamEntry``); imported
    here, not at module level, so everything else runs without it.
    """
    import ROOT

    aFile = ROOT.TFile.Open(str(path))
    if not aFile or aFile.IsZombie():
        raise OSError("cannot open %s" % path)
    try:
        hdr = aFile.Get("beamline")
        if not hdr:
            raise KeyError("no 'beamline' header in %s" % path)
        return {
            "names": [str(n) for n in hdr.GetNames()],
            "demand": [float(v) for v in hdr.GetDemand()],
            "measured": [float(v) for v in hdr.GetMeasured()],
            "types": [int(t) for t in hdr.GetTypes()],
        }
    finally:
        aFile.Close()


def hist_files(db, run_ids, output_path):
    """Per-subrun histogram files of `run_ids` (run database ids).

    One entry per state.file_list 'root' row with status DONE, in filebase
    order: ``{"run_db_id", "run_number", "local"}``, where ``local`` is
    ``<output_path>/run<N>/<filebase>_hists.root`` as jobs.py writes it.
    Rows in any other status are left out and listed in ``skipped``.
    """
    numbers = {}
    for run_id in run_ids:
        number = db.get_midas_run_number(run_id)
        if number is None:
            raise RuntimeError("run %d has no MIDAS run number (never started?)" % run_id)
        numbers[run_id] = int(number)
    found, skipped = [], []
    for row in db.find_files(list(run_ids), "root"):
        number = numbers[row["run_id"]]
        local = "%s/run%05d/%s_hists.root" % (str(output_path).rstrip("/"), number, row["filebase"])
        if row.get("status", "DONE") != "DONE":
            skipped.append((local, row.get("status")))
            continue
        found.append({"run_db_id": row["run_id"], "run_number": number, "local": local})
    return found, skipped, [numbers[r] for r in run_ids]


def remote_path(local, local_prefix, remote_prefix):
    """`local` with `local_prefix` replaced by `remote_prefix`, or None when
    it does not start with `local_prefix`."""
    local_prefix = str(local_prefix).rstrip("/") + "/"
    remote_prefix = str(remote_prefix).rstrip("/") + "/"
    local = str(local)
    if not local.startswith(local_prefix):
        return None
    return remote_prefix + local[len(local_prefix):]


def schedule_configs(db, configs, table, target_config, dry_run=False):
    """Write the runs for `configs` (what `NextConfiguration()` returns).

    'iter': every row of `table` times the one `target_position` config
    `target_config`, one sequence with on_complete `mt_add`, no merge.
    'final': as before, five-point scan times degrader scan, merge only.

    Returns one dict per config describing what was (or, with `dry_run`,
    would be) scheduled.  With `dry_run` nothing is written.
    """
    import pioneer.nearline.run as nl_run

    scheduled = []
    for aConfig in configs:
        if aConfig['type'] == 'iter':
            centre = db.load_config("target_position", target_config)
            if centre is None:
                raise RuntimeError("target_position config id %s is not in the run database"
                                   % target_config)
            entry = {
                "type": "iter",
                "config_type": table,
                "rows": list(aConfig['currents']),
                "target_position": dict(centre),
                "num_ev": ITER_EVENTS,
                "on_complete": "mt_add",
                "seq_id": None,
                "run_ids": [],
            }
            if not dry_run:
                mrs = nl_run.midas_run_sequence(db)
                mrs.set_config_list(table, aConfig['currents'])
                centre_seq = nl_run.midas_run_sequence(db)
                centre_seq.set_config_list("target_position", [centre])
                centre_seq.set_on_complete("mt_add")
                mrs.set_subsequence(centre_seq)
                mrs.num_ev = ITER_EVENTS
                entry["run_ids"] = mrs.schedule()
                entry["seq_id"] = centre_seq.seq_id
            scheduled.append(entry)
        elif aConfig['type'] == 'final':
            entry = {
                "type": "final",
                "config_type": table,
                "rows": list(aConfig['currents']),
                "num_ev": FINAL_EVENTS,
                "on_complete": "merge",
                "run_ids": [],
            }
            if not dry_run:
                mrs = nl_run.midas_run_sequence(db)
                mrs.set_config_list(table, aConfig['currents'])
                fiveScan = nl_run.five_point_sequence(db)
                fiveScan.set_on_complete("merge") # it shall only merge and not submit to minitwin.
                dscan = nl_run.degrader_scan(db)
                dscan.set_subsequence(fiveScan)
                mrs.set_subsequence(dscan)
                mrs.num_ev = FINAL_EVENTS
                entry["run_ids"] = mrs.schedule()
            scheduled.append(entry)
    return scheduled


class TuningLoop:
    """The daemon's side of the loop.

    `db` is a `pioneer.rundb.interface.interface`, `mt` a
    `miniTwinInterface`, `odb` anything with `odb_exists`/`odb_get`/`odb_set`
    (the daemon's `midas.client.MidasClient`), `message(text, is_error=...)`
    where errors go.
    """

    def __init__(self, db, mt, odb, message=None, header_reader=read_beamline_header,
                 clock=time.time):
        self.db = db
        self.mt = mt
        self.odb = odb
        self.clock = clock
        self._message = message or (lambda msg, is_error=False: print(msg))
        #: path -> beam header dict; tests replace it, the default needs ROOT
        self.header_reader = header_reader
        #: the step whose run is being taken (see STATE_DEFAULTS), or None
        self.active = None
        self._saved_last_id = 0
        self.enabled = None

    def restore(self):
        """Take the last proposal id and the active step from the ODB, so a
        restart neither re-schedules the outstanding proposal nor forgets
        which step the run in flight belongs to."""
        last_id, self.active = load_state(self.odb)
        self.mt.last_proposal_id = last_id
        self._saved_last_id = last_id
        if last_id or self.active:
            self.message("Tuning: restored last proposal id %d%s" % (
                last_id, ", active step %s (seq %s)" % (self.active.get("step_id"), self.active.get("seq_id"))
                if self.active else ""))

    def set_active(self, step):
        self.active = dict(step) if step else None
        save_step(self.odb, self.active)

    def refresh_enable(self):
        """Re-read /Nearline/config/MiniTwin enable, the pause switch.
        Returns True while the loop may poll and schedule."""
        enabled = bool(odb_value(self.odb, ODB_CONFIG + "/MiniTwin enable", False))
        if enabled != self.enabled:
            if self.enabled is not None or not enabled:
                self.message("Tuning: loop %s (MiniTwin enable = %s)"
                             % ("resumed" if enabled else "paused", "y" if enabled else "n"))
            self.enabled = enabled
        return enabled

    def message(self, msg, is_error=False):
        try:
            self._message(msg, is_error=is_error)
        except Exception:                              # noqa: BLE001 -- reporting must not raise
            print(msg)

    # -- settings from the ODB ---------------------------------------------

    @property
    def update_table(self):
        return odb_value(self.odb, ODB_CONFIG + "/MiniTwin updates", "pim1_epics")

    @property
    def target_config(self):
        return int(odb_value(self.odb, ODB_CONFIG + "/MiniTwin target config",
                             CONFIG_DEFAULTS["MiniTwin target config"]))

    @property
    def output_path(self):
        return odb_value(self.odb, ODB_CONFIG + "/Output path", DEFAULT_OUTPUT_PATH)

    @property
    def local_prefix(self):
        return odb_value(self.odb, ODB_CONFIG + "/MiniTwin local prefix",
                         CONFIG_DEFAULTS["MiniTwin local prefix"])

    @property
    def remote_prefix(self):
        return odb_value(self.odb, ODB_CONFIG + "/MiniTwin remote prefix",
                         CONFIG_DEFAULTS["MiniTwin remote prefix"])

    # -- proposals -> runs -------------------------------------------------

    def poll_and_schedule(self, dry_run=False):
        """Ask the service for a newer proposal and schedule it.

        The proposal id is stored before the runs are written: a proposal
        whose scheduling fails is reported and not retried by itself (retake
        it with the CLI, see the README), rather than scheduled twice."""
        configs = self.mt.NextConfiguration()
        proposal_id = self.mt.last_proposal_id
        if not dry_run and proposal_id != self._saved_last_id:
            save_last_id(self.odb, proposal_id)
            self._saved_last_id = proposal_id
        if not configs:
            return []
        hints = self.mt.last_run_hints or {}
        scheduled = schedule_configs(self.db, configs, self.update_table,
                                     self.target_config, dry_run=dry_run)
        if dry_run:
            return scheduled
        for entry in scheduled:
            if entry["type"] == "iter":
                self.set_active({
                    "proposal_id": proposal_id,
                    "step_id": hints.get("step_id"),
                    "attempt": hints.get("attempt"),
                    "plan": hints.get("plan"),
                    "seq_id": entry["seq_id"],
                })
                self.message("Tuning: proposal %d%s scheduled as run %s in sequence %s" % (
                    proposal_id, " (step %s)" % hints["step_id"] if hints.get("step_id") else "",
                    ", ".join(str(r) for r in entry["run_ids"]), entry["seq_id"]))
        return scheduled

    def step_for_sequence(self, seq_id):
        """The active step when `seq_id` is its sequence, else None."""
        if self.active and self.active.get("seq_id") == seq_id:
            return dict(self.active)
        return None

    # -- finished runs -> context ------------------------------------------

    def context_parts(self, run_ids):
        """Everything a context of `run_ids` (run database ids) is made of:
        ``(context_id, remote file paths, MIDAS run numbers, beam header)``.
        The header is read from the first subrun's file on this machine."""
        files, skipped, numbers = hist_files(self.db, run_ids, self.output_path)
        for local, status in skipped:
            self.message("Tuning: leaving out %s (file status %s)" % (local, status))
        if not files:
            raise RuntimeError("no finished nearline histogram files for run(s) %s"
                               % ", ".join(str(n) for n in numbers))
        remote = []
        for f in files:
            path = remote_path(f["local"], self.local_prefix, self.remote_prefix)
            if path is None:
                self.message("Tuning: %s is not under the local prefix %s; posting it unchanged"
                             % (f["local"], self.local_prefix))
                path = f["local"]
            remote.append(path)
        header = self.header_reader(files[0]["local"])
        context_id = "_".join("run%05d" % n for n in numbers)
        return context_id, remote, numbers, header

    def build_context(self, run_ids, step=None):
        """The context of `run_ids` as it would be posted; nothing is sent."""
        context_id, files, numbers, header = self.context_parts(run_ids)
        return self.mt.BuildContextFiles(context_id, files, numbers, header, step=step)

    def post_runs(self, run_ids, step=None):
        """Build the context of `run_ids` and post it through the retry queue."""
        context_id, files, numbers, header = self.context_parts(run_ids)
        return self.mt.AddContextFiles(context_id, files, numbers, header, step=step)

    def post_sequence(self, seq_id):
        """Post the context of a finished `mt_add` sequence, then mark the
        sequence DONE, or FAILED with a MIDAS error message if that raised."""
        try:
            run_ids = self.db.get_all_runs_in_sequence(seq_id)
            step = self.step_for_sequence(seq_id)
            context = self.post_runs(run_ids, step=step)
        except Exception as exc:                       # noqa: BLE001 -- reported, sequence FAILED
            self.message("Tuning: context for sequence %d not posted: %s" % (seq_id, exc),
                         is_error=True)
            self.db.update_status("run_sequence", seq_id, "FAILED")
            return None
        self.db.update_status("run_sequence", seq_id, "DONE")
        if step is not None:
            self.set_active(None)
        return context
