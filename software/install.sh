#!/usr/bin/env bash
# install.sh -- build the pinky-identical nearline software stack under ONE prefix.
#
#   bash install.sh [--force] PREFIX [step ...]
#
# Steps, in order (default: all of them):
#   rpms    fetch pinky's exact Fedora RPMs the builds need and unpack them into
#           PREFIX/sysroot (no root, no dnf install)
#   root    the official ROOT binary tarball -> PREFIX/root-<version>
#   gsl     Microsoft GSL (header only)       -> PREFIX/gsl/{GSL-<v>,build,install}
#   clhep   CLHEP                             -> PREFIX/clhep/{<v>,build,install}
#   midas   MIDAS, installed in place         -> PREFIX/midas
#   gaudi   Gaudi                             -> PREFIX/gaudi/{source,build,install}
#   python  psycopg wheels                    -> PREFIX/python
#   main    clone of main + setup.sh build    -> PREFIX/src/main
#   verify  PASS/FAIL table, normalised build dumps in PREFIX/verify
#
# Every step is idempotent: a finished step is skipped unless its pins (or, for
# rpms, the host's packages) changed, or --force is given. Nothing is written
# outside PREFIX: the script re-executes itself under `env -i`, points HOME,
# TMPDIR and every cache into PREFIX, and never calls sudo. It only runs on
# piana (hostname pioneer-analysis; elsewhere BT2026_ALLOW_HOST=1, never on
# pinky), and only into an empty directory or one it has marked as its own.
# See README.md ("Running it") for the piana invocation.
#
# Environment knobs (all optional):
#   JOBS=6                     parallel make jobs
#   MAIN_SRC=/home/pioneer/bt2026/main
#                              read-only git checkout of main to clone from
#   REFERENCE_DIR=DIR          pinky's normalised dumps (default: software/reference)
#   BT2026_ALLOW_HOST=1        allow a host other than pioneer-analysis (not pinky)
#   BT2026_NO_SYSROOT=1        use the host's /usr as is, fetch no RPMs (only for
#                              building a reference on a machine that has pinky's packages)

set -euo pipefail

SELF=$(readlink -f "${BASH_SOURCE[0]}")
SWDIR=$(dirname "$SELF")
PIE5_REPO=$(dirname "$SWDIR")
ALL_STEPS=(rpms root gsl clhep midas gaudi python main verify)
MARKER=.bt2026-software
PIE5_MIRROR=/home/pioneer/bt2026/beamtime2026_pie5   # the website's checkout: never touched

usage() {
    sed -n '2,/^$/{s/^# \{0,1\}//;p}' "$SELF"
}

# --- arguments -----------------------------------------------------------------
FORCE=0
ARGS=()
for a in "$@"; do
    case $a in
        --force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "install.sh: unknown option $a" >&2; exit 2 ;;
        *) ARGS+=("$a") ;;
    esac
done
if [ ${#ARGS[@]} -lt 1 ]; then usage >&2; exit 2; fi
PREFIX=${ARGS[0]}
STEPS=("${ARGS[@]:1}")
[ ${#STEPS[@]} -gt 0 ] || STEPS=("${ALL_STEPS[@]}")
for s in "${STEPS[@]}"; do
    case " ${ALL_STEPS[*]} " in *" $s "*) ;; *) echo "install.sh: unknown step '$s'" >&2; exit 2 ;; esac
done

refuse() { echo "install.sh: refusing: $*" >&2; exit 2; }

# --- where it may run, and into what -------------------------------------------
# The real host name, not BT2026_HOST: a wrong PREFIX on pinky could delete the
# DAQ's own packages.
HOSTNAME_NOW=$(hostname 2>/dev/null || uname -n)
case $HOSTNAME_NOW in
    [Pp][Ii][Nn][Kk][Yy]|[Pp][Ii][Nn][Kk][Yy].*) refuse "this is pinky ($HOSTNAME_NOW); install.sh is for piana" ;;
    pioneer-analysis|pioneer-analysis.*) ;;
    *) [ "${BT2026_ALLOW_HOST:-}" = 1 ] || refuse "host '$HOSTNAME_NOW' is not pioneer-analysis (set BT2026_ALLOW_HOST=1 to allow it)" ;;
esac

