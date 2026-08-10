"""Client side.  **Single self-contained file, stdlib only, vendorable by copy.**

Two classes:

:class:`BeamTuneClient`
    A thin, honest HTTP wrapper that raises :class:`BeamTuneError` on failure.
    For scripts, notebooks and tests.

:class:`NearlineTwinInterface`
    The drop-in replacement for ``pioneer.nearline.miniTwinInterface`` in the
    ``beamtime2026_pie5`` DAQ daemon.  Same four methods, so swapping it in
    needs no other change there.  **It never raises and never blocks for long.**

That last property is not a nicety.  ``NearlineDaemon.mainloop()`` has no
exception handling anywhere and its only pacing is ``client.communicate()``, so
an exception or a slow call inside ``NextConfiguration()`` would freeze MIDAS
transition handling and nearline job dispatch -- the whole DAQ control loop, not
just the tuning feature.  Hence: short timeouts, every error swallowed, ``[]``
returned, and a circuit breaker that stops touching the socket entirely for a
cooldown after repeated failures.

Contexts that could not be delivered are kept in a small bounded queue and
retried on the next call.  That is safe precisely because ``POST /v1/context``
is idempotent in ``context_id`` -- re-posting a delivered context is a no-op.

Deliberately conservative Python: no dependencies, no ``match``, and all
annotations are strings via ``__future__``, so this file runs on 3.7+ wherever
the daemon happens to live.
"""

from __future__ import annotations

import collections
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8420"
CONTEXT_SCHEMA = "beamtune.context/v1"

#: measurement file role, chosen by extension.  The nearline stitches ROOT into
#: maps.h5 before posting (see the StitchJob), so ".h5" is the normal path and
#: the ROOT roles are the fallback.
ROLE_BY_SUFFIX = {".h5": "maps", ".hdf5": "maps", ".npz": "maps", ".root": "merged_root"}


class BeamTuneError(RuntimeError):
    """Any failure talking to the service."""


# ---------------------------------------------------------------------------
# raising client
# ---------------------------------------------------------------------------

class BeamTuneClient:
    def __init__(self, base_url=DEFAULT_URL, timeout=10.0, token=None):
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.token = token or os.environ.get("BEAMTUNE_TOKEN")

    def _call(self, method, path, body=None):
        url = self.base_url + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["X-Beamtune-Token"] = self.token
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:                          # noqa: BLE001
                payload = {"error": {"type": "HTTPError", "message": str(exc)}}
            if exc.code >= 500 or exc.code in (400, 401, 404, 405, 413):
                error = payload.get("error") or {}
                raise BeamTuneError("%s %s -> %s: %s" % (
                    method, path, exc.code,
                    error.get("message") or payload)) from exc
            return exc.code, payload
        except Exception as exc:                       # noqa: BLE001 -- socket, DNS, timeout
            raise BeamTuneError("%s %s failed: %s" % (method, path, exc)) from exc

    # -- the three operations ----------------------------------------------

    def post_context(self, context):
        """1) Here is a new context."""
        return self._call("POST", "/v1/context", context)[1]

    def proposal(self, since=0):
        """2) What is the new current I should set?"""
        return self._call("GET", "/v1/proposal?since=%d" % int(since))[1]

    def reset(self, reason="", config_overrides=None):
        """3) Clear your internal state of the beam."""
        body = {"reason": reason}
        if config_overrides:
            body["config_overrides"] = config_overrides
        return self._call("POST", "/v1/reset", body)[1]

    # -- the rest -----------------------------------------------------------

    def health(self):
        return self._call("GET", "/v1/health")[1]

    def state(self):
        return self._call("GET", "/v1/state")[1]

    def config(self):
        return self._call("GET", "/v1/config")[1]

    def events(self, limit=50):
        return self._call("GET", "/v1/events?limit=%d" % int(limit))[1]

    def history(self, limit=0):
        """Compact per-event summary. ``limit=0`` means all of it.

        Prefer this to events() for anything that only needs settings and
        objectives -- an event carrying an inline map is orders of magnitude
        bigger, and the default limit counts *events*, so proposals halve the
        number of measurements you get back.
        """
        return self._call("GET", "/v1/history?limit=%d" % int(limit))[1]

    def control(self, op, **fields):
        body = {"op": op}
        body.update(fields)
        return self._call("POST", "/v1/control", body)[1]

    def wait_for_proposal(self, since=0, timeout=120.0, interval=1.0):
        """Block until a proposal newer than ``since`` appears.  Scripts only --
        the DAQ daemon must never block, and uses NextConfiguration() instead."""
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            payload = self.proposal(since)
            if payload.get("ready"):
                return payload
            time.sleep(interval)
        raise BeamTuneError("no proposal newer than %s within %.0fs" % (since, timeout))


