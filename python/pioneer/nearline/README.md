# `pioneer.nearline` — the nearline job and the daemon that runs it

One MIDAS file in, one RNTuple and one `_hists.root` out, with **both** detector
systems — WaveDREAM and PSM/MuSiP — decoded and reconstructed in the same pass.
`nearline_job.py` is that job, written to be read and edited by a person during a
run: every number is in one settings block at the top with a comment saying what
it does, and the assembly below should not need touching. That same file is
also the template: for every file the DAQ produces, the daemon fills in its
paths and writes the result next to the outputs as `<filebase>.py`, a complete
standalone job and the record of what processed that run. The histogram file is
what the nearline website reads during a shift; the RNTuple is for the offline
pass.

| file | what it is |
|---|---|
| `nearline_job.py` | **the edit-me file *and* the template.** A Gaudi options file: settings block, then linear assembly. `gaudirun.py` execs it; it is not a module and must not be imported. It carries eleven `${name}` placeholders, all inside string literals, so it is valid Python and runs unrendered |
| `render.py` | fills those placeholders and writes the complete job next to the outputs as `<filebase>.py`. `render_job()` is what both callers use; `python -m pioneer.nearline.render IN OUT` renders and stops |
| `process.py` | **process one file by hand**: `python -m pioneer.nearline.process <midas file> [--out-dir DIR]` renders the job and runs `gaudirun.py` on it. Standard library plus `render` only, so it imports where `jobs.py` cannot |
| `jobs.py` | job classes the daemon schedules: `GaudiJob` (this job), `RsyncJob`, `CleanJob`, `MergeJob`, `DummyJob` |
| `daemon.py` | the long-running process: MIDAS client, per-resource queues, dispatch and status write-back to the run database |
| `run.py` | run-sequence definitions (`midas_run_sequence`, `midas_run`) written into the run database |
| `combine_files.py` | merges the sub-run histograms of one run into a single normalised file |
| `beamtune_client.py`, `miniTwinInterface.py` | the daemon's clients for the beam-tune service and the mini-twin |
| `README.md` | this file |

## What the job runs

```
PIMidasSelector       one .mid/.mid.lz4, every event, no bank filter
  |
PIMidasDecoder        PITMidasWaveDream, PITMidasMusip
  |
PIWDSettingsSummary   top level, no waveform needed -> WDSettingsHeader
  |
WDAnalysisSeq         gated on /Event/wd_waveform, in THIS order
  |    PIWDRFPhase -> PIWDWaveformAnalysis -> PIWDCalibrator
  |
WDScalerSeq           gated on /Event/wd_scalers
  |    PIWDScalerMonitor                         histograms only
  |
PSMMuPixSeq           gated on /Event/muquad
  |    PIPSMMuPixMonitor                         histograms only
  |
PSMSMASeq             gated on /Event/mutrig
  |    PIPSMSMAMonitor                           histograms only
  |
PSMRecoSeq            gated on /Event/mutrig
  |    PIPSMAllTrackReco, PIPSMPatternReco,
  |    PIPSMComputeWeight, PIPSMDelayedCoincidence
  |
PIAOutputStream       RNTuple "rec"                -> <out>.root
PIHistogramSvc        histograms/<instance>/<name> -> <out>_hists.root
```

| instance | TES inputs | TES outputs | histograms |
|---|---|---|---|
| `PITMidasWaveDream` | WaveDREAM banks | `/Event/wd_event_header`, `wd_waveform`, `wd_channel_time`, `wd_timebase`, `wd_scalers` | — |
| `PITMidasMusip` | `H000` | `/Event/muquad`, `/Event/mutrig`, `/Event/rf` | `musip/current` (only when `PSM_CURRENT_CHANNEL` is set); with `PSM_SMA_DIAGNOSTICS` also `musip/sma_word_types`, `sma_words_per_channel`, `sma_bank_words_per_frame`, `sma_trigger_words_per_frame`, `sma_frame_span_ms`, `sma_frame_gap_ms`, `sma_live_time`, `sma_fine_coarse_diff`, `sma_fine_vs_coarse`, `sma_fine_bit_occupancy` |
| `PIWDSettingsSummary` | ODB conditions tables | `WDSettingsHeader` | — |
| `PIWDRFPhase` | `/Event/wd_waveform`, `wd_channel_time` | `/Event/wd_rf_phase` | `rf_phase`, `rf_amplitude`, `rf_residual` |
| `PIWDWaveformAnalysis` | `/Event/wd_waveform`, `wd_channel_time`, `wd_rf_phase` | `/Event/wd_features` | `ppamp`, `le_time`, `ppamp_vs_channel`; with a role table also `baseline_vs_channel`, `baseline_rms_vs_channel`, `fired_vs_channel`, `coincidence` and, per scintillator channel, `charge_vs_amp_chNN`, `letime_vs_amp_chNN`, `charge_vs_rfphase_chNN` |
| `PIWDCalibrator` | `/Event/wd_features`, `wd_rf_phase` | `/Event/wd_hits` | — |
| `PIWDScalerMonitor` | `/Event/wd_scalers` | — | `readings` and, per board `NNN` in `WD_SCALER_BOARDS`, `rate_vs_time_bNNN`, `mean_rate_bNNN`, `threshold_bNNN`, `fpga_temp_vs_time_bNNN` |
| `PIPSMMuPixMonitor` | `/Event/muquad`; `/Event/mutrig` (optional, only with `PSM_TIMEWALK`) | — | `L<n>_chip<vid>_xy` and `L<n>_xy` per MuPix chip and plane, `hits_per_chip`, `tot_vs_chip`, `L<n>_mult`, and from the L1/L2 coincidence `dt`, `npairs`, `npartners`, `dx`, `dy`, `track_xy`, `xxp`, `yyp`, and the same tracks on fixed axes `track_xy_expanded`, `xxp_central`, `yyp_central`, plus their acceptance-weighted twins `track_xy_expanded_w`, `xxp_central_w`, `yyp_central_w`; with `PSM_TIMEWALK` the all-pairs timewalk `tw_dt_vs_tot_L<n>_<vid>`, `tw_dt_vs_stot_L<n>_<vid>`, `tw_tot_vs_stot_L<n>_<vid>` per plane and counter S1-S5 (`<vid>` 2001, 2003-2006) |
| `PIPSMSMAMonitor` | `/Event/mutrig`; `/Event/rf` (optional, only when `PSM_RF_CHANNEL` is set) | — | `hits_per_counter`, `tot_vs_counter`, `hits_per_event_vs_counter`, `tot`, `fine_time_vs_counter`, `counters`, the S1-S5 coincidence views `pattern`, `s1_partners`, `pattern_duplicates`, `dt_to_s1`, `dt_to_s1_wide`, `pattern_counters`; with the RF input also `rf_period`, `rf_pulses_per_gate`, `rf_offset_vs_pulse`, `rf_phase`, `rf_veto_gap`, `rf_counters` and one `rf_phase_vs_tot_<vid>` per cabled counter |
| `PIPSMAllTrackReco` (a `PIPSMSimpleTrackReco`) | `/Event/muquad`, `/Event/mutrig`; `/Event/rf` (optional, only when `PSM_RF_CHANNEL` is set) | `/Event/exp_all_tracks` | `xy`, `xxp`, `yyp`, `nhits`, `nseed`, plus their acceptance-weighted twins `xy_w`, `xxp_w`, `yyp_w`; with the RF input also `xy_vs_s1phase`; with `PSM_TIMEWALK` the track-only timewalk `tw_dt_vs_tot_L<n>_<vid>`, `tw_tot_vs_stot_L<n>_<vid>`, and `tw_cluster_size_L<n>`, `tw_seeds` |
| `PIPSMPatternReco` | `/Event/exp_all_tracks` | `/Event/exp_pattern` | — |
| `PIPSMComputeWeight` | `/Event/exp_all_tracks` | `/Event/exp_track_weights` | — |
| `PIPSMDelayedCoincidence` | `/Event/exp_all_tracks`, `exp_track_weights` | `/Event/exp_tagged` | `counters`, `class`, `dt`, `sb`, `stop`, `xp`, `xp_w`, `yp`, `yp_w`, `xy`, `xy_w`, `xxp`, `xxp_w`, `yyp`, `yyp_w` |

**The SMA monitor reads `/Event/mutrig` alone.** `PIPSMSMAMonitor` is the
counter-side companion of the MuPix monitor: no pixel hits, no data-side channel
map, no tracklets, so it still says what the scintillator counters are doing
when those are what is broken. Six histograms: `hits_per_counter`, whose
relative heights are the relative rates; `tot_vs_counter`, the ToT spectrum of
each counter; `hits_per_event_vs_counter`; `tot`, every cabled counter together;
`fine_time_vs_counter`, the low 20 bits of the SMA time stamp, which are the
word's fine field unchanged, where a stuck or ramping field shows as
structure; and `counters`, the exposure — frames, hits, parked hits, hits with
an unknown vid. The counter axis is the raw `MUTRIG` map `PIGeometrySvc` serves,
so it follows the cabling of the run being processed, and the parked index is
the Degrader id every uncabled channel sits on — which is where the idle FEB's
ToT 0 and ToT 255 words land, and why that index is left out of the ToT
judgements.

