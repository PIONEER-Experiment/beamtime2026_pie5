"""The command layer between a caller and the views: arguments in, one JSON string out.

Both callers come through here -- the MIDAS RPC server for the custom page, and
`python -m pioneer.rundb.view` for a shell -- so the page and the command line
cannot drift apart, and every rule about what may be asked for lives in one
place.

Nothing in this module talks to PostgreSQL or to MIDAS.  It imports neither, on
purpose: it can be read, tested and reasoned about without a database or an
experiment, and a test asserts that the imports stay out.

Two groups of commands:

* READ_COMMANDS never change anything and are always answered.
* ACTION_COMMANDS change the run database and are refused with
  `kind: "denied"` unless the client was started with an actions module *and*
  the ODB flag says they are allowed.  Both gates are checked here, on every
  single call, so arming one of them alone changes nothing.

Every reply is an envelope, and the envelope is returned for errors as well --
`mjsonrpc` drops the reply string of an RPC that returns anything other than
SUCCESS, so a failure reported as a status code would reach the page as nothing
at all.
"""

import json
import re
import time
from datetime import datetime

# Views that only read.  The command-line tool offers exactly these.
#
# `preview_five_point` is here rather than beside the action it previews: it
# writes nothing, so it is not behind the ODB flag.  It does need the action
# module, because the database it reads is the one the client was told to write
# and nothing else knows that connection string.
READ_COMMANDS = ("status", "runlog", "queue", "run", "sequences", "config",
                 "preview_five_point")

# What `python -m pioneer.rundb.view` offers.  `preview_five_point` is left out
# of it: that command needs a list of configuration ids and the action module's
# connection string, neither of which the read command line has, and the manual
# way to preview a scan is `python -m pioneer.rundb.actions five-point …`
# without `--confirm`.
CLI_COMMANDS = tuple(cmd for cmd in READ_COMMANDS if cmd != "preview_five_point")

# Commands that write to the run database.  Behind two gates, see the module
# docstring; the module that implements them is supplied separately.
ACTION_COMMANDS = ("schedule_five_point",)

# The error kinds the JSON contract names.
ERROR_KINDS = ("usage", "db", "too_large", "denied", "unknown_command", "internal")

# Bounds on what a caller may ask for.  A page that asks for everything is a
# page that makes the client hold a large reply in memory and Postgres scan
# more than it needs to.
MAX_ROWS = 200
DEFAULT_RUNLOG_ROWS = 50
DEFAULT_QUEUE_ROWS = 50
DEFAULT_SEQUENCE_ROWS = 20

# What each command accepts: name -> (kind, default).  `required` has no
# default and must be given.
_ARGUMENTS = {
    "status": {},
    "runlog": {"limit": ("rows", DEFAULT_RUNLOG_ROWS), "before_id": ("id_or_none", None)},
    "queue": {"limit": ("rows", DEFAULT_QUEUE_ROWS)},
    "run": {"id": ("required_id", None)},
    "sequences": {"limit": ("rows", DEFAULT_SEQUENCE_ROWS)},
    "config": {"id": ("required_id", None)},
    "schedule_five_point": {
        "config_ids": ("required_id_list", None),
        "requested_events": ("events", 1_000_000),
    },
    # The same arguments as the action, so that what is previewed and what is
    # then scheduled cannot be two different things.
    "preview_five_point": {
        "config_ids": ("required_id_list", None),
        "requested_events": ("events", 1_000_000),
    },
}

# Anything that looks like a password is removed before a message is sent on.
_PASSWORD = re.compile(r"(password|sslpassword)\s*=\s*\S+", re.IGNORECASE)


class CommandError(Exception):
    """A command cannot be carried out, and the caller should be told why."""

    def __init__(self, kind: str, message: str, hint: str | None = None, **extra):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.extra = extra


