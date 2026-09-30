# `software/` — the nearline software stack, the same on pinky and piana

The nearline jobs (`python/pioneer/nearline/`) need ROOT, Gaudi, CLHEP,
Microsoft GSL, MIDAS and a build of `main`. Pinky has them. This folder
rebuilds the same stack on the analysis server piana (`pioneer-analysis`),
from the same sources, commits, options and Fedora packages, so that a job run
on piana gives the same output as on pinky.

| file | what it is |
|---|---|
| `env.sh` | **the one script to source**, on either host. It sets up the stack and this repository's `python/`, and removes an active conda env from the shell. |
| `install.sh` | builds the whole stack under one directory on piana. No root rights, and nothing outside that directory changes. |
| `versions.env` | every pin: tarballs and their sha256, git commits, the Fedora RPM list. |
| `parity.py` | writes the normalised build dumps that `install.sh verify` compares with pinky's. |
| `reference/` | pinky's normalised build dumps (captured read-only on 2026-09-30), what `verify` compares against. |
| `reference-accepted/` | the reviewed differences between those and a correct piana build (see "Known differences"). |

Everything on piana lives under `/home/pioneer/bt2026/software`:

```
sysroot/          pinky's Fedora RPMs that piana lacks, unpacked (cmake, -devel headers, numpy, pip)
root-6.38.04/     the official ROOT binary tarball, as on pinky
gsl/ clhep/       GSL 4.0.0 and CLHEP 2.4.7.1: source, build/, install/
midas/            MIDAS, installed into its own source tree, as on pinky
gaudi/            Gaudi: source/, build/, install/
python/           psycopg (pinky has it in ~/.local)
src/main/         the build of main (build/ and install/ inside, as on pinky)
logs/             one log per install step
verify/           the verify table and the normalised dumps
MANIFEST.txt      every tarball, commit and RPM that went in, with checksums
downloads/ rpms/ cache/ tmp/   downloads and caches; safe to delete after an install
.bt2026-software  marks the directory as install.sh's own
```

A complete install needs about 6 GB of disk.

## Running it

### Install (piana)

First, a clone of this repository inside the prefix, on the branch that
carries `software/` (it has to be pushed first; the website's checkout at
`/home/pioneer/bt2026/beamtime2026_pie5` is only read, and stays on `develop`):

```bash
git clone --no-hardlinks /home/pioneer/bt2026/beamtime2026_pie5 \
    /home/pioneer/bt2026/software/src/beamtime2026_pie5
cd /home/pioneer/bt2026/software/src/beamtime2026_pie5
git fetch origin feature/piana-software && git checkout feature/piana-software
```

Then run the installer inside `tmux` (a `systemd-run --scope` dies with the ssh
session that started it), niced, at idle I/O priority and inside a
memory-capped scope, so it cannot starve the website:

```bash
tmux new -s bt2026-install
cd /home/pioneer/bt2026/software/src/beamtime2026_pie5
nice -n 19 ionice -c3 systemd-run --user --scope \
    -p MemoryMax=16G -p MemoryHigh=12G -p MemorySwapMax=0 \
    bash software/install.sh /home/pioneer/bt2026/software
```

