"""pioneer.conddb.pgservice: libpq service expansion without libpq.

The service-file cases below were checked against psql 18.6 (libpq's
parseServiceFile): each asserts what libpq does with the same file.
"""

import pytest

from pioneer.conddb.pgservice import (ServiceNotFound, describe, format_conninfo,
                                      parse_conninfo, resolve_conninfo, service_files)

BASE = "host=testbeam-pgdb\nport=5432\ndbname=conditions\n"
SERVICES = """\
# comment
[pioneer-conditions]
host=testbeam-pgdb
port=5432
dbname=conditions
user=cond_viewer
password=secret
sslpassword=alsosecret
connect_timeout=10

[pioneer-conditions-admin]
host=testbeam-pgdb
port=5432
dbname=conditions
user=cond_admin
"""


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "pg_service.conf"
    path.write_text(SERVICES)
    return {"PGSERVICEFILE": str(path), "HOME": str(tmp_path / "home")}


def _env_with(tmp_path, text):
    path = tmp_path / "case.conf"
    path.write_text(text)
    return {"PGSERVICEFILE": str(path), "HOME": str(tmp_path / "home")}


def _app(tmp_path, text):
    got = resolve_conninfo("service=t", _env_with(tmp_path, text))
    return parse_conninfo(got).get("application_name")


def test_service_expands_to_the_stable_explicit_string(env):
    got = resolve_conninfo("service=pioneer-conditions", env)
    assert got == ("host=testbeam-pgdb port=5432 dbname=conditions user=cond_viewer "
                   "connect_timeout=10")


def test_secrets_and_service_are_dropped(env):
    got = resolve_conninfo("service=pioneer-conditions password=other", env)
    for word in ("password", "secret", "other", "service"):
        assert word not in got


def test_explicit_keys_override_the_service(env):
    got = resolve_conninfo("port=5999 service=pioneer-conditions user=me sslmode=disable", env)
    assert got == ("host=testbeam-pgdb port=5999 dbname=conditions user=me "
                   "connect_timeout=10 sslmode=disable")


# -- the service file, as libpq reads it ---------------------------------------

def test_the_first_of_two_groups_of_one_name_wins(tmp_path):
    text = "[t]\n" + BASE + "application_name=first\n[t]\n" + BASE + "application_name=second\n"
    assert _app(tmp_path, text) == "first"


def test_the_first_value_of_a_key_wins(tmp_path):
    assert _app(tmp_path, "[t]\n" + BASE + "application_name=a\napplication_name=b\n") == "a"


def test_lines_outside_the_group_are_not_parsed(tmp_path):
    text = "garbage before any group\n[other]\nthis is not key value\n[t]\n" + BASE
    assert resolve_conninfo("service=t", _env_with(tmp_path, text)).startswith("host=testbeam")


@pytest.mark.parametrize("line", ["application_name = x", "APPLICATION_NAME=x",
                                  "; a comment", "no equals sign", "nokeyword=x"])
def test_what_libpq_rejects_in_the_group_is_a_syntax_error(tmp_path, line):
    with pytest.raises(ValueError, match="syntax error in service file .* line 5"):
        resolve_conninfo("service=t", _env_with(tmp_path, "[t]\n" + BASE + line + "\n"))


def test_a_nested_service_is_refused(tmp_path):
    with pytest.raises(ValueError, match="nested"):
        resolve_conninfo("service=t", _env_with(tmp_path, "[t]\n" + BASE + "service=x\n"))


def test_lines_are_trimmed_but_values_after_the_equals_sign_are_not(tmp_path):
    assert _app(tmp_path, "[t]\n" + BASE + "   application_name=ind   \n") == "ind"
    assert _app(tmp_path, "[t]\n" + BASE + "application_name= lead\n") == " lead"
    assert _app(tmp_path, "[t]\n" + BASE + "application_name=\n") == ""


def test_a_group_header_may_carry_text_after_the_bracket(tmp_path):
    assert resolve_conninfo("service=t", _env_with(tmp_path, "[t] junk\n" + BASE))


def test_an_indented_line_is_not_a_continuation(tmp_path):
    # configparser would fold this into the previous value, newline and all
    got = resolve_conninfo("service=t", _env_with(tmp_path, "[t]\n" + BASE + "  user=u\n"))
    assert "\n" not in got and got.endswith("user=u")


def test_a_line_that_fills_libpqs_buffer_is_refused(tmp_path):
    text = "[t]\n" + BASE + "application_name=" + "x" * 1100 + "\n"
    with pytest.raises(ValueError, match="too long"):
        resolve_conninfo("service=t", _env_with(tmp_path, text))


def test_a_missing_pgservicefile_is_an_error_not_a_fall_through(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "pg_service.conf").write_text("[t]\n" + BASE)
    with pytest.raises(ValueError, match="not found") as err:
        resolve_conninfo("service=t", {"PGSERVICEFILE": str(tmp_path / "nope.conf"),
                                       "PGSYSCONFDIR": str(etc)})
    assert not isinstance(err.value, ServiceNotFound)


