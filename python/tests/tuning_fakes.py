"""In-memory stand-ins for the run database, the ODB and the beam-tuning
service, for the tuning-loop tests.  No MIDAS, no Postgres, no network."""


class FakeOdb:
    """Flat path -> value store with the three calls the loop uses."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.messages = []

    def odb_exists(self, path):
        return path in self.values or any(k.startswith(path + "/") for k in self.values)

    def odb_get(self, path):
        if path not in self.values:
            raise KeyError(path)
        return self.values[path]

    def odb_set(self, path, value):
        self.values[path] = value

    def msg(self, text, is_error=False):
        self.messages.append((text, is_error))


class FakeDb:
    """Just enough of `pioneer.rundb.interface.interface`."""

    def __init__(self):
        self.configs = {"target_position": {
            1: {"id": 1, "seq_id": 1, "xpos": 0.0, "ypos": 0.0},
            2: {"id": 2, "seq_id": 2, "xpos": 0.0, "ypos": 0.0},
            3: {"id": 3, "seq_id": 2, "xpos": 17.0, "ypos": 17.0},
            4: {"id": 4, "seq_id": 2, "xpos": -17.0, "ypos": 17.0},
            5: {"id": 5, "seq_id": 2, "xpos": 17.0, "ypos": -17.0},
            6: {"id": 6, "seq_id": 2, "xpos": -17.0, "ypos": -17.0},
        }, "degrader_position": {
            10: {"id": 10, "seq_id": 1, "position": 0.0},
        }}
        self.next_config_id = 100
        self.runs = {}          # run db id -> dict(status, midas_run_number, requested_events, configs)
        self.next_run_id = 400
        self.sequences = {}     # seq id -> dict(status, on_complete, runs)
        self.next_seq_id = 50
        self.files = []         # state.file_list rows
        self.jobs = []          # state.postproc_job rows (job_type, status, midas_run_id)
        self.status_updates = []
        self.written = []       # (table, values) of add_new_configuration

    # config tables
    def load_config(self, table, cfg_id):
        cfg = self.configs.get(table, {}).get(cfg_id)
        return dict(cfg) if cfg is not None else None

    def load_config_sequence(self, table, seq_id):
        return [dict(c) for c in self.configs.get(table, {}).values() if c.get("seq_id") == seq_id]

    def add_new_configuration(self, table, values):
        cfg_id = self.next_config_id
        self.next_config_id += 1
        self.written.append((table, dict(values)))
        self.configs.setdefault(table, {})[cfg_id] = dict(values, id=cfg_id)
        return cfg_id

    # runs and sequences
    def schedule_new_run(self, num_ev, configs):
        run_id = self.next_run_id
        self.next_run_id += 1
        self.runs[run_id] = {"status": "PENDING", "midas_run_number": None,
                             "requested_events": int(num_ev), "configs": list(configs)}
        return run_id

    def register_sequence(self, run_ids, on_complete):
        seq_id = self.next_seq_id
        self.next_seq_id += 1
        self.sequences[seq_id] = {"status": "PENDING", "on_complete": on_complete,
                                  "runs": list(run_ids)}
        return seq_id

    def find_sequences(self, status, limit=1):
        found = [dict(s, id=i) for i, s in sorted(self.sequences.items()) if s["status"] == status]
        return found[:limit]

    def get_sequence_entry(self, id):
        seq = self.sequences.get(id)
        return None if seq is None else {"id": id, "status": seq["status"],
                                         "on_complete": seq["on_complete"]}

    def get_all_runs_in_sequence(self, id):
        return list(self.sequences.get(id, {}).get("runs", []))

    def get_midas_run_number(self, run_id):
        return self.runs.get(run_id, {}).get("midas_run_number")

    def get_run_id(self, midas_run_number):
        for run_id, run in self.runs.items():
            if run["midas_run_number"] == midas_run_number:
                return run_id
        return None

    def find_files(self, run_ids, extensions):
        if isinstance(run_ids, int):
            run_ids = [run_ids]
        if isinstance(extensions, str):
            extensions = [extensions]
        rows = [f for f in self.files if f["run_id"] in run_ids and f["fileext"] in extensions]
        return sorted(rows, key=lambda f: f["filebase"])

    def update_status(self, table, id, new_status):
        self.status_updates.append((table, id, new_status))
        if table == "run_sequence" and id in self.sequences:
            self.sequences[id]["status"] = new_status
        return True

    def get_sequence_progress(self, seq_id):
        seq = self.sequences.get(seq_id)
        if seq is None:
            return None
        runs = []
        for run_id in sorted(seq["runs"]):
            run = self.runs[run_id]
            jobs = [j for j in self.jobs if j["midas_run_id"] == run_id and j["job_type"] == "nearline"]
            runs.append({
                "run_db_id": run_id,
                "run_number": run["midas_run_number"],
                "status": run["status"],
                "requested_events": run["requested_events"],
                "nearline_total": len(jobs),
                "nearline_done": sum(1 for j in jobs if j["status"] == "DONE"),
                "nearline_failed": sum(1 for j in jobs if j["status"] in ("FAILED", "BLOCKED", "ERROR", "CANCELLED")),
            })
        return {"id": seq_id, "status": seq["status"], "on_complete": seq["on_complete"], "runs": runs}

    # helpers for the tests
    def add_run(self, run_number, status="DONE", subruns=0, seq_id=None, file_status="DONE"):
        run_id = self.schedule_new_run(1e6, [])
        self.runs[run_id]["status"] = status
        self.runs[run_id]["midas_run_number"] = run_number
        for i in range(subruns):
            self.files.append({"id": len(self.files) + 1, "run_id": run_id,
                               "filebase": "run%05d_%05d" % (run_number, i),
                               "fileext": "root", "producer": "nearline", "status": file_status})
            self.jobs.append({"midas_run_id": run_id, "job_type": "nearline", "status": file_status})
        if seq_id is not None:
            self.sequences.setdefault(seq_id, {"status": "CLAIMED", "on_complete": "mt_add", "runs": []})
            self.sequences[seq_id]["runs"].append(run_id)
        return run_id


class FakeHttp:
    """Stands in for `BeamTuneClient` inside a `miniTwinInterface`."""

    def __init__(self, proposals=None, fail=False):
        self.proposals = list(proposals or [])
        self.contexts = []
        self.daq = []
        self.since = []
        self.fail = fail

    def _maybe_fail(self):
        if self.fail:
            from pioneer.nearline.beamtune_client import BeamTuneError
            raise BeamTuneError("service down")

    def proposal(self, since=0):
        self._maybe_fail()
        self.since.append(since)
        for p in self.proposals:
            if p.get("proposal_id", 0) > since:
                return dict(p)
        return {"ready": False, "state": "ready",
                "last_proposal_id": max([p.get("proposal_id", 0) for p in self.proposals] or [0])}

    #: context_id -> HTTP status the service answers that context with
    reject = None

    def post_context(self, context):
        self._maybe_fail()
        status = (self.reject or {}).get(context.get("context_id"))
        if status:
            from pioneer.nearline.beamtune_client import BeamTuneError
            raise BeamTuneError("POST /v1/context -> %d: SchemaError" % status, status=status)
        self.contexts.append(context)
        return {"accepted": True}

    daq_fail = False

    def post_daq(self, report):
        self._maybe_fail()
        if self.daq_fail:
            from pioneer.nearline.beamtune_client import BeamTuneError
            raise BeamTuneError("POST /v1/daq failed")
        self.daq.append(report)
        return {"accepted": True}

    def config(self):
        return {"config": {}}


_ABSENT = object()


def proposal(pid, currents=None, step_id="ASM12_90.44", attempt=0, plan="quick_run00588_ASM12",
             in_reply_to=_ABSENT):
    p = {"ready": True, "proposal_id": pid, "done": False,
         "currents": currents or {"ASM12:SOL:2": 90.44, "QTB12": 56.12},
         "run": {"step_id": step_id, "attempt": attempt, "plan": plan}}
    if in_reply_to is not _ABSENT:
        p["in_reply_to"] = in_reply_to
    return p


def in_reply_to(context_id, outcome="done", step_id="ASM12_90.44", attempt=0,
                answered_proposal_id=5, note=""):
    return {"context_id": context_id, "answered_proposal_id": answered_proposal_id,
            "step_id": step_id, "attempt": attempt, "outcome": outcome, "note": note}
