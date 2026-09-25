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

from pioneer.nearline.beamtune_client import DAQ_SCHEMA

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

#: DAQ progress reports (POST /v1/daq): looked at no more often than this,
#: posted only when the stage, a run status or the subrun counts changed, or
#: the events sent moved by EVENTS_STEP of the requested number.
MONITOR_INTERVAL_S = 10.0
EVENTS_STEP = 0.1

#: run-database statuses, as utils.status in rundb/db_config.sql
PENDING_STATUSES = {"HOLDING", "PENDING", "DEPENDING"}
SUCCESS_STATUSES = {"DONE"}
FAILURE_STATUSES = {"FAILED", "BLOCKED", "ERROR", "CANCELLED"}

#: what the running experiment shows about the run in progress
ODB_RUN_DB_PK = "/Runinfo/Run DB PK"
ODB_EVENTS_SENT = "/Equipment/WDWaveforms/Statistics/Events sent"

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


class ScheduleError(RuntimeError):
    """A proposal could not be scheduled; already reported when raised."""


def utc_now(clock=time.time):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def daq_report(proposal_id, stage, step=None, seq_id=None, runs=None, events=None,
               subruns=None, message=None, sent_utc=None):
    """One DAQ progress report as POST /v1/daq takes it."""
    return {
        "schema": DAQ_SCHEMA,
        "proposal_id": int(proposal_id),
        "step_id": (step or {}).get("step_id"),
        "stage": stage,
        "seq_id": seq_id,
        "runs": list(runs or []),
        "events": events,
        "subruns": subruns,
        "message": message,
        "sent_utc": sent_utc or utc_now(),
    }