**Its `finalize()` WARNINGs are data diagnostics.** It reports no SMA hits at
all; every cabled counter empty, which can also mean the map's interval parks
everything; per counter, one that took no hits, one
whose commonest ToT value takes nearly all of them (a pulser or a stuck field
rather than a spectrum), and one made mostly of ToT 0 and 255 (the idle words,
so that counter is not seeing its TOT box); and any hit carrying a vid the raw
map does not know.

**Two of those TES paths are this job's choice, not a default.**
`/Event/exp_all_tracks` and `/Event/exp_track_weights` are names the script
assigns and passes explicitly to the PSM algorithms; their own defaults are
`/Event/tracker_fr`, `/Event/dtar_fr` and `/Event/exp_simple_tracks`, which are
the simulation's names. The three decoder paths above them — `/Event/wd_waveform`,
`/Event/muquad`, `/Event/mutrig` — *are* the tools' defaults, which is why
changing a tool's path property without changing the `_TES_*` constants breaks
the sequencer gates silently.

**The order inside `WDAnalysisSeq` is load-bearing.** `PIWDWaveformAnalysis`
consumes `/Event/wd_rf_phase` as a third input — it evaluates the per-event RF
fit at each raw leading edge — so `PIWDRFPhase` must run before it. This job
uses the sequential event loop, where `Members` order *is* execution order, and
the script builds the list as `[rf, ana]` for that reason. `PIWDRFPhase` is also
no longer optional: it fatals at `initialize()` when its `RFTable` does not
resolve, so `WD_RF_TABLE = ""` is rejected by `check()` rather than dropping the
algorithm.

**The instance names and the `_hists.root` suffix are a contract with the
website.** It reads `histograms/<AlgorithmInstanceName>/<name>` out of
`<run>_hists.root`. Renaming `PIPSMAllTrackReco`, renaming a histogram, or
changing how `hist_file()` derives the file name breaks a plot on the shift
display, and nothing in this job will complain. The per-channel WaveDREAM
histogram names **embed the board-local channel number**, zero-padded to two
digits — `charge_vs_amp_ch03`, `letime_vs_amp_ch03`, `charge_vs_rfphase_ch03` —
so the set of names depends on which channels the role table marks `scint`
(channels 0–4 on run 193), and the website contract includes those names.

**One WARNING from `PIWDWaveformAnalysis` is a data diagnostic, not a
configuration error.** At `finalize()` it reports, per channel, the traces that
did *not* cross the recorded trigger level but *would* have crossed it mirrored
to the other polarity. Read it against `fired_vs_channel`: a channel with many
mirrored crossings and few firings has its `TriggerLevel` sign wrong in the ODB
or a swapped cable, which otherwise reads as a dead channel. A handful of them
next to a healthy firing count is just pile-up and noise, and nothing needs
changing. Run 193 emits exactly this warning.

Each sequencer gates per event on what actually decoded (`RequireObjects`),
which is why **no bank filter is set on the selector**: the filter is any-of over
a fixed bank list, so any list would drop one system the first time a board
serial or an event id changed, and unclaimed banks go to the `PITMidasNull`
fallback at no cost. Both frontends write into one MIDAS experiment in the
`joined` DAQ profile, so a single run file carries both systems, and their bank
names are disjoint by agreement — WaveDREAM owns `WDEH`, `DRSV`, `DRST`, `ADCW`,
`TDCW`, `TRGW`, `TRGI`, `TINP`, `SCLR` and the serial-keyed `S/T/X/D<nnn>`;
musip owns `H000` (`HT00`–`HT99`) plus `SSFE RCNT PCLS PCMS PVSC MTCR MTCH MTCF
MTCE MTCP MTTM MTSM`. That agreement is written down in
`midas_files/wavedream-scalar-readout/docs/REGISTRY.md`.

## Settings

Everything below is the block between `===== SETTINGS =====` and
`===== END OF SETTINGS =====` in `nearline_job.py`, read **once at configuration
time** — so editing it is safe while a job runs: the running job is unaffected
and the next one picks up the new value. Container settings hold bare file names
resolved against `CONDITIONS_DIR`; an absolute path is honoured unchanged.

### Job

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `EVT_MAX` | `-1` | `-1` is the whole file. A few thousand while tuning turns minutes into seconds. `NL_EVTMAX` in the environment wins |
| `OUTPUT_LEVEL` | `"INFO"` | `INFO` is what the shift log wants; `DEBUG` makes the decoder print per event and the job crawl |
| `WD_ENABLED` | `True` | Off drops the whole WaveDREAM chain (waveforms → features → RF → hits). Nothing downstream announces the missing collections |
| `PSM_DECODE` | `True` | Off leaves the `H000` banks undecoded, so `/Event/muquad`, `/Event/mutrig` and `/Event/rf` never exist |
| `PSM_RECO` | `True` | Requires `PSM_DECODE`. Off leaves the hits unreconstructed and the PSM monitoring histograms unbooked |

### Conditions

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `CONDITIONS_DIR` | `NL_CONDITIONS_DIR`, else `/simulation/reco_testbeam/conditions` | Every bare container name resolves against it. Wrong and `check()` prints one "conditions container does not exist" line per file, naming the absolute path |
| `PG_CONNECTIONS` | `[]` | libpq conninfo strings for the campaign database; `NL_PG` (`os.pathsep`-separated) wins. **Password in `PGPASSWORD`, never in the string** — the service stamps the conninfo into every output file |
| `ODB_SPECS` | `odb/bt2026_runinfo.json`, `odb/bt2026_wavedream_daq.json`, `odb/bt2026_wavedream_scalers.json`, `odb/bt2026_isel.json` | Map subtrees of the begin-of-run ODB dump to conditions tables. Drop one and the DAQ settings the run was actually taken with reach neither the algorithms nor the provenance header |
| `ODB_PRELOAD` | `runinfo`, `wd_board_settings`, `wd_channel_settings`, `wd_scaler_names` | Resolved at `initialize()` rather than lazily, so a misconfigured job dies in the first second naming the missing ODB path instead of at the first event that needs it |
| `ODB_OVERRIDES` | `""` | Run-indexed corrections applied to a private copy of the ODB tree before any table is mapped, for a setting that was recorded wrong. `""` uses the ODB exactly as recorded |
| `SETTINGS_SUMMARY` | `True` | Writes the resolved ODB tables into the output as a `WDSettingsHeader`. Off and the file no longer records the DAQ configuration it was taken with. Costs one algorithm and no per-event time |

