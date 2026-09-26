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
import os
import signal
import sys
import time

import midas
import midas.client

from pioneer.rundb import commands, pg
from pioneer.rundb.view import RunDbView

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
        allowed = actions_allowed(client) if cmd in commands.ACTION_COMMANDS else False
        try:
            envelope, reply = commands.dispatch_envelope(
                self.view, self.actions, cmd, args,
                max_len=max_len, actions_allowed=allowed,
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
            if allowed and self.actions is not None:
                worked = bool(envelope.get("ok"))
                line = (f"{self.view.client_name}: action {cmd} "
                        f"{'accepted' if worked else 'refused'}: {args}")
                if worked:
                    # What it created, in the log, so the Messages page answers
                    # "which runs are these?" without anybody opening psql.
                    data = envelope.get("data") or {}
                    line += (f" -> sequence {data.get('sequence_id')}, "
                             f"runs {data.get('run_ids')}")
                client.msg(line, is_error=False)
            else:
                reason = ("actions not built into this client"
                          if self.actions is None
                          else f"actions disabled in {ROOT}/Allow actions")
                client.msg(f"{self.view.client_name}: refused action {cmd} "
                           f"({reason}): {args}", is_error=False)

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
