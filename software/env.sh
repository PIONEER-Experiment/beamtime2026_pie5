# env.sh -- the one script to source for the PSM nearline jobs (bash only).
#
#   source /path/to/beamtime2026_pie5/software/env.sh
#
# Sets up ROOT, Gaudi, CLHEP, GSL, MIDAS, the main (reco) install and this
# repository's python/ exactly as pinky's ~/.bashrc does, on either host:
#
#   pinky (hostname PINKY)              the stack under /home/pinky/...
#   piana (hostname pioneer-analysis)   the stack install.sh built under
#                                       /home/pioneer/bt2026/software
#
# Overrides (set before sourcing):
#   BT2026_HOST   pretend to be this host (PINKY or pioneer-analysis)
#   BT2026_SW     piana: the install prefix     (default /home/pioneer/bt2026/software)
#   BT2026_RECO   the main checkout with install/ (pinky: ~/bt2026/reco/repo,
#                 piana: $BT2026_SW/src/main)
#   BT2026_PIE5   the beamtime2026_pie5 checkout (default: the one holding this file)
#
# Only the sourcing shell changes, and no file is written. An active conda env
# is deactivated, and every variable and path entry that points into conda or
# into the previous MIDASSYS is removed. Then every path this script manages
# is taken out of PATH, LD_LIBRARY_PATH, PYTHONPATH and CMAKE_PREFIX_PATH and
# put back in pinky's order, so sourcing it again (or after pinky's ~/.bashrc)
# gives the same result. If a piece of the stack is missing, it says what and
# returns 1 before changing anything.

if [ -z "${BASH_VERSION:-}" ]; then
    echo "env.sh: needs bash" >&2
    return 1 2>/dev/null || exit 1
fi
if ! (return 0 2>/dev/null); then
    echo "env.sh: source it (source ${BASH_SOURCE[0]}), do not run it" >&2
    exit 1
fi