### WaveDREAM

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `WD_CHANNELS` | `list(range(16))` | Board-local ids. A channel left out has **no** leading edge and **no** charge anywhere downstream, silently. All 16 because from run 175 the board transmits all of them (`DRSChannelTxEnable 0x3ffff`) and the logic copies are physics — π-stop and delayed-µ live there. A channel the board does not transmit costs nothing: no waveform arrives, so no feature is made. The price is that the channel-agnostic `ppamp` and `le_time` now mix ~10 mV counter pulses with ~800 mV logic levels and **become bimodal**; `ppamp_vs_channel` is per channel and reads better for it |
| `WD_CAL_CHANNELS` | `[0, 1, 2, 3, 4]` | Must be a subset of `WD_CHANNELS` and covered by both calibration tables; `PIWDCalibrator` fails at `initialize()` naming the first channel it cannot cover |
| `WD_BASELINE_SAMPLES` | `50` | Too few and the baseline is noisy; too many and a pulse arriving early is averaged into it, biasing every charge low |
| `WD_CF_FRACTION` | `0.5` | Constant fraction of the pulse extremum defining the leading-edge time |
| `WD_INTEGRATE_PRE_NS` / `WD_INTEGRATE_POST_NS` | `5.0` / `40.0` | Charge window `(edge - PRE, edge + POST)` in ns. `POST` must cover the full fall time of the slowest tube or the energy scale drifts with pulse shape |
| `WD_AMP_MAX` / `WD_TIME_MAX` | `1.0` / `1100.0` | Monitoring axis upper edges, V and ns. `WD_AMP_MAX` bounds the **peak-to-peak** excursion (and doubles as the upper edge of `rf_amplitude`); set it just above the ADC ceiling so saturation is visible instead of piled into the overflow bin. The per-pulse amplitude is a different, smaller quantity with its own axis, `WD_PULSE_AMP_MAX` |
| `WD_ROLE_TABLE` / `WD_ROLE_TAG` | `"wd_channel_map"` / `""` | What each channel is cabled to (`scint`, `nim`, `rf`, `current`, `spare`), which is what decides who gets per-channel histograms and who is counted as a counter. `""` books **only** the channel-agnostic histograms — no `fired_vs_channel`, no `coincidence`, no per-channel plots — and the algorithm says so at `initialize()`. A role string this build does not know is fatal, rather than silently dropping a channel out of the monitoring |
| `WD_CHANNEL_SETTINGS_TABLE` | `"wd_channel_settings"` | Per-channel discriminator levels, from the begin-of-run ODB dump: signed volts, negative on bt2026 because the pulses are negative-going. The sign also picks which extremum the firing test looks at. `""` puts every channel on the per-role fallback below, so `fired_vs_channel` and `coincidence` stop meaning "above the level the hardware actually used" |
| `WD_SCINT_THR_FALLBACK_V` / `WD_NIM_THR_FALLBACK_V` | `0.024` / `0.1` | Absolute amplitude in V counting as a pulse on a channel whose recorded level is `0`, i.e. one that was never configured. A scintillator pulse and a NIM level are an order of magnitude apart, so **one fallback for both would be wrong for one of them**: too low and every noise excursion fires, too high and a real counter never does |
| `WD_STRICT_THRESHOLDS` | `False` | `TriggerLevel` is referenced after `TriggerGain` while the recorded amplitude comes through `FrontendGain`, and nothing in the ODB maps the integer gain code to a linear factor. bt2026 runs both at unity, so this stays off and the job only warns; `True` refuses to run instead, which is what you want the day the gains are no longer unity |
| `WD_CHARGE_MIN` / `WD_CHARGE_MAX` | `-0.5` / `3.0` | Charge axis in V ns for `charge_vs_amp_chNN` and `charge_vs_rfphase_chNN`. Measured on run00172: p99 = 0.58, max = 2.57 over 99460 pulses, so `3.0` is full scale with headroom. The minimum is **below zero on purpose** — a window with no pulse integrates to a small negative number — and raising it to 0 buries the pedestal in the underflow bin |
| `WD_PULSE_AMP_MAX` | `0.3` | Upper edge in V of the absolute-amplitude axis, which is **not** `WD_AMP_MAX`: the amplitude is the extremum relative to the baseline and is several times smaller than the peak-to-peak excursion, so sharing one axis wastes most of it. Per-channel p99 on run00172 runs 0.046 to 0.125 with a 0.67 maximum, so `0.3` keeps the MIP peak in the middle |
| `WD_BASELINE_MIN_V` / `WD_BASELINE_MAX_V` | `-0.5` / `0.1` | Baseline axis in V for `baseline_vs_channel`. It reaches −0.5 on purpose: run00172 has baselines down to −0.46, which is a pulse landing inside the baseline window, and seeing that tail is the whole point of the plot. Tighten it and the pathology becomes underflow |
| `WD_BASELINE_RMS_MAX_V` | `0.02` | Upper edge in V of `baseline_rms_vs_channel`. The noise floor on run00172 is ~0.0015 V, so `0.02` resolves it in the low bins and still leaves the tail — a pulse in the baseline window, 0.7 % of traces there — inside the axis |
| `WD_PHASE_BINS` | `48` | Bin count for `PIWDRFPhase`'s `rf_phase`, `rf_amplitude` and `rf_residual`, and for the RF-phase axis of `charge_vs_rfphase_chNN`. 48 matches the nearline site's `energy_vs_rfphase`, so the histogram and the derived cube can be compared bin for bin; change it and that comparison needs a rebin |
| `WD_RF_RESIDUAL_MAX` | `0.05` | Upper edge in V of the `rf_residual` histogram — **an axis, not a cut**: `PIWDRFPhase` books the histogram with it and rejects nothing on it, so no pulse, phase or hit is lost whatever it is set to. `rf_residual` is the one plot that says whether the fitted sine actually describes the trace: the residual RMS sits near the noise floor when the frequency in `wd_rf` is right and grows quickly when it is not, so the axis is tight on purpose. Widen it and a bad fit stops standing out; narrow it and the interesting tail is all overflow |
| `WD_CONDITIONS_FILES` | `bt2026_wavedream_timebase.json`, `bt2026_wavedream_calibration.json` | Supply `wd_timebase` and the three calibration tables. Without the timebase the analysis falls back to a uniform nominal cell width and every time is wrong at the 1–2 % level. Two files defining the same table name is a hard error, so **replace a file, never stack one** |
| `WD_RF_TABLE` | `"wd_rf"` | **Required whenever `WD_ENABLED`.** It names the RF channel and the fixed per-run frequency; `PIWDRFPhase` fatals at `initialize()` if it does not resolve, and `PIWDWaveformAnalysis` consumes `/Event/wd_rf_phase`, so `""` is **rejected by `check()`** and no longer a way to drop the algorithm |
| `WD_ALIGN_TABLE` / `WD_ECAL_TABLE` | `"wd_time_alignment"` / `"wd_energy_calibration"` | Either one `""` drops `PIWDCalibrator` and `/Event/wd_hits`. Set both or clear both |
| `WD_TAG` | `""` | Pins one conditions tag for all three tables. A tag is a **version** of a table, not a time period. Leave `""` during a beam period unless you are deliberately reprocessing with old constants |
| `WD_RF_REFINE` | `False` | Re-scans the RF frequency per event instead of trusting the `wd_rf` constant. **Off in production**: the point of the table is that the frequency is a known, provenanced per-run number. Turn it on to *derive* that number for a new run — the fitted value lands in `_Event_wd_rf_phase.frequency_hz` — or to diagnose drift. It costs `2 × WD_RF_REFINE_POINTS` extra fits per event |
| `WD_RF_REFINE_POINTS` / `WD_RF_REFINE_SPAN` | `41` / `0.01` | Grid points per scan stage, and the half-width of the coarse scan as a fraction of the nominal frequency. Both are inert with `WD_RF_REFINE = False`. Fewer than 2 points is rejected by `check()` (the grid step would be 0/0); a span far wider than the real drift just wastes the coarse grid, a span narrower than it pins the scan to the edge |

### WaveDREAM scalers

`PIWDScalerMonitor` histograms the scaler readout. The scaler frontend writes
one reading per board every 5 s in events of their own that carry no
waveforms, so `WDAnalysisSeq` never sees them; the monitor runs in its own
`WDScalerSeq`, gated on `/Event/wd_scalers`. Scaler indices 0–15 are the
analogue inputs, 16 the pattern trigger, 17 the external trigger, 18 the
external clock; their names are in `wd_scaler_names`, recorded in the
`WDSettingsHeader`.

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `WD_SCALER_MONITOR` | `True` | Off drops the module. Requires `WD_ENABLED`: only `PITMidasWaveDream` decodes the scaler banks into `/Event/wd_scalers` |
| `WD_SCALER_BOARDS` | `[36]` | Board serials that get the per-board histograms. A serial read out but not listed lands only in `readings` and is named in the end-of-job warning; a listed serial with no readings leaves its histograms empty and is warned about too |
| `WD_SCALER_TIME_BIN_S` / `WD_SCALER_TIME_MAX_S` | `5.0` / `7200.0` | Bin width and upper edge in s of the board-time axis (seconds since the board was configured). The bin equals the readout period, one reading per bin. A run past the upper edge piles into the overflow bin; `finalize()` counts those readings and says to raise the edge |
| `WD_SCALER_FILL_STALE` | `False` | Readings the frontend flagged stale are counted and skipped; `True` fills them as well |

### PSM decode

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_RF_CHANNEL` | `6` | MuTrig **raw** readout channel (`chipid*32 + channel`, consumed before the map lookup) carrying the accelerator RF gated by S1. `None` means no `/Event/rf` at all. Follows the SMA board cabling documented in the open interval of `mutrig_channel_map`; it is a job flag, not a conditions interval, so a file from an earlier cabling reprocessed with this job needs an override |
| `PSM_CURRENT_CHANNEL` | `7` | Same raw-id convention, the proton-current pulse. It is what books `histograms/musip/current`, and **without it `combine_files.py` cannot merge sub-runs**. Same cabling caveat as `PSM_RF_CHANNEL` |
| `PSM_QUAD_PIXEL_PITCH` | `0.08` | MuPix pitch in mm; the local hit position is `(col + 0.5) * pitch`, so a wrong pitch scales every position and every slope |
| `PSM_SMA_COARSE_SHIFT` | `None` | The SMA word's coarse field is the time in ns shifted right by this, and it has differed between run ranges (3, i.e. 8 ns ticks, then 15, then 14). `None` takes the run's value from the `sma_coarse_shift` table in `bt2026_psm_readout_map.json`; a run no interval covers stops the job at `initialize()` rather than guessing. An integer here overrides the table, for a run whose shift has been measured (psm-analysis `sma-tot-vs-wd/mupix_phase.py RUN --time-check`) but not yet entered. Wrong, and every counter hit and RF pulse lands at a time no MuPix hit shares: the tracklets lose their L pairs while the SMA monitor's RF plots still look fine. Outside 0-18 is rejected by `check()` |
| `PSM_SMA_DIAGNOSTICS` | `True` | Books the decoder's raw-word SMA diagnostics under `histograms/musip/sma_*` (the list is in the table above). Off, they are simply absent; the decoder's own default is off so that other jobs using it do not grow them. Read them as counts: the live fraction is `sma_live_time` bin 1 / (bin 1 + bin 2), and for a subrun run the gaps *between* subruns are in no file, so the merged value is slightly high. Below coarse shift 12 the SMA time wraps (2.1 s at shift 3), so a frame span or gap longer than about 1.07 s folds back and is not caught by the decoder's `smaDiagMaxMs` cut. `sma_fine_coarse_diff` is coarse minus fine over the shared bits in ns: the latch offset of the two fields sits near 0 (up to ~16 us at shift 3), a flipped fine bit b at ±2^b ns; `sma_fine_vs_coarse` calls a word a mismatch beyond ±20 us (`smaDiagLatchToleranceNs`) and names the fine bits that differ |
| `PSM_QUAD_TIME_BIN_NS` | `8.0` | Hardware fact — MuPix counts in 8 ns. Change it only if the DAQ clock changes. There is no MuTrig counterpart: since the trigger encoding, `PITMidasMusip` reports that time in ns directly and `trigTimeBinWidth` is gone |

### PSM geometry

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_GEOMETRY_BASE` | `"GEOCOND:psm_geometry"` | The layer `PIGeometrySvc` builds the `GeoHeader` from. Empty and the decoder throws on hit one; anything but a `GEOCOND:<table>` form is rejected, because that is the only form this job takes |
| `PSM_GEOMETRY_MAPS` | `["MUPIX:mupix_chip_map", "MUTRIG:mutrig_channel_map"]` | Raw-readout-id → detector-id maps. The `NAME` side must match the decoder's `muPixMap`/`muTrigMap` defaults; without them the decoder cannot turn a chip id or `chipid*32+channel` into a detector id and throws naming the raw id on the first hit |
| `PSM_GEOMETRY_TRANS` | `["COND:isel"]` | Adds the XY-stage translation read from `/Equipment/XYTable`. A run whose ODB has no XYTable equipment fails at `initialize()` with "source absent" rather than silently using a stale stage position; set `[]` and `PSM_WEIGHT_STRATEGY = 0` to process one (the acceptance weights need the stage position) |
| `PSM_GEOMETRY_FILES` | `bt2026_psm_geometry.json`, `bt2026_psm_readout_map.json` | Supply the base table and the two map tables. Empty with a `GEOCOND` base is a hard indexing error at startup |

