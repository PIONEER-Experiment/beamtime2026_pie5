# MIDAS custom pages

## `caenhv.html` — CAEN DT1470ET high voltage

Operator page for the 4-channel `CaenHV` slow-control equipment: one row per
channel with VSET / VMON / ISET / IMON / MAXV, an on/off checkbox, a decoded
STAT word and the per-channel alarm state, plus a small read-only table of the
ramp speeds and trip times.

Everything it shows comes from `/Equipment/CaenHV/...` and `/Alarms/Alarms/...`.
The page writes only to three keys, all through the normal MIDAS controls:

| control | ODB key | how |
| --- | --- | --- |
| VSET | `/Equipment/CaenHV/Variables/Demand[i]` | `modbvalue` inline edit (click, type, Enter) |
| ISET | `/Equipment/CaenHV/Settings/Current Limit[i]` | `modbvalue` inline edit |
| MAXV | `/Equipment/CaenHV/Settings/Voltage Limit[i]` | `modbvalue` inline edit |
| On/Off | `/Equipment/CaenHV/Variables/ChState[i]` | checkbox; switching **on** asks for confirmation first |

Switching a channel on pops up a `dlgConfirm` that also reminds the operator
that a channel whose front-panel switch is in OFF or KILL (status `DIS` /
`KILL`) will not come on: the board acknowledges `PAR:ON` and silently does
nothing. Switching off is immediate, no confirmation.

`IMON` shows `n/a` while the driver's "never read" sentinel (`-1`) is in
`Variables/Current`. The decoded Status, Alarm, Pol, Name and IMON cells are
refreshed by the page's own 1 s `mjsonrpc_db_get_values` poll; the plain
numeric cells are `modbvalue` and are refreshed by mhttpd itself.

STAT bit numbers are duplicated in the page's `HV_BITS` array. The single
source of truth is `scfe/caen_hv_fe.h` (`enum stat_bit_t`) — if a bit moves
there, move it here too.

### ODB registration

mhttpd serves `/Custom/<name>` files relative to the **one** global key
`/Custom/Path`, so register the page with:

```
odbedit -e caenhv -c 'create STRING /Custom/Path'
odbedit -e caenhv -c 'set /Custom/Path /workdir/beamtime2026_pie5/custom'
odbedit -e caenhv -c 'create STRING /Custom/CaenHV'
odbedit -e caenhv -c 'set /Custom/CaenHV caenhv.html'
```

The page then appears in the left-hand menu as **CaenHV** and at
`?cmd=custom&page=CaenHV`.

`/Custom/Path` must be the path **as mhttpd sees it**. mhttpd runs inside the
`testbeam-midas` container, where `/home/jlabo/github/pioneer/testbeam-env` is
mounted at `/workdir`, so the container path is
`/workdir/beamtime2026_pie5/custom` and the host path is
`/home/jlabo/github/pioneer/testbeam-env/beamtime2026_pie5/custom`. Use the
container path. A trailing slash is optional (mhttpd adds one); a bare `/` or a
value with no `/` in it is rejected with an `add_custom_path` error.

> **Warning — `/Custom/Path` is single-global.** There is one such key for the
> whole experiment and other frontends fight over it. In particular musip's
> `quads_config_fe` **rewrites `/Custom/Path` on every start** to
> `$HOME/musip/custom`, which would orphan this page at its next restart. See
> `midas_files/wavedream-scalar-readout/docs/REGISTRY.md`, section "Things that
> are single-global and get fought over". If both frontends must coexist in one
> experiment, either symlink this file into whatever directory wins, or keep this
> page in a MIDAS experiment (e.g. `caenhv`) that musip does not attach to.

### Not registered here

This directory contains files only. Nothing in the repo writes `/Custom/*`;
registration is a deliberate manual step.
