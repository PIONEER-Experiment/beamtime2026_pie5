# Copied from Josh: beam-tuning-client/beamtune/client.py

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8420"
CONTEXT_SCHEMA = "beamtune.context/v1"
DAQ_SCHEMA = "beamtune.daq/v1"

#: measurement file role, chosen by extension.  The nearline stitches ROOT into
#: maps.h5 before posting (see the StitchJob), so ".h5" is the normal path and
#: the ROOT roles are the fallback.
ROLE_BY_SUFFIX = {".h5": "maps", ".hdf5": "maps", ".npz": "maps", ".root": "merged_root"}


class BeamTuneError(RuntimeError):
    """Any failure talking to the service.  ``status`` is the HTTP status
    when the service answered, None when it could not be reached."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


#: 4xx answers that say nothing about the body sent: a token, route or rate
#: problem that the next request would hit the same way.  Any other 4xx means
#: the service refused this body and will refuse it again.
NOT_ABOUT_THE_BODY = (401, 403, 404, 405, 408, 429)


def is_permanent_rejection(exc):
    """True when `exc` is the service refusing this body for good (a 4xx
    other than NOT_ABOUT_THE_BODY): retrying it cannot succeed."""
    status = getattr(exc, "status", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in NOT_ABOUT_THE_BODY


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
                    error.get("message") or payload), status=exc.code) from exc
            return exc.code, payload
        except Exception as exc:                       # noqa: BLE001 -- socket, DNS, timeout
            raise BeamTuneError("%s %s failed: %s" % (method, path, exc)) from exc

    # -- the three operations ----------------------------------------------

    def _post_checked(self, path, body):
        # _call hands some 4xx back as (status, payload); a POST whose body was
        # not taken must raise, with the status, so the caller can tell.
        status, payload = self._call("POST", path, body)
        if status >= 400:
            error = payload.get("error") or {} if isinstance(payload, dict) else {}
            raise BeamTuneError("POST %s -> %s: %s" % (path, status, error.get("message") or payload),
                                status=status)
        return payload

    def post_context(self, context):
        """1) Here is a new context."""
        return self._post_checked("/v1/context", context)

    def proposal(self, since=0):
        """2) What is the new current I should set?"""
        return self._call("GET", "/v1/proposal?since=%d" % int(since))[1]

    def post_daq(self, report):
        """DAQ progress of the step in flight (POST /v1/daq).  The service
        keeps the latest report per proposal; it never changes a proposal."""
        return self._post_checked("/v1/daq", report)

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