### MuPix monitor

The low-level MuPix check, and the only part of the job that reads
`/Event/muquad` **alone**: no scintillator hits, no channel map, no tracklets.
That is the point of it — it still says what the pixel planes are doing when
the parts it is checking are what is broken, and a source or cosmic run that
makes no scintillator coincidence at all still produces its plots.

Everything it draws is decided from `PIGeometrySvc` at `initialize()`: which
channels are MuPix, which plane each is on, each chip's footprint, and the
L1 → L2 lever arm. There is no plane or distance setting here for that reason —
`L1Plane`/`L2Plane` default to the two MuPix planes of lowest z, in that order,
and `DistanceL12` to their measured separation, so neither can drift away from
the geometry the hits were placed with. The algorithm logs all of it, including
the chip index that `hits_per_chip` and `tot_vs_chip` run over.

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_MUPIX_MONITOR` | `True` | Off drops the whole module. Requires `PSM_DECODE`: only the musip decoding tool produces `/Event/muquad`, and the monitor needs the `PIGeometrySvc` that `PSM_DECODE` creates |
| `PSM_MUPIX_WINDOW_NS` | `40.0` | Symmetric L1/L2 half-window in ns. Loose on purpose — the MuPix stamp is an 8 ns count and the two planes are different chips, so a window at the resolution of the clock throws real pairs away. Set it from the run's own `histograms/PIPSMMuPixMonitor/dt`, which is scanned over the wider `PSM_MUPIX_DT_RANGE_NS` for exactly that |
| `PSM_MUPIX_PIXELS_PER_BIN` | `1` | Pixels per bin of every hit map. 1 is one bin per pixel — 256 x 250 per chip and 512 x 500 per plane on bt2026, about 4 MB of histogram, and the granularity at which a dead column or a hot pixel is visible. `n` divides the bin count of every map by `n²` |
| `PSM_MUPIX_DT_RANGE_NS` / `PSM_MUPIX_DT_BINS` | `204.0` / `51` | Half-width and bins of the `dt` histogram. 204 over 51 bins puts each 8 ns MuPix tick at a bin **centre**; a round 200 puts it on a bin edge, where ROOT's edge convention splits the coincidence peak across two bins |
| `PSM_MUPIX_SLOPE_RANGE_MRAD` | `0.0` | Half-width in mrad of this module's own x'/y' axes. `0` derives the full geometric acceptance of the two planes at the lever arm (±1365 / ±1333 mrad on bt2026), so nothing a pair can produce reaches an overflow bin. Set it to `PSM_PHASE_SPACE_SLOPE_RANGE_MRAD` to read these next to `PIPSMAllTrackReco`'s phase space instead, and expect the tails outside that window to pile up |
| `PSM_MUPIX_EXPANDED_RANGE_MM` | `41.6` | Half-width in mm of the fixed x/y axes of `track_xy_expanded`, `xxp_central` and `yyp_central` (260 bins, 0.32 mm = 4 pixels). It covers the standard five-point scan at +-17 mm (`PSM_POSITIONS_MM`) and the +-20 mm 3x3 grid, plus the 20.48 mm half-width of a plane (40.48 mm), rounded up to a whole number of 0.32 mm bins. The monitor shifts the axis by a quarter pixel (0.02 mm), so a half-pixel stage offset such as 17 mm puts no pixel centre on a bin edge. It is fixed rather than taken from the plane footprint so that every run of a stage scan books the same axes and the runs merge bin by bin. Too small, and tracks go to the overflow bins; `initialize()` warns when the L1 footprint is not inside it |
| `PSM_MUPIX_CENTRAL_SLOPE_MRAD` | `100.0` | Rough half-width in mrad of the x'/y' axes of `xxp_central` and `yyp_central`. A slope from two pixel planes only takes whole multiples of one pixel over the lever arm (2.67 mrad on bt2026), so the monitor books one bin per step, each step at a bin **centre**, and rounds this up to a whole number of steps: 100 gives 77 bins over ±102.67 mrad. A fixed bin width would show a comb of alternately full and empty bins instead |
| `PSM_MUPIX_ALL_PAIRS` | `0` | Pairs every L2 hit inside the window instead of only the one nearest in time. Each extra pair is a combinatorial ghost carrying a slope no particle had, so this is a diagnostic for a busy run, not a production setting. `npartners` reports the ambiguity either way |
| `PSM_TIMEWALK` | `True` | The MuPix timewalk against the scintillators, in two samples (see "MuPix timewalk" below). Here it sets the monitor's `CounterInput` to `/Event/mutrig`, which is read as an **optional** input: `PSMMuPixSeq` stays gated on `/Event/muquad` alone, and a frame without SMA hits skips only the timewalk fills (the count is logged at finalize). It also sets the monitor's `ConditionsTable` to the PSM channel map, from which it takes the counters S1-S5 (the channel-map file is then loaded even with `PSM_RECO` off), and turns on `PIPSMAllTrackReco`'s track-only histograms (`Timewalk`, off by default in the algorithm). Off, neither set is booked |

**MuPix timewalk.** Per plane `L1`/`L2` and counter S1-S5, with dt =
t(pixel) − t(Sn) on 300 bins of 2 ns over [−150, 450) and the pixel ToT in
units of 256 ns. The counters are the S1-S5 channels of the PSM channel map
(`PSM_CHANNEL_MAP_TABLE`), which both algorithms read, so the detector ids are
written down in one place (`<vid>` = 2001, 2003, 2004, 2005, 2006 in the bt2026
map; 2002 is the Degrader id):

* **all pairs**, `PIPSMMuPixMonitor/tw_*`: every pixel hit against every Sn hit
  of the same readout frame with dt in [−150, 450) fills `tw_dt_vs_tot_L<n>_<vid>`
  (dt vs pixel ToT) and `tw_dt_vs_stot_L<n>_<vid>` (dt vs Sn ToT, 0-63; higher
  SMA codes go to the overflow); the pairs with dt in the prompt window
  [−100, 450) (`PromptWindow`) fill `tw_tot_vs_stot_L<n>_<vid>` (pixel ToT vs Sn
  ToT). The window holds the walk tail of the lowest ToTs, to about +400 ns.
* **track only**, `PIPSMAllTrackReco/tw_*`: for every scintillator cluster
  holding an S1 hit whose L window has exactly one pixel cluster per plane,
  every pixel of the two clusters against the Sn hit nearest the cluster's
  S1 time within ±50 ns (`TimewalkPartnerNs`), searched in all SMA hits of the
  frame (not only the 2 ns S-S cluster): `tw_dt_vs_tot_L<n>_<vid>` (dt inside
  [−150, 450)) and `tw_tot_vs_stot_L<n>_<vid>` (every such pixel).
  `tw_cluster_size_L<n>` is the size of those clusters; `tw_seeds` counts the
  S1-holding seeds by outcome (track, ambiguous, no L1, no L2), then the
  clusters dropped as crosstalk ghosts per plane, then the seeds whose L1 or
  L2 window held more than `lClusterMaxHits` (64) hits and was called
  ambiguous without being clustered. An Sn ToT beyond 63 fills the overflow of
  `tw_tot_vs_stot` too.

The reference for both definitions is the Python prototype in
`psm-analysis-josh-2026/mupix-timewalk/` (`timewalk_lib.py`), whose all-pairs
arrays these histograms reproduce bin for bin inside the axes. The prototype
drops the Sn ToTs above 63 where these fill the overflow, so the entry counts
differ by the overflow; see the prototype's README for the comparison.

`PixelPitch` is passed `PSM_QUAD_PIXEL_PITCH`, the same number the decoder
placed the hits with: binned at any other pitch the maps stop being one bin per
pixel.

**The `x'` convention is `PIPSMSimpleTrackReco`'s.** Both compute
`1000 (x2 − x1) / distance` in mrad, so `PIPSMMuPixMonitor/xxp` and
`PIPSMAllTrackReco/xxp` can be laid on top of each other — with the caveat that
one is a scintillator-defined particle and the other is any L1/L2 time
coincidence, which is the comparison worth making.