def progress_stage(progress):
    """``(stage, message)`` of a sequence from
    ``interface.get_sequence_progress``: failed, posted (the sequence is
    DONE), scheduled (every run pending), nearline (every run DONE, the
    context not yet posted) or running."""
    if progress is None:
        return "failed", "sequence not found in the run database"
    runs = progress.get("runs") or []
    if not runs:
        return "failed", "sequence %s has no runs" % progress.get("id")
    bad = [r for r in runs if r["status"] in FAILURE_STATUSES]
    if bad:
        return "failed", "; ".join("run %s %s" % (r["run_number"] if r["run_number"] is not None
                                                  else "(db id %s)" % r["run_db_id"], r["status"])
                                   for r in bad)
    nearline_failed = sum(int(r["nearline_failed"]) for r in runs)
    if nearline_failed:
        return "failed", "%d nearline job(s) failed" % nearline_failed
    if progress["status"] in FAILURE_STATUSES:
        return "failed", "sequence %s" % progress["status"]
    if progress["status"] == "DONE":
        return "posted", None
    if all(r["status"] in PENDING_STATUSES for r in runs):
        return "scheduled", None
    if all(r["status"] in SUCCESS_STATUSES for r in runs):
        return "nearline", None
    return "running", None


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
        # progress reports: what was last sent, and when the state was last looked at
        self._last_monitor = 0.0
        self._last_key = None
        self._last_events = None
        self._last_report = None
        self._last_monitor_error = None
        #: nearline output tree to use instead of /Nearline/config/Output path
        self.output_override = None

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

    def sync_state(self):
        """Pick up what the manual CLI wrote to /Nearline/MiniTwin while this
        process was running: a newer last proposal id, a new or cleared step."""
        last_id, active = load_state(self.odb)
        if last_id > self.mt.last_proposal_id:
            self.mt.last_proposal_id = last_id
        self._saved_last_id = max(self._saved_last_id, last_id)
        if active != self.active:
            self.active = active
            self._last_key = None

    def set_active(self, step):
        self.active = dict(step) if step else None
        save_step(self.odb, self.active)
        self._last_key = None
        self._last_events = None

    def refresh_enable(self):
        """Re-read /Nearline/config/MiniTwin enable, the pause switch.
        Returns True while the loop may poll and schedule.  Going off posts
        one `paused` report; coming back on repeats the last report."""
        enabled = bool(odb_value(self.odb, ODB_CONFIG + "/MiniTwin enable", False))
        try:
            self.sync_state()
        except Exception as exc:                       # noqa: BLE001 -- keep the loop going
            self.message("Tuning: could not read /Nearline/MiniTwin: %s" % exc)
        if enabled != self.enabled:
            was = self.enabled
            self.enabled = enabled
            if was is not None or not enabled:
                self.message("Tuning: loop %s (MiniTwin enable = %s)"
                             % ("resumed" if enabled else "paused", "y" if enabled else "n"))
            if not enabled:
                self._report_paused()
            elif was is not None:
                self._report_resumed()
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
        if self.output_override:
            return self.output_override
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
        whose scheduling fails is reported (MIDAS error, `failed` DAQ report),
        raises ScheduleError, and is not retried by itself -- retake it with the CLI,
        see the README -- rather than scheduled twice."""
        configs = self.mt.NextConfiguration()
        proposal_id = self.mt.last_proposal_id
        if not dry_run and proposal_id != self._saved_last_id:
            save_last_id(self.odb, proposal_id)
            self._saved_last_id = proposal_id
        if not configs:
            return []
        hints = self.mt.last_run_hints or {}
        try:
            scheduled = schedule_configs(self.db, configs, self.update_table,
                                         self.target_config, dry_run=dry_run)
        except Exception as exc:
            if not dry_run:
                self.message("Tuning: proposal %d could not be scheduled: %s" % (proposal_id, exc),
                             is_error=True)
                self.report(daq_report(proposal_id, "failed", step=hints,
                                       message="not scheduled: %s" % exc,
                                       sent_utc=utc_now(self.clock)))
            raise ScheduleError("proposal %d not scheduled: %s" % (proposal_id, exc)) from exc
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
                self.report_progress()
        return scheduled

    def step_for_sequence(self, seq_id):
        """The active step when `seq_id` is its sequence, else None."""
        if self.active and self.active.get("seq_id") == seq_id:
            return dict(self.active)
        return None

    def step_for_run(self, run_id):
        """The active step when run `run_id` (run database id) is in its sequence."""
        if not self.active or not self.active.get("seq_id"):
            return None
        if run_id in self.db.get_all_runs_in_sequence(self.active["seq_id"]):
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

    def post_runs(self, run_ids, step=None, require_delivery=False):
        """Build the context of `run_ids` and post it through the retry queue.
        With `step` (the active one), report `posted` and clear the step.
        With `require_delivery` (the CLI, which has no later retry) raise
        when the service did not take it, leaving the step as it was."""
        context_id, files, numbers, header = self.context_parts(run_ids)
        context = self.mt.AddContextFiles(context_id, files, numbers, header, step=step)
        if require_delivery and self.mt.pending:
            raise RuntimeError("the service did not take context %s" % context_id)
        if step is not None:
            queued = self.mt.pending > 0
            self._report_final(step, "posted", "context %s %s" % (
                context_id, "queued, service not reachable yet" if queued else "delivered"))
            self.set_active(None)
        return context

    def post_sequence(self, seq_id):
        """Post the context of a finished `mt_add` sequence, then mark the
        sequence DONE, or FAILED with a MIDAS error message if that raised."""
        step = self.step_for_sequence(seq_id)
        try:
            run_ids = self.db.get_all_runs_in_sequence(seq_id)
            context = self.post_runs(run_ids, step=step)
        except Exception as exc:                       # noqa: BLE001 -- reported, sequence FAILED
            self.message("Tuning: context for sequence %d not posted: %s" % (seq_id, exc),
                         is_error=True)
            if step is not None:
                self._report_final(step, "failed", "context not posted: %s" % exc)
            self.db.update_status("run_sequence", seq_id, "FAILED")
            return None
        self.db.update_status("run_sequence", seq_id, "DONE")
        return context

    # -- DAQ progress reports ----------------------------------------------

    def collect_progress(self, step):
        """The DAQ report of `step` from the run database and the ODB."""
        seq_id = step.get("seq_id")
        progress = self.db.get_sequence_progress(seq_id) if seq_id else None
        stage, message = progress_stage(progress)
        runs = (progress or {}).get("runs") or []
        subruns = None
        if progress is not None:
            subruns = {"done": sum(int(r["nearline_done"]) for r in runs),
                       "total": sum(int(r["nearline_total"]) for r in runs)}
        events = None
        running = [r for r in runs if r["status"] == "RUNNING"]
        if running:
            # Events sent is the running experiment's counter; only trust it
            # when the ODB says the run in progress is ours.
            pk = odb_value(self.odb, ODB_RUN_DB_PK, 0)
            ours = [r for r in running if r["run_db_id"] == pk]
            if ours:
                sent = odb_value(self.odb, ODB_EVENTS_SENT, None)
                events = {"sent": int(sent) if sent is not None else None,
                          "requested": int(ours[0]["requested_events"] or 0) or None}
        return daq_report(
            step["proposal_id"], stage, step=step, seq_id=seq_id,
            runs=[{"run_db_id": r["run_db_id"], "run_number": r["run_number"],
                   "status": r["status"]} for r in runs],
            events=events, subruns=subruns, message=message,
            sent_utc=utc_now(self.clock))

    def monitor(self):
        """Called every mainloop iteration: at most every MONITOR_INTERVAL_S,
        while a step is active and the loop is not paused, post its progress
        if it changed.  Never raises."""
        try:
            if self.active is None or self.enabled is False:
                return False
            now = self.clock()
            if now - self._last_monitor < MONITOR_INTERVAL_S:
                return False
            self._last_monitor = now
            return self.report_progress(only_if_changed=True)
        except Exception as exc:                       # noqa: BLE001 -- never into the mainloop
            self._progress_error(exc)
            return False

    def report_progress(self, only_if_changed=False):
        """Collect and post the active step's progress.  Never raises."""
        try:
            if self.active is None:
                return False
            report = self.collect_progress(self.active)
            if only_if_changed and not self._changed(report):
                return False
        except Exception as exc:                       # noqa: BLE001
            self._progress_error(exc)
            return False
        self._last_monitor_error = None
        return self.report(report)

    def _progress_error(self, exc):
        # once per distinct error, not every 10 s
        text = "Tuning: progress report failed: %s" % exc
        if text != self._last_monitor_error:
            self._last_monitor_error = text
            self.message(text)

    def _changed(self, report):
        if _report_key(report) != self._last_key:
            return True
        events = report.get("events") or {}
        sent, requested = events.get("sent"), events.get("requested")
        if sent is None or not requested:
            return False
        if self._last_events is None:
            return True
        return abs(sent - self._last_events) >= EVENTS_STEP * requested

    def report(self, report):
        """Post one DAQ report; remembers it when the service took it.
        Reports without a proposal id are not sent.  Never raises."""
        try:
            if not report.get("proposal_id"):
                return False
            if not self.mt.PostDaq(report):
                return False
        except Exception as exc:                       # noqa: BLE001
            self.message("Tuning: DAQ report not sent: %s" % exc)
            return False
        self._last_key = _report_key(report)
        self._last_events = (report.get("events") or {}).get("sent")
        if report.get("stage") != "paused":
            self._last_report = dict(report)
        return True

    def _report_final(self, step, stage, message):
        """`posted` or `failed` for `step`, with its runs when they can be read."""
        try:
            report = self.collect_progress(step)
        except Exception:                              # noqa: BLE001
            report = daq_report(step["proposal_id"], stage, step=step,
                                seq_id=step.get("seq_id"), sent_utc=utc_now(self.clock))
        report["stage"] = stage
        report["message"] = message
        report["events"] = None
        self.report(report)

    def _report_paused(self):
        if self.active:
            step = self.active
        else:
            step = {"proposal_id": self.mt.last_proposal_id}
        self.report(daq_report(step.get("proposal_id") or 0, "paused", step=step,
                               seq_id=step.get("seq_id"),
                               message="MiniTwin enable is off: not polling for proposals",
                               sent_utc=utc_now(self.clock)))

    def _report_resumed(self):
        # the monitor posts the step's state at its next look; without a step
        # repeat the last report, so the page stops showing `paused`
        self._last_key = None
        self._last_monitor = 0.0
        if self.active is None and self._last_report is not None:
            report = dict(self._last_report)
            report["message"] = "loop resumed"
            report["sent_utc"] = utc_now(self.clock)
            self.report(report)