# ---------------------------------------------------------------------------
# the MIDAS-side drop-in
# ---------------------------------------------------------------------------

class NearlineTwinInterface:
    """Drop-in for ``pioneer.nearline.miniTwinInterface.miniTwinInterface``.

    ``NextConfiguration()`` returns ``[{column: value}]`` -- one row for the
    config table named by ``config_type``, which the daemon schedules as a new
    MIDAS run sequence.  ``[]`` means "nothing to do", which is also what every
    failure returns.
    """

    def __init__(self, base_url=DEFAULT_URL, file_root=None,
                 config_type="psm_currents", column_map=None, value_format=float,
                 initial_knobs=None, timeout=2.0, token=None,
                 breaker_failures=3, breaker_cooldown_s=30.0,
                 max_pending=16, logger=None):
        self.client = BeamTuneClient(base_url, timeout=timeout, token=token)
        #: state.file_list rows carry filebase/fileext but no directory; this is
        #: the daemon's `nearline_output_path` from the ODB.
        self.file_root = file_root
        self.config_type = config_type
        self.column_map = dict(column_map or {})
        self.value_format = value_format
        self.logger = logger or (lambda message: print("[beamtune] " + message))

        self._last_id = 0
        self._last_applied = dict(initial_knobs) if initial_knobs else None
        self._pending = collections.deque(maxlen=int(max_pending))
        self._failures = 0
        self._breaker_failures = int(breaker_failures)
        self._breaker_cooldown_s = float(breaker_cooldown_s)
        self._muted_until = 0.0
        self._counter = 0

    # -- the four methods the daemon calls ---------------------------------

    def AddContext(self, ctxt):                        # noqa: N802 -- daemon's API
        """One completed sequence.  ``ctxt`` is a merged file path (or an envelope)."""
        try:
            context = (self._envelope_from_dict(ctxt) if isinstance(ctxt, dict)
                       else self._envelope_from_files([ctxt]))
            return self._enqueue(context)
        except Exception as exc:                       # noqa: BLE001 -- never escape
            self._log("AddContext failed to build a context: %r" % (exc,))
            return False

    def AddContextFiles(self, files):                  # noqa: N802 -- daemon's API
        """``files`` are ``state.file_list`` rows: filebase / fileext / run_id."""
        try:
            paths, run_ids = [], []
            for row in files or []:
                paths.append(self._path_from_row(row))
                if isinstance(row, dict) and row.get("run_id") is not None:
                    run_ids.append(row["run_id"])
            if not paths:
                return False
            return self._enqueue(self._envelope_from_files(paths, run_ids=run_ids))
        except Exception as exc:                       # noqa: BLE001 -- never escape
            self._log("AddContextFiles failed to build a context: %r" % (exc,))
            return False

    def NextConfiguration(self):                       # noqa: N802 -- daemon's API
        """Poll for a newer proposal.  Returns ``[]`` for anything but success.

        Called every mainloop iteration, so it also doubles as the retry pump
        for contexts that could not be delivered earlier.
        """
        try:
            self._flush()
            if self._muted():
                return []
            payload = self.client.proposal(self._last_id)
            self._succeed()
        except Exception as exc:                       # noqa: BLE001 -- never escape
            self._fail("NextConfiguration: %r" % (exc,))
            return []

        if not payload.get("ready"):
            return []

        proposal_id = int(payload.get("proposal_id", 0))
        if proposal_id <= self._last_id:
            return []
        self._last_id = proposal_id

        currents = payload.get("currents") or {}
        if payload.get("done"):
            self._log("strategy reports done at proposal %d; not scheduling" % proposal_id)
            return []
        if not currents:
            return []

        # Remember what we asked for: the daemon hands us files later, not
        # settings, so this is how a context knows which currents produced it.
        self._last_applied = dict(currents)
        self._log("proposal %d -> %s%s" % (
            proposal_id, currents,
            " (clamped: %s)" % payload["clamped"] if payload.get("clamped") else ""))
        return [self._row(currents)]

    # -- helpers ------------------------------------------------------------

    def _row(self, currents):
        row = {}
        for name, value in currents.items():
            row[self.column_map.get(name, name)] = self.value_format(value)
        return row

    def _path_from_row(self, row):
        if not isinstance(row, dict):
            return str(row)
        if row.get("path"):
            return str(row["path"])
        name = "%s.%s" % (row.get("filebase", "unknown"), row.get("fileext", "root"))
        return os.path.join(str(self.file_root), name) if self.file_root else name

    def _next_context_id(self, seed):
        if seed:
            return str(seed)
        self._counter += 1
        return "ctx-%d-%d" % (int(time.time()), self._counter)

    def _envelope_from_files(self, paths, run_ids=None):
        paths = [str(p) for p in paths]
        stem = os.path.splitext(os.path.basename(paths[0]))[0] if paths else ""
        files = []
        for path in paths:
            suffix = os.path.splitext(path)[1].lower()
            files.append({"role": ROLE_BY_SUFFIX.get(suffix, "other"), "path": path})
        return self._envelope(context_id=self._next_context_id(stem), files=files,
                              run_ids=run_ids)

    def _envelope_from_dict(self, payload):
        """Accept a caller-built envelope (or a partial one) and fill the gaps."""
        if payload.get("measurement") and payload.get("setting"):
            envelope = dict(payload)
            envelope.setdefault("schema", CONTEXT_SCHEMA)
            envelope.setdefault("context_id", self._next_context_id(None))
            return envelope
        return self._envelope(
            context_id=self._next_context_id(payload.get("context_id")),
            files=payload.get("files") or [],
            inline=payload.get("inline"),
            knobs=payload.get("knobs"),
            objective=payload.get("objective"),
            run_ids=payload.get("run_ids"))

    def _envelope(self, context_id, files=None, inline=None, knobs=None,
                  objective=None, run_ids=None):
        knobs = dict(knobs) if knobs else self._applied_knobs()
        measurement = {"kind": "psm_nearline", "valid": True,
                       "files": files or []}
        if inline:
            measurement["inline"] = inline
        if objective is not None:
            measurement["objective"] = objective
        return {
            "schema": CONTEXT_SCHEMA,
            "context_id": context_id,
            "responds_to": ({"proposal_id": self._last_id} if self._last_id else None),
            "provenance": {"run_ids": list(run_ids or []),
                           "config_type": self.config_type},
            "setting": {"units": "A", "knobs": knobs},
            "measurement": measurement,
        }

    def _applied_knobs(self):
        """The setting this measurement was taken at.

        Normally the currents from the proposal we last handed the daemon.  On
        the very first sequence there is no such proposal, so fall back to the
        service's configured initial currents (fetched once, lazily).
        """
        if self._last_applied:
            return dict(self._last_applied)
        config = (self.client.config().get("config") or {})
        initial = (config.get("knobs") or {}).get("initial_currents") or {}
        if not initial:
            raise BeamTuneError(
                "no setting is known for this measurement: pass initial_knobs= to "
                "NearlineTwinInterface, or set knobs.initial_currents in the "
                "service config")
        self._last_applied = dict(initial)
        return dict(initial)

    # -- delivery, with retry and a circuit breaker -------------------------

    def _enqueue(self, context):
        if len(self._pending) == self._pending.maxlen:
            dropped = self._pending[0]
            self._log("pending queue full; dropping oldest context %s"
                      % dropped.get("context_id"))
        self._pending.append(context)
        return self._flush()

    def _flush(self):
        """Deliver queued contexts oldest-first.  Safe to call at 1 Hz."""
        while self._pending:
            if self._muted():
                return False
            context = self._pending[0]
            try:
                result = self.client.post_context(context)
                self._succeed()
            except Exception as exc:                   # noqa: BLE001 -- never escape
                self._fail("post_context(%s): %r" % (context.get("context_id"), exc))
                return False
            self._pending.popleft()
            if result.get("duplicate"):
                self._log("context %s was already known" % context.get("context_id"))
            for warning in result.get("warnings") or []:
                self._log("warning for %s: %s" % (context.get("context_id"), warning))
        return True

    def _muted(self):
        return time.time() < self._muted_until

    def _succeed(self):
        self._failures = 0
        self._muted_until = 0.0

    def _fail(self, message):
        self._failures += 1
        if self._failures == self._breaker_failures:
            self._muted_until = time.time() + self._breaker_cooldown_s
            self._log("%s (%d consecutive failures; muting for %.0fs)"
                      % (message, self._failures, self._breaker_cooldown_s))
        elif self._failures < self._breaker_failures:
            self._log(message)
        else:
            self._muted_until = time.time() + self._breaker_cooldown_s

    def _log(self, message):
        try:
            self.logger(message)
        except Exception:                              # noqa: BLE001 -- logging must not kill the daemon
            pass

    # -- introspection for the operator -------------------------------------

    @property
    def pending(self):
        return len(self._pending)

    @property
    def muted(self):
        return self._muted()