def test_servicefile_in_the_string_wins_over_pgservicefile(tmp_path, env):
    own = tmp_path / "own.conf"
    own.write_text("[pioneer-conditions]\nhost=own\ndbname=d\n")
    got = resolve_conninfo(f"service=pioneer-conditions servicefile={own}", env)
    assert got == "host=own dbname=d"


def test_missing_service_names_every_file_searched(env, tmp_path):
    env = dict(env, PGSYSCONFDIR=str(tmp_path / "etc"))
    with pytest.raises(ServiceNotFound) as err:
        resolve_conninfo("service=nope", env)
    msg = str(err.value)
    assert "'nope'" in msg and env["PGSERVICEFILE"] in msg
    assert str(tmp_path / "etc" / "pg_service.conf") + " (missing)" in msg
    assert isinstance(err.value, ValueError)


def test_home_file_when_pgservicefile_is_unset_and_optional_when_missing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert service_files({"HOME": str(home)}) == [home / ".pg_service.conf"]
    with pytest.raises(ServiceNotFound):
        resolve_conninfo("service=pioneer-conditions-admin", {"HOME": str(home)})
    (home / ".pg_service.conf").write_text(SERVICES)
    got = resolve_conninfo("service=pioneer-conditions-admin", {"HOME": str(home)})
    assert got.startswith("host=testbeam-pgdb")


def test_sysconfdir_is_the_fallback_when_the_user_file_lacks_the_service(tmp_path):
    user = tmp_path / "user.conf"
    user.write_text("[other]\nhost=x\n")
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "pg_service.conf").write_text("[pioneer-conditions]\nhost=sys\ndbname=c\n")
    env = {"PGSERVICEFILE": str(user), "PGSYSCONFDIR": str(etc)}
    assert resolve_conninfo("service=pioneer-conditions", env) == "host=sys dbname=c"


def test_user_file_wins_over_sysconfdir(tmp_path):
    user = tmp_path / "user.conf"
    user.write_text("[s]\nhost=user\ndbname=d\n")
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "pg_service.conf").write_text("[s]\nhost=sys\ndbname=d\n")
    assert resolve_conninfo("service=s", {"PGSERVICEFILE": str(user),
                                          "PGSYSCONFDIR": str(etc)}) == "host=user dbname=d"


# -- what the result must hold ---------------------------------------------------

@pytest.mark.parametrize("text", ["dbname=c user=u", "host=h user=u", "service=t"])
def test_a_result_without_host_or_dbname_is_refused(tmp_path, text):
    env = _env_with(tmp_path, "[t]\nuser=u\n")
    with pytest.raises(ValueError, match="has no"):
        resolve_conninfo(text, env)


def test_hostaddr_counts_as_a_host(env):
    assert resolve_conninfo("hostaddr=10.0.0.1 dbname=c", env) == "dbname=c hostaddr=10.0.0.1"


def test_an_empty_service_name_is_refused(env):
    with pytest.raises(ValueError, match="empty"):
        resolve_conninfo("service=", env)


def test_a_plain_conninfo_is_normalised(env):
    got = resolve_conninfo("user=u  dbname=d host=h password=p port=1", env)
    assert got == "host=h port=1 dbname=d user=u"


# -- the conninfo string ---------------------------------------------------------

def test_quoting_round_trips():
    keys = {"host": "h", "options": "-c search_path=a b", "application_name": "it's",
            "sslrootcert": "C:\\x", "sslcert": ""}
    text = format_conninfo(keys)
    assert "options='-c search_path=a b'" in text
    assert parse_conninfo(text) == keys


def test_parse_accepts_libpq_forms():
    assert parse_conninfo(" host = h  dbname='a b' user=x\\ y ") == {
        "host": "h", "dbname": "a b", "user": "x y"}


@pytest.mark.parametrize("bad", ["host", "host='open", "postgresql://h/db", "HOST=h",
                                 "nokeyword=x"])
def test_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_conninfo(bad)


def test_a_parse_error_does_not_echo_the_string():
    with pytest.raises(ValueError) as err:
        parse_conninfo("host=h password=hunter2 'oops")
    assert "hunter2" not in str(err.value)


def test_describe_is_host_port_dbname_only():
    text = "host=testbeam-pgdb port=5432 dbname=conditions user=cond_viewer password=s"
    assert describe(text) == "host=testbeam-pgdb port=5432 dbname=conditions"
    assert describe("hostaddr=10.0.0.1 dbname=c") == "host=10.0.0.1 dbname=c"
    assert describe("dbname=c") == "host=<default> dbname=c"
    assert describe("host='unterminated password=s") == "<unparseable conninfo>"