**One WARNING from it is a data diagnostic.** At `finalize()` it reports, per
chip, hits whose position lies outside that chip's own footprint — a pixel word
carrying a column or a row the sensor does not have. Each one lands in an
overflow bin of that chip's map, but on the **plane** map it is drawn at a
position belonging to a neighbouring chip, so the plane map has to be read
against that number. Run 166 emits it for `L1 10012` on a few percent of its
hits.

### SMA monitor

The counter-side twin of the MuPix monitor, and the only other part of the job
that reads one decoded collection **alone** — `/Event/mutrig`, no pixel hits, no
data-side channel map, no tracklets. A run whose tracklet reco reconstructs
nothing still tells you which counters took hits and what their ToT looked like.

The counter axis is not configured here: `initialize()` asks `PIGeometrySvc` for
the raw `MUTRIG` map, takes the distinct detector ids out of it and logs the
index it built, raw channels and all. `ParkedVid` is set to `2002`, the Degrader
id the map parks every uncabled channel on; that index carries the idle FEB's
own words and is therefore excluded from the ToT judgements below. Change the
parked id in the map and this number has to follow it.

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_SMA_MONITOR` | `True` | Off drops the whole module. Requires `PSM_DECODE`: only the musip decoding tool produces `/Event/mutrig`, and the monitor takes the raw `MUTRIG` map from the `PIGeometrySvc` that `PSM_DECODE` creates |
| `PSM_SMA_HITS_PER_EVENT_MAX` | `20000` | Top of the per-counter hits-per-event axis; everything above it lands in the last bin. 20,000 is the H000 bank cap, so nothing a frame can hold is clipped; a busy counter exceeds a few hundred hits per frame |
| `PSM_SMA_DEGENERATE_TOT_SHARE` | `0.95` | Share of a cabled counter's hits at its commonest ToT value above which `finalize()` calls it degenerate. One value repeated is a pulser or a stuck field, not a spectrum. Lower it and a genuinely narrow spectrum starts warning |
| `PSM_SMA_MARKER_TOT_SHARE` | `0.5` | Share at ToT 0 or 255 above which a cabled counter is called marker-dominated. Those two values are the idle FEB's own words, so a counter made mostly of them is not seeing its TOT box |

Both shares are judgements about **cabled** counters only. The parked index is
expected to be all 0 and 255 and is never reported for it.

**RF phase.** When `PSM_RF_CHANNEL` is set, the job also hands the monitor
`/Event/rf` (`RFInput`). The SMA sees the accelerator RF only through S1's gate:
after each S1 hit a burst of three or four RF pulses about 19.75 ns apart comes
through. Every S1 hit opens a 125 ns gate (`RFGateNs`); the burst ends about
115 ns after S1. A gate that holds another S1 hit is **vetoed**:
the second hit's own burst can land in it, still pass the pulse count and give
the first hit the wrong phase. Two S1 hits at the same time do not veto each
other. Among the gates that are not vetoed, the monitor's default rule,
`RFPhaseRule = "last"`, accepts a gate holding 2 to 4 pulses and takes the
phase from the **last** pulse in it. The first pulse of a gate is the gate
opening itself, a fixed ~47 ns after S1, and when an RF edge falls on it the two
merge into one pulse. The burst length therefore depends on the phase, while
the last pulse is always a real RF edge. The musip DQM's rule is kept as
`RFPhaseRule = "dqm"`: exactly four pulses, phase from the third. It keeps only
the phase region where the burst has four pulses, under a fifth of the S1 hits.
Under either rule the period is the gap between the last two pulses. Every
other cabled counter's hit takes the phase of the nearest valid S1 gate within
50 ns, from the same RF pulse, measured from its own time. Out come `rf_period`,
`rf_pulses_per_gate` and `rf_offset_vs_pulse` (the burst structure, over the
gates that were not vetoed), `rf_phase` for S1, `rf_phase_vs_tot_<vid>` per
cabled counter, `rf_veto_gap` (time from each vetoed S1 hit to the S1 hit inside
its gate) and `rf_counters` (frames without an RF object, pulses, S1 gates,
valid gates, other hits, paired hits, vetoed gates). `finalize()` prints the
vetoed share next to the valid one.
`rf_period` has read about 19.6 ns under `last` and 20.4 ns under `dqm` against
the RF's 19.75 ns, so it describes the SMA time stamp and is not a frequency
measurement. The rule and its parameters are the algorithm's `RF*` properties,
left at their defaults here. `/Event/rf` exists only in frames where the decoder
saw an RF pulse, which the monitor treats as an empty pulse list, and when the
run's map does not cable S1 the monitor says so at `initialize()` and books no
RF histograms. With `PSM_RF_CHANNEL = None` none of this runs and the monitor is
unchanged.

### PSM reco

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_CHANNEL_MAP_FILE` | `"bt2026_psm_channel_map.json"` | Container holding the data-side channel map the tracklet reco reads |
| `PSM_CHANNEL_MAP_TABLE` / `PSM_CHANNEL_MAP_TAG` | `"psm_channel_map"` / `""` | The table supplies the **whole** map, which is why no per-channel job option is set here; one that were set would override the table |
| `PSM_SEED_ON` | `-1` | Negative is unseeded: the earliest scintillator hit not already absorbed starts a new tracklet, so an isolated delayed pulse (the muon from a stopped pion) forms its own tracklet instead of being lost |
| `PSM_REQUIRE_L_HITS` | `0` | Requiring exactly one L1 and one L2 pixel cluster discards every delayed tracklet, because delayed pulses have no tracker hits |
| `PSM_SEED_ON_L` | `0` | Source runs only: seed tracklets on L1 tracker hits and pair each with the nearest L2 hit inside `PSM_LPAIR_WINDOW_NS`. A source on the tracker makes L1/L2 coincidences with no scintillator involved, and both scintillator-seeded modes attach L hits only to a scintillator cluster, so they reconstruct nothing from such a run. On in beam running it throws away the scintillator seed that defines a particle |
| `PSM_LPAIR_WINDOW_NS` | `40.0` | L1 to L2 half-window in ns for that mode. The two-plane correlation from a source is much broader than the tracker time resolution, so this is generous on purpose; inert while `PSM_SEED_ON_L` is `0` |
| `PSM_L_WINDOW_BEFORE_NS` / `PSM_L_WINDOW_AFTER_NS` | `100.0` / `160.0` | A scintillator cluster at `t` takes its L1/L2 hits from `[t - before, t + after)` (`thrMupix` / `thrMupixUpper`). Measured with the SMA and MuPix times on one base, t(MuPix) − t(S1) has a sharp edge at −90 ns, peaks at −52 ns and has a timewalk tail to about +150 ns, so the window opens just before the edge and closes past the tail. Too narrow and prompt tracklets lose their L pair; too wide and more of them see a second hit on one plane and are flagged ambiguous. The S-S clustering window (`thrScint`, 2 ns) is separate and untouched. An empty window is rejected by `check()` |
| `PSM_L_CLUSTER_DIST_MM` | `0.12` | The L hits of each plane inside the L window are clustered by distance (`lClusterDistMm`): two hits at most this far apart in mm, global x/y, are linked (single linkage, 1e-6 mm slack, no time condition beyond the window), and exactly one cluster per plane makes the L pair, placed at the mean of the cluster's pixel centres; more than one cluster on a plane flags the tracklet `lAmbiguous`. 0.12 takes the eight touching pixels at the 0.08 mm pitch (edge 0.08, corner 0.113 mm), also across a chip boundary, and nothing further. `0` switches the clustering off: then a second hit of a plane, even the neighbouring pixel of the same particle, makes the tracklet ambiguous, which is how the reco worked before. A plane with more than 64 hits in the window (the algorithm's `lClusterMaxHits`) is ambiguous without being clustered, which bounds the pairwise work |
| `PSM_DROP_CROSSTALK_GHOSTS` | `False` | Before the one-cluster-per-plane test, drop MuPix crosstalk ghosts (`dropCrosstalkGhosts`): a cluster whose largest ToT is at most 3 and that has a pixel of higher ToT of another cluster of the window on the same chip, at most one column and 40-43, 81-85 or 122-127 rows away. Most of the ambiguity left after the clustering is this. Needs the `PIGeometrySvc` of `PSM_DECODE`, from which each hit's column and row are recovered; `check()` refuses it without. Off until decided |
| `PSM_AGGREGATE` | `1` | Fills the phase-space histograms inside the algorithm while the data is in memory. This is what makes the job a monitoring job rather than a converter |
| `PSM_AGGREGATE_PROMPT_ONLY` | `1` | Restricts that filling to prompt-like tracklets: an unambiguous L1/L2 pair **and** at least one prompt-channel hit, the prompt channel being whatever `PromptChannel = -1` in the channel map resolves to (S1 on bt2026). Under a seeded configuration the seed already guarantees both and this changes nothing. In the unseeded mode this job runs (`PSM_SEED_ON = -1`) it is what keeps `PIPSMAllTrackReco`'s TH3s meaning "prompt tracks": off, an isolated delayed pulse forms its own tracklet with no L pair, its position is a sentinel, and it piles into the overflow bins of every phase-space plot |
| `PSM_DISTANCE_L12` | `30.0` | L1 → L2 lever arm in mm, used to turn `x2 − x1` into a slope. Must match the telescope as built or every angle is scaled wrong. Source of truth: `beamline-simulation/psm/psm_scan_config.py` `DIST_L12_MM` |
| `PSM_DELAYED_WINDOW_NS` | `(20.0, 70.0)` | The π → µ tag window in ns (τ = 26 ns), `[MIN, MAX)` |
| `PSM_REQUIRE_SEED_HIT` | `1` | Requires the **prompt** half of a coincidence to have a prompt-channel hit of its own. Off, any tracklet inside the window can play the prompt role — including, in the unseeded mode, a delayed pulse that formed its own tracklet — and `counters`/`class` then count pairs no particle made. The delayed half is selected by `PSM_DELAYED_WINDOW_NS` and `PSM_S5_THR`, not by this |
| `PSM_LAYER_THR` / `PSM_S5_THR` | `0.2` / `0.2` | Stopping-layer and through-going thresholds. MeV in simulation, but **raw MuTrig ToT on data** until a ToT-to-MeV calibration exists, so both need retuning the first time real hits arrive |
| `PSM_POSITIONS_MM` | `(0,0), (17,17), (-17,17), (-17,-17), (17,-17)` | Telescope stage positions `(dx, dy)` in mm for the acceptance weighting — XY-stage coordinates, the same numbers as `/Equipment/XYTable`. The algorithms apply isel's `(-x, y)` translation themselves, so these are not pre-negated. Source of truth: `psm_scan_config.py` `POSITIONS_MM` |
| `PSM_WEIGHT_MARGIN_MM` | `2.0` | Fiducial erosion in mm applied to each stage position's L1/L2 plane footprint before the containment test, so a track just inside or outside a plane edge is not double-counted or lost between neighbouring scan positions |
| `PSM_WEIGHT_STRATEGY` | `1` | `0`: every tracklet gets weight 1. `1`: weight `0` unless the track is inside this run's own window at both L1 and L2, else `1/N`, `N` the number of `PSM_POSITIONS_MM` windows containing the track at both L1 and L2, each window being the conditions footprint (`PIGeometrySvc`) of the L1/L2 plane moved from this run's own stage position to that config position and eroded by `PSM_WEIGHT_MARGIN_MM`. Over the runs of a scan the weights a trajectory would receive then sum to 1 wherever at least one run can see it; a run at none of the positions (within 0.01 mm) counts its own window as one more and warns that its weights will not sum to 1 with the scan. `2`: also require containment at the track's stop-layer depth (`PIPSMAllTrackReco` only — the MuPix monitor has no scintillators to define a stop layer and is capped at `min(strategy, 1)`). Strategies `1` and `2` need `PSM_GEOMETRY_TRANS` to include `"COND:isel"`, or every run is treated as sitting at the design position; `check()` flags both requirements |
| `PSM_PHASE_SPACE_BINS` | `320` | Bins per axis of the tagged `xy`/`xxp`/`yyp` TH2Ds. Must be a positive multiple of 64 or `check()` refuses to start: 320 = 5 x 64, so the histogram rebins onto the minitwin's 64-bin maps without splitting a bin. Source of truth: `beamline-simulation/psm/psm_scan_config.py` `NBINS_2D` |
| `PSM_PHASE_SPACE_POS_RANGE_MM` | `37.0` | Half-width of the x/y axis in mm, applied to both PSM algorithms. This is the minitwin det10 window (`psm_scan_config.py` `X_WINDOW`), not a display choice — move it and the histograms stop being model input. The algorithm's own default, 2.5, is a single-position zoom |
| `PSM_PHASE_SPACE_SLOPE_RANGE_MRAD` | `950.0` | Half-width of the x'/y' axis in **mrad** (`psm_scan_config.py` `A_WINDOW`), likewise on both algorithms. The 1D `xp`/`yp` spectra keep their own narrower `SlopeRange`: they are the shift zoom, not model input |

