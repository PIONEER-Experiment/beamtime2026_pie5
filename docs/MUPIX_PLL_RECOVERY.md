# MuPix PLL recovery before each run

This is for a shifter running the sequencer (`sequencer/sequencer_operator.py`),
or anyone who suspects a MuPix chip has lost its PLL lock.

## What it does

Before every run, after the run config has been loaded, the sequencer checks
the 8 MuPix chips (FEB 0, chips 0-7, LVDS links 0-23):

1. It reads `/Equipment/Quads/Variables/PCLS` twice, 3 s apart.
2. A chip is **bad** if any of its 3 links has lost READY (the same READY you
   see on the Quads LVDS page) while the FPGA receiver PLL of that link is
   still locked, or gains more than 1e7 8b10b errors per second. A broken chip
   shows about 1e8/s; a healthy one shows under 1e3/s. A link that is noisy but
   READY (around 1e5/s) is not bad. A counter that went down (reset) gives no
   rate and is only noted in the log.
3. For the bad chips only, it does what the Quads page's **Reset PLL?** box does,
   with a pause after each step:

   | Step | Then wait |
   |---|---|
   | ASICMask[0] = the bad chips, EnPLL = 1 for each | 1 s |
   | MupixConfig, until the frontend sets it back to false | 1 s (the PLL pulse) |
   | EnPLL = 0 for each | 1 s |
   | MupixConfig, until the frontend sets it back to false | 1 s |
   | ASICMask[0] and EnPLL put back | 3 s for the links to settle, then the 3 s check again |

   One round takes about 10 s plus the time the frontend needs for the two
   MupixConfig commands (up to 15 s each before it gives up). The first check
   adds 3 s once.
4. It tries up to 3 times (`mupixMaxRetries`), each time only on the chips
   that are still bad.

Afterwards `ASICMask[0]` is put back to what it was before, and EnPLL is 0 for
the chips it touched. That happens even if the frontend does not answer or the
sequence is stopped halfway; a MupixConfig the step sent that is still pending
is set back to false first, so the frontend does not run it late.

Nothing is written unless the run is stopped (checked before each round).
Chips whose links are switched off in `LVDSLinkMask` are skipped. The SMA
board (FEB 1) and the unused links 24-35 are never looked at. If the ODB does
not say FEB 0 is the only active Quads board (`FEBsActive[0]=y`,
`FEBsQuads[0]=y`, `FEBsSMA[0]=n`, and no other FEB both Active and Quads),
nothing is checked or written.

Every step goes to the MIDAS message log, one line each, starting with
`MuPix PLL recovery:`, with the chip number, READY bits and error rates.

When all chips are fine, nothing is written to the ODB and you only see one line
in the message log.

## The yellow alarm

You get the yellow **Seq operator** banner and a message on the Sequencer page
in these cases:

| Message | What it means | What to do |
|---|---|---|
| `MuPix chip(s) 2, 6 still bad after 3 automatic PLL reset round(s)` | The automatic reset did not bring these chips back | Try **Reset PLL?** on the Quads page for those chips (below). If they do not come back, note it in the elog and decide whether to run without them |
| `MuPix PLL check: PCLS did not change ... not updating` | The two PCLS reads were identical and the key was not rewritten, so there is no way to judge the chips. Nothing was written | Check that the Quads frontend is running and its periodic updates are coming in |
| `MuPix PLL check: FEB 0 not readable ...` or `FPGA receiver PLL unlocked ...` | FEB 0 is not being read out (status words 0), or the FEB's own receiver PLL is unlocked. Resetting a chip would not help. Nothing was written | Check the FEB (power, optical link, Quads frontend) |
| `MuPix PLL recovery aborted: MupixConfig still true after 15 s` | The Quads frontend did not take the configure command | Check the Quads frontend. The message log says which chips were involved. They may still have EnPLL = 1 on the chip until the next MupixConfig |
| `MuPix PLL recovery aborted: MupixConfig was already pending before the recovery started` | Someone else's MupixConfig (the Quads page, a script) had not finished. Nothing was written | Wait for it to finish, then check the chips |
| `MuPix PLL recovery aborted: run is running, not stopped` | A run was started while the step was working. Nothing more was written | Nothing; check the chips after the run |
| `MuPix PLL recovery aborted: FEB layout not as expected` | `FEBsActive`/`FEBsQuads`/`FEBsSMA` are not what the step expects. Nothing was written | Check the Quads settings; do not switch the step back on until this is understood |
| `MuPix PLL recovery failed (...)` | Something unexpected, for example a missing ODB key | Read the error in the message log and tell the DAQ expert |

Press **OK** on the Sequencer page when you are done. The run is never
blocked: after OK the sequence continues. The usual "Please check the PLL Lock
is ok" pause still comes after this step.

## Running it by hand on pinky

The same code can be run from a terminal, without the sequencer. In a pinky
login shell, `~/.bashrc` already puts MIDAS and
`/home/pinky/bt2026/beamtime2026_pie5/python` on `PYTHONPATH`:

```bash
# read-only: READY bits, 8b10b rates and a verdict per chip
python3 -m pioneer.sequencer.mupix_recovery --check

# check, reset the bad chips, check again (up to 3 rounds)
python3 -m pioneer.sequencer.mupix_recovery --recover

# reset only chips 2 and 6: the first time without a check, then verify and
# retry on those two only (other bad chips are reported, not touched)
python3 -m pioneer.sequencer.mupix_recovery --recover --chips 2,6
```

From a shell that does not read `~/.bashrc` (ssh with a command, cron), set
the paths yourself:

```bash
MIDASSYS=/home/pinky/packages/midas \
PYTHONPATH=/home/pinky/bt2026/beamtime2026_pie5/python:/home/pinky/packages/midas/python \
MIDAS_EXPT_NAME=bt2026 MIDAS_EXPTAB=/home/pinky/online/exptab \
python3 -m pioneer.sequencer.mupix_recovery --check
```

Exit codes, for both `--check` and `--recover`:

| Code | Meaning |
|---|---|
| 0 | all checked chips OK |
| 1 | chip(s) bad (after `--recover`: still bad) |
| 2 | wrong command line |
| 3 | no verdict: PCLS not updating, or FEB 0 not readable |
| 4 | refused or aborted: FEB layout, run not stopped, frontend did not answer, no requested chip enabled |

`--recover` refuses to run unless the run is stopped; `--allow-running` overrides
that. `--chips` only takes 0-7, chips masked in `LVDSLinkMask` are skipped, and
`--retries` is capped at 3.

## Switching it off

On the Sequencer page, set the parameter **mupixRecovery** to off before
starting the sequence. **mupixMaxRetries** (0-3) sets the number of reset rounds;
0 means check and alarm, but never reset.

## Manual fallback: the Quads page

Quads page → select the chip(s) → tick **Reset PLL?** → **Configure** (not
**Configure all**, which reconfigures every chip). That is the same recipe the
automatic step uses. Afterwards check on the LVDS page
that the chip's three links are READY again and that the 8b10b counters have
stopped climbing.

Do not use **ResetASICs** or **MupixTDACConfig** for this: both act on every
chip.