def _now() -> str:
    """The local time with its offset, which is what the page shows."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_message(text: str) -> str:
    """One line, no traceback, no password.

    Whatever goes in an envelope is shown on a page that a shifter reads at
    three in the morning and may end up in a screenshot, so it gets the first
    line of the text with any connection-string password blanked out.
    """
    first = str(text).strip().splitlines()
    line = first[0].strip() if first else "unknown error"
    return _PASSWORD.sub(r"\1=***", line)


def _as_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise CommandError("usage", f"{name} must be a whole number")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise CommandError("usage", f"{name} must be a whole number") from None
    return number


def parse_args(cmd: str, args) -> dict:
    """Check and normalise the arguments of one command.

    `args` is the JSON object the caller sent, as a string (that is how jrpc
    carries it), as a dict, or empty.  Unknown keys are refused rather than
    ignored: a page asking for something this client does not understand is a
    version mismatch, and silently dropping it would hide that.
    """
    if cmd not in _ARGUMENTS:
        raise CommandError("unknown_command", f"unknown command {cmd!r}",
                           hint="known commands: " + ", ".join(known_commands()))

    if args is None or args == "":
        given = {}
    elif isinstance(args, str):
        try:
            given = json.loads(args)
        except ValueError as exc:
            raise CommandError("usage", f"arguments are not JSON: {safe_message(exc)}") from None
    elif isinstance(args, dict):
        given = dict(args)
    else:
        raise CommandError("usage", "arguments must be a JSON object")

    if not isinstance(given, dict):
        raise CommandError("usage", "arguments must be a JSON object")

    spec = _ARGUMENTS[cmd]
    unknown = sorted(set(given) - set(spec))
    if unknown:
        raise CommandError(
            "usage",
            f"{cmd} does not take " + ", ".join(unknown),
            hint="accepted: " + (", ".join(sorted(spec)) or "no arguments"),
        )

    out = {}
    for name, (kind, default) in spec.items():
        value = given.get(name, None)
        if kind == "rows":
            if value is None:
                out[name] = default
            else:
                # Clamped, not refused: a page asking for too much should get
                # what it is allowed to have rather than an error.
                out[name] = max(1, min(MAX_ROWS, _as_int(name, value)))
        elif kind == "id_or_none":
            out[name] = None if value is None else _as_int(name, value)
        elif kind == "required_id":
            if value is None:
                raise CommandError("usage", f"{cmd} needs {name}")
            out[name] = _as_int(name, value)
        elif kind == "required_id_list":
            if value is None:
                raise CommandError("usage", f"{cmd} needs {name}")
            if not isinstance(value, (list, tuple)) or not value:
                raise CommandError("usage", f"{name} must be a non-empty list of ids")
            out[name] = [_as_int(name, item) for item in value]
        elif kind == "events":
            out[name] = default if value is None else _as_int(name, value)
    return out


def known_commands() -> tuple:
    """Every command this client answers, read ones first."""
    return READ_COMMANDS + ACTION_COMMANDS


def ok_envelope(cmd: str, data: dict, query_ms: int) -> dict:
    return {"ok": True, "cmd": cmd, "generated": _now(), "query_ms": query_ms, "data": data}


def error_envelope(cmd: str, kind: str, message: str, hint: str | None = None, **extra) -> dict:
    if kind not in ERROR_KINDS:
        kind = "internal"
    error = {"kind": kind, "message": safe_message(message)}
    if hint:
        error["hint"] = hint
    error.update(extra)
    return {"ok": False, "cmd": cmd, "error": error}


# The smallest valid reply there is.  Used when a caller's buffer cannot hold
# even the explanation of why it could not hold the reply.
_FLOOR = '{"ok":false}'


def _encode(envelope: dict, compact: bool = False) -> str:
    """JSON, ASCII only.

    `ensure_ascii=True` is explicit and load bearing: MIDAS copies the reply
    into a fixed buffer counting *characters* and truncates the *bytes*
    (`midas/callbacks.py`), so a non-ASCII character would make the two counts
    disagree and could cut a character in half.  Keeping the text ASCII makes
    one byte one character and the size arithmetic below exact.
    """
    separators = (",", ":") if compact else None
    return json.dumps(envelope, default=str, ensure_ascii=True, separators=separators)


def encoded_length(text: str) -> int:
    """The number of bytes the reply occupies, which is what MIDAS counts."""
    return len(text.encode("utf-8"))


def fits(text: str, max_len: int | None) -> bool:
    """Whether a reply of this size arrives whole.

    Strictly smaller, not "at most": the MIDAS callback writes
    `min(len, max_len - 1)` bytes and then a terminating NUL, so a reply
    exactly `max_len` long comes back one byte short -- and a page that then
    retried with `max_reply_length = needed` would be one byte short again,
    every time.
    """
    return max_len is None or encoded_length(text) < max_len


def encode_within(envelope: dict, max_len: int | None, text: str | None = None) -> str:
    """Encode an envelope so that it arrives whole, whatever the buffer is.

    Three steps down: the envelope itself, a `too_large` envelope naming the
    size that was needed, the same without its hint and without spaces, and
    finally the bare floor -- which cannot be made any smaller, so a caller
    offering fewer bytes than that gets it anyway.
    """
    text = _encode(envelope) if text is None else text
    if fits(text, max_len):
        return text

    cmd = str(envelope.get("cmd") or "")
    needed = encoded_length(text)
    text = _encode(error_envelope(
        cmd, "too_large", "reply too large",
        hint="ask again with a larger buffer or a smaller limit",
        needed=needed, limit=int(max_len),
    ))
    if fits(text, max_len):
        return text

    text = _encode(error_envelope(cmd, "too_large", "reply too large",
                                  needed=needed, limit=int(max_len)), compact=True)
    return text if fits(text, max_len) else _FLOOR


def _call(view, actions, cmd: str, args: dict, actions_allowed: bool):
    """Run one command against the view, or against the actions module."""
    if cmd == "status":
        return view.status(actions_allowed=bool(actions_allowed),
                           actions_built=actions is not None)
    if cmd == "runlog":
        return view.runlog(limit=args["limit"], before_id=args["before_id"])
    if cmd == "queue":
        return view.queue(limit=args["limit"])
    if cmd == "run":
        return view.run(args["id"])
    if cmd == "sequences":
        return view.sequences(limit=args["limit"])
    if cmd == "config":
        return view.config(args["id"])
    if cmd == "preview_five_point":
        # One gate, not two: this only reads, so the ODB flag does not come
        # into it.  What it needs is the action module, which is where the
        # connection string for the database it reads lives.
        if actions is None:
            raise CommandError(
                "denied",
                "this client cannot preview a scan",
                hint="preview needs a client started with --allow-actions",
            )
        return actions.preview_five_point(**args)
    if cmd in ACTION_COMMANDS:
        if actions is None or not actions_allowed:
            raise CommandError(
                "denied",
                "actions are disabled on this client",
                hint="start the client with --allow-actions and --write-dsn, and set "
                     "/RunDBView/Allow actions to true",
            )
        return getattr(actions, cmd)(**args)
    raise CommandError("unknown_command", f"unknown command {cmd!r}",
                       hint="known commands: " + ", ".join(known_commands()))


def dispatch(view, actions, cmd: str, args=None, max_len: int | None = None,
             actions_allowed: bool = False) -> str:
    """Answer one command.  Always returns a JSON string, never raises."""
    return dispatch_envelope(view, actions, cmd, args, max_len, actions_allowed)[1]


def dispatch_envelope(view, actions, cmd: str, args=None, max_len: int | None = None,
                      actions_allowed: bool = False) -> tuple:
    """Answer one command, giving back both the envelope and its text.

    `max_len` is the largest reply the caller can take; `None` means no limit,
    which is what the command line uses.  A reply that does not fit comes back
    as a small `too_large` envelope naming the size that would have been
    needed, so the page can ask again with a bigger buffer instead of trying to
    parse a reply that was cut in half.

    The envelope is returned beside the text so that a caller which needs to
    know whether the command worked does not have to search a reply that may be
    hundreds of kilobytes long.
    """
    cmd = str(cmd or "").strip()
    try:
        args = parse_args(cmd, args)
        started = time.monotonic()
        data = _call(view, actions, cmd, args, actions_allowed)
        query_ms = int(round((time.monotonic() - started) * 1000))
        envelope = ok_envelope(cmd, data, query_ms)
    except CommandError as exc:
        envelope = error_envelope(cmd, exc.kind, exc.message, exc.hint, **exc.extra)
    except Exception as exc:  # noqa: BLE001 - the caller gets an envelope, whatever happened
        # ViewError and anything like it carry their own kind; anything else is
        # a bug in this client and says so rather than leaking a traceback.
        # `data` is how a failure says what it left behind -- an action that
        # gave up half way names the runs it had already created, and that has
        # to reach the page beside the message.
        kind = getattr(exc, "kind", None)
        hint = getattr(exc, "hint", None)
        extra = getattr(exc, "data", None)
        envelope = error_envelope(cmd, kind if kind in ERROR_KINDS else "internal",
                                  str(exc) or exc.__class__.__name__, hint,
                                  **(extra if isinstance(extra, dict) else {}))

    text = _encode(envelope)
    if not fits(text, max_len):
        # The reply is replaced on the way out, and the caller is told what it
        # is actually holding rather than what it would have been.
        text = encode_within(envelope, max_len, text=text)
        envelope = json.loads(text)
    return envelope, text