case $PREFIX in
    /*) ;;
    *) refuse "PREFIX must be an absolute path (got '$PREFIX')" ;;
esac
PREFIX=$(readlink -m "$PREFIX")
REAL_HOME=$(readlink -m "${HOME:-/nonexistent}")
case $PREFIX in
    /|/usr|/usr/*|/etc|/etc/*|/opt|/tmp|/home|/root|/var|/var/*|/bin|/lib|/lib64|/sbin|/boot|/boot/*|/srv|/mnt|/media)
        refuse "PREFIX=$PREFIX is a system directory" ;;
esac
# is / contains / is inside  (one of the paths it must never write into)
related() {   # a b -> 0 if a == b, a inside b, or b inside a
    local a=${1%/} b=${2%/}
    [ "$a" = "$b" ] || [[ $a == "$b"/* ]] || [[ $b == "$a"/* ]]
}
MAIN_SRC_CHECK=$(readlink -m "${MAIN_SRC:-/home/pioneer/bt2026/main}")
for p in "$MAIN_SRC_CHECK" "$PIE5_MIRROR"; do
    related "$PREFIX" "$p" && refuse "PREFIX=$PREFIX overlaps $p (a read-only source)"
done
# (Only in the first pass: after the re-exec HOME is PREFIX/tmp/home.)
if [ "${_BT2026_CLEAN:-}" != 1 ] && { [ "$PREFIX" = "$REAL_HOME" ] || [[ $REAL_HOME == "$PREFIX"/* ]]; }; then
    refuse "PREFIX=$PREFIX is or contains the home directory $REAL_HOME"
fi
# Empty, or marked by an earlier run, or holding nothing but this repository
# at PREFIX/src/<name> (the piana layout: clone first, then install).
if [ -d "$PREFIX" ] && [ ! -f "$PREFIX/$MARKER" ]; then
    for e in "$PREFIX"/* "$PREFIX"/.[!.]*; do
        [ -e "$e" ] || [ -L "$e" ] || continue
        if [ "$e" = "$PREFIX/src" ]; then
            for f in "$PREFIX"/src/* "$PREFIX"/src/.[!.]*; do
                [ -e "$f" ] || [ -L "$f" ] || continue
                [ "$(readlink -m "$f")" = "$PIE5_REPO" ] && continue
                refuse "PREFIX=$PREFIX is not empty and has no $MARKER ($f)"
            done
            continue
        fi
        refuse "PREFIX=$PREFIX is not empty and has no $MARKER ($e)"
    done
elif [ -e "$PREFIX" ] && [ ! -d "$PREFIX" ]; then
    refuse "PREFIX=$PREFIX is not a directory"
fi

# --- re-execute under a clean environment ----------------------------------------
# The login shell on piana activates a conda env (gcc 13, cmake 3.26, python
# 3.13) and exports MIDASSYS; none of that may reach the builds.
if [ "${_BT2026_CLEAN:-}" != 1 ]; then
    mkdir -p "$PREFIX"
    [ -f "$PREFIX/$MARKER" ] || echo "install.sh prefix, created $(date -u +%FT%TZ) on $HOSTNAME_NOW" > "$PREFIX/$MARKER"
    keep=()
    for v in JOBS MAIN_SRC REFERENCE_DIR BT2026_NO_SYSROOT BT2026_ALLOW_HOST USER LOGNAME TERM \
             http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
        if [ -n "${!v+x}" ]; then keep+=("$v=${!v}"); fi
    done
    fl=(); [ $FORCE = 1 ] && fl=(--force)
    exec /usr/bin/env -i _BT2026_CLEAN=1 "${keep[@]}" \
        HOME="$PREFIX/tmp/home" PATH=/usr/bin:/bin LANG=C.UTF-8 \
        /usr/bin/bash --noprofile --norc "$SELF" "${fl[@]}" "$PREFIX" "${STEPS[@]}"
fi
[ -f "$PREFIX/$MARKER" ] || refuse "no $MARKER in $PREFIX"

umask 022
# shellcheck source=versions.env
source "$SWDIR/versions.env"

JOBS=${JOBS:-6}
MAIN_SRC=${MAIN_SRC:-/home/pioneer/bt2026/main}
REFERENCE_DIR=${REFERENCE_DIR:-$SWDIR/reference}
ACCEPTED_DIR=$SWDIR/reference-accepted
NO_SYSROOT=${BT2026_NO_SYSROOT:-0}
SYSROOT=$PREFIX/sysroot
ROOTDIR=$PREFIX/root-$ROOT_VERSION
LOGDIR=$PREFIX/logs
STAMPS=$PREFIX/.stamps
DL=$PREFIX/downloads

# Every cache and scratch file of every tool goes into the prefix.
export HOME=$PREFIX/tmp/home
export TMPDIR=$PREFIX/tmp
export XDG_CACHE_HOME=$PREFIX/cache
export XDG_CONFIG_HOME=$PREFIX/tmp/config
export XDG_DATA_HOME=$PREFIX/tmp/data
export XDG_STATE_HOME=$PREFIX/tmp/state
export PIP_CACHE_DIR=$PREFIX/cache/pip
export PIP_CONFIG_FILE=/dev/null
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1
export PYTHONNOUSERSITE=1
export PYTHONPYCACHEPREFIX=$PREFIX/cache/pycache
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_TERMINAL_PROMPT=0
mkdir -p "$HOME" "$TMPDIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" \
         "$XDG_STATE_HOME" "$LOGDIR" "$STAMPS" "$DL" "$PREFIX/manifest.d" "$PREFIX/verify"
RPMDEF=(--define "_tmppath $TMPDIR")

# --- helpers ---------------------------------------------------------------------
WARNFILE=$LOGDIR/warnings.txt
say()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARNING: %s\n' "$*" >&2; printf 'WARNING: %s\n' "$*" >> "$WARNFILE"; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

# Only ever delete below the (marked) prefix.
inside_prefix() {
    local p; p=$(readlink -m "$1")
    case $p in "$PREFIX"/?*) return 0 ;; *) return 1 ;; esac
}
safe_rm() {
    local p
    [ -f "$PREFIX/$MARKER" ] || die "no $MARKER in $PREFIX"
    for p in "$@"; do
        inside_prefix "$p" || die "refusing to delete '$p' (outside $PREFIX)"
        if [ -e "$p" ] || [ -L "$p" ]; then chmod -R u+w "$p" 2>/dev/null || true; rm -rf "$p"; fi
    done
}

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# download URL FILE SHA256 -> $DL/FILE, verified
download() {
    local url=$1 file=$2 sum=$3 out=$DL/$2
    if [ -f "$out" ] && [ "$(sha256_of "$out")" = "$sum" ]; then
        say "cached $file"
    else
        say "downloading $url"
        curl -fL --retry 3 --silent --show-error -o "$out.part" "$url"
        [ "$(sha256_of "$out.part")" = "$sum" ] || die "sha256 mismatch for $file (expected $sum, got $(sha256_of "$out.part"))"
        mv "$out.part" "$out"
    fi
    echo "tarball $file sha256=$sum url=$url" >> "$MANIFEST_PART"
}

# What the host has of every pinned package: "name.arch epoch:version-release sigmd5".
host_rpm_state() {
    local line name epoch ver rel arch src md5
    local -a q=()
    for line in "${RPMS[@]}"; do read -r name epoch ver rel arch src md5 <<<"$line"; q+=("$name.$arch"); done
    rpm "${RPMDEF[@]}" -q --qf '%{NAME}.%{ARCH} %{EPOCHNUM}:%{VERSION}-%{RELEASE} %{SIGMD5}\n' "${q[@]}" 2>&1 || true
}

# Pins of a step, so a changed pin re-runs the step (and the steps built on it).
# The rpms key includes the host's packages: a `dnf update` on piana re-runs it.
step_key() {
    case $1 in
        rpms)   { echo "$NO_SYSROOT"; printf '%s\n' "${RPMS[@]}"; host_rpm_state; } | sha256sum | cut -c1-16 ;;
        root)   echo "$ROOT_SHA256" | cut -c1-16 ;;
        gsl)    echo "$GSL_SHA256:$(step_key rpms)" | sha256sum | cut -c1-16 ;;
        clhep)  echo "$CLHEP_SHA256:$(step_key rpms)" | sha256sum | cut -c1-16 ;;
        midas)  echo "$MIDAS_SHA:${MIDAS_SUBMODULES[*]}:$(step_key root):$(step_key rpms)" | sha256sum | cut -c1-16 ;;
        gaudi)  echo "$GAUDI_SHA:$(step_key root):$(step_key gsl):$(step_key clhep)" | sha256sum | cut -c1-16 ;;
        python) echo "${PSYCOPG_WHEELS[*]}:$(step_key rpms)" | sha256sum | cut -c1-16 ;;
        main)   echo "$MAIN_SHA:$MAIN_SETUP_ARGS:$MAIN_CMAKE_FLAGS:$(step_key gaudi):$(step_key midas)" | sha256sum | cut -c1-16 ;;
        verify) date +%s ;;   # always runs
    esac
}

# The build environment: pinky's /usr is PREFIX/sysroot/usr, and GSL's install
# stands in for pinky's /usr/local. Header and library directories go through
# CPLUS_INCLUDE_PATH / LIBRARY_PATH so that gcc -- and CMake's probe of gcc --
# treats them as system directories, exactly like /usr/include on pinky.
build_env() {
    export PATH=/usr/bin:/bin
    unset LD_LIBRARY_PATH CPLUS_INCLUDE_PATH C_INCLUDE_PATH LIBRARY_PATH PKG_CONFIG_PATH \
          CMAKE_PREFIX_PATH PYTHONPATH ROOTSYS MIDASSYS
    local inc=""
    if [ -d "$PREFIX/gsl/install/include" ]; then inc=$PREFIX/gsl/install/include; fi
    if [ "$NO_SYSROOT" != 1 ]; then
        [ -f "$STAMPS/rpms" ] || die "run the rpms step first"
        export PATH=$SYSROOT/usr/bin:$PATH
        export LD_LIBRARY_PATH=$SYSROOT/usr/lib64
        inc=${inc:+$inc:}$SYSROOT/usr/include
        export LIBRARY_PATH=$SYSROOT/usr/lib64
        export PKG_CONFIG_PATH=$SYSROOT/usr/lib64/pkgconfig:$SYSROOT/usr/share/pkgconfig
        export CMAKE_PREFIX_PATH=$SYSROOT/usr
        local sp
        for sp in "$SYSROOT"/usr/lib64/python3.*/site-packages "$SYSROOT"/usr/lib/python3.*/site-packages; do
            if [ -d "$sp" ]; then export PYTHONPATH=${PYTHONPATH:+$PYTHONPATH:}$sp; fi
        done
    fi
    if [ -n "$inc" ]; then export CPLUS_INCLUDE_PATH=$inc C_INCLUDE_PATH=$inc; fi
    local p
    for p in "$PREFIX/gsl/install" "$PREFIX/clhep/install" "$PREFIX/gaudi/install"; do
        if [ -d "$p" ]; then export CMAKE_PREFIX_PATH=$p${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}; fi
    done
    if [ -f "$ROOTDIR/bin/thisroot.sh" ]; then
        # thisroot.sh prepends ROOT to PATH, LD_LIBRARY_PATH, PYTHONPATH and
        # CMAKE_PREFIX_PATH, as pinky's ~/.bashrc does. Not written for set -u.
        set +u
        pushd "$ROOTDIR" >/dev/null
        # shellcheck disable=SC1091
        source bin/thisroot.sh
        popd >/dev/null
        set -u
        [ "${ROOTSYS:-}" = "$ROOTDIR" ] || die "thisroot.sh set ROOTSYS='${ROOTSYS:-}', expected $ROOTDIR"
    fi
    CMAKE=$(command -v cmake) || die "no cmake on PATH (did the rpms step run?)"
    if [ "$NO_SYSROOT" != 1 ] && [ "$CMAKE" != "$SYSROOT/usr/bin/cmake" ]; then
        die "cmake resolves to $CMAKE, expected $SYSROOT/usr/bin/cmake"
    fi
    # CC/CXX stay unset, as on pinky: CMake then finds /usr/bin/cc and /usr/bin/c++
    # (gcc 16.2.1), and records those names in the cache like pinky's does.
    unset CC CXX
    [ "$(command -v c++)" = /usr/bin/c++ ] || die "c++ resolves to $(command -v c++), expected /usr/bin/c++"
}

