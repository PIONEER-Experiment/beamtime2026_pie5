#!/usr/bin/env python3
"""Compare every copy of this package's files against the originals here.

Two copies of a schema are two schemas as soon as one is edited. The DDL here is
the source of truth; three other places carry a copy because they cannot read
these files at the time they need them:

  * ``reco_testbeam/tests/test_conditions_{sqlite,pg}.cpp`` embed it between
    ``// --- schema_<dialect>.sql begin ---`` markers, so the compiled C++ tests
    exercise the real DDL from a build tree that need not have this repo beside
    it. Compared BYTE FOR BYTE.
  * ``psm-nearline-website-2026/db/conddb_schema.sql`` lets the site build a dev
    database and run its tests without importing a loader. Its comment header is
    its own, so only the SQL statements are compared.

  * ``beamline-simulation/psm/psm_conditions.py`` is a copy of ``containers.py``,
    kept there because a notebook in that repo imports it by path and a
    simulation checkout should not need this repo beside it. Compared from the
    end of the module docstring, so each may keep its own header.

Run it after touching any ``.sql`` or ``containers.py`` here. Exit code is the
number of copies that disagree; a copy whose repository is not checked out here
is reported as skipped, not as a failure.

    python3 check_copies.py [--env-root DIR]

Paths are found relative to this file (``<env>/beamtime2026_pie5/python/
pioneer/conddb``) and can each be overridden: ``PICOND_RECO_TESTS``,
``PICOND_SITE_SCHEMA``, ``PICOND_CONTAINERS_COPY``.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

_results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    _results.append((tag, name, detail))
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    _results.append(("SKIP", name, why))
    print(f"[SKIP] {name} -- {why}")


def embedded_ddl(text: str, dialect: str) -> str | None:
    """The .sql copy a C++ test carries between its begin/end markers.

    The test embeds the DDL as a raw string literal; this pulls the SQL back
    out, dropping the C++ raw-string delimiters.
    """
    match = re.search(rf"// --- schema_{dialect}\.sql begin ---\n(.*?)"
                      rf"// --- schema_{dialect}\.sql end ---", text, re.S)
    if not match:
        return None
    body = match.group(1)
    opened = re.match(r'.*?R"([A-Za-z_]*)\(\n', body, flags=re.S)
    if opened:
        # Only the delimiter the literal opened with closes it, so a `)";` that
        # is part of the SQL cannot end it early.
        body = body[opened.end():]
        body = re.sub(rf'\){re.escape(opened.group(1))}";?\s*\Z', "", body)
    return body


def statements(sql: str) -> list[str]:
    """The SQL with comments, blank lines and indentation removed.

    For a copy that is allowed its own header but not its own schema.
    """
    out = []
    for line in sql.splitlines():
        line = re.sub(r"--.*$", "", line).strip()
        if line:
            out.append(re.sub(r"\s+", " ", line))
    return out


def check_cpp_embeds(tests_dir: Path) -> None:
    if not tests_dir.is_dir():
        skip("C++ embedded DDL", f"{tests_dir} not present")
        return
    for dialect, source in (("sqlite", "test_conditions_sqlite.cpp"),
                            ("pg", "test_conditions_pg.cpp")):
        cpp = tests_dir / source
        expected = (HERE / f"schema_{dialect}.sql").read_text()
        if not cpp.exists():
            record(f"{source} embeds schema_{dialect}.sql", False, "file missing")
            continue
        got = embedded_ddl(cpp.read_text(), dialect)
        if got is None:
            record(f"{source} embeds schema_{dialect}.sql", False,
                   f"no '// --- schema_{dialect}.sql begin ---' marker")
            continue
        record(f"{source} embeds schema_{dialect}.sql verbatim", got == expected,
               "" if got == expected else
               f"{len(got)} vs {len(expected)} bytes; re-copy the .sql between the markers")


def check_site_copy(site_schema: Path) -> None:
    if not site_schema.exists():
        skip("website conddb_schema.sql", f"{site_schema} not present")
        return
    want = statements((HERE / "schema_pg.sql").read_text())
    got = statements(site_schema.read_text())
    if got == want:
        record("website conddb_schema.sql matches schema_pg.sql", True,
               f"{len(want)} statement line(s)")
        return
    missing = [line for line in want if line not in got]
    extra = [line for line in got if line not in want]
    detail = f"{len(got)} vs {len(want)} statement line(s)"
    if missing:
        detail += f"; missing e.g. {missing[0][:60]!r}"
    if extra:
        detail += f"; unexpected e.g. {extra[0][:60]!r}"
    record("website conddb_schema.sql matches schema_pg.sql", False, detail)


def check_containers_copy(copy: Path) -> None:
    """`containers.py` and its copy must agree from the docstring's end onwards."""
    if not copy.exists():
        skip("psm_conditions.py copy of containers.py", f"{copy} not present")
        return

    def body(text: str) -> str:
        # Everything from the first statement after the module docstring; each
        # copy is allowed its own header saying which one it is.
        marker = "from __future__ import annotations"
        return text[text.index(marker):] if marker in text else text

    want = body((HERE / "containers.py").read_text())
    got = body(copy.read_text())
    record("psm_conditions.py matches containers.py", got == want,
           "" if got == want else
           f"{len(got)} vs {len(want)} bytes after the docstring; re-copy this file")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env-root", default=None,
                    help="directory holding the sibling checkouts "
                         "(default: four levels above this file)")
    args = ap.parse_args()

    env = Path(args.env_root).resolve() if args.env_root else HERE.parents[3]
    tests = Path(os.environ.get("PICOND_RECO_TESTS")
                 or env / "main" / "reco_testbeam" / "tests")
    site = Path(os.environ.get("PICOND_SITE_SCHEMA")
                or env / "psm-nearline-website-2026" / "db" / "conddb_schema.sql")
    containers = Path(os.environ.get("PICOND_CONTAINERS_COPY")
                      or env / "beamline-simulation" / "psm" / "psm_conditions.py")

    print(f"conditions DDL source of truth: {HERE}")
    check_cpp_embeds(tests)
    check_site_copy(site)
    check_containers_copy(containers)

    failed = [r for r in _results if r[0] == "FAIL"]
    skipped = [r for r in _results if r[0] == "SKIP"]
    print(f"\n{len(_results) - len(failed) - len(skipped)} ok, {len(failed)} out of date, "
          f"{len(skipped)} not checked")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