**S1 RF phase of a tracklet.** With `PSM_RF_CHANNEL` set, `PIPSMAllTrackReco`
also reads `/Event/rf` (`RFInput`) and gives every tracklet holding an S1 hit the
RF phase of its earliest S1 hit, in the new `s1rfphase` column of
`/Event/exp_all_tracks` (NaN when the tracklet has no S1 hit, the gate is not
valid, or another S1 hit of the frame lies inside it). The rule is the SMA
monitor's, with the same `RF*` property names and defaults (the last pulse of a
2-4 pulse gate in the 125 ns after S1, with the same S1 veto), so the two
phases agree. The prompt tracklets that fill `xy` and have a valid phase also
fill `xy_vs_s1phase`, a TH3F of (x at L1, y at L1, S1 RF phase) on `xy`'s 64-bin
x/y grid and one bin per ns over the (t, t + RFGateNs] gate, 0.5 to 125.5 ns by
default. The phase axis follows `RFGateNs` unless `nbinsS1Phase` is set non-zero,
in which case `s1PhaseMin`/`s1PhaseMax` apply. The SMA time is a whole number of
ns, so every whole-ns window is a whole number of bins, and bin n holds phase n
ns. A MuPix map for any phase window is a projection of it; for [90, 105) ns,
`h.GetZaxis().SetRange(90, 104); h.Project3D("yx")`.

### Output

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `WRITE_NTUPLE` | `True` | Off is a pure monitoring pass; the histogram file is unaffected. See "Output size" |
| `NTUPLE_RULES` | `[]` | Ordered `keep <glob>` / `drop <glob>` rules over TES paths, later rules winning. A path no rule matches is **kept**, so empty persists everything and a newly registered collection is never lost by omission |

## Conditions

Several sources are loaded at once and **they never merge**: whichever layer owns
a table serves all of it.

| precedence | layer | `PIConditionsSvc` property | what it holds |
|---|---|---|---|
| highest | JSON containers | `JsonFiles` | the shipped `bt2026_*.json` under `CONDITIONS_DIR`, plus any local override |
| middle | PostgreSQL | `PgConnections` | the campaign database (`PG_CONNECTIONS` / `NL_PG`) |
| lowest | ODB | `OdbTables` | subtrees of the begin-of-run ODB dump, mapped by the spec files in `ODB_SPECS` |

`Preload` resolves `ODB_PRELOAD` (plus `wd_timebase` when WaveDREAM is on) at
`initialize()`, so a table nothing can supply fails the job in the first second.

| container | tables it supplies |
|---|---|
| `bt2026_wavedream_timebase.json` | `wd_timebase` |
| `bt2026_wavedream_calibration.json` | `wd_rf`, `wd_time_alignment`, `wd_energy_calibration`, `wd_channel_map` |
| `bt2026_psm_geometry.json` | `psm_geometry` |
| `bt2026_psm_readout_map.json` | `mupix_chip_map`, `mutrig_channel_map` |
| `bt2026_psm_channel_map.json` | `psm_channel_map` |
| `odb/bt2026_runinfo.json` | `runinfo` |
| `odb/bt2026_wavedream_daq.json` | `wd_board_settings`, `wd_channel_settings` |
| `odb/bt2026_wavedream_scalers.json` | `wd_scaler_names` |
| `odb/bt2026_isel.json` | `isel` — only read when `PSM_GEOMETRY_TRANS` contains `COND:isel` |

`WD_ROLE_TABLE` names `wd_channel_map`, the cabling table in
`bt2026_wavedream_calibration.json` (schema `wd_channel_map`, tag
`wd036-run114`, one interval per cabling change). The per-channel trigger levels
do **not** come with it: they are a DAQ *setting*, not a cabling *decision*, so
`WD_CHANNEL_SETTINGS_TABLE` reads `wd_channel_settings` from the ODB layer
(`odb/bt2026_wavedream_daq.json`), which is why that table is in `ODB_PRELOAD`.
Where a recorded level is `0` — a channel the DAQ never configured — the
algorithm falls back per role, to `WD_SCINT_THR_FALLBACK_V` on a scintillator
and `WD_NIM_THR_FALLBACK_V` on a NIM copy, and names every such channel once at
`initialize()`.

**PostgreSQL is forthcoming and will become the default conditions source.**
Today every constant comes from the JSON containers. When the campaign database
is served, `PG_CONNECTIONS` (or `NL_PG`) is set **once**, in the daemon's
environment, the JSON list shrinks to whatever is being overridden locally, and
precedence makes those files win over the database. The password goes in
`PGPASSWORD` and never in the conninfo, because the service stamps the conninfo
into every output file. The loader and inspection tools are in [`../conddb/`](../conddb/)
(`json2pg.py`, `condtool.py`; see its README).