That runs every step in order: `rpms root gsl clhep midas gaudi python main
verify`. A complete install took 12 to 17 minutes in the local test (6 jobs, ROOT
tarball downloaded in under a minute); Gaudi is the longest step (4.5 to 7 minutes). Each
step writes `logs/<step>.log`; the terminal shows one line per step, and every
warning (a host package that is not pinky's build, for example).

`install.sh` only runs on piana (hostname `pioneer-analysis`; any other host
needs `BT2026_ALLOW_HOST=1`, and pinky is always refused). It only installs
into an empty directory, one holding nothing but this clone at `src/<name>`,
or one it has marked with `.bt2026-software` before; it refuses a prefix that
is, contains or lies inside `main`'s checkout or the website's
`beamtime2026_pie5`, and one that is or contains the home directory.

`install.sh` does not depend on the shell it is started from. It restarts
itself with an empty environment (`env -i`, `PATH=/usr/bin:/bin`), so the
conda env and the `MIDASSYS` of the login shell do not reach the build. It
points `HOME`, `TMPDIR` and every cache (pip, dnf, git, XDG) into the prefix.

Options, set in front of the command:

| variable | default | meaning |
|---|---|---|
| `JOBS` | 6 | parallel compile jobs (in the local test the container peaked at 5.2 GB, page cache included, during Gaudi and main) |
| `MAIN_SRC` | `/home/pioneer/bt2026/main` | the checkout `main` is cloned from. It is only read: the clone uses `file://`, and each submodule is cloned from the matching submodule of that checkout, so nothing is fetched from GitHub |
| `REFERENCE_DIR` | `software/reference` | pinky's normalised dumps; `verify` diffs against them |

### Use it

```bash
source /home/pioneer/bt2026/software/src/beamtime2026_pie5/software/env.sh
```

It works in an interactive shell and in `bash -lc '...'`. It prints one line:

```
bt2026 env: host=piana root=6.38.04 reco=/home/pioneer/bt2026/software/src/main pie5=... sw=... (conda stripped)
```

On pinky the same file reproduces `~/.bashrc`'s setup (PATH, LD_LIBRARY_PATH,
PYTHONPATH, CMAKE_PREFIX_PATH, ROOTSYS, MIDASSYS, MIDAS_EXPTAB,
MIDAS_EXPT_NAME, PIONEERSYS), without the empty PYTHONPATH entry the bashrc
leaves behind. The host is picked by `hostname`. To use another checkout, set
`BT2026_RECO` (the main checkout), `BT2026_PIE5` (this repository) or
`BT2026_SW` (the prefix) before sourcing; `BT2026_HOST` pretends to be the
other host.

