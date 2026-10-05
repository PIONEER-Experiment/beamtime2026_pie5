"""The MIDAS client that answers the run-database page.

It registers one jrpc function and does nothing else: no equipment, no event
requests, no run transitions.  A custom page calls

    mjsonrpc_call("jrpc", {client_name: "RunDBView", cmd, args, max_reply_length})

mhttpd forwards that to this process, `serve` hands it to
`pioneer.rundb.commands.dispatch`, and the JSON string comes back the same way.
No second port, no second web server, same origin as the rest of mhttpd.

Two things about jrpc decide the shape of this file:

* mhttpd drops the reply string whenever the callback returns a status other
  than SUCCESS, so the caller would see a status code and no message.  The
  callback here therefore returns SUCCESS for everything, including errors,
  which travel inside the JSON envelope where the page can read them.
* the caller says how long a reply it can take, and MIDAS truncates anything
  longer without telling anybody -- truncated JSON does not parse.  `dispatch`
  gets that limit and answers over-long replies with a short `too_large`
  envelope instead.

Configuration lives in `/RunDBView`, seeded when missing and never overwritten,
so what an operator edits survives a restart.  The one exception is
`/RunDBView/Database`, which is written on every connect: it is a description
of where this client is pointed (no password), not a setting.

Credentials never come from the ODB.  They come from `--dsn` or
`$PIONEER_RUNDB_DSN` on the command line that started this process.
"""

import argparse
import functools
import json
import os
import signal
import sys
import time

import midas
import midas.client

from pioneer.rundb import commands, pg
from pioneer.rundb.view import RunDbView
from pioneer.rundb import midas_commands as mcmd

# Where the page's settings live.  Not under /Custom: mhttpd turns every
# subdirectory of /Custom into an entry in the side menu.
ROOT = "/RunDBView"

# Seeded when absent, never overwritten.  `Database` is handled separately.
DEFAULTS = {
    "Allow actions": False,
    "Poll seconds": 5.0,
    "Runlog rows": 50,
    "Runlog refresh seconds": 30.0,
    "Max reply kB": 256,
    "Stale seconds": 20.0,
    "Beamline" : "PiE5",
    "Client name": "RunDBView",
}

# Where the python sequencer says whether it is running.  Read for the two
# clear-queue commands only, see `sequencer_running`.
SEQUENCER_RUNNING = "/PySequencer/State/Running"

# Where the sequencer writes the database id of the run it has loaded
# (`sequencer/config_loader.py`); 0 between runs.  Read beside the flag above,
# see `loaded_run_id`.
LOADED_RUN = "/Nearline/Info/Run DB PK"

# The commands that need to know whether the sequencer is running.
SEQUENCER_COMMANDS = ("clear_queue", "preview_clear_queue")

# The longest a refused clear's summary of its arguments may be in an audit
# line.  The caller chose those arguments, and a refusal is exactly the case
# where they may be anything at all.
AUDIT_ARGS_CHARS = 200

# How many run ids an audit line spells out before it says "and N more".  A
# MIDAS message is one line on the Messages page and has a length limit of its
# own; a clear of a long queue would otherwise be cut off mid-list.
AUDIT_IDS_SHOWN = 40

_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True


def seed(client, dsn: str) -> int:
    """Create any missing key under `/RunDBView`, leaving existing ones alone.

    Key by key rather than as one subtree: `odb_set` removes unspecified keys
    by default, so writing the whole tree would delete anything an operator had
    added to it.
    """
    created = 0
    for key, value in DEFAULTS.items():
        path = f"{ROOT}/{key}"
        if client.odb_exists(path):
            continue
        client.odb_set(path, value)
        created += 1

    # Not a setting: where this client is pointed, for the page to show.
    client.odb_set(f"{ROOT}/Database", pg.describe_dsn(dsn))
    return created


def actions_allowed(client) -> bool:
    """Read the ODB flag, now.

    Read on every action call and never cached: turning actions off has to take
    effect immediately, without restarting the client, and the flag is the only
    thing an operator can reach from the web interface.
    """
    try:
        return bool(client.odb_get(f"{ROOT}/Allow actions"))
    except Exception:  # noqa: BLE001 - a missing or unreadable key means "no"
        return False