**The run number is never configured.** `PIMidasSelector` publishes the
begin-of-run ODB dump as `"ODBHeader"` in `PIHeaderSvc` during its own
`initialize()`, and `PIConditionsSvc` reads `/Runinfo/Run number` out of that
header during **its** `initialize()` to pick intervals of validity. That is why
the `ExtSvc` order in the script is load-bearing: reorder it and every table
resolves for run 0.

`PromptChannel = -1` in `bt2026_psm_channel_map.json` is not a missing value:
a negative prompt channel means `PIPSMRecoCore` uses `S1Channel`.

## Running it

Start the analysis container and source the environment inside it:

```bash
cd <your testbeam-env checkout> && ./start-midas-container.sh
docker exec -it testbeam-midas bash
source /software/setup_container_env.sh
pushd /software/root/install && source bin/thisroot.sh && popd
source /simulation/docker/setenv.sh
```

### Processing a file

```bash
export PYTHONPATH=/workdir/beamtime2026_pie5/python:$PYTHONPATH
python -m pioneer.nearline.process /workdir/scratch/online/run00175.mid.lz4 \
  --out-dir /workdir/scratch/nearline
```

That is the daemon's flow without the daemon or the run database, and it is
what to reach for when a run has to be processed by hand. It renders
`nearline_job.py` for that one file and runs `gaudirun.py` on the result, so it
leaves the **same three artefacts the daemon leaves**, named after the part of
the file name before the first dot:

```
/workdir/scratch/nearline/run00175.py           the complete job that ran
/workdir/scratch/nearline/run00175.root         the RNTuple
/workdir/scratch/nearline/run00175_hists.root   the histograms
```

The histogram file is the full set the daemon writes, the MuPix timewalk
(`PIPSMMuPixMonitor/tw_*`, `PIPSMAllTrackReco/tw_*`, with `PSM_TIMEWALK` on) and
the clustered L pairs (`PSM_L_CLUSTER_DIST_MM`) included: this is the way to
remake them for one subrun file by hand.

`--evt-max N` truncates, `--render-only` writes the `.py` and stops so you can
edit it before running it, and `--job PATH` renders some other copy of the job
file. If `gaudirun.py` is not on `PATH` it exits 2 and prints the three
`source` lines above instead of a Gaudi import traceback.

**Reproducing a run is running its `.py`:** `gaudirun.py run00175.py`. The
rendered file names its own input, output, event limit, conditions directory
and database connections, so it ignores every `NL_*` variable and re-processes
the same file the same way whatever the shell around it says. That is true of a
daemon's rendered file and a `process` one alike — they come out of the same
renderer, and differ only in `rendered_at`, `rendered_by` and `job_id`. The two
host-dependent variables are the exception that proves it: `NL_CONDITIONS_DIR`
and `NL_PG` **of the shell that renders** are baked into the file at that
moment, exactly as the daemon bakes in its own, so set them before the
`process` command rather than before the `gaudirun.py` that re-runs it.

### A quick look, or a variant job

The environment form runs the checked-in file *unrendered*, which is the fast
way to look at something and the way to run a variant of the job. A WaveDREAM
quick look on the lab run, 2000 events:

```bash
NL_MIDAS=/workdir/midas_files/run00193.mid.lz4 NL_OUT=/tmp/run00193.root \
  NL_EVTMAX=2000 gaudirun.py /workdir/beamtime2026_pie5/python/pioneer/nearline/nearline_job.py
```

A joined file carries both systems, so the same command with
`fake_run00913_mutrig.mid` runs both halves. Here it runs the PSM half only,
with a setting changed through an overrides file rather than by editing the
shared script:

```bash
cat > /tmp/nl_overrides.py <<'EOF'
# reassigns settings over the block in nearline_job.py
WD_ENABLED = False
EOF
NL_MIDAS=/workdir/midas_files/fake_run00913_mutrig.mid NL_OUT=/tmp/run00913.root \
  NL_OVERRIDES=/tmp/nl_overrides.py \
  gaudirun.py /workdir/beamtime2026_pie5/python/pioneer/nearline/nearline_job.py
```

| variable | required | what it does |
|---|---|---|
| `NL_MIDAS` | yes | the input `.mid` / `.mid.lz4` |
| `NL_OUT` | yes | the RNTuple path; the histogram file is the same name with `.root` replaced by `_hists.root` |
| `NL_EVTMAX` | no | overrides `EVT_MAX` |
| `NL_PG` | no | `os.pathsep`-separated conninfo strings, overriding `PG_CONNECTIONS` |
| `NL_CONDITIONS_DIR` | no | overrides `CONDITIONS_DIR` |
| `NL_OVERRIDES` | no | a small Python file `exec`'d over the settings, for a variant job |

**Every variable in that table is read only by an unrendered job file.** A
rendered one ignores all six — including `NL_OVERRIDES`, which the renderer
does not fold in either — so an overrides file is an interactive mechanism and
nothing else. A variant that has to be reproducible is made by editing the
settings block, or by editing a rendered copy and running that.

**Anything you find yourself setting more than once belongs in the settings
block, not in the environment.** The environment is for the one-off; the block
is the record of how this experiment processes its data.

## Via the daemon

`GaudiJob.format_config_file()` calls `render_job()` on `nearline_job.py` and
writes the result into the **output** directory as `<filebase>.py`;
`build_command()` is then just `gaudirun.py <that file>`. The rendered file is
not a shim around the job — it *is* the job, every setting and every line of
assembly included, with the eleven placeholders filled:

| field | what the daemon puts there |
|---|---|
| `in_file`, `out_file` | absolute paths; `evt_max` is `-1`, so a stray `NL_EVTMAX` in the daemon's environment cannot truncate a run |
| `conditions_dir`, `pg` | `NL_CONDITIONS_DIR` and `NL_PG` **of the daemon's environment**, baked in at render time. Empty means the job's own defaults |
| `rendered_at`, `rendered_by` | UTC timestamp to the second, and `user@host` |
| `job_source`, `job_git` | the job file it was rendered from, and `git describe --always --dirty` of it — so a file says which version of the job made it, dirty tree included |
| `job_id`, `run_id` | the run database's own ids for this piece of work |

The last three rows are also printed by the banner at startup — `[nearline]
rendered <when> by <who> job=<id> run=<id>` and `[nearline] source <path> @
<describe>` — so a log says as much about its provenance as the file does.
Because the rendered file reads nothing from the environment, it stays valid
after the daemon is restarted, after the conditions tree moves, and on a
machine that never had the daemon's variables set: it is the record of what
processed that run, and re-running it is `gaudirun.py run00175.py`.

**`dry_run_all_jobs = True` at `jobs.py:18`** means every job today only prints
its command and sleeps. It has to be flipped to `False` for an end-to-end test.
Note that `format_config_file()` renders even in dry-run, so the `.py` appears
next to the outputs either way.

The daemon process needs `gaudirun.py` on `PATH`, the build's generated `Conf`
modules on `PYTHONPATH`, and `NL_CONDITIONS_DIR` pointing at a checkout of
`reco_testbeam/conditions`. On pinky the repo is at
`/home/pinky/bt2026/beamtime2026_pie5` and **there is no `/simulation`**, so the
default `CONDITIONS_DIR` is wrong there: `NL_CONDITIONS_DIR` is not optional.
It is now baked into every file the daemon renders, which is the other half of
the reason a pinky-rendered job re-runs correctly anywhere the conditions tree
is at that path.

## What fails early, on purpose

`check()` runs before a single Configurable is touched and reports **every**
problem it finds in one message, rather than the first:

1. `NL_MIDAS` or `NL_OUT` unset — the message shows **both** ways in: the interactive `NL_MIDAS=... NL_OUT=... gaudirun.py nearline_job.py`, and `python -m pioneer.nearline.process <midas file> --out-dir DIR`, which needs no environment at all. (A rendered file cannot reach this one: its paths are filled in.)
2. `NL_MIDAS` does not exist on disk.
3. The directory of `NL_OUT` is not an existing directory.
4. A resolved conditions container or ODB spec does not exist — one line per file, naming the absolute path.
5. Both halves off (`WD_ENABLED` and `PSM_DECODE`): nothing would decode.
6. `PSM_RECO` without `PSM_DECODE`: nothing would produce `/Event/muquad` and `/Event/mutrig`.
7. `PSM_DECODE` with an empty `PSM_GEOMETRY_BASE`: no `GeoHeader`, and the decoder throws on hit one.
8. `PSM_GEOMETRY_BASE` not in `GEOCOND:<table>` form.
9. `PSM_MUPIX_MONITOR` without `PSM_DECODE`: nothing would produce `/Event/muquad`, and the monitor takes the chip footprints from the `PIGeometrySvc` that `PSM_DECODE` creates.
10. `PSM_MUPIX_PIXELS_PER_BIN` below 1: it is how many pixels share one bin of a hit map.
11. `PSM_MUPIX_DT_RANGE_NS` or `PSM_MUPIX_DT_BINS` not positive: a half-width and a bin count.
12. `PSM_MUPIX_WINDOW_NS` not positive: a non-positive half-window pairs nothing at all.
13. `PSM_MUPIX_EXPANDED_RANGE_MM` or `PSM_MUPIX_CENTRAL_SLOPE_MRAD` not positive: both are half-widths of symmetric axes.
14. `PSM_SMA_MONITOR` without `PSM_DECODE`: nothing would produce `/Event/mutrig`, and the monitor takes the raw `MUTRIG` channel map from the `PIGeometrySvc` that `PSM_DECODE` creates.
15. `PSM_SMA_HITS_PER_EVENT_MAX` below 1: it is the top of an axis counting hits per event.
16. `PSM_SMA_DEGENERATE_TOT_SHARE` or `PSM_SMA_MARKER_TOT_SHARE` outside `(0, 1]`: both are shares of one counter's hits.
17. `PSM_SMA_COARSE_SHIFT` neither `None` nor an integer 0-18: above 18 the coarse field no longer pins the fine field's wrap.
18. `PSM_RF_CHANNEL` not an integer, outside 0-15, or equal to `PSM_CURRENT_CHANNEL`: the SMA word's channel field is 4 bits, and the decoder takes the RF channel first, so the current pulses would become RF pulses.
19. A `GEOCOND` base with an empty `PSM_GEOMETRY_FILES`: nothing supplies the table it names.
20. `COND:isel` in `PSM_GEOMETRY_TRANS` without `bt2026_isel.json` in `ODB_SPECS`.
21. `PSM_WEIGHT_STRATEGY` not `0`, `1` or `2`.
22. `PSM_WEIGHT_STRATEGY >= 1` without both `PSM_DECODE` and `PSM_GEOMETRY_BASE`: no `PIGeometrySvc` to take the L1/L2 plane footprints from.
23. `PSM_WEIGHT_STRATEGY >= 1` without `COND:isel` in `PSM_GEOMETRY_TRANS`: every run would be treated as sitting at the design stage position.
24. Exactly one of `WD_ALIGN_TABLE` / `WD_ECAL_TABLE` set: `PIWDCalibrator` needs both.
25. `WD_ENABLED` with an empty `WD_RF_TABLE`: `PIWDRFPhase` runs first in `WDAnalysisSeq` and `PIWDWaveformAnalysis` reads `/Event/wd_rf_phase`, so the RF table cannot be empty.
26. `WD_ROLE_TABLE` set with an empty `WD_CONDITIONS_FILES`: nothing would supply the `wd_channel_map` table.
27. `WD_CHANNEL_SETTINGS_TABLE` set with an empty `ODB_SPECS`: only the begin-of-run ODB dump serves `wd_channel_settings`.
28. `WD_CAL_CHANNELS` not a subset of `WD_CHANNELS`: they would have no features to calibrate.
29. `WD_SCALER_MONITOR` without `WD_ENABLED`: nothing would produce `/Event/wd_scalers`.
30. `WD_SCALER_TIME_BIN_S` not positive or not below `WD_SCALER_TIME_MAX_S`, or a serial listed twice in `WD_SCALER_BOARDS`.
31. `WD_RF_REFINE` on with `WD_RF_REFINE_POINTS` below 2: a scan needs at least 2 points.
32. `PSM_PHASE_SPACE_BINS` not a positive multiple of 64: the phase-space histograms would not rebin onto the 64-bin minitwin export exactly.
33. `PSM_PHASE_SPACE_POS_RANGE_MM` or `PSM_PHASE_SPACE_SLOPE_RANGE_MRAD` not positive: both are half-widths of a symmetric axis.
34. `PSM_L_WINDOW_BEFORE_NS` and `PSM_L_WINDOW_AFTER_NS` giving an empty L-hit window: no tracklet would get an L pair.
35. `PSM_DROP_CROSSTALK_GHOSTS` on without `PSM_DECODE` and a `PSM_GEOMETRY_BASE`: the ghost rule recovers each hit's column and row from the chip placement the `PIGeometrySvc` serves.
36. `OUTPUT_LEVEL` not one of `DEBUG`, `ERROR`, `INFO`, `WARNING`.

## The phase-space histograms are minitwin input

The PSM phase-space histograms are not free-form monitoring plots. They are the
input the minitwin model consumes, so their axes are a contract, held by the
three `PSM_PHASE_SPACE_*` settings and by `check()`:

| histogram | what fills it | binning | rebin to the model's 64 |
|---|---|---|---|
| `PIPSMDelayedCoincidence/{xy,xxp,yyp}` (+ `_w`) | tagged prompts | 320 x 320 | `Rebin2D(5, 5)`, exact |
| `PIPSMAllTrackReco/{xy,xxp,yyp}` (+ `_w`) | every prompt-like tracklet | 64 x 64 x 6 (stop layer) | none needed; sum the stop axis away |

Both are on the minitwin det10 window — x, y over +-37 mm and x', y' over
+-950 mrad — which is `beamline-simulation/psm/psm_scan_config.py`
(`X_WINDOW`, `A_WINDOW`) and `minitwin/data/axes_v8.yaml`. The offline producer
`analysis/josh/psm_scan_hists.py` books the same 320 bins on the same windows,
so a nearline histogram and an offline one are comparable bin for bin.

`counters` carries the exposure: bin 1 `n_frames`, bin 2 `n_prompt`, bin 3
`n_tagged`, mirroring the `counters` histogram that offline producer writes. The
bins are deliberately unlabelled — `PIHistogramSvc` hands an algorithm the
per-slot clone, never the prototype it merges into, so a label set at
`initialize()` would reach one slot only. `n_frames` counts events that passed
`PSMRecoSeq`'s `/Event/mutrig` gate, which is a smaller number than the offline
`df.Count()` over every rec entry.

**x' and y' are `1000 * (x2 - x1) / PSM_DISTANCE_L12`, in mrad, everywhere.**
`PIPSMSimpleTrackReco` used to fill `(x1 - x2) / d` in radians, mirrored and a
factor of 1000 off from every other consumer — the nearline site's cubes,
`PIPSMDelayedCoincidence`, `PIPSMRecoCore` and `psm_scan_config.py`. That is
fixed, so the two `xxp` histograms in this job now mean the same thing and only
differ in their selection and binning.

## The merge step depends on `PSM_CURRENT_CHANNEL`

`combine_files.py` sums the histograms of a run's sub-runs and divides every one
of them by `Integral()` of `histograms/musip/current`, raising when that
histogram is missing or its integral is not positive. So **`PSM_DECODE = False`,
`PSM_CURRENT_CHANNEL = None`, or a run with no current pulses means no sub-run
merge** — and it surfaces at merge time, hours later, not while the run is taken.

**Two known defects, not fixed here.** `MergeJob.build_job_description_file()`
feeds `combine_files` the RNTuple file `<filebase>.root` while the histograms
live in `<filebase>_hists.root`, so the merge looks in the wrong file; and the
cross-run loop at `combine_files.py:114-116` iterates `histos.items()` while
indexing `combined_histos[name]` with a `name` leaked from the previous loop,
which happens to work for one run and fails on a sequence of more than one.

## Output size

`PIAOutputStream` persists **everything** registered on the TES, raw DRS traces
included, so the RNTuple comes out roughly the size of the MIDAS input: the
20k-event lab run 193 gives a 248 MB RNTuple next to a 19 kB histogram file,
and writing it dominates the job's time. `WD_CHANNELS` is not a lever on that:
the waveforms are persisted either way, and widening it from 6 to 16 channels
adds about 1 % to the file.
`WRITE_NTUPLE = False` is a pure monitoring pass and the right
setting for a shift display; the histogram file is unaffected.
The histogram file is small but no longer negligible on a PSM run: the six
320-bin phase-space TH2Ds are 7.4 MB uncompressed between them, and 3000 events
of `fake_run00913_mutrig.mid` measured 172 kB on disk (33 kB before they were
widened), because the arrays are mostly exact zeros and compress hard. Each Hive
slot clones every prototype, so that 7.4 MB is per slot — irrelevant under the
sequential event loop this job runs, but not free if it ever goes concurrent.
`PIPSMMuPixMonitor` adds about 4.8 MB uncompressed on top of that — 4.1 MB of it
one bin per pixel over eight chips and two planes, which is the price of seeing
a single dead column, and `PSM_MUPIX_PIXELS_PER_BIN` is the knob that gives it
back. The three fixed-axis track plots are about 0.4 MB of the rest. It compresses at least as hard as the phase space does: on a 400-event
slice of run 166 the whole histogram file went from 48 kB to 101 kB with the
module on. The per-plane `L<n>_mult` axes account for most of the rest, and they
run to 16383 hits per event on purpose — a MuPix readout frame is not one
particle, and run 165 puts over 6000 L2 hits in a single frame.
`NTUPLE_RULES = ["drop *", "keep /Event/wd_hits", ...]` keeps a shrunken file,
and selection happens once at `initialize()`, so the rules cost nothing per
event. There is nothing to tune in the writer itself: it is fixed at ZSTD-1
(`Compression = 501`, about 40 % less CPU than ROOT's default ZSTD-5 for about
8 % more disk) with one reused entry for the whole job.