def _report_key(report):
    return (report.get("stage"),
            tuple((r.get("run_db_id"), r.get("status")) for r in report.get("runs") or []),
            tuple(sorted((report.get("subruns") or {}).items())))


# ---------------------------------------------------------------------------
# manual path: python -m pioneer.nearline.tuning {schedule,post}
# ---------------------------------------------------------------------------

class MemoryOdb:
    """Stand-in ODB for --no-odb: the defaults plus what the command line
    gives, kept in memory and forgotten at exit."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def odb_exists(self, path):
        return path in self.values

    def odb_get(self, path):
        return self.values[path]

    def odb_set(self, path, value):
        self.values[path] = value


def _parser():
    import argparse
    import os

    # the same options on both commands, after the command name
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=None,
                        help="beam-tuning service (default: /Nearline/config/MiniTwin URL)")
    common.add_argument("--dry-run", action="store_true",
                        help="print what would be scheduled or sent; write and send nothing")
    common.add_argument("--no-odb", action="store_true",
                        help="do not connect to MIDAS: ODB defaults, nothing remembered")
    common.add_argument("--output-path", default=None,
                        help="nearline output tree (default: /Nearline/config/Output path)")
    common.add_argument("--midas-host", default=os.environ.get("MIDAS_SERVER_HOST", "localhost"))
    common.add_argument("--midas-expt", default=os.environ.get("MIDAS_EXPT_NAME"))

    parser = argparse.ArgumentParser(
        prog="python -m pioneer.nearline.tuning",
        description="Drive the tuning loop by hand, exactly as the nearline daemon does.")
    sub = parser.add_subparsers(dest="command", required=True)
    sched = sub.add_parser("schedule", parents=[common],
                           help="take the current proposal once and write its run")
    sched.add_argument("--since", type=int, default=None,
                       help="proposal id to ask past (default: /Nearline/MiniTwin/Last proposal id); "
                            "one less than a proposal id retakes that proposal")
    post = sub.add_parser("post", parents=[common],
                          help="post run N's histogram files as a context")
    post.add_argument("--run", type=int, required=True, help="MIDAS run number")
    return parser


def _connect_odb(args):
    if args.no_odb:
        values = {ODB_CONFIG + "/" + k: v for k, v in CONFIG_DEFAULTS.items()}
        values.update({ODB_STATE + "/" + k: v for k, v in STATE_DEFAULTS.items()})
        return MemoryOdb(values), None
    import midas.client
    client = midas.client.MidasClient("NearlineTuning", host_name=args.midas_host,
                                      expt_name=args.midas_expt)
    return client, client


def _print_json(obj):
    import json
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None, db=None, odb=None, http=None, header_reader=None):
    """The command line.  `db`, `odb`, `http` and `header_reader` replace the
    real run database, MIDAS client, service client and ROOT header reader
    (the tests use them)."""
    from pioneer.nearline.beamtune_client import DEFAULT_URL
    from pioneer.nearline.miniTwinInterface import miniTwinInterface

    args = _parser().parse_args(argv)
    client = None
    if odb is None:
        odb, client = _connect_odb(args)
    try:
        if db is None:
            import pioneer.rundb.interface
            db = pioneer.rundb.interface.interface(user="bot", password="bot")
        if not args.dry_run and not args.no_odb:
            ensure_odb_keys(odb)

        url = args.url or odb_value(odb, ODB_CONFIG + "/MiniTwin URL", DEFAULT_URL)
        table = odb_value(odb, ODB_CONFIG + "/MiniTwin updates", "pim1_epics")
        mt = miniTwinInterface(base_url=url, config_type=table,
                               logger=lambda m: print("[beamtune] " + m))
        if http is not None:
            mt.client = http

        def message(text, is_error=False):
            print(("ERROR: " if is_error else "") + text)

        loop = TuningLoop(db, mt, odb, message=message,
                          header_reader=header_reader or read_beamline_header)
        # a one-off override, not written to the ODB
        loop.output_override = args.output_path
        loop.restore()

        if args.command == "schedule":
            return _cmd_schedule(args, loop, odb, url)
        return _cmd_post(args, loop, db)
    finally:
        if client is not None:
            client.disconnect()


def _cmd_schedule(args, loop, odb, url):
    if args.since is not None:
        loop.mt.last_proposal_id = args.since
    if not args.dry_run and odb_value(odb, ODB_CONFIG + "/MiniTwin enable", False) and not args.no_odb:
        print("note: MiniTwin enable is on, so a running daemon polls the service too; "
              "the proposal id is shared through the ODB")
    since = loop.mt.last_proposal_id
    try:
        scheduled = loop.poll_and_schedule(dry_run=args.dry_run)
    except ScheduleError as exc:
        print("ERROR: %s" % exc)
        return 2
    if not scheduled:
        print("no proposal newer than %d from %s" % (since, url))
        return 1
    if args.dry_run:
        print("dry run: would schedule (nothing written)")
    _print_json(scheduled)
    return 0


def _cmd_post(args, loop, db):
    run_id = db.get_run_id(args.run)
    if run_id is None:
        print("ERROR: MIDAS run %d is not in the run database" % args.run)
        return 2
    step = loop.step_for_run(run_id)
    if step is None:
        print("run %d is not the active step's run: posting without responds_to/step_id" % args.run)
    try:
        if args.dry_run:
            context = loop.build_context([run_id], step=step)
            print("dry run: would send (nothing sent)")
            _print_json(context)
            return 0
        context = loop.post_runs([run_id], step=step, require_delivery=True)
    except Exception as exc:                           # noqa: BLE001 -- a shifter reads this
        print("ERROR: run %d not posted: %s" % (args.run, exc))
        return 2
    if step is not None and step.get("seq_id"):
        # the daemon must not post this sequence again
        db.update_status("run_sequence", step["seq_id"], "DONE")
    print("posted context %s with %d file(s)" % (context["context_id"],
                                                   len(context["measurement"]["files"])))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