# Run one configure/build/install in the current build_env.
cmake_build() {   # src build [cmake args...]
    local src=$1 bld=$2; shift 2
    mkdir -p "$bld"
    say "cmake $src"
    ( cd "$bld" && "$CMAKE" -G "Unix Makefiles" "$@" "$src" )
    say "make -j$JOBS"
    make -C "$bld" -j"$JOBS"
    make -C "$bld" install
}

# --- step: rpms ---------------------------------------------------------------------
DNF_OPTS=()
dnf_opts() {
    DNF_OPTS=(--setopt=cachedir="$PREFIX/cache/dnf" --setopt=system_cachedir="$PREFIX/cache/dnf"
              --setopt=logdir="$LOGDIR/dnf" --setopt=persistdir="$PREFIX/cache/dnf-persist"
              --setopt=keepcache=True -y -q)
    mkdir -p "$LOGDIR/dnf" "$PREFIX/cache/dnf" "$PREFIX/cache/dnf-persist"
}

rpm_file() { echo "$1-$3-$4.$5.rpm"; }                       # name epoch ver rel arch
rpm_nevra() { if [ "$2" = 0 ]; then echo "$1-$3-$4.$5"; else echo "$1-$2:$3-$4.$5"; fi; }

step_rpms() {
    if [ "$NO_SYSROOT" = 1 ]; then
        say "BT2026_NO_SYSROOT=1: using the host /usr as is"
        echo "rpms none (BT2026_NO_SYSROOT=1)" >> "$MANIFEST_PART"
        return 0
    fi
    [ ${#RPMS[@]} -gt 0 ] || die "versions.env lists no RPMs"
    local rpmdir=$PREFIX/rpms
    mkdir -p "$rpmdir"
    local line name epoch ver rel arch src md5 have hmd5 want file
    local -a fetch=() host=() mismatch=() coremis=()
    for line in "${RPMS[@]}"; do
        read -r name epoch ver rel arch src md5 <<<"$line"
        want="$epoch:$ver-$rel"
        have=$(rpm "${RPMDEF[@]}" -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}\n' "$name.$arch" 2>/dev/null | head -1 || true)
        hmd5=$(rpm "${RPMDEF[@]}" -q --qf '%{SIGMD5}\n' "$name.$arch" 2>/dev/null | head -1 || true)
        case $have in *"not installed"*) have="" hmd5="" ;; esac
        if [ "$have" = "$want" ] && [ "$hmd5" = "$md5" ]; then
            host+=("$line")
        elif [[ $name =~ $RPMS_HOST_ONLY_RE ]]; then
            coremis+=("$name.$arch host=${have:-none}/${hmd5:-} pinky=$want/$md5")
        else
            if [ -n "$have" ]; then mismatch+=("$name.$arch host=$have/$hmd5 pinky=$want/$md5"); fi
            fetch+=("$line")
        fi
    done
    say "${#RPMS[@]} RPMs pinned: ${#host[@]} provided by this host as pinky's build, ${#fetch[@]} to unpack into the sysroot"
    for line in "${mismatch[@]}"; do warn "host has a different build of $line; using pinky's in the sysroot"; done
    for line in "${coremis[@]}"; do
        warn "base-system package differs from pinky, NOT replaced: $line -- this host is not a parity host"
        echo "rpm host-differs $line" >> "$MANIFEST_PART"
    done

    # 1. download what is not cached yet: dnf first, then koji's signed copy by NVR.
    local -a need=()
    for line in "${fetch[@]}"; do
        read -r name epoch ver rel arch src md5 <<<"$line"
        [ -f "$rpmdir/$(rpm_file "$name" "$epoch" "$ver" "$rel" "$arch")" ] || need+=("$line")
    done
    if [ ${#need[@]} -gt 0 ]; then
        dnf_opts
        local -a specs=()
        for line in "${need[@]}"; do read -r name epoch ver rel arch src md5 <<<"$line"; specs+=("$(rpm_nevra "$name" "$epoch" "$ver" "$rel" "$arch")"); done
        say "dnf download of ${#specs[@]} packages"
        dnf download "${DNF_OPTS[@]}" --skip-unavailable --destdir="$rpmdir" "${specs[@]}" || say "dnf download reported errors; trying koji for what is missing"
        for line in "${need[@]}"; do
            read -r name epoch ver rel arch src md5 <<<"$line"
            file=$(rpm_file "$name" "$epoch" "$ver" "$rel" "$arch")
            if [ -f "$rpmdir/$file" ]; then echo dnf > "$rpmdir/$file.origin"; continue; fi
            # koji keeps every build; the signed copy is under data/signed/<keyid>/.
            local sname=${src%-*-*} srest=${src#"${src%-*-*}"-}
            local url="https://kojipkgs.fedoraproject.org/packages/$sname/${srest%-*}/${srest#*-}/data/signed/$FEDORA_KEYID/$arch/$file"
            say "koji: $url"
            curl -fL --retry 3 --silent --show-error -o "$rpmdir/$file.part" "$url" || die "cannot fetch $file from the repos or koji"
            mv "$rpmdir/$file.part" "$rpmdir/$file"
            echo koji > "$rpmdir/$file.origin"
        done
    fi

    # 2. every package must be pinky's build (NEVRA and header digest) and carry
    #    a valid Fedora signature. The key goes into a key database inside the
    #    prefix, so the check does not depend on the host's rpm database.
    local keydb=$PREFIX/cache/rpmkeys key
    mkdir -p "$keydb"
    for key in /etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary /etc/pki/rpm-gpg/RPM-GPG-KEY-44-fedora; do
        if [ -f "$key" ]; then rpmkeys "${RPMDEF[@]}" --define "_dbpath $keydb" --import "$key"; break; fi
    done
    # Built as sysroot.new with the final paths written into it, then swapped in.
    local new=$SYSROOT.new
    safe_rm "$new"
    mkdir -p "$new"
    local sig sigmd5 got origin
    for line in "${fetch[@]}"; do
        read -r name epoch ver rel arch src md5 <<<"$line"
        file=$(rpm_file "$name" "$epoch" "$ver" "$rel" "$arch")
        got=$(rpm "${RPMDEF[@]}" -qp --nosignature --qf '%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE} %{ARCH}' "$rpmdir/$file")
        [ "$got" = "$name $epoch:$ver-$rel $arch" ] || die "$file is '$got', not pinky's $name $epoch:$ver-$rel $arch"
        sig=$(rpmkeys "${RPMDEF[@]}" --define "_dbpath $keydb" --checksig "$rpmdir/$file" 2>&1 | sed 's/.*: *//') || true
        [[ $sig == *"signatures OK"* ]] || die "$file: signature check says '$sig' (need 'digests signatures OK')"
        sigmd5=$(rpm "${RPMDEF[@]}" -qp --nosignature --qf '%{SIGMD5}' "$rpmdir/$file")
        [ "$sigmd5" = "$md5" ] || die "$file: header digest $sigmd5, pinky's is $md5"
        origin=$(cat "$rpmdir/$file.origin" 2>/dev/null || echo cache)
        echo "rpm sysroot $name-$ver-$rel.$arch sigmd5=$sigmd5 sha256=$(sha256_of "$rpmdir/$file") checksig='$sig' from=$origin" >> "$MANIFEST_PART"
        # Via a file, not a pipe: cpio stops reading at the archive trailer, and
        # rpm2cpio writing its padding into the closed pipe then dies of SIGPIPE.
        rpm2cpio "$rpmdir/$file" > "$TMPDIR/unpack.cpio" || die "rpm2cpio $file failed"
        ( cd "$new" && cpio -idm --quiet --no-absolute-filenames \
              --nonmatching './usr/lib/.build-id*' < "$TMPDIR/unpack.cpio" ) || die "unpacking $file failed"
        rm -f "$TMPDIR/unpack.cpio"
        chmod -R u+w "$new"
    done
    for line in "${host[@]}"; do
        read -r name epoch ver rel arch src md5 <<<"$line"
        echo "rpm host $name-$ver-$rel.$arch sigmd5=$md5" >> "$MANIFEST_PART"
    done

    # 3. make the relocated tree self-consistent.
    say "relinking the sysroot"
    python3 - "$new" <<'EOF'
import os, sys
root = sys.argv[1]
fixed = dangling = 0
for dp, dn, fn in os.walk(root):
    for n in dn + fn:
        p = os.path.join(dp, n)
        if not os.path.islink(p):
            continue
        t = os.readlink(p)
        here = "/" + os.path.relpath(os.path.dirname(p), root)      # this dir as seen on pinky
        tgt = os.path.normpath(t if t.startswith("/") else os.path.join(here, t))
        in_root = root + tgt
        if os.path.lexists(in_root) and os.path.normpath(in_root) != os.path.normpath(p):
            if not t.startswith("/"):
                continue                                              # already relative, inside
            new = os.path.relpath(in_root, os.path.dirname(p))        # absolute /usr/.. -> into the sysroot
        elif os.path.lexists(tgt):
            new = tgt                                                 # the host provides the target
        else:
            dangling += 1
            continue
        if new != t:
            os.unlink(p); os.symlink(new, p); fixed += 1
print(f"[install] symlinks rewritten: {fixed}, left dangling (target on neither side): {dangling}")
EOF
    # 4. The libraries of the host-provided packages are linked into the sysroot's
    # lib64: the unpacked CMake configs (TBB, boost, xerces-c, ...) refer to their
    # runtime libraries relative to their own location and check that they exist.
    # Base-system packages (glibc, libstdc++, ...) are left out. Likewise header
    # files that a host package puts into a directory of an unpacked -devel
    # package: python3-libs ships pyconfig-64.h next to python3-devel's
    # /usr/include/python3.14/pyconfig.h.
    local f d n=0
    for line in "${host[@]}"; do
        read -r name epoch ver rel arch src md5 <<<"$line"
        while IFS= read -r f; do
            d=$(dirname "$f")
            case $f in
                /usr/include/*/*) [ -d "$new$d" ] || continue ;;
                /usr/lib64/*.so*|/usr/lib/*.so*)
                    [[ $name =~ $RPMS_HOST_ONLY_RE ]] && continue
                    [ "$d" = /usr/lib64 ] || [ "$d" = /usr/lib ] || continue ;;
                *) continue ;;
            esac
            if [ -f "$f" ] && [ ! -e "$new$f" ] && [ ! -L "$new$f" ]; then
                mkdir -p "$new$d"
                ln -s "$f" "$new$f"
                n=$((n + 1))
            fi
        done < <(rpm "${RPMDEF[@]}" -ql "$name.$arch")
    done
    say "linked $n libraries and headers of host-provided packages into the sysroot"
    # pkg-config files name /usr; point them at the (final) sysroot.
    local pc
    for pc in "$new"/usr/lib64/pkgconfig/*.pc "$new"/usr/share/pkgconfig/*.pc; do
        [ -f "$pc" ] || continue
        sed -i -E "s#^(prefix|exec_prefix|libdir|includedir|sharedlibdir)=/usr#\1=$SYSROOT/usr#" "$pc"
    done
    # Linker scripts (GNU ld "INPUT(...)") with absolute /usr/lib64 paths.
    while IFS= read -r f; do
        if head -c 200 "$f" | grep -q 'GNU ld script' && grep -q '/usr/lib64/' "$f"; then
            sed -i -E "s#(^|[ (])/usr/lib64/([^ )]+)#\1$SYSROOT/usr/lib64/\2#g" "$f"
            say "rewrote linker script $(basename "$f")"
        fi
    done < <(find "$new/usr/lib64" -maxdepth 1 -name '*.so' -type f -size -2k 2>/dev/null)

    # 5. swap in.
    safe_rm "$SYSROOT.old"
    if [ -e "$SYSROOT" ]; then mv "$SYSROOT" "$SYSROOT.old"; fi
    mv "$new" "$SYSROOT"
    safe_rm "$SYSROOT.old"
}

# --- step: root ---------------------------------------------------------------------
step_root() {
    download "$ROOT_URL" "$ROOT_TARBALL" "$ROOT_SHA256"
    local unpack=$PREFIX/tmp/root-unpack
    safe_rm "$unpack"
    mkdir -p "$unpack"
    say "unpacking $ROOT_TARBALL"
    tar -xzf "$DL/$ROOT_TARBALL" -C "$unpack"
    say "writing the file manifest"
    python3 "$SWDIR/parity.py" manifest "$unpack/root" > "$PREFIX/verify/root_manifest.txt"
    python3 "$SWDIR/parity.py" links "$unpack/root" > "$PREFIX/verify/root_links.txt"
    # The binary tarball is relocatable: swap it in as a whole.
    safe_rm "$ROOTDIR.old"
    if [ -e "$ROOTDIR" ]; then mv "$ROOTDIR" "$ROOTDIR.old"; fi
    mv "$unpack/root" "$ROOTDIR"
    safe_rm "$ROOTDIR.old" "$unpack"
    echo "root $ROOTDIR manifest=verify/root_manifest.txt files=$(wc -l < "$PREFIX/verify/root_manifest.txt")" >> "$MANIFEST_PART"
}

# --- step: gsl ----------------------------------------------------------------------
step_gsl() {
    download "$GSL_URL" "$GSL_TARBALL" "$GSL_SHA256"
    mkdir -p "$PREFIX/gsl"
    tar -xzf "$DL/$GSL_TARBALL" -C "$PREFIX/gsl"
    build_env
    # As on pinky (its CMakeCache): cmake 4 needs CMAKE_POLICY_VERSION_MINIMUM for
    # GSL 4.0.0, and the tests (which would clone googletest) are off.
    cmake_build "$PREFIX/gsl/GSL-$GSL_VERSION" "$PREFIX/gsl/build" \
        -DCMAKE_INSTALL_PREFIX="$PREFIX/gsl/install" \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DGSL_CXX_STANDARD=14 -DGSL_TEST=OFF
    python3 "$SWDIR/parity.py" manifest "$PREFIX/gsl/install" include/gsl share/cmake/Microsoft.GSL \
        > "$PREFIX/verify/gsl_manifest.txt"
}

# --- step: clhep --------------------------------------------------------------------
step_clhep() {
    download "$CLHEP_URL" "$CLHEP_TARBALL" "$CLHEP_SHA256"
    mkdir -p "$PREFIX/clhep"
    tar -xzf "$DL/$CLHEP_TARBALL" -C "$PREFIX/clhep"
    build_env
    cmake_build "$PREFIX/clhep/$CLHEP_VERSION/CLHEP" "$PREFIX/clhep/build" \
        -DCMAKE_INSTALL_PREFIX="$PREFIX/clhep/install" \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_CXX_STANDARD=20 \
        -DCLHEP_BUILD_DOCS=OFF -DCLHEP_SINGLE_THREAD=OFF
}

# git clone URL DIR SHA: a detached checkout of exactly SHA.
git_checkout() {
    local url=$1 dir=$2 sha=$3
    say "git clone $url"
    git clone -q --no-checkout "$url" "$dir"
    git -C "$dir" -c advice.detachedHead=false checkout -q "$sha"
    [ "$(git -C "$dir" rev-parse HEAD)" = "$sha" ] || die "$dir is not at $sha"
    echo "git $dir $(git -C "$dir" rev-parse HEAD) url=$url" >> "$MANIFEST_PART"
}

# --- step: midas --------------------------------------------------------------------
step_midas() {
    git_checkout "$MIDAS_URL" "$PREFIX/midas" "$MIDAS_SHA"
    # --recursive: mscb carries mxml as its own submodule.
    git -C "$PREFIX/midas" submodule -q update --init --recursive
    local line path sha got
    for line in "${MIDAS_SUBMODULES[@]}"; do
        read -r path sha <<<"$line"
        got=$(git -C "$PREFIX/midas/$path" rev-parse HEAD)
        if [ "$got" != "$sha" ]; then
            say "midas/$path: $got -> pinky's $sha"
            git -C "$PREFIX/midas/$path" -c advice.detachedHead=false checkout -q "$sha"
        fi
        echo "git midas/$path $(git -C "$PREFIX/midas/$path" rev-parse HEAD)" >> "$MANIFEST_PART"
    done
    git -C "$PREFIX/midas" submodule status --recursive | sed 's/^/submodule-status midas /' >> "$MANIFEST_PART"
    build_env
    # No CMAKE_INSTALL_PREFIX: MIDAS installs into its own source tree by default, as on pinky.
    cmake_build "$PREFIX/midas" "$PREFIX/midas/build"
}

# --- step: gaudi --------------------------------------------------------------------
step_gaudi() {
    mkdir -p "$PREFIX/gaudi"
    git_checkout "$GAUDI_URL" "$PREFIX/gaudi/source" "$GAUDI_SHA"
    build_env
    cmake_build "$PREFIX/gaudi/source" "$PREFIX/gaudi/build" \
        -DCMAKE_INSTALL_PREFIX="$PREFIX/gaudi/install" \
        -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
        -DBUILD_TESTING=FALSE \
        -DGAUDI_CXX_STANDARD:STRING=23 \
        -DGAUDI_USE_AIDA=FALSE -DGAUDI_USE_HEPPDT=FALSE \
        -DGAUDI_USE_CLHEP=ON -DGAUDI_USE_XERCESC=ON \
        -DGAUDI_USE_CPPUNIT=FALSE -DGAUDI_USE_GPERFTOOLS=FALSE -DGAUDI_USE_JEMALLOC=FALSE \
        -DGAUDI_USE_UNWIND=FALSE -DGAUDI_USE_INTELAMPLIFIER=FALSE -DGAUDI_USE_DOXYGEN=FALSE
}

# --- step: python -------------------------------------------------------------------
step_python() {
    build_env
    local req=$PREFIX/tmp/requirements.txt
    printf '%s\n' "${PSYCOPG_WHEELS[@]}" > "$req"
    # pip itself: pinky's python3-pip RPM from the sysroot (piana's python has none).
    python3 -m pip --version
    python3 -m pip install --no-deps --only-binary=:all: --require-hashes \
        --target "$PREFIX/python" -r "$req"
    local w
    for w in "${PSYCOPG_WHEELS[@]}"; do echo "wheel $w" >> "$MANIFEST_PART"; done
}

# --- step: main ---------------------------------------------------------------------
# Runs inside env.sh, as a shifter would, minus <reco>/install/setenv.sh which
# the build is about to create. Values reach the command string only through
# the environment (_BT_*), never by pasting them into it.
in_env() {   # command string, then extra VAR=value assignments
    local cmd=$1; shift
    env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 TMPDIR="$TMPDIR" \
        XDG_CACHE_HOME="$XDG_CACHE_HOME" XDG_CONFIG_HOME="$XDG_CONFIG_HOME" \
        XDG_DATA_HOME="$XDG_DATA_HOME" XDG_STATE_HOME="$XDG_STATE_HOME" \
        GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONNOUSERSITE=1 \
        BT2026_HOST=pioneer-analysis BT2026_SW="$PREFIX" BT2026_RECO="$PREFIX/src/main" \
        BT2026_PIE5="$PIE5_REPO" \
        _BT_ENVSH="$SWDIR/env.sh" _BT_PREFIX="$PREFIX" _BT_JOBS="$JOBS" \
        ${BT2026_SKIP_RECO:+BT2026_SKIP_RECO=1} \
        $( [ "$NO_SYSROOT" = 1 ] && echo BT2026_SYSROOT= ) "$@" \
        /usr/bin/bash --noprofile --norc -c 'source "$_BT_ENVSH" || exit 1; '"$cmd"
}

step_main() {
    local src=$MAIN_SRC dst=$PREFIX/src/main
    [ -d "$src/.git" ] || [ -f "$src/.git" ] || die "MAIN_SRC=$src is not a git checkout"
    inside_prefix "$src" && die "MAIN_SRC must not be inside $PREFIX"
    git -C "$src" cat-file -e "$MAIN_SHA^{commit}" 2>/dev/null \
        || die "$src does not have commit $MAIN_SHA (fetch it there first)"
    mkdir -p "$PREFIX/src"
    # file:// makes git copy objects instead of hard-linking them, so the source
    # repository's files are only ever read.
    git_checkout "file://$src" "$dst" "$MAIN_SHA"
    local line path sha got
    for line in "${MAIN_SUBMODULES[@]}"; do
        read -r path sha <<<"$line"
        got=$(git -C "$dst" ls-tree HEAD "$path" | awk '{print $3}')
        [ "$got" = "$sha" ] || die "main $MAIN_SHA records $path at $got, versions.env says $sha"
        [ -e "$src/$path/.git" ] || die "$src/$path is not an initialised submodule"
        git -C "$dst" config "submodule.$path.url" "file://$src/$path"
        git -C "$dst" -c protocol.file.allow=always submodule -q update --init "$path"
        [ "$(git -C "$dst/$path" rev-parse HEAD)" = "$sha" ] || die "$dst/$path is not at $sha"
        echo "git src/main/$path $sha" >> "$MANIFEST_PART"
    done
    say "setup.sh $MAIN_SETUP_ARGS (CMAKE_FLAGS=$MAIN_CMAKE_FLAGS), $JOBS jobs"
    # setup.sh builds with --parallel $(nproc); nproc honours OMP_NUM_THREADS.
    # shellcheck disable=SC2016
    BT2026_SKIP_RECO=1 in_env 'cd "$_BT_PREFIX/src/main" && OMP_NUM_THREADS=$_BT_JOBS CMAKE_FLAGS="$_BT_CMAKE_FLAGS" ./setup.sh $_BT_SETUP_ARGS' \
        _BT_CMAKE_FLAGS="$MAIN_CMAKE_FLAGS" _BT_SETUP_ARGS="$MAIN_SETUP_ARGS"
    grep -q "End of setup. All done." "$dst/setup.log" || die "setup.sh did not finish (see $dst/setup.log)"
    [ -f "$dst/install/setenv.sh" ] || die "setup.sh wrote no install/setenv.sh"
}

# --- step: verify -------------------------------------------------------------------
# Functional checks, then the normalised dumps in the formats of the pinky
# reference (software/reference/: cmake_la_*.txt, cmake_extra_*.txt,
# makeflags_*.txt, compile_commands_*_flat.txt, manifests). A dump passes when
# it equals pinky's, or when its diff to pinky's equals the reviewed one in
# software/reference-accepted/. Anything else fails.
VROWS=()
vrow() { VROWS+=("$(printf '%-4s  %-30s %s' "$1" "$2" "$3")"); }
vcheck() {   # name command-string [expected-substring]
    local name=$1 cmd=$2 want=${3:-} out rc=0
    out=$(in_env "$cmd" 2>&1) || rc=$?
    echo "=== $name: $cmd (rc=$rc)"; echo "$out" | tail -20
    if [ $rc -eq 0 ] && { [ -z "$want" ] || grep -q -- "$want" <<<"$out"; }; then
        vrow PASS "$name" "$(grep -v '^\s*$' <<<"$out" | tail -1 | cut -c1-100)"
    else
        vrow FAIL "$name" "rc=$rc $(tail -1 <<<"$out" | cut -c1-90)"
    fi
}
compare_ref() {   # label file [reference file name, default: the same name]
    local name=${3:-$(basename "$2")} ref acc
    ref=$REFERENCE_DIR/$name
    acc=$ACCEPTED_DIR/$(basename "$2").diff
    rm -f "$2.diff"
    if [ ! -s "$2" ]; then
        vrow FAIL "$1" "$(basename "$2") is empty"
    elif [ ! -f "$ref" ]; then
        vrow FAIL "$1" "no reference $ref"
    elif diff -q "$ref" "$2" >/dev/null; then
        vrow PASS "$1" "identical to pinky's $name"
    else
        diff "$ref" "$2" > "$2.diff" || true
        if [ -f "$acc" ] && diff -q "$acc" "$2.diff" >/dev/null; then
            vrow PASS "$1" "known diff to pinky ($(grep -c '^[<>]' "$2.diff") lines, = reference-accepted/$(basename "$acc"))"
        else
            vrow FAIL "$1" "$(grep -c '^<' "$2.diff") pinky-only / $(grep -c '^>' "$2.diff") piana-only lines, not the accepted diff: $2.diff"
        fi
    fi
}
vdump() {   # pkg builddir kinds...
    local pkg=$1 bld=$2 kind out; shift 2
    for kind in "$@"; do
        case $kind in
            cmake-la)    out=$PREFIX/verify/cmake_la_$pkg.txt ;;
            cmake-extra) out=$PREFIX/verify/cmake_extra_$pkg.txt ;;
            makeflags)   out=$PREFIX/verify/makeflags_$pkg.txt ;;
            cc)          out=$PREFIX/verify/compile_commands_${pkg}_flat.txt ;;
        esac
        rm -f "$out" "$out.diff"
        if python3 "$SWDIR/parity.py" --host piana --prefix "$PREFIX" "$kind" "$bld" > "$out" 2> "$out.err"; then
            rm -f "$out.err"
            compare_ref "$pkg $kind" "$out"
        else
            vrow FAIL "$pkg $kind" "parity.py failed: $(tail -1 "$out.err")"
        fi
    done
}

# Every pinned package the host provided must still be pinky's build, and every
# base-system package must be pinky's whether or not it was fetched.
verify_host_rpms() {
    local line name epoch ver rel arch src md5 now bad=() n=0
    local provided
    provided=$(awk '$1=="rpm" && $2=="host" {print $3}' "$PREFIX/manifest.d/rpms.txt" 2>/dev/null || true)
    for line in "${RPMS[@]}"; do
        read -r name epoch ver rel arch src md5 <<<"$line"
        if grep -qxF "$name-$ver-$rel.$arch" <<<"$provided" || [[ $name =~ $RPMS_HOST_ONLY_RE ]]; then
            n=$((n + 1))
            now=$(rpm "${RPMDEF[@]}" -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE} %{SIGMD5}' "$name.$arch" 2>/dev/null || echo "not installed")
            [ "$now" = "$epoch:$ver-$rel $md5" ] || bad+=("$name.$arch: $now")
        fi
    done
    if [ ${#bad[@]} -eq 0 ]; then
        vrow PASS "host RPMs are pinky's" "$n host-provided/base-system packages at pinky's NVR and SIGMD5"
    else
        vrow FAIL "host RPMs are pinky's" "${#bad[@]} drifted: ${bad[*]:0:3}"
        printf '%s\n' "${bad[@]}" > "$PREFIX/verify/host_rpm_drift.txt"
    fi
    local gccpin
    gccpin=$(printf '%s\n' "${RPMS[@]}" | awk '$1=="gcc"{print $3"-"$4}')
    now=$(rpm "${RPMDEF[@]}" -q --qf '%{VERSION}-%{RELEASE}' gcc 2>/dev/null || true)
    if [ "$now" = "$gccpin" ]; then vrow PASS "gcc package" "gcc-$now"; else vrow FAIL "gcc package" "gcc-$now, pinky has gcc-$gccpin"; fi
}

step_verify() {
    local note=""
    if [ "$NO_SYSROOT" = 1 ]; then note=" (BT2026_NO_SYSROOT)"; fi
    # shellcheck disable=SC2016
    {
    vcheck "env.sh sources" "true"
    vcheck "toolchain not conda" 'for t in gcc g++ cmake python3; do p=$(command -v $t); echo -n "$t=$p "; case $p in /usr/bin/*|"$BT2026_SW"/sysroot/usr/bin/*) ;; *) exit 1;; esac; done; echo'
    vcheck "root-config --version" "root-config --version" "$ROOT_VERSION"
    vcheck "cmake --version" "cmake --version | head -1" "4.3.0"
    vcheck "gcc --version" "gcc --version | head -1" "16.2.1"
    vcheck "python imports" 'cd / && python3 -c "
import os, ROOT, midas, midas.client, psycopg, numpy
rs, ms = os.environ[\"ROOTSYS\"], os.environ[\"MIDASSYS\"]
assert ROOT.gROOT.GetVersion() == \"'"$ROOT_VERSION"'\", ROOT.gROOT.GetVersion()
assert os.path.realpath(ROOT.__file__).startswith(os.path.realpath(rs) + os.sep), ROOT.__file__
assert os.path.realpath(midas.__file__).startswith(os.path.realpath(ms) + os.sep), midas.__file__
print(\"ROOT\", ROOT.gROOT.GetVersion(), \"from ROOTSYS, midas from MIDASSYS, psycopg\", psycopg.__version__, \"numpy\", numpy.__version__)"'
    vcheck "gaudirun.py --help" "cd / && gaudirun.py --help | head -3" "sage"
    vcheck "PIONEERSYS" 'test -d "$PIONEERSYS/reco_testbeam/conditions" && echo "PIONEERSYS=$PIONEERSYS"'
    vcheck "shared libs resolve" 'bad=; for f in "$BT2026_SW"/gaudi/install/lib/*.so "$PIONEERSYS"/install/lib/*.so "$BT2026_SW"/midas/lib/*.so; do if ldd "$f" | grep -q "not found"; then echo "$f:"; ldd "$f" | grep "not found"; bad=1; fi; done; test -z "$bad" && echo "all libraries resolve"'
    }
    verify_host_rpms

    # main's own unit tests: reported, not a parity criterion, so a failure is a
    # WARN row (README: psm_reco fails in unoptimised builds, a test bug).
    local ct rc=0
    # shellcheck disable=SC2016
    ct=$(in_env 'cd "$_BT_PREFIX/src/main/build" && ctest -j"$_BT_JOBS" 2>&1') || rc=$?
    echo "=== main ctest (rc=$rc)"; echo "$ct" | tail -25
    if [ $rc -eq 0 ]; then
        vrow PASS "main ctest" "$(grep 'tests passed' <<<"$ct")"
    else
        vrow WARN "main ctest" "$(grep 'tests passed' <<<"$ct"); failed: $(grep -oE '[0-9]+ - [A-Za-z0-9_]+ \(Failed[^)]*\)' <<<"$ct" | tr '\n' ' ')"
    fi

    # ROOT: the unpack-time manifest, and that nothing appeared since but what
    # python and cling write at run time (__pycache__/, lib/modules.timestamp).
    # The comparison with pinky's tree skips the same entries on both sides.
    local rt='(/__pycache__/|  lib/modules\.timestamp$)'
    if [ -f "$PREFIX/verify/root_manifest.txt" ]; then
        grep -Ev "$rt" "$PREFIX/verify/root_manifest.txt" > "$PREFIX/verify/root_manifest_nopyc.txt" || true
        python3 "$SWDIR/parity.py" manifest "$ROOTDIR" | grep -Ev "$rt" > "$PREFIX/tmp/root_now.txt" || true
        if diff -q "$PREFIX/verify/root_manifest_nopyc.txt" "$PREFIX/tmp/root_now.txt" >/dev/null; then
            vrow PASS "ROOT tree untouched" "$(wc -l < "$PREFIX/tmp/root_now.txt") files as unpacked (run-time .pyc and modules.timestamp not counted)"
        else
            vrow FAIL "ROOT tree untouched" "differs from the unpack-time manifest"
        fi
        rm -f "$PREFIX/tmp/root_now.txt"
        local want got
        want=$(grep -v '^#' "$REFERENCE_DIR/root_manifest_pinky.sha256" 2>/dev/null || true)
        got="$(sha256sum < "$PREFIX/verify/root_manifest_nopyc.txt" | cut -d' ' -f1) $(wc -l < "$PREFIX/verify/root_manifest_nopyc.txt")"
        if [ -z "$want" ]; then
            vrow FAIL "ROOT manifest vs pinky" "no $REFERENCE_DIR/root_manifest_pinky.sha256"
        elif [ "$want" = "$got" ]; then
            vrow PASS "ROOT manifest vs pinky" "sha256 and file count equal pinky's ($got)"
        else
            vrow FAIL "ROOT manifest vs pinky" "got '$got', pinky '$want' (diff against the WP1 root_manifest_pinky.txt)"
        fi
    else
        vrow FAIL "ROOT manifest" "no verify/root_manifest.txt (root step not run?)"
    fi
    compare_ref "GSL manifest vs pinky" "$PREFIX/verify/gsl_manifest.txt" gsl_manifest_pinky.txt

    vdump gsl   "$PREFIX/gsl/build"      cmake-la cmake-extra
    vdump clhep "$PREFIX/clhep/build"    cmake-la cmake-extra makeflags
    vdump midas "$PREFIX/midas/build"    cmake-la cmake-extra makeflags cc
    vdump gaudi "$PREFIX/gaudi/build"    cmake-la cmake-extra makeflags cc
    vdump main  "$PREFIX/src/main/build" cmake-la cmake-extra makeflags
    {
        echo "verify $(date -u +%FT%TZ) prefix=$PREFIX$note reference=$REFERENCE_DIR"
        printf '%s\n' "${VROWS[@]}"
    } > "$PREFIX/verify/verify.txt"
    echo; cat "$PREFIX/verify/verify.txt"; echo
    ! grep -q '^FAIL' "$PREFIX/verify/verify.txt"
}

# --- driver ---------------------------------------------------------------------------
# The directories a step (re)creates. rpms and root build a .new tree and swap
# it in themselves; the CMake builds cannot move after the fact (their paths are
# compiled in), so a redo moves the old tree aside to <dir>.old, builds in
# place, and on failure puts the old tree back.
step_dirs() {
    case $1 in
        gsl) echo "$PREFIX/gsl" ;; clhep) echo "$PREFIX/clhep" ;; midas) echo "$PREFIX/midas" ;;
        gaudi) echo "$PREFIX/gaudi" ;; python) echo "$PREFIX/python" ;; main) echo "$PREFIX/src/main" ;;
    esac
}

run_step() {
    local step=$1 key log=$LOGDIR/$1.log t0 rc=0 dir oldstamp=""
    key=$(step_key "$step")
    if [ $FORCE = 0 ] && [ -f "$STAMPS/$step" ] && [ "$(cat "$STAMPS/$step")" = "$key" ]; then
        say "$step: done already (use --force to redo)"
        return 0
    fi
    dir=$(step_dirs "$step")
    if [ -n "$dir" ]; then
        # a previous run that died half-way: its .old is the last good tree
        if [ -e "$dir.old" ]; then safe_rm "$dir"; mv "$dir.old" "$dir"; fi
        if [ -e "$dir" ]; then mv "$dir" "$dir.old"; fi
    fi
    [ -f "$STAMPS/$step" ] && oldstamp=$(cat "$STAMPS/$step")
    rm -f "$STAMPS/$step"
    MANIFEST_PART=$PREFIX/manifest.d/$step.txt
    : > "$MANIFEST_PART.new"
    MANIFEST_PART=$MANIFEST_PART.new
    : > "$WARNFILE"
    t0=$(date +%s)
    say "$step: started $(date '+%F %T'), log $log"
    # Not `( ... ) || rc=$?`: inside an || list bash ignores set -e for the whole
    # subshell, and a failing configure would roll on into make.
    set +e
    ( set -euo pipefail; "step_$step" ) > "$log" 2>&1
    rc=$?
    set -e
    if [ -s "$WARNFILE" ]; then sed 's/^/[install] /' "$WARNFILE" >&2; fi
    if [ $rc -ne 0 ]; then
        tail -n 40 "$log" >&2
        if [ -n "$dir" ] && [ -e "$dir.old" ]; then
            safe_rm "$dir"; mv "$dir.old" "$dir"
            [ -n "$oldstamp" ] && echo "$oldstamp" > "$STAMPS/$step"
            say "$step: the previous $dir is back in place"
        fi
        rm -f "$MANIFEST_PART"
        [ "$step" = verify ] && cat "$PREFIX/verify/verify.txt" 2>/dev/null
        die "$step failed after $(( $(date +%s) - t0 )) s (full log: $log)"
    fi
    if [ -n "$dir" ]; then safe_rm "$dir.old"; fi
    mv "$MANIFEST_PART" "${MANIFEST_PART%.new}"
    echo "$key" > "$STAMPS/$step"
    say "$step: done in $(( $(date +%s) - t0 )) s"
    if [ "$step" = verify ]; then cat "$PREFIX/verify/verify.txt"; fi
    {
        echo "# MANIFEST of $PREFIX -- written by install.sh, $(date -u +%FT%TZ)"
        echo "# pie5 repo $PIE5_REPO at $(git -C "$PIE5_REPO" rev-parse HEAD 2>/dev/null || echo '?')"
        local s
        for s in "${ALL_STEPS[@]}"; do
            if [ -f "$PREFIX/manifest.d/$s.txt" ]; then echo "## $s"; cat "$PREFIX/manifest.d/$s.txt"; fi
        done
    } > "$PREFIX/MANIFEST.txt"
}

say "prefix $PREFIX, steps: ${STEPS[*]}, jobs $JOBS"
for s in "${STEPS[@]}"; do run_step "$s"; done
say "all requested steps finished"
