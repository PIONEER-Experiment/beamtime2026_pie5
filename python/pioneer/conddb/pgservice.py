"""Expand a libpq ``service=NAME`` into an explicit, password-free conninfo.

Every host names its conditions database by one libpq service,
``pioneer-conditions`` (read) or ``pioneer-conditions-admin`` (write), defined
in ``~/.pg_service.conf`` with the password in ``~/.pgpass``. The C++
conditions service records only what it can parse out of the conninfo it is
given (``PICondPgLayer::Describe``, which does not expand services), so a bare
``service=pioneer-conditions`` would reach every ConditionsHeader as
``host=<default>``. This module does the expansion first, in Python, and hands
the job the explicit ``host= port= dbname= user=`` string instead.

    >>> resolve_conninfo("service=pioneer-conditions")          # doctest: +SKIP
    'host=testbeam-pgdb port=5432 dbname=conditions user=cond_viewer'

The service file is read the way libpq 18 reads it (parseServiceInfo and
parseServiceFile in fe-connect.c), not the way configparser would. The user file
is ``servicefile=`` from the string if given, else ``$PGSERVICEFILE`` -- which
must then exist -- else ``~/.pg_service.conf`` if it exists; a service not
defined there is looked up in ``$PGSYSCONFDIR/pg_service.conf`` (only when
``PGSYSCONFDIR`` is set: libpq's compiled-in default differs per build, and
guessing it would make the answer depend on the host in a way nobody could
see). Within a file: every line is trimmed at both ends, empty lines and lines
starting with ``#`` are skipped, only the lines of the first ``[NAME]`` group
are parsed (everything else, including lines before the first group, is
ignored), each is split at its first ``=`` with no further trimming, the key
must be a libpq keyword, and the first value of a key wins. Keys written
explicitly next to ``service=`` win over the service's keys, as in libpq.

The secrets (``password``, ``sslpassword``, the SCRAM keys, the OAuth client
secret) are dropped wherever they come from, and so are ``service`` and
``servicefile``: the result is meant to be baked into rendered job files and
stamped into output files, and the password belongs in ``~/.pgpass`` (or
``PGPASSWORD``), which libpq still reads when the job connects. A result without
a host (or hostaddr) or without a dbname is refused, so that no output file can
record ``host=<default>``.

The standard library only. The nearline job runs this under gaudirun.py's
python inside the testbeam-midas container, where psycopg is not installed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = ["ServiceNotFound", "resolve_conninfo", "describe", "parse_conninfo",
           "format_conninfo", "service_files"]

# The keys that lead the formatted string, in this order; everything else
# follows sorted by name, so one conninfo always formats to one string.
_LEADING = ("host", "port", "dbname", "user")
# Never part of the result: the secrets, and the indirection this module removes.
_DROPPED = ("password", "sslpassword", "scram_client_key", "scram_server_key",
            "oauth_client_secret", "service", "servicefile")
# libpq 18's connection keywords (PQconndefaults); a service file line or a
# conninfo key outside this set is an error, as it is for libpq.
KEYWORDS = frozenset("""
    service servicefile user password passfile channel_binding connect_timeout dbname
    host hostaddr port client_encoding options application_name
    fallback_application_name keepalives keepalives_idle keepalives_interval
    keepalives_count tcp_user_timeout sslmode sslnegotiation sslcompression sslcert
    sslkey sslcertmode sslpassword sslrootcert sslcrl sslcrldir sslsni requirepeer
    require_auth min_protocol_version max_protocol_version ssl_min_protocol_version
    ssl_max_protocol_version gssencmode krbsrvname gsslib gssdelegation replication
    target_session_attrs load_balance_hosts scram_client_key scram_server_key
    oauth_issuer oauth_client_id oauth_client_secret oauth_scope