def sequencer_running(client) -> bool:
    """Whether the sequencer is running, read from the ODB now.

    Read on every clear-queue call and never taken from the caller: it decides
    whether the run at the head of the queue is protected, and a page must not
    be able to talk the client into cancelling a run the sequencer may be
    loading into the ODB at that moment.  A read that fails, a missing key or
    anything other than an explicit false counts as running -- the cost of being
    wrong that way is one run left in the queue for a second clear, the cost
    of being wrong the other way is a run started on settings that were
    cancelled under it.

    "Explicit false" is `False` or the integer 0: the MIDAS python client
    returns a BOOL key as the int 0 or 1 (seen with midas ee45b114), so a
    test for `False` alone would read a stopped sequencer as running.
    """
    try:
        value = client.odb_get(SEQUENCER_RUNNING)
    except Exception:  # noqa: BLE001 - unreadable means "assume it is running"
        return True
    stopped = isinstance(value, int) and value == 0    # False, or a BOOL read as 0
    return not stopped


def loaded_run_id(client):
    """The database id of the run the sequencer has loaded, or None.

    Read only while the sequencer is running, beside `sequencer_running`: the
    run it has picked is protected by identity as well as by being the head of
    the queue, because the head can move (a run with a lower priority number
    queued while the sequencer waits at a prompt) while the run being set up
    stays the same.  A failed read, a missing key, 0 or anything that is not a
    positive whole number protects nothing extra; the priority head is still
    kept either way.
    """
    try:
        value = client.odb_get(LOADED_RUN)
    except Exception:  # noqa: BLE001 - nothing extra to protect
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _ids_text(ids) -> str:
    """A list of run ids for an audit line, cut at `AUDIT_IDS_SHOWN`."""
    ids = list(ids or [])
    text = ", ".join(str(item) for item in ids[:AUDIT_IDS_SHOWN])
    if len(ids) > AUDIT_IDS_SHOWN:
        text += f" and {len(ids) - AUDIT_IDS_SHOWN} more"
    return f"[{text}]"


def _clear_args_text(args) -> str:
    """What a clear asked for, short enough for one message line.

    The other actions echo their arguments as sent, but a clear carries up to
    two thousand run ids; the audit line says how many, which statuses and who,
    each piece cut short and the whole at `AUDIT_ARGS_CHARS`.  Anything that
    does not parse is echoed as it is (cut the same way), which is itself
    worth seeing.
    """
    try:
        given = json.loads(args) if isinstance(args, str) else dict(args or {})
    except (TypeError, ValueError):
        return repr(args)[:AUDIT_ARGS_CHARS]
    if not isinstance(given, dict):
        return repr(args)[:AUDIT_ARGS_CHARS]
    run_ids = given.get("run_ids")
    count = len(run_ids) if isinstance(run_ids, (list, tuple)) else repr(run_ids)[:20]
    text = (f"operator {repr(given.get('operator'))[:70]}, {count} run id(s) given, "
            f"include_holding {repr(given.get('include_holding', False))[:20]}")
    return text[:AUDIT_ARGS_CHARS]


def _audit_success(cmd: str, args, data: dict) -> str:
    """The part of an accepted action's audit line that says what it did."""
    if cmd == "clear_queue":
        cancelled = data.get("cancelled") or []
        kept = data.get("kept_head") or []
        line = (f"operator {data.get('operator')!r} cancelled {len(cancelled)} run(s) "
                f"{_ids_text(cancelled)} ({', '.join(data.get('statuses') or [])})")
        if kept:
            line += f", kept next run {_ids_text(kept)} (sequencer running)"
        line += f", skipped {len(data.get('skipped') or [])}"
        return line
    # What it created, in the log, so the Messages page answers "which runs
    # are these?" without anybody opening psql.
    return (f"{args} -> sequence {data.get('sequence_id')}, "
            f"runs {data.get('run_ids')}")