`env.sh` only changes the shell that sources it. It writes no file. It
deactivates conda and then removes every exported variable and every path
entry that points into a conda install or into the `MIDASSYS` the shell had
before (piana's `~/.bashrc` sets `/home/pioneer/Software/midas/`). Then it
takes every path it manages out of `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH` and
`CMAKE_PREFIX_PATH` and puts them back in pinky's order, so sourcing it twice,
or after pinky's `~/.bashrc`, gives the same result. If a piece of the stack
is missing it lists what is missing and returns 1, before changing anything.
On piana it also sets `PYTHONPYCACHEPREFIX` to `<prefix>/cache/pycache`, so
python writes no `__pycache__` into the checkouts. The sysroot's `pip` would
install into `~/.local` by default: always give it `--target`.

### Process one subrun by hand

```bash
source /home/pioneer/bt2026/software/src/beamtime2026_pie5/software/env.sh
nice -n 19 ionice -c3 systemd-run --user --scope -p MemoryMax=16G -p MemorySwapMax=0 \
    python -m pioneer.nearline.process /home/pioneer/inbox/run01012_00001.mid.lz4 \
    --out-dir /home/pioneer/bt2026/software/parity/out
```

This writes `run01012_00001.py` (the rendered job), `.root` and `_hists.root`
into the output directory. See `python/pioneer/nearline/README.md` for the
options.

### Redo one step

```bash
cap="nice -n 19 ionice -c3 systemd-run --user --scope -p MemoryMax=16G -p MemoryHigh=12G -p MemorySwapMax=0"
$cap bash software/install.sh /home/pioneer/bt2026/software gaudi            # only if its pins changed
$cap bash software/install.sh --force /home/pioneer/bt2026/software gaudi    # always
$cap bash software/install.sh /home/pioneer/bt2026/software main verify
```

A step that has finished is skipped unless its pins in `versions.env`, or the
pins of a step it builds on, have changed; the `rpms` step (and everything
built on it) also re-runs when one of the host's pinned packages changed, e.g.
after a `dnf update`. `--force` redoes the named steps from scratch. A rebuilt
Gaudi does not rebuild `main` by itself unless Gaudi's pins changed; name
`main` too.

**Stop nearline jobs on piana before a `--force` of gsl, clhep, midas,
gaudi, python or main.** Those builds have their install paths compiled in, so
the old tree is moved aside to `<dir>.old`, the new one is built in place, and
only if that fails is the old tree put back; while the step runs, the stack is
incomplete. `rpms` and `root` build a new tree next to the old one and swap it
in at the end.

### What verify checks

`verify` prints a table and writes it to `verify/verify.txt`:

* the toolchain comes from `/usr/bin` and the sysroot, not conda;
* `root-config --version` is 6.38.04, cmake is 4.3.0, gcc is 16.2.1;
* every pinned package the host provided, and every base-system package
  (glibc, gcc, libstdc++, python3, ...), is still pinky's build: same
  name-version-release and header digest (`SIGMD5`); the `gcc` package is
  pinky's `16.2.1-2.fc44`;
* `python3 -c "import ROOT, midas.client, psycopg, numpy"` works, ROOT is
  6.38.04, and `ROOT` and `midas` are imported from `$ROOTSYS` and `$MIDASSYS`
  (not from piana's system `python3-root`);
* `gaudirun.py --help` works, `PIONEERSYS` is set;
* every library of Gaudi, MIDAS and main finds all its dependencies (`ldd`);
* `ctest` in main's build. A failing test is a `WARN` row, not a failure: the
  tests are main's, not a parity criterion, and pinky has never run them
  (its `build/Testing/` is empty). `psm_reco` fails in this unoptimised build:
  `test_cluster_ghosts` (`reco_testbeam/tests/test_psm_reco.cpp:1662-1677`)
  reads `lp.c1.lead`, a pointer into a hit vector that the test's lambda has
  already destroyed. It passes in an optimised build by luck. A bug in the
  test, reported, not patched here;
* the ROOT tree is still byte for byte what the tarball unpacked (files that
  python and cling write at run time, `__pycache__/` and
  `lib/modules.timestamp`, are not counted).

It also writes the normalised dumps, in the format of pinky's reference
capture (`cmake_la_<pkg>.txt`, `cmake_extra_<pkg>.txt`, `makeflags_<pkg>.txt`,
`compile_commands_<pkg>_flat.txt`, `root_manifest.txt`, `gsl_manifest.txt`).
Paths are replaced by placeholders (`@ROOT@`, `@MIDAS@`, `@CLHEP@`, `@GAUDI@`,
`@GSL@`, `@RECO@`) and `<prefix>/sysroot/usr` by `/usr`, so a plain `diff`
against pinky's files shows only real differences. `verify` diffs each dump
against pinky's in `reference/` (the WP1 capture, normalised the same way; the
ROOT manifest as a sha256). A dump passes when it is identical, or when its
diff is exactly the reviewed one in `reference-accepted/` (the known
differences below). Any other diff, or a missing reference, is a `FAIL` row,
and the `.diff` file next to the dump shows it. The accepted diffs are for the
prefix `/home/pioneer/bt2026/software` (main's RPATH padding depends on the
length of the prefix).

## How the stack matches pinky

**ROOT** is the official binary tarball pinky unpacked. `verify` keeps a
sha256 manifest of every file.

**GSL, CLHEP, MIDAS, Gaudi, main** are built from the same tarballs and
commits, with the same CMake options as pinky's `CMakeCache.txt`, the same
`/usr/bin/gcc` 16.2.1 and cmake 4.3.0. As on pinky, Gaudi and main are built
with an empty `CMAKE_BUILD_TYPE` (no `-O`), and only Gaudi writes a
`compile_commands.json` (MIDAS switches its own on).

**Fedora packages.** The builds use 109 of pinky's RPMs (what their CMake
caches, compiler dependency files and link lines resolve under `/usr`, and what
the installed libraries load). Piana has most of them at exactly pinky's
version; `versions.env` lists all of them plus what the missing ones need
(`cmake-data`, `jsoncpp`, `xerces-c`, …) and `python3-numpy`/`python3-pip`
with their dependencies. For each package, `install.sh`:

* uses the host's copy when the host has exactly pinky's build: the same
  name-version-release and the same header digest (`SIGMD5`, pinky's is in
  `versions.env`);
