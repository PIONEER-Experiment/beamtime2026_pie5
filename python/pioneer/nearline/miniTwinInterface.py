# This is the basic minitwin interface

from __future__ import annotations

import collections
import math
import time
import os

from pioneer.nearline.beamtune_client import (
    BeamTuneClient,
    BeamTuneError,
    DEFAULT_URL,
    CONTEXT_SCHEMA,
    NOT_ABOUT_THE_BODY,
    is_permanent_rejection,
)

#: failures in a row, counted against one context (see _count_failure),
#: after which it goes to the back of the queue so the others get through
STUCK_AFTER = 5
#: failures in total after which a context is dropped as if refused
GIVE_UP_AFTER = 20

#: beam-header device types whose Demand the loop can set (the knobs)
CONFIGURABLE_DEVICES = (1, 4, 5)

#: measurement file role of a per-subrun nearline histogram file
HIST_ROOT_ROLE = "hist_root"


def knobs_from_header(header):
    """``(knobs, readback)`` from a beam header, types 1/4/5 only.

    ``header`` is a dict with the lists ``names``, ``demand``, ``measured``
    and ``types`` (what ``tuning.read_beamline_header`` returns).  A NaN or
    infinite value is refused: JSON cannot carry it and the service would
    reject the context."""
    knobs, readback = {}, {}
    for name, demand, measured, dev_type in zip(header["names"], header["demand"],
                                                header["measured"], header["types"]):
        if int(dev_type) in CONFIGURABLE_DEVICES:
            demand, measured = float(demand), float(measured)
            if not (math.isfinite(demand) and math.isfinite(measured)):
                raise ValueError("beam header channel %s has a non-finite value "
                                 "(demand %r, measured %r)" % (name, demand, measured))
            knobs[str(name)] = demand
            readback[str(name)] = measured
    return knobs, readback


#: The maps sent to the service, in its order: x-x', y-y', x-y.  For now the
#: unweighted 128x128 MuPix monitor maps; they have to be switched again (to
#: the maps on the minitwin's window) before the minitwin runs on them.
#: combine_files merges the same list.
miniTwin_histograms = [
    "histograms/PIPSMMuPixMonitor/xxp",
    "histograms/PIPSMMuPixMonitor/yyp",
    "histograms/PIPSMMuPixMonitor/track_xy",
]

#: bins per axis of an inline map
MAP_BINS = 64