class Server:
    """Holds the view and the actions module between calls."""

    def __init__(self, view: RunDbView, actions=None):
        self.view = view
        self.actions = actions
        self.calls = 0
        self.last_cmd = None

    def serve(self, client, cmd, args, max_len):
        """The jrpc callback.  Always SUCCESS, always a JSON string.

        The python client dispatches this from inside `cm_yield`, that is from
        the same thread as the main loop, so the one database connection this
        process holds is never used from two places at once.  What bounds the
        call is the statement timeout on that connection: a slow query gives up
        rather than leaving mhttpd waiting.
        """
        # Stripped once, here, and used for everything after: the gate and the
        # audit line have to be deciding about the same command the dispatcher
        # runs, and the dispatcher strips too.  `" schedule_five_point "` must
        # not be able to slip past a gate and then be executed.
        cmd = str(cmd or "").strip()
        self.calls += 1
        self.last_cmd = cmd
        if cmd in mcmd.MidasCommands:
            # Catch commands that require the midas client to answer properly,
            # e.g. for ODB access or extra messages
            reply = mcmd.call(client = client, cmd = cmd, args = args, view = self.view)

        else:
            # The flag is read for every action and for `status` -- the page
            # decides from the status reply whether to show its buttons at all,
            # so `status` has to report the flag as it is now.  No other read
            # looks at it.
            reads_flag = cmd in commands.ACTION_COMMANDS or cmd == "status"
            allowed = actions_allowed(client) if reads_flag else False
            # Whether the sequencer is running, and which run it has loaded,
            # are the server's to say, not the page's: both are read from the
            # ODB for every clear-queue call and handed to the command layer
            # beside the caller's arguments, which cannot carry them
            # (`commands.parse_args` refuses them as unknown keys).
            extra = None
            if cmd in SEQUENCER_COMMANDS:
                extra = {"sequencer_running": sequencer_running(client)}
                if extra["sequencer_running"]:
                    loaded = loaded_run_id(client)
                    if loaded is not None:
                        extra["loaded_run_id"] = loaded
            try:
                envelope, reply = commands.dispatch_envelope(
                    self.view, self.actions, cmd, args,
                    max_len=max_len, actions_allowed=allowed, server_args=extra,
                )
            except Exception as exc:  # noqa: BLE001 - the page must get JSON whatever happens
                envelope = commands.error_envelope(
                    str(cmd), "internal", f"{exc.__class__.__name__}: {exc}")
                reply = commands.encode_within(envelope, max_len)

            if cmd in commands.ACTION_COMMANDS:
                # Every attempt leaves a line in the MIDAS message log, whether it
                # was carried out or not: the run database is shared, and "who
                # scheduled these runs" has to be answerable from the Messages
                # page -- as does "I pressed the button and nothing happened",
                # which is what a shifter sees when the gates are closed.  A
                # refusal is not an error condition, so it is not highlighted.
                # Both gates, the same way the command layer reads them: the ODB
                # flag can be true on a client that has no action module at all.
                shown = _clear_args_text(args) if cmd == "clear_queue" else args
                if allowed and self.actions is not None:
                    worked = bool(envelope.get("ok"))
                    if worked:
                        line = (f"{self.view.client_name}: action {cmd} accepted: "
                                + _audit_success(cmd, args, envelope.get("data") or {}))
                    else:
                        error = envelope.get("error") or {}
                        line = (f"{self.view.client_name}: action {cmd} refused: {shown}")
                        if cmd == "clear_queue":
                            # Why, since the arguments alone no longer say it.
                            line += (f" ({error.get('kind')}: "
                                     f"{str(error.get('message'))[:AUDIT_ARGS_CHARS]})")
                    client.msg(line, is_error=False)
                else:
                    reason = ("actions not built into this client"
                            if self.actions is None
                            else f"actions disabled in {ROOT}/Allow actions")
                    client.msg(f"{self.view.client_name}: refused action {cmd} "
                            f"({reason}): {shown}", is_error=False)

        return midas.status_codes["SUCCESS"], reply