* otherwise downloads pinky's build (`dnf download` as a normal user, with its
  cache in the prefix; when the Fedora repos have moved on, koji's signed copy
  `kojipkgs.fedoraproject.org/packages/<src>/<v>/<r>/data/signed/<keyid>/…`),
  and unpacks it into `sysroot/` only if `rpmkeys --checksig` reports
  "digests signatures OK" against the Fedora 44 key and its `SIGMD5` equals
  pinky's. If the host has a different build, it warns;
* never replaces a base-system package (glibc, gcc, libstdc++, python3, ...):
  if one differs from pinky's, it warns that this host is not a parity host,
  and `verify` fails;
* records the sha256 and `SIGMD5` of each package in `MANIFEST.txt`.

The unpacked tree is made usable in place: absolute symlinks are pointed into
the sysroot, `.so` links whose target the host provides point to the host,
and `.pc` files name the sysroot. During the build, the sysroot's headers and
libraries are passed to gcc through `CPLUS_INCLUDE_PATH` and `LIBRARY_PATH`.
gcc then treats them as system directories, just as pinky treats
`/usr/include`, and CMake leaves them off the compile lines. GSL's
`install/include` is handled the same way, as it stands in for pinky's
`/usr/local/include`. `env.sh` sets the same variables, so ACLiC and cling see
the same headers.

### Known differences from pinky

These are expected, and are what a `diff` against pinky's reference shows
(measured on the local Fedora 44 test, 2026-09-30):

* every path under the prefix, which the placeholders hide;
* **GSL** `cmake_la`: pinky's cache has `GIT_EXECUTABLE`, `PKG_CONFIG_ARGN` and
  `PKG_CONFIG_EXECUTABLE`, left over from an earlier configure with the tests
  on. The installed headers and CMake files are identical (`gsl_manifest`);
* **MIDAS**: pinky's build tree was configured with CMake 3.31.11 and gcc 15
  (before the Fedora 44 upgrade), so its cache names
  `/usr/lib/gcc/x86_64-redhat-linux/15/libgomp.so` and its `link.txt` files
  have the older CMake's flag order and no `-lgcc_s_asneeded`. The compile
  commands are identical. Whether pinky's MIDAS objects were compiled by gcc 15
  or 16 has to be checked on pinky (`readelf -p .comment`);
* **Gaudi**: pinky's cache marks `CMAKE_INSTALL_PREFIX`, `GAUDI_USE_AIDA` and
  `GAUDI_USE_HEPPDT` as `-MODIFIED` (they were edited in `ccmake`; a command
  line cannot set that flag), and has `UUID_LIBRARIES=/lib64/libuuid.so` where
  piana has `/usr/lib64/libuuid.so` (the same file: `/lib64` is a link to
  `/usr/lib64`). The compile commands are identical;
* pinky's `~/.cmake/packages` registry is not read on piana: every build runs
  with `HOME` inside the prefix, so the user package registry there is empty
  (no `CMAKE_FIND_USE_PACKAGE_REGISTRY` entry is needed in the caches);
* the git checkouts of MIDAS and Gaudi are detached at the pinned commit, so
  MIDAS's generated `git-revision.h` names no branch;
* **main**: `SITE` in the cache is the host name, and the padding of the build
  RPATH in `link.txt` differs in length (CMake pads it to the length of the
  install path, which is longer on piana). `UUID_LIBRARIES` as for Gaudi;
* on piana the sysroot's `lib64` is on `LD_LIBRARY_PATH` and the sysroot's
  `bin` sits right before `/usr/local/bin` in `PATH`;
* `cmake`, `ctest` and the `-devel` files come from the sysroot, byte-identical
  RPM contents, but relinked as described above.

## Testing it without piana

`scratch/piana-software/container/` (in the testbeam-env workspace) holds a
Fedora 44 image with only the packages piana has, a user `pioneer` with
piana's real `~/.bashrc`, and stand-ins for its miniforge `conda` and `mamba`
(an activated `PIONEER` env with conda-forge's compiler variables; its
compilers and python fail with "CONDA LEAK"). The install runs there as that
user, with the prefix bind-mounted, and a filesystem snapshot before and after
shows that nothing outside the prefix was written
(`scratch/piana-software/evidence/`).
