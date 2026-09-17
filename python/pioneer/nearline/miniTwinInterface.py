# This is the basic minitwin interface

from __future__ import annotations

import collections
import time
import os
import ROOT

from pioneer.nearline.beamtune_client import (
    BeamTuneClient,
    BeamTuneError,
    DEFAULT_URL,
    CONTEXT_SCHEMA,
    ROLE_BY_SUFFIX
)

class miniTwinInterface:
    """
    ``NextConfiguration()`` returns ``[{column: value}]`` -- one row for the
    config table named by ``config_type``, which the daemon schedules as a new
    MIDAS run sequence.  ``[]`` means "nothing to do", which is also what every
    failure returns.
    """

    def __init__(self, base_url=DEFAULT_URL,
                 config_type="psm_currents", column_map=None, value_format=float,
                 timeout=2.0, token=None,
                 breaker_failures=3, breaker_cooldown_s=30.0,
                 max_pending=16, logger=None):
        self.client = BeamTuneClient(base_url, timeout=timeout, token=token)
        #: state.file_list rows carry filebase/fileext but no directory; this is
        #: the daemon's `nearline_output_path` from the ODB.
        self.config_type = config_type
        self.column_map = dict(column_map or {})
        self.value_format = value_format
        self.logger = logger or (lambda message: print("[beamtune] " + message))

        self._last_id = 0
        #: The ``run`` annex of the proposal last handed out (PROTOCOL.md
        #: section 6): how that setting is to be taken.  None when the backend
        #: sent no hints -- the daemon then keeps its default run sequence.
        self._last_run = None
        self._columns_fetched = False
        self._pending = collections.deque(maxlen=int(max_pending))
        self._failures = 0
        self._breaker_failures = int(breaker_failures)
        self._breaker_cooldown_s = float(breaker_cooldown_s)
        self._muted_until = 0.0
        self._counter = 0

    # -- the four methods the daemon calls ---------------------------------

    def AddContext(self, ctxt):                        # noqa: N802 -- daemon's API
        """One completed sequence.  ``ctxt`` is a merged file path."""
        aFile = ROOT.TFile.Open(ctxt)

        beam_hdr = aFile.Get("beam")

        histo_names = [

        ]

        histos = [self.serialise(aFile.Get(n)) for n in histo_names]

        theMessage = {
            "schema" : CONTEXT_SCHEMA,
            "context_id" : ctxt,
            "settings" : {
                "knobs" : {k : v for k,v in zip(beam_hdr.GetNames(), beam_hdr.GetDemand())},
                "readback" : {k : v for k,v in zip(beam_hdr.GetNames(), beam_hdr.GetMeasured())}
            },
            "measurement" : {
                "inline" : {
                    "maps" : histos,
                    "axes": {"x": [-37.0, 37.0], "y": [-37.0, 37.0],
                            "px": [-950.0, 950.0], "py": [-950.0, 950.0]},
                    }
            }
        }

        self._enqueue(theMessage)

    def serialise(self, hist):
        if not hist.InheritsFrom("TH2"):
            raise NotImplementedError("not yet implemented for " + hist.IsA().GetName())

        rebin_factor_x = 64. / hist.GetNbinsX()
        rebin_factor_y = 64. / hist.GetNbinsY()
        if (int(rebin_factor_x) != rebin_factor_x or int(rebin_factor_y) != rebin_factor_y):
            raise ValueError("histogram %s has %d x %d bins, which is not a multiple of 64" % (hist.GetName(), hist.GetNbinsX(), hist.GetNbinsY()))

        hist.RebinX(int(rebin_factor_x))
        hist.RebinY(int(rebin_factor_y))
        return [[hist.GetBinContent(x, y) for x in range(1, hist.GetNbinsX() + 1)] for y in range(1, hist.GetNbinsY() + 1)]

    def AddContextFiles(self, files):
        raise DeprecationWarning("Files must be merged first.")

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
        self._last_run = dict(payload["run"]) if isinstance(payload.get("run"), dict) else None
        self._log("proposal %d -> %s%s%s" % (
            proposal_id, currents,
            " (clamped: %s)" % payload["clamped"] if payload.get("clamped") else "",
            " [step %s]" % self._last_run.get("step_id")
            if self._last_run and self._last_run.get("step_id") else ""))
        return [self._row(currents)]

    def NextRunPlan(self):                             # noqa: N802 -- daemon's API
        """``NextConfiguration()`` plus how to take the run.

        Returns ``None`` when there is nothing new, else
        ``{"rows": [{column: value}], "config_type": ..., "run": {...} | None}``
        where ``run`` is the proposal's annex (``step_id``, ``positions``,
        ``stop`` ...).  A daemon that schedules its own sequence per proposal
        (telescope positions, run length) reads it here; the plain
        ``NextConfiguration()`` contract is untouched for daemons that do not.
        """
        rows = self.NextConfiguration()
        if not rows:
            return None
        return {"rows": rows, "config_type": self.config_type,
                "run": (dict(self._last_run) if self._last_run else None)}

    @property
    def last_run_hints(self):
        """The ``run`` annex of the proposal last handed out, or None."""
        return dict(self._last_run) if self._last_run else None

    # -- helpers ------------------------------------------------------------

    def _row(self, currents):
        columns = self._column_map()
        row = {}
        for name, value in currents.items():
            row[columns.get(name, name)] = self.value_format(value)
        return row

    def _column_map(self):
        """``column_map`` given at construction, else the service's
        ``knobs.columns`` (from the beam file), fetched once and lazily.  A
        failure here is not fatal: the row falls back to knob names, which is
        what every daemon got before this existed."""
        if self.column_map or self._columns_fetched:
            return self.column_map
        self._columns_fetched = True
        try:
            config = (self.client.config().get("config") or {})
            columns = (config.get("knobs") or {}).get("columns") or {}
            if isinstance(columns, dict) and columns:
                self.column_map = {str(k): str(v) for k, v in columns.items()}
                self._log("column map from service: %d knobs" % len(self.column_map))
        except Exception as exc:                       # noqa: BLE001 -- never escape
            self._log("could not fetch knobs.columns from the service: %r" % (exc,))
        return self.column_map

    def _envelope(self, context_id, files=None, inline=None, knobs=None,
                  objective=None, run_ids=None, readback=None, valid=True):
        knobs = dict(knobs) if knobs else self._applied_knobs()
        measurement = {"kind": "psm_nearline", "valid": bool(valid),
                       "files": files or []}
        if inline:
            measurement["inline"] = inline
        if objective is not None:
            measurement["objective"] = objective
        provenance = {"run_ids": list(run_ids or []), "config_type": self.config_type}
        if self._last_run:
            # Hand the plan step back to the backend so it can tick it off
            # without guessing from the knob vector.
            for key in ("step_id", "attempt", "plan"):
                if self._last_run.get(key) is not None:
                    provenance[key] = self._last_run[key]
        return {
            "schema": CONTEXT_SCHEMA,
            "context_id": context_id,
            "responds_to": ({"proposal_id": self._last_id} if self._last_id else None),
            "provenance": provenance,
            "setting": ({"units": "A", "knobs": knobs, "readback": dict(readback)}
                        if readback else {"units": "A", "knobs": knobs}),
            "measurement": measurement,
        }

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