""".split())
# fgets() into a 1024-byte buffer: a line that fills it is an error in libpq.
_MAX_LINE = 1023


class ServiceNotFound(ValueError):
    """``service=NAME`` names a service no service file defines."""


def service_files(environ=None, servicefile=None) -> list[Path]:
    """The service files libpq would consider, in lookup order."""
    env = os.environ if environ is None else environ
    if servicefile:
        user = servicefile
    elif env.get("PGSERVICEFILE"):
        user = env["PGSERVICEFILE"]
    else:
        user = str(Path(env.get("HOME") or Path.home()) / ".pg_service.conf")
    files = [Path(user).expanduser()]
    if env.get("PGSYSCONFDIR"):
        files.append(Path(env["PGSYSCONFDIR"]).expanduser() / "pg_service.conf")
    return files


_KEY = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*")


def parse_conninfo(text: str) -> dict[str, str]:
    """A libpq keyword/value conninfo string -> {key: value}.

    Values may be bare (up to the next whitespace) or single-quoted; inside
    either, a backslash escapes the next character. A later duplicate key
    wins, as in libpq. URIs (``postgresql://...``) are not accepted: the job
    and the tools only ever pass the keyword form.
    """
    text = str(text)
    if re.match(r"\s*postgres(ql)?://", text):
        raise ValueError("conninfo must be in keyword=value form, not a URI: "
                         "write 'host=H port=P dbname=D user=U' or 'service=NAME'")
    out: dict[str, str] = {}
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return out
        match = _KEY.match(text, pos)
        if not match:
            # The position only: the text there could be a password.
            raise ValueError(f"malformed conninfo at character {pos + 1}: expected key=value")
        key = match.group(1)
        if key not in KEYWORDS:
            raise ValueError(f"invalid connection option {key!r}")
        pos = match.end()
        value = []
        if pos < len(text) and text[pos] == "'":
            pos += 1
            while True:
                if pos >= len(text):
                    raise ValueError(f"unterminated quoted value for {key!r} in conninfo")
                ch = text[pos]
                if ch == "\\" and pos + 1 < len(text):
                    value.append(text[pos + 1])
                    pos += 2
                elif ch == "'":
                    pos += 1
                    break
                else:
                    value.append(ch)
                    pos += 1
        else:
            while pos < len(text) and not text[pos].isspace():
                if text[pos] == "\\" and pos + 1 < len(text):
                    value.append(text[pos + 1])
                    pos += 2
                else:
                    value.append(text[pos])
                    pos += 1
        out[key] = "".join(value)


def _quote(value: str) -> str:
    """One value as libpq reads it back: bare when it can be, else quoted."""
    if value and not re.search(r"[\s'\\]", value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def format_conninfo(keys: dict[str, str]) -> str:
    """{key: value} -> the stable string: host, port, dbname, user, rest sorted."""
    order = [k for k in _LEADING if k in keys] + sorted(k for k in keys if k not in _LEADING)
    return " ".join(f"{k}={_quote(str(keys[k]))}" for k in order)


def _read_service(path: Path, name: str) -> dict[str, str] | None:
    """The keys of [name] in one service file, as libpq's parseServiceFile reads
    them; None when the file has no such group. ValueError on what libpq rejects."""
    keys: dict[str, str] = {}
    found = False
    with open(path, encoding="utf-8", errors="surrogateescape", newline="\n") as f:
        for linenr, raw in enumerate(f, start=1):
            if len(raw) >= _MAX_LINE:
                raise ValueError(f"line {linenr} too long in service file {path}")
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                if found:
                    break                  # the end of the group: the first one wins
                found = (line[1:1 + len(name)] == name and len(line) > len(name) + 1
                         and line[len(name) + 1] == "]")
                continue
            if not found:
                continue                   # other groups, and lines before any group
            key, sep, value = line.partition("=")
            if not sep or key not in KEYWORDS:
                raise ValueError(f"syntax error in service file {path}, line {linenr}")
            if key in ("service", "servicefile"):
                raise ValueError(f'nested "{key}" specifications not supported in '
                                 f"service file {path}, line {linenr}")
            keys.setdefault(key, value)    # the first value of a key wins
    return keys if found else None


def resolve_conninfo(text: str, environ=None) -> str:
    """Expand ``service=NAME`` and return the explicit, password-free conninfo.

    A string without ``service=`` is normalised the same way (secrets dropped,
    stable key order). Raises ServiceNotFound, naming every file searched, when
    no service file defines NAME; ValueError for a service file libpq would
    reject, a missing ``$PGSERVICEFILE``, an empty service name, or a result
    without a host or a dbname.
    """
    given = parse_conninfo(text)
    keys: dict[str, str] = {}
    if "service" in given:
        name = given["service"]
        if not name:
            raise ValueError("service= is empty: name the service, e.g. service=pioneer-conditions")
        env = os.environ if environ is None else environ
        files = service_files(env, given.get("servicefile"))
        user, sysconf = files[0], files[1:]
        explicit = bool(given.get("servicefile") or env.get("PGSERVICEFILE"))
        found = None
        if explicit and not user.is_file():
            raise ValueError(f"service file {user} not found "
                             f"({'servicefile=' if given.get('servicefile') else 'PGSERVICEFILE'})")
        if user.is_file():
            found = _read_service(user, name)
        for path in sysconf:
            if found is None and path.is_file():
                found = _read_service(path, name)
        if found is None:
            searched = ", ".join(f"{p}{'' if p.is_file() else ' (missing)'}" for p in files)
            raise ServiceNotFound(f"libpq service '{name}' is not defined in any service "
                                  f"file; searched: {searched}")
        keys.update(found)
    keys.update(given)
    for key in _DROPPED:
        keys.pop(key, None)
    missing = [k for k, ok in (("host", keys.get("host") or keys.get("hostaddr")),
                               ("dbname", keys.get("dbname"))) if not ok]
    if missing:
        raise ValueError(f"the conninfo ({describe(format_conninfo(keys))}) has no "
                         f"{' and no '.join(missing)}: name them, so that the output files "
                         "record which server and database served the constants")
    return format_conninfo(keys)


def describe(conninfo: str) -> str:
    """host/port/dbname of a conninfo, as PICondPgLayer::Describe prints them.

    Never the user, never the password: this is what goes into logs and banners.
    An unparseable string is described as such rather than echoed, because the
    offending token could be the password.
    """
    try:
        keys = parse_conninfo(conninfo)
    except ValueError:
        return "<unparseable conninfo>"
    out = "host=" + (keys.get("host") or keys.get("hostaddr") or "<default>")
    if keys.get("port"):
        out += " port=" + keys["port"]
    if keys.get("dbname"):
        out += " dbname=" + keys["dbname"]
    return out