class ActionAdapter:
    """The action module with the write connection string already bound.

    The command layer passes on only what the caller asked for; which database
    is written, and with which credentials, is decided by the command line that
    started this process and can never come in over RPC.
    """

    def __init__(self, module, write_dsn: str):
        self.module = module
        self.write_dsn = write_dsn

    def __getattr__(self, name):
        return functools.partial(getattr(self.module, name), write_dsn=self.write_dsn)


def load_actions(write_dsn: str) -> ActionAdapter:
    """Import the action module, if this client was told to arm it.

    A separate module so that a client started without `--allow-actions` has no
    code path that can write at all, and so that a checkout without the action
    module still serves every read command.
    """
    from pioneer.rundb import actions as actions_module

    return ActionAdapter(actions_module, write_dsn)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pioneer.rundb.rpc_server",
        description="Answer the run-database custom page over MIDAS jrpc.",
    )
    parser.add_argument("--experiment", default=os.environ.get("MIDAS_EXPT_NAME"),
                        help="MIDAS experiment name; default $MIDAS_EXPT_NAME")
    parser.add_argument("--client", default="RunDBView",
                        help="client name the page calls; must be unique in the experiment")
    parser.add_argument("--dsn", default=None,
                        help=f"read-only libpq connection string; default ${pg.DSN_ENV}")
    parser.add_argument("--timeout-ms", type=int, default=pg.DEFAULT_TIMEOUT_MS,
                        help="statement timeout for every query")
    parser.add_argument("--cycle-ms", type=int, default=200,
                        help="how long one pass through the MIDAS loop waits")
    parser.add_argument("--allow-actions", action="store_true",
                        help="build the action path; the ODB flag still has to allow it")
    parser.add_argument("--write-dsn", default=None,
                        help="connection string used for actions; needs --allow-actions")
    args = parser.parse_args(argv)

    if not args.experiment:
        print("error: no experiment; pass --experiment or set MIDAS_EXPT_NAME",
              file=sys.stderr)
        return 2
    if args.allow_actions and not args.write_dsn:
        print("error: --allow-actions needs --write-dsn", file=sys.stderr)
        return 2

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    dsn = args.dsn or pg.default_dsn()
    view = RunDbView(dsn=dsn, timeout_ms=args.timeout_ms, client_name=args.client)

    actions = None
    if args.allow_actions:
        try:
            actions = load_actions(args.write_dsn)
        except ImportError as exc:
            print(f"error: actions were asked for but are not available: {exc}",
                  file=sys.stderr)
            return 2

    server = Server(view, actions)
    print(f"{args.client}: reading {pg.describe_dsn(dsn)}, "
          f"actions {'built' if actions else 'not built'}", flush=True)

    delay = 1.0
    while not _stop:
        try:
            # The context manager is what frees the RPC server resources on the
            # way out; disconnecting without it leaves stale state that the
            # next register_jrpc_callback trips over after a reconnect.
            with midas.client.MidasClient(args.client, expt_name=args.experiment,
                                          throw_if_already_running=True) as client:
                created = seed(client, dsn)
                if created:
                    print(f"{args.client}: seeded {created} key(s) under {ROOT}",
                          flush=True)
                client.msg(f"{args.client}: connected to {pg.describe_dsn(dsn)}, "
                           f"actions {'enabled' if actions else 'disabled'}")
                client.register_jrpc_callback(server.serve)

                delay = 1.0
                while not _stop:
                    client.communicate(args.cycle_ms)

        except KeyboardInterrupt:
            break
        except Exception as exc:  # noqa: BLE001 - a MIDAS bounce must not end the client
            if _stop:
                break
            print(f"{args.client}: lost MIDAS ({exc.__class__.__name__}: {exc}); "
                  f"retrying in {delay:.0f}s", file=sys.stderr, flush=True)
            waited = 0.0
            while waited < delay and not _stop:
                time.sleep(0.1)
                waited += 0.1
            delay = min(delay * 2, 30.0)

    view.close()
    print(f"{args.client}: stopped after {server.calls} call(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
