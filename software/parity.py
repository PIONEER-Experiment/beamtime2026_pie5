#!/usr/bin/env python3
"""Normalised dumps for comparing a build on piana with the same build on pinky.

    parity.py [--host pinky | --host piana --prefix P] cmake-la     BUILD_DIR
    parity.py [...]                                   cmake-extra  BUILD_DIR
    parity.py [...]                                   makeflags    BUILD_DIR
    parity.py [...]                                   cc           BUILD_DIR
    parity.py                                         manifest     BASE [SUBPATH ...]
    parity.py                                         links        BASE [SUBPATH ...]

The formats are those of the pinky reference captured in
scratch/piana-software/reference/ (cmake_la.sh, flatten_build.py, manifest.sh,
normalise.sed), so a plain `diff` of the two sides is the comparison:

  cmake-la     every CMakeCache.txt entry `cmake -LA` prints (all but INTERNAL,
               STATIC, UNINITIALIZED), sorted bytewise
  cmake-extra  the UNINITIALIZED entries (command-line -D the project never
               declared) and the names marked -MODIFIED, which -LA hides
  makeflags    "<relpath>\\t<line>" for every line of every flags.make, link.txt
               and *.rsp below BUILD_DIR, sorted
  cc           BUILD_DIR/compile_commands.json as
               "<file>\\t<directory relative to BUILD_DIR>\\t<command>", sorted
  manifest     "<sha256>  <path relative to BASE>" of every regular file
  links        "<path> -> <target>" of every symlink

Normalisation (not for manifest/links): host install paths become @ROOT@,
@MIDAS@, @CLHEP@, @GAUDI@, @GSL@, @RECO@. On piana the unpacked RPM tree
<prefix>/sysroot/usr becomes /usr (where the same files live on pinky), and on
pinky GSL's /usr/local becomes @GSL@/install. Whatever still names a host path
afterwards is a real difference.

Standard library only (python >= 3.8).
"""

import argparse
import hashlib
import json
import os
import sys

PINKY = [
    ("/home/pinky/software/root-6.38.04", "@ROOT@"),
    ("/home/pinky/packages/midas", "@MIDAS@"),
    ("/home/pinky/packages/clhep", "@CLHEP@"),
    ("/home/pinky/packages/gaudi", "@GAUDI@"),
    ("/home/pinky/packages/gsl", "@GSL@"),
    ("/usr/local", "@GSL@/install"),
    ("/home/pinky/bt2026/reco/repo", "@RECO@"),
]


def piana_map(prefix):
    p = prefix.rstrip("/")
    return [
        (p + "/sysroot/usr", "/usr"),
        (p + "/root-6.38.04", "@ROOT@"),
        (p + "/midas", "@MIDAS@"),
        (p + "/clhep", "@CLHEP@"),
        (p + "/gaudi", "@GAUDI@"),
        (p + "/gsl", "@GSL@"),
        (p + "/src/main", "@RECO@"),
        (p, "@SW@"),
    ]


def normaliser(args):
    if args.host == "pinky":
        pairs = PINKY          # applied in order, like normalise.sed
    elif args.host == "piana":
        if not args.prefix:
            sys.exit("parity.py: --host piana needs --prefix")
        pairs = piana_map(args.prefix)   # most specific first
    else:
        pairs = []

    def norm(text):
        for path, key in pairs:
            text = text.replace(path, key)
        return text
    return norm


def cbytes(lines):
    """Sort like LC_ALL=C sort."""
    return sorted(lines, key=lambda s: s.encode())


def read_cache(build):
    with open(os.path.join(build, "CMakeCache.txt"), errors="replace") as fh:
        return [l.rstrip("\n") for l in fh]


def entry_type(line):
    name_type = line.split("=", 1)[0]
    return name_type.rsplit(":", 1)[-1] if ":" in name_type else ""


def cmd_cmake_la(a, norm):
    out = [norm(l) for l in read_cache(a.path)
           if l and not l.startswith(("#", "//"))
           and entry_type(l) not in ("INTERNAL", "STATIC", "UNINITIALIZED")]
    return cbytes(out)


def cmd_cmake_extra(a, norm):
    cache = read_cache(a.path)
    unini = cbytes(norm(l) for l in cache
                   if l and not l.startswith(("#", "//")) and entry_type(l) == "UNINITIALIZED")
    modified = cbytes(l[:-len("-MODIFIED:INTERNAL=ON")] for l in cache
                      if l.endswith("-MODIFIED:INTERNAL=ON"))
    return (["# UNINITIALIZED (command-line -D not declared by the project)"] + unini
            + ["# -MODIFIED:INTERNAL=ON"] + modified)


def cmd_makeflags(a, norm):
    out = []
    for root, _, files in os.walk(a.path):
        for f in files:
            if f in ("flags.make", "link.txt") or f.endswith(".rsp"):
                p = os.path.join(root, f)
                rel = os.path.relpath(p, a.path)
                with open(p, errors="replace") as fh:
                    for line in fh:
                        line = line.rstrip("\n")
                        if line.strip() and not line.lstrip().startswith("#"):
                            out.append(norm(f"{rel}\t{line}"))
    return cbytes(out)


def cmd_cc(a, norm):
    with open(os.path.join(a.path, "compile_commands.json")) as fh:
        entries = json.load(fh)
    out = []
    for e in entries:
        cmd = e.get("command") or " ".join(e["arguments"])
        d = os.path.relpath(e["directory"], a.path)
        out.append(norm(f"{e['file']}\t{d}\t{cmd}"))
    return cbytes(out)


def walk(base, subs, want_links):
    rows = []
    for sub in subs or ["."]:
        top = os.path.join(base, sub)
        for root, dirs, files in os.walk(top):
            for n in dirs + files:
                p = os.path.join(root, n)
                rel = os.path.normpath(os.path.relpath(p, base))
                if os.path.islink(p):
                    if want_links:
                        rows.append((rel, f"{rel} -> {os.readlink(p)}"))
                elif n in files and not want_links:
                    h = hashlib.sha256()
                    with open(p, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    rows.append((rel, f"{h.hexdigest()}  {rel}"))
    rows.sort(key=lambda r: r[0].encode())
    return [r[1] for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", choices=["pinky", "piana"])
    ap.add_argument("--prefix", help="piana install prefix (with --host piana)")
    ap.add_argument("what", choices=["cmake-la", "cmake-extra", "makeflags", "cc",
                                     "manifest", "links"])
    ap.add_argument("path", help="build directory, or BASE for manifest/links")
    ap.add_argument("subpaths", nargs="*", help="manifest/links: subpaths of BASE")
    a = ap.parse_args()
    if a.what in ("manifest", "links"):
        lines = walk(a.path, a.subpaths, a.what == "links")
    else:
        norm = normaliser(a)
        lines = {"cmake-la": cmd_cmake_la, "cmake-extra": cmd_cmake_extra,
                 "makeflags": cmd_makeflags, "cc": cmd_cc}[a.what](a, norm)
    for l in lines:
        print(l)


if __name__ == "__main__":
    main()