def serialise_hist(hist, bins=MAP_BINS):
    """A TH2 as ``bins`` x ``bins`` nested lists, rebinned in place.

    Orientation as beam-tuning-client's psm_maps: ``out[i][j]`` is x bin i,
    y bin j -- rows are the histogram's x axis (x, or y for y-y'), columns
    its y axis (x', y', or y for x-y).  Each axis must have a multiple of
    ``bins`` bins; it is summed down by nbins // bins.
    """
    if not hist.InheritsFrom("TH2"):
        raise NotImplementedError("not yet implemented for " + hist.IsA().GetName())
    nx, ny = hist.GetNbinsX(), hist.GetNbinsY()
    if nx % bins or ny % bins:
        raise ValueError("histogram %s has %d x %d bins, which is not a multiple of %d"
                         % (hist.GetName(), nx, ny, bins))
    if nx != bins:
        hist.RebinX(nx // bins)
    if ny != bins:
        hist.RebinY(ny // bins)
    return [[float(hist.GetBinContent(ix, iy)) for iy in range(1, hist.GetNbinsY() + 1)]
            for ix in range(1, hist.GetNbinsX() + 1)]


def hist_ranges(hist):
    """``((xlo, xhi), (ylo, yhi))`` of a TH2's axes."""
    xa, ya = hist.GetXaxis(), hist.GetYaxis()
    return ((float(xa.GetXmin()), float(xa.GetXmax())),
            (float(ya.GetXmin()), float(ya.GetXmax())))


def _same_range(a, b, tol=1e-6):
    scale = max(1.0, abs(a[0]), abs(a[1]))
    return abs(a[0] - b[0]) <= tol * scale and abs(a[1] - b[1]) <= tol * scale


def inline_maps(hists):
    """``measurement.inline`` from the three maps (x-x', y-y', x-y, the order
    of miniTwin_histograms): ``{"maps": [...], "axes": {x, px, y, py}}``,
    the axes read from the histograms themselves.  Raises ValueError when
    x-y does not span the same x and y as x-x' and y-y'.  Rebins in place."""
    xxp, yyp, xy = hists
    (x, px), (y, py) = hist_ranges(xxp), hist_ranges(yyp)
    xy_x, xy_y = hist_ranges(xy)
    if not (_same_range(xy_x, x) and _same_range(xy_y, y)):
        raise ValueError("x-y map spans x %s, y %s but x-x' and y-y' span x %s, y %s"
                         % (list(xy_x), list(xy_y), list(x), list(y)))
    factors = [(h.GetNbinsX() // MAP_BINS, h.GetNbinsY() // MAP_BINS) for h in hists]
    flat = {f for pair in factors for f in pair}
    return {
        "maps": [serialise_hist(h) for h in hists],
        "axes": {"x": list(x), "px": list(px), "y": list(y), "py": list(py)},
        # bins summed into one per axis: one number when all agree
        "rebin": flat.pop() if len(flat) == 1 else [list(pair) for pair in factors],
    }


def label_inline(inline, names, n_files):
    """Say where inline maps come from, for the service to show: the
    histogram paths, that the daemon made them, and from how many files."""
    inline["names"] = list(names)
    inline["source"] = "daemon"
    inline["n_files"] = int(n_files)
    return inline


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
        #: a map given here is used as it is, never fetched
        self._fixed_columns = bool(self.column_map)
        self.value_format = value_format
        self.logger = logger or (lambda message: print("[beamtune] " + message))

        self._last_id = 0
        #: The ``run`` annex of the proposal last handed out (PROTOCOL.md
        #: section 6): how that setting is to be taken.  None when the backend
        #: sent no hints -- the daemon then keeps its default run sequence.
        self._last_run = None
        #: ``in_reply_to`` of the proposal last taken: which context the
        #: service had fed its backend before computing it, and the outcome.
        #: None when the service sent none (a kick, or an older service).
        self._last_reply = None
        #: the service's newest proposal id as last reported, or None
        self.service_last_id = None
        #: called with each context the service took; the tuning loop keeps
        #: the id of the last one in the ODB
        self.on_delivered = None
        #: called with (context, error) for a context the service refused for
        #: good (see beamtune_client.is_permanent_rejection); it is dropped
        self.on_rejected = None
        #: called once with (context, error) when a context is moved to the
        #: back of the queue after STUCK_AFTER failures in a row
        self.on_stuck = None
        #: context id -> {"row", "total", "serial", "warned"}: failures that
        #: count against that context
        self._ctx_failures = {}
        #: goes up with every successful call; shows the service is answering
        self._success_serial = 0
        self._columns_fetched = False
        #: why the column map could not be fetched, while it cannot; no
        #: proposal is taken meanwhile
        self.column_map_error = None
        self._pending = collections.deque(maxlen=int(max_pending))
        self._failures = 0
        self._breaker_failures = int(breaker_failures)
        self._breaker_cooldown_s = float(breaker_cooldown_s)
        self._muted_until = 0.0
        # DAQ reports have a breaker of their own, so that their successes
        # cannot hide failing contexts and proposals
        self._daq_failures = 0
        self._daq_muted_until = 0.0
        self._counter = 0

    # -- the four methods the daemon calls ---------------------------------

    def AddContext(self, ctxt, step=None, header_reader=None, exposure=None):  # noqa: N802
        """One completed sequence.  ``ctxt`` is a merged file path; the context
        id is its stem (``seq00057``) -- never a path, the service makes a
        directory of it.  ``step`` is the proposal the sequence was scheduled
        with, or None.  The beam header is read by ``header_reader``
        (default ``tuning.read_beamline_header``: ROOT, else uproot).
        ``exposure`` (see ``tuning.TuningLoop.exposure``) goes in as
        ``measurement.exposure`` when given."""
        import ROOT        # lazy: the rest of this module works without ROOT
        from pathlib import Path

        if header_reader is None:
            from pioneer.nearline.tuning import read_beamline_header as header_reader

        filename = str(ctxt)
        knobs, readback = knobs_from_header(header_reader(filename))
        aFile = ROOT.TFile.Open(filename)
        if not aFile or aFile.IsZombie():
            raise OSError("cannot open %s" % filename)
        try:
            hists = [aFile.Get(n) for n in miniTwin_histograms]
            for name, hist in zip(miniTwin_histograms, hists):
                if not hist:
                    raise KeyError("%s has no %s" % (filename, name))
            inline = label_inline(inline_maps(hists), miniTwin_histograms, 1)
        finally:
            aFile.Close()

        context = self._envelope(Path(filename).stem, inline=inline, knobs=knobs,
                                 readback=readback, step=step, exposure=exposure)
        self._enqueue(context)
        return context

    def serialise(self, hist):
        return serialise_hist(hist)

    def BuildContextFiles(self, context_id, files, run_ids, header, step=None,  # noqa: N802
                          inline=None, exposure=None):
        """A context made of file paths, nothing read from the histograms.

        ``files`` are the paths as the service sees them (role hist_root),
        ``run_ids`` the MIDAS run numbers, ``header`` the beam header of the
        first file (see ``knobs_from_header``), ``step`` the proposal the run
        was scheduled with (``proposal_id``, ``step_id``, ``attempt``,
        ``plan``) or None when that is not known.  ``inline`` (see
        ``inline_maps``) goes in as ``measurement.inline`` next to the files;
        the service reads it first.  ``exposure`` (run seconds and WaveDREAM
        events, see ``tuning.TuningLoop.exposure``) goes in as
        ``measurement.exposure`` when given.
        """
        knobs, readback = knobs_from_header(header)
        return self._envelope(
            str(context_id),
            files=[{"path": str(f), "role": HIST_ROOT_ROLE} for f in files],
            knobs=knobs, readback=readback, run_ids=run_ids, step=step, inline=inline,
            exposure=exposure)

    def AddContextFiles(self, context_id, files, run_ids, header, step=None,  # noqa: N802
                        inline=None, exposure=None):
        """``BuildContextFiles`` and post it through the retry queue.  A post
        that fails stays queued and is retried; this only raises when the
        context cannot be built."""
        context = self.BuildContextFiles(context_id, files, run_ids, header, step=step,
                                         inline=inline, exposure=exposure)
        self._enqueue(context)
        return context

    def Enqueue(self, context):                        # noqa: N802
        """Post a built context through the retry queue.  True when the
        queue is empty afterwards.  Never raises."""
        try:
            return self._enqueue(context)
        except Exception as exc:                       # noqa: BLE001 -- never escape
            self._log("Enqueue: %r" % (exc,))
            return False

    def NextConfiguration(self):                       # noqa: N802 -- daemon's API
        """Poll for a newer proposal.  Returns ``[]`` for anything but success.

        Called every mainloop iteration, so it also doubles as the retry pump
        for contexts that could not be delivered earlier.  The proposal is
        asked for first: a context the service keeps failing on must not
        stop the polling (and a successful poll is what lets that context's
        failures count, see _count_failure).
        """
        try:
            if self._muted():
                return []
            payload = self.client.proposal(self._last_id)
            self._succeed()
        except Exception as exc:                       # noqa: BLE001 -- never escape
            self._fail("NextConfiguration: %r" % (exc,))
            return []
        finally:
            try:
                self._flush()
            except Exception as exc:                   # noqa: BLE001 -- never escape
                self._log("flush: %r" % (exc,))

        if not payload.get("ready"):
            if payload.get("last_proposal_id") is not None:
                self.service_last_id = int(payload["last_proposal_id"])
            return []

        proposal_id = int(payload.get("proposal_id", 0))
        self.service_last_id = proposal_id
        if proposal_id <= self._last_id:
            return []
        currents = payload.get("currents") or {}
        if currents and not payload.get("done"):
            try:
                self._check_columns(proposal_id, currents)
            except Exception as exc:                   # noqa: BLE001 -- retried next call
                # Every knob needs its run-database column, or the insert of
                # the row fails after the proposal was taken; leave the
                # proposal for the next call (the watermark does not move).
                error = "proposal %d not taken, no usable knobs.columns: %s" % (proposal_id, exc)
                if error != self.column_map_error:
                    self._log(error)        # once per episode, not every call
                self.column_map_error = error
                return []
        self.column_map_error = None
        self._last_id = proposal_id
        reply = payload.get("in_reply_to")
        self._last_reply = dict(reply) if isinstance(reply, dict) else None

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
        return [{
            "type" : "iter",
            "currents" : [self._row(currents)]
            }
            ]

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
    def last_reply(self):
        """``in_reply_to`` of the proposal last taken, or None."""
        return dict(self._last_reply) if self._last_reply else None

    @property
    def last_proposal_id(self):
        """The newest proposal id seen; proposals up to it are not handed out."""
        return self._last_id

    @last_proposal_id.setter
    def last_proposal_id(self, value):
        self._last_id = int(value or 0)

    def PostDaq(self, report):                         # noqa: N802
        """Send one DAQ progress report.  Not queued (only the latest one
        matters), behind a circuit breaker of its own.
        Returns True when the service took it; never raises."""
        try:
            if time.time() < self._daq_muted_until:
                return False
            self.client.post_daq(report)
            self._daq_failures = 0
            self._daq_muted_until = 0.0
            return True
        except Exception as exc:                       # noqa: BLE001 -- never escape
            self._daq_failures += 1
            if self._daq_failures <= self._breaker_failures:
                self._log("post_daq(%s): %r" % (report.get("stage"), exc))
            if self._daq_failures >= self._breaker_failures:
                self._daq_muted_until = time.time() + self._breaker_cooldown_s
            return False

    def Drop(self, context_id):                        # noqa: N802
        """Take a queued context out of the queue again (the CLI, which
        cannot retry later)."""
        kept = [c for c in self._pending if c.get("context_id") != context_id]
        self._pending.clear()
        self._pending.extend(kept)

    def Flush(self):                                   # noqa: N802
        """Retry queued contexts without asking for a proposal (the daemon
        while paused).  With no proposal poll to show the service is up, a
        failing head context gets one GET /v1/health first: only when that
        answers does its next failure count against it.  Never raises."""
        try:
            if self._pending and not self._muted():
                rec = self._ctx_failures.get(self._pending[0].get("context_id"))
                if rec is not None and rec["serial"] == self._success_serial:
                    try:
                        self.client.health()
                        self._succeed()
                    except Exception as exc:           # noqa: BLE001 -- the service is not well
                        self._fail("health: %r" % (exc,))
                        return False
            return self._flush()
        except Exception as exc:                       # noqa: BLE001 -- never escape
            self._log("Flush: %r" % (exc,))
            return False

    @property
    def last_run_hints(self):
        """The ``run`` annex of the proposal last handed out, or None."""
        return dict(self._last_run) if self._last_run else None

    # -- helpers ------------------------------------------------------------

    def _row(self, currents):
        """The run-database row of a proposal: every knob under its column.
        _check_columns has made sure each knob has one; knob names are never
        used as columns."""
        columns = self._column_map()
        return {columns[name]: self.value_format(value) for name, value in currents.items()}

    def _check_columns(self, proposal_id, currents):
        """Raise unless every knob of `currents` has a column.  A knob the
        cached map does not have makes the next call fetch the map again
        (the service may have switched beam files)."""
        columns = self._column_map()
        missing = sorted(name for name in currents if name not in columns)
        if missing:
            if not self._fixed_columns:
                self.column_map, self._columns_fetched = {}, False
            raise BeamTuneError("knob(s) %s have no run-database column in knobs.columns"
                                % ", ".join(missing))

    def _column_map(self):
        """``column_map`` given at construction, else the service's
        ``knobs.columns`` (from the beam file), fetched lazily.  No answer,
        an error answer, or a config without (or with an empty)
        ``knobs.columns`` raises, and it is asked again next time: rows are
        never written with knob names as columns."""
        if self.column_map or self._columns_fetched:
            return self.column_map
        answer = self.client.config()
        # _call hands some 4xx back as a payload: that is no answer either
        if not isinstance(answer, dict) or answer.get("error") or not isinstance(answer.get("config"), dict):
            raise BeamTuneError("GET /v1/config gave no config: %r"
                                % ((answer or {}).get("error") if isinstance(answer, dict) else answer))
        columns = (answer["config"].get("knobs") or {}).get("columns") or {}
        if not isinstance(columns, dict) or not columns:
            raise BeamTuneError("GET /v1/config has no knobs.columns (knob -> run-database column)")
        self.column_map = {str(k): str(v) for k, v in columns.items()}
        self._columns_fetched = True
        self._log("column map from service: %d knobs" % len(self.column_map))
        return self.column_map

    def _envelope(self, context_id, files=None, inline=None, knobs=None,
                  objective=None, run_ids=None, readback=None, valid=True, step=None,
                  exposure=None):
        if not knobs:
            raise ValueError("context %s has no knobs (no type 1/4/5 device in the beam header)"
                             % context_id)
        knobs = dict(knobs)
        measurement = {"kind": "psm_nearline", "valid": bool(valid),
                       "files": files or []}
        if inline:
            measurement["inline"] = inline
        if objective is not None:
            measurement["objective"] = objective
        if exposure is not None:
            measurement["exposure"] = exposure
        provenance = {"run_ids": list(run_ids or []), "config_type": self.config_type}
        step = step or {}
        # Hand the plan step back to the backend so it can tick it off
        # without guessing from the knob vector. Unknown means omitted: the
        # service then matches the context by its setting.
        for key in ("step_id", "attempt", "plan"):
            if step.get(key) is not None:
                provenance[key] = step[key]
        provenance["source"] = "nearline-daemon"
        envelope = {"schema": CONTEXT_SCHEMA, "context_id": context_id}
        if step.get("proposal_id"):
            envelope["responds_to"] = {"proposal_id": int(step["proposal_id"])}
        envelope.update({
            "provenance": provenance,
            "setting": ({"units": "A", "knobs": knobs, "readback": dict(readback)}
                        if readback else {"units": "A", "knobs": knobs}),
            "measurement": measurement,
        })
        return envelope

    # -- delivery, with retry and a circuit breaker -------------------------

    def _enqueue(self, context):
        if len(self._pending) == self._pending.maxlen:
            dropped = self._pending.popleft()
            self._log("pending queue full; dropping oldest context %s"
                      % dropped.get("context_id"))
            self._drop(dropped, BeamTuneError("dropped: the queue of %d undelivered contexts was full"
                                              % self._pending.maxlen))
        self._pending.append(context)
        return self._flush()

    def _drop(self, context, exc):
        """A context leaves the queue undelivered: tell on_rejected."""
        self._ctx_failures.pop(context.get("context_id"), None)
        if self.on_rejected is not None:
            try:
                self.on_rejected(context, exc)
            except Exception as cb_exc:                # noqa: BLE001 -- never escape
                self._log("on_rejected(%s): %r" % (context.get("context_id"), cb_exc))

    def _count_failure(self, context, exc):
        """Count a failed post against `context` -- but only when the service
        has answered some other call since this context last failed, so a
        service that is simply down (every call failing) is left to the
        breaker and costs no context anything.  Token, route and rate
        answers (NOT_ABOUT_THE_BODY) never count either.  Returns
        "rotate", "drop" or None."""
        if getattr(exc, "status", None) in NOT_ABOUT_THE_BODY:
            return None
        context_id = context.get("context_id")
        rec = self._ctx_failures.get(context_id)
        if rec is None:
            rec = self._ctx_failures[context_id] = {"row": 0, "total": 0, "serial": None,
                                                    "warned": False}
        if rec["serial"] is not None and rec["serial"] == self._success_serial:
            return None
        rec["serial"] = self._success_serial
        rec["row"] += 1
        rec["total"] += 1
        if rec["total"] >= GIVE_UP_AFTER:
            return "drop"
        if rec["row"] >= STUCK_AFTER and len(self._pending) > 1:
            rec["row"] = 0
            return "rotate"
        return None

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
                if is_permanent_rejection(exc):
                    # the service is there and refuses this body: retrying
                    # it forever would hold up every context behind it
                    self._succeed()
                    self._pending.popleft()
                    self._log("context %s rejected, dropped: %s" % (context.get("context_id"), exc))
                    self._drop(context, exc)
                    continue
                verdict = self._count_failure(context, exc)
                self._fail("post_context(%s): %r" % (context.get("context_id"), exc))
                if verdict == "drop":
                    self._pending.popleft()
                    self._log("context %s dropped after %d failures" % (context.get("context_id"),
                                                                        GIVE_UP_AFTER))
                    self._drop(context, BeamTuneError("given up after %d failed posts; last: %s"
                                                      % (GIVE_UP_AFTER, exc),
                                                      status=getattr(exc, "status", None)))
                    continue
                if verdict == "rotate":
                    # let the contexts behind it through; it is tried again later
                    self._pending.rotate(-1)
                    rec = self._ctx_failures[context.get("context_id")]
                    if not rec["warned"] and self.on_stuck is not None:
                        rec["warned"] = True
                        try:
                            self.on_stuck(context, exc)
                        except Exception as cb_exc:    # noqa: BLE001 -- never escape
                            self._log("on_stuck(%s): %r" % (context.get("context_id"), cb_exc))
                    continue
                return False
            self._pending.popleft()
            self._ctx_failures.pop(context.get("context_id"), None)
            if self.on_delivered is not None:
                try:
                    self.on_delivered(context)
                except Exception as exc:               # noqa: BLE001 -- never escape
                    self._log("on_delivered(%s): %r" % (context.get("context_id"), exc))
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
        self._success_serial += 1

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

    @property
    def daq_muted(self):
        return time.time() < self._daq_muted_until