# --- helpers (all named _bt2026_*, removed again at the end) -------------------
_bt2026_under() {   # path root -> 0 if path is root or below it
    local p=${1%/} r=${2%/}
    [ -n "$r" ] && { [ "$p" = "$r" ] || [[ $p == "$r"/* ]]; }
}
_bt2026_filter() {  # colon-list, then paths to drop (each: the path and everything below)
    local list=$1 out="" e r keep IFS=:
    shift
    for e in $list; do
        [ -n "$e" ] || continue
        keep=1
        for r in "$@"; do if _bt2026_under "$e" "$r"; then keep=""; break; fi; done
        [ -n "$keep" ] && out=${out:+$out:}$e
    done
    printf '%s' "$out"
}
_bt2026_drop() {    # colon-list, then exact entries to drop
    local list=$1 out="" e r keep IFS=:
    shift
    for e in $list; do
        [ -n "$e" ] || continue
        keep=1
        for r in "$@"; do if [ "${e%/}" = "${r%/}" ]; then keep=""; break; fi; done
        [ -n "$keep" ] && out=${out:+$out:}$e
    done
    printf '%s' "$out"
}
_bt2026_set() {     # var value -> export, or unset when empty
    if [ -n "$2" ]; then export "$1=$2"; else unset "$1"; fi
}
_bt2026_pre() { local cur=${!1:-}; export "$1=$2${cur:+:$cur}"; }
_bt2026_app() { local cur=${!1:-}; export "$1=${cur:+$cur:}$2"; }
_bt2026_dedup() {   # colon-list -> first occurrence of each entry, no empty entries
    local out="" e IFS=:
    for e in $1; do
        [ -n "$e" ] || continue
        case ":$out:" in *":$e:"*) ;; *) out=${out:+$out:}$e ;; esac
    done
    printf '%s' "$out"
}
_bt2026_before_system_bin() {   # dir -> PATH with dir right before /usr/local/bin (else /usr/bin)
    local out="" e anchor=/usr/bin IFS=:
    case ":$PATH:" in *:/usr/local/bin:*) anchor=/usr/local/bin ;; esac
    for e in $PATH; do
        [ -n "$e" ] || continue
        [ "$e" = "$anchor" ] && out=${out:+$out:}$1
        out=${out:+$out:}$e
    done
    case ":$out:" in *":$1:"*) ;; *) out=$1${out:+:$out} ;; esac
    printf '%s' "$out"
}

_bt2026_env() {
    local here host sw reco pie5 sysroot root gaudi clhep gsl midas
    here=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || return 1

    host=${BT2026_HOST:-$(hostname 2>/dev/null || uname -n)}
    pie5=${BT2026_PIE5:-$(dirname "$here")}
    case $host in
        PINKY|PINKY.*|pinky|pinky.*)
            host=pinky
            sw=""
            root=/home/pinky/software/root-6.38.04
            gaudi=/home/pinky/packages/gaudi/install
            clhep=/home/pinky/packages/clhep/install
            gsl=""                                   # in /usr/local, found by default
            midas=/home/pinky/packages/midas
            reco=${BT2026_RECO:-/home/pinky/bt2026/reco/repo}
            sysroot=""
            ;;
        pioneer-analysis|pioneer-analysis.*|piana)
            host=piana
            sw=${BT2026_SW:-/home/pioneer/bt2026/software}
            root=$sw/root-6.38.04
            gaudi=$sw/gaudi/install
            clhep=$sw/clhep/install
            gsl=$sw/gsl/install
            midas=$sw/midas
            reco=${BT2026_RECO:-$sw/src/main}
            sysroot=${BT2026_SYSROOT-$sw/sysroot}    # BT2026_SYSROOT= (empty): no sysroot
            ;;
        *)
            echo "env.sh: unknown host '$host'; set BT2026_HOST=PINKY or BT2026_HOST=pioneer-analysis" >&2
            return 1
            ;;
    esac

    # --- everything must exist before anything is changed -----------------------
    local missing=() p
    for p in "$root/bin/thisroot.sh" "$gaudi/bin" "$gaudi/lib" "$gaudi/python" "$clhep/lib" \
             "$midas/lib" "$midas/python" "$pie5/python" ${gsl:+"$gsl/include"} \
             ${sysroot:+"$sysroot/usr/bin/cmake"} ${sysroot:+"$sysroot/usr/lib64"}; do
        [ -e "$p" ] || missing+=("$p")
    done
    if [ -z "${BT2026_SKIP_RECO:-}" ] && [ ! -f "$reco/install/setenv.sh" ]; then
        missing+=("$reco/install/setenv.sh")
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        echo "env.sh: not set up, missing on $host:" >&2
        printf '  %s\n' "${missing[@]}" >&2
        [ "$host" = piana ] && echo "  (install with: bash $here/install.sh $sw)" >&2
        return 1
    fi

    # --- conda and the old MIDASSYS out ------------------------------------------
    # The roots to remove, collected before `conda deactivate` forgets them.
    local roots=() v e conda_seen=""
    for v in CONDA_PREFIX $(compgen -v CONDA_PREFIX_) MAMBA_ROOT_PREFIX; do
        [ -n "${!v:-}" ] && roots+=("${!v}")
    done
    for v in CONDA_EXE MAMBA_EXE; do
        [ -n "${!v:-}" ] && roots+=("$(dirname "$(dirname "${!v}")")")
    done
    local IFS_save=$IFS
    IFS=:
    for e in $PATH; do   # conda installs not announced by any variable
        case $e in */miniforge3|*/miniforge3/*|*/miniconda3|*/miniconda3/*|*/anaconda3|*/anaconda3/*| \
                   */mambaforge|*/mambaforge/*|*/micromamba|*/micromamba/*|*/conda|*/conda/*)
            roots+=("${e%%/bin}") ;;
        esac
    done
    IFS=$IFS_save
    local good=() r
    for r in "${roots[@]}"; do   # never a system or home directory, whatever a variable says
        r=${r%/}
        case $r in ""|/|/usr|/usr/local|/opt|/home|"${HOME%/}"|"$root"|"$sw"|"$midas") continue ;; esac
        good+=("$r")
    done
    roots=("${good[@]}")
    [ ${#roots[@]} -gt 0 ] && conda_seen=1
    if declare -F conda >/dev/null; then
        local i
        for i in 1 2 3 4 5 6 7 8 9 10; do
            [ -n "${CONDA_PREFIX:-}" ] || break
            conda deactivate >/dev/null 2>&1 || break
        done
    fi
    local oldmidas=${MIDASSYS%/}
    local listvars=" PATH LD_LIBRARY_PATH PYTHONPATH CMAKE_PREFIX_PATH PKG_CONFIG_PATH MANPATH INFOPATH XDG_DATA_DIRS CPATH LIBRARY_PATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH DYLD_LIBRARY_PATH SHLIB_PATH LIBPATH "
    local drop=("${roots[@]}")
    if [ -n "$oldmidas" ] && [ "$oldmidas" != "$midas" ]; then drop+=("$oldmidas"); fi
    if [ ${#drop[@]} -gt 0 ]; then
        local hit
        for v in $(compgen -e); do
            case $v in HOME|PWD|OLDPWD|SHELL|USER|LOGNAME|TERM|BT2026_*|MIDASSYS) continue ;; esac
            hit=""
            for r in "${drop[@]}"; do [[ ${!v} == *"$r"* ]] && { hit=1; break; }; done
            [ -n "$hit" ] || continue
            if [[ $listvars == *" $v "* ]]; then
                _bt2026_set "$v" "$(_bt2026_filter "${!v}" "${drop[@]}")"
            else
                unset "$v"
            fi
        done
    fi
    unset PYTHONHOME

    # --- take out everything this script manages ---------------------------------
    local sp=()
    if [ -n "$sysroot" ]; then
        for p in "$sysroot"/usr/lib64/python3.*/site-packages "$sysroot"/usr/lib/python3.*/site-packages; do
            [ -d "$p" ] && sp+=("$p")
        done
    fi
    _bt2026_set PATH "$(_bt2026_drop "$PATH" "$root/bin" "$gaudi/bin" "$midas/bin" "$reco/install/bin" ${sysroot:+"$sysroot/usr/bin"})"
    _bt2026_set LD_LIBRARY_PATH "$(_bt2026_drop "${LD_LIBRARY_PATH:-}" "$root/lib" "$gaudi/lib" "$clhep/lib" "$reco/install/lib" ${sysroot:+"$sysroot/usr/lib64"})"
    _bt2026_set PYTHONPATH "$(_bt2026_drop "${PYTHONPATH:-}" "$pie5/python" "$root/lib" "$midas/python" "$gaudi/python" "$reco/analyser" "$reco/install/python" ${sw:+"$sw/python"} "${sp[@]}")"
    _bt2026_set CMAKE_PREFIX_PATH "$(_bt2026_drop "${CMAKE_PREFIX_PATH:-}" "$root" "$gaudi" "$clhep" ${gsl:+"$gsl"} ${sysroot:+"$sysroot/usr"})"
    _bt2026_set PKG_CONFIG_PATH "$(_bt2026_drop "${PKG_CONFIG_PATH:-}" ${sysroot:+"$sysroot/usr/lib64/pkgconfig" "$sysroot/usr/share/pkgconfig"})"
    for v in DYLD_LIBRARY_PATH SHLIB_PATH LIBPATH; do _bt2026_set "$v" "$(_bt2026_drop "${!v:-}" "$root/lib")"; done
    _bt2026_set MANPATH "$(_bt2026_drop "${MANPATH:-}" "$root/man")"
    for v in JUPYTER_PATH JUPYTER_CONFIG_PATH; do _bt2026_set "$v" "$(_bt2026_drop "${!v:-}" "$root/etc/notebook")"; done
    unset ROOTSYS

    # --- and put it back, in the order of pinky's ~/.bashrc ------------------------
    # piana: pinky's /usr packages live in the sysroot. Its bin goes right before
    # the system bin directories, its libraries and python site-packages behind
    # everything (they stand in for the system ones), and gcc treats its headers
    # and GSL's as system headers, the way /usr/include and /usr/local/include
    # are on pinky.
    if [ -n "$sysroot" ]; then
        export PATH=$(_bt2026_before_system_bin "$sysroot/usr/bin")
    fi
    _bt2026_pre PATH "$gaudi/bin"
    _bt2026_pre LD_LIBRARY_PATH "$clhep/lib"
    _bt2026_pre LD_LIBRARY_PATH "$gaudi/lib"
    _bt2026_pre CMAKE_PREFIX_PATH "$clhep"
    _bt2026_pre CMAKE_PREFIX_PATH "$gaudi"
    _bt2026_app PYTHONPATH "$midas/python"
    _bt2026_app PYTHONPATH "$gaudi/python"
    _bt2026_app PATH "$midas/bin"
    export MIDASSYS=$midas
    if [ "$host" = pinky ]; then
        export MIDAS_EXPTAB=/home/pinky/online/exptab MIDAS_EXPT_NAME=bt2026
    fi

    # ROOT's own script prepends ROOT to PATH, LD_LIBRARY_PATH, PYTHONPATH and
    # CMAKE_PREFIX_PATH. It finds its location from the working directory, and
    # is not written for `set -u`.
    local rc=0 u_was=""
    [[ $- == *u* ]] && u_was=1
    set +u
    if pushd "$root" >/dev/null; then
        # shellcheck disable=SC1091
        source bin/thisroot.sh || rc=$?
        popd >/dev/null || true
    else
        rc=1
    fi
    [ -n "$u_was" ] && set -u
    if [ $rc -ne 0 ] || [ "${ROOTSYS:-}" != "$root" ]; then
        echo "env.sh: $root/bin/thisroot.sh failed (ROOTSYS='${ROOTSYS:-}')" >&2
        return 1
    fi

    # main's install/setenv.sh: PIONEERSYS, install/bin, install/lib, analyser,
    # install/python (appended).
    if [ -z "${BT2026_SKIP_RECO:-}" ]; then
        # shellcheck disable=SC1091
        source "$reco/install/setenv.sh" || { echo "env.sh: $reco/install/setenv.sh failed" >&2; return 1; }
    fi
    _bt2026_pre PYTHONPATH "$pie5/python"

    if [ -n "$sysroot" ]; then
        _bt2026_app CMAKE_PREFIX_PATH "$gsl"
        _bt2026_app CMAKE_PREFIX_PATH "$sysroot/usr"
        _bt2026_app LD_LIBRARY_PATH "$sysroot/usr/lib64"
        _bt2026_app PKG_CONFIG_PATH "$sysroot/usr/lib64/pkgconfig"
        _bt2026_app PKG_CONFIG_PATH "$sysroot/usr/share/pkgconfig"
        export CPLUS_INCLUDE_PATH=$gsl/include:$sysroot/usr/include
        export C_INCLUDE_PATH=$gsl/include:$sysroot/usr/include
        export LIBRARY_PATH=$sysroot/usr/lib64
        # pip --user on pinky -> $BT2026_SW/python here; then the RPM site-packages
        # (numpy, pip), as /usr/lib*/python3.14/site-packages on pinky. (That pip
        # would install into ~/.local by default: always give it --target.)
        _bt2026_app PYTHONPATH "$sw/python"
        for p in "${sp[@]}"; do _bt2026_app PYTHONPATH "$p"; done
        # numpy's BLAS goes through flexiblas, whose backends sit at a compiled-in
        # /usr/lib64/flexiblas path; name pinky's default backend explicitly.
        if [ -f "$sysroot/usr/lib64/flexiblas/libflexiblas_openblas-openmp.so" ]; then
            export FLEXIBLAS=$sysroot/usr/lib64/flexiblas/libflexiblas_openblas-openmp.so
        fi
        # Keep python's bytecode out of the checkouts the jobs import from.
        export PYTHONPYCACHEPREFIX=$sw/cache/pycache
    fi
    for v in PATH LD_LIBRARY_PATH PYTHONPATH CMAKE_PREFIX_PATH; do
        _bt2026_set "$v" "$(_bt2026_dedup "${!v:-}")"
    done

    export BT2026_ENV="host=$host root=$(root-config --version 2>/dev/null) reco=$reco pie5=$pie5${sw:+ sw=$sw}"
    echo "bt2026 env: $BT2026_ENV${conda_seen:+ (conda stripped)}"
}

if _bt2026_env; then _bt2026_rc=0; else _bt2026_rc=1; fi
unset -f $(compgen -A function _bt2026_)
eval "unset _bt2026_rc; return $_bt2026_rc"
