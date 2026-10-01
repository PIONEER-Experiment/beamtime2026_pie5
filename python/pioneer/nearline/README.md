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
pass. The constants come from the conditions database by default (see
*Conditions*); when it is down, jobs fail and *Conditions DB down* below is the
shifter's page.

| file | what it is |
|---|---|
| `nearline_job.py` | **the edit-me file *and* the template.** A Gaudi options file: settings block, then linear assembly. `gaudirun.py` execs it; it is not a module and must not be imported. It carries twelve `${name}` placeholders, all inside string literals, so it is valid Python and runs unrendered |
| `render.py` | fills those placeholders and writes the complete job next to the outputs as `<filebase>.py`. `render_job()` is what both callers use; `python -m pioneer.nearline.render IN OUT` renders and stops |
| `process.py` | **process one file by hand**: `python -m pioneer.nearline.process <midas file> [--out-dir DIR] [--light] [--conditions SOURCE]` renders the job and runs `gaudirun.py` on it. Standard library plus `render` and `pioneer.conddb.pgservice` only, so it imports where `jobs.py` cannot |
| `jobs.py` | job classes the daemon schedules: `GaudiJob` (this job), `RsyncJob`, `CleanJob`, `MergeJob`, `DummyJob` |
| `daemon.py` | the long-running process: MIDAS client, per-resource queues, dispatch and status write-back to the run database |
| `run.py` | run-sequence definitions (`midas_run_sequence`, `midas_run`) written into the run database |
| `combine_files.py` | merges the sub-run histograms of one run into a single normalised file |
| `beamtune_client.py`, `miniTwinInterface.py` | the daemon's clients for the beam-tune service and the mini-twin |
| `tuning.py` | the tuning loop: a proposal becomes one run, the run's histogram files go back as a context, DAQ progress is reported; `python -m pioneer.nearline.tuning {schedule,post}` runs it by hand |
| `README.md` | this file |

## What the job runs

```
PIMidasSelector       one .mid/.mid.lz4, every event, no bank filter
  |
PIMidasDecoder        PITMidasWaveDream, PITMidasMusip
  |
PSMSMACalSeq          gated on /Event/mutrig; runs whenever PSM_DECODE is on
  |    PIPSMSMACalibration                       /Event/mutrig -> /Event/mutrig_cal + /Event/sma_hits
  |
PSMTimewalkSeq        gated on /Event/muquad; runs whenever PSM_DECODE is on
  |    PIPSMMuPixTimewalkCorrection              /Event/muquad -> /Event/muquad_twc
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
  |    PIPSMMuPixMonitor                         reads /Event/muquad_twc; histograms only
  |
PSMSMASeq             gated on /Event/mutrig_cal
  |    PIPSMSMAMonitor                           reads /Event/mutrig_cal; histograms only
  |
PSMRecoSeq            gated on /Event/mutrig_cal
  |    PIPSMAllTrackReco (L hits /Event/muquad_twc, S hits /Event/mutrig_cal),
  |    PIPSMPatternReco, PIPSMComputeWeight, PIPSMDelayedCoincidence
  |
PIAOutputStream       RNTuple "rec"                -> <out>.root
PIHistogramSvc        histograms/<instance>/<name> -> <out>_hists.root
```

| instance | TES inputs | TES outputs | histograms |
|---|---|---|---|
| `PITMidasWaveDream` | WaveDREAM banks | `/Event/wd_event_header`, `wd_waveform`, `wd_channel_time`, `wd_timebase`, `wd_scalers` | — |
| `PITMidasMusip` | `H000` | `/Event/muquad`, `/Event/mutrig`, `/Event/rf` (only on a run with an RF channel) | `musip/current` (only on a run with a proton-current channel); with `PSM_SMA_DIAGNOSTICS` also `musip/sma_word_types`, `sma_words_per_channel`, `sma_bank_words_per_frame`, `sma_trigger_words_per_frame`, `sma_frame_span_ms`, `sma_frame_gap_ms`, `sma_live_time`, `sma_fine_coarse_diff`, `sma_fine_vs_coarse`, `sma_fine_bit_occupancy`; with `PSM_PIXEL_MASK` also `musip/mupix_masked_hits` (pixel words the mask dropped, one bin per chip, labelled `<vid> (raw <id>)`) and `musip/mupix_masked_hits_per_pixel` (one bin per masked pixel, labelled `<vid> c<col> r<row>`) |
| `PITMidasMusip` | `H000` | (state product) | `/Event/sma_time_state` (PIPSMSMATimeState): per H000 bank and SMA channel, the fine-offset k, how it was found, mixed/undetermined flags, word counters and S5 class counts; one entry per bank |
| `PIPSMSMACalibration` | `/Event/mutrig` | `/Event/mutrig_cal`; `/Event/sma_hits` (PIPSMSMAHits, index-parallel to `mutrig_cal`: id, time, aligned TOT and NIM times, ToT, NIM width, flags, raw TOT/NIM indices, the ToT written) | always `sma_live_seconds` (one bin: the summed SMA frame spans, last minus first hit time of every frame, frames over 10 s left out; the live time the merge step multiplies the WaveDREAM rate with); per counter whose NIM copy the run cables (named by the TOT id): `dt_raw_<id>`, `dt_aligned_<id>` (t_NIM − nearest t_TOT, ±200 ns), `dt_wide_<id>` (all TOT words within 2^19 ns, the fine-field span), `dt_vs_tot_<id>` (the walk), `classes_<id>`, `nim_width_<id>`, `nim_candidates_<id>` |
| `PIPSMMuPixTimewalkCorrection` | `/Event/muquad`; `/Event/mutrig_cal` (optional, only with `PSM_TIMEWALK`) | `/Event/muquad_twc` | `twc_hits` (hits corrected, hits passed through without constants, events without counters (no `/Event/mutrig_cal`), events); with `PSM_TIMEWALK` the all-pairs dt(pixel − S1) vs pixel ToT before and after the correction, `twc_dt_vs_tot_raw_<vid>` / `twc_dt_vs_tot_cor_<vid>` per chip (detector ids 10011-10014, 10021-10024) and `twc_dt_vs_tot_raw_L<n>` / `twc_dt_vs_tot_cor_L<n>` per plane |
| `PIWDSettingsSummary` | ODB conditions tables | `WDSettingsHeader` | — |
| `PIWDRFPhase` | `/Event/wd_waveform`, `wd_channel_time` | `/Event/wd_rf_phase` | `rf_phase`, `rf_amplitude`, `rf_residual` |
| `PIWDWaveformAnalysis` | `/Event/wd_waveform`, `wd_channel_time`, `wd_rf_phase` | `/Event/wd_features` | `ppamp`, `le_time`, `ppamp_vs_channel`; with a role table also `baseline_vs_channel`, `baseline_rms_vs_channel`, `fired_vs_channel`, `coincidence` and, per scintillator channel, `charge_vs_amp_chNN`, `letime_vs_amp_chNN`, `charge_vs_rfphase_chNN` |
| `PIWDCalibrator` | `/Event/wd_features`, `wd_rf_phase` | `/Event/wd_hits` | — |
| `PIWDScalerMonitor` | `/Event/wd_scalers` | — | `readings` and, per board `NNN` in `WD_SCALER_BOARDS`, `rate_vs_time_bNNN`, `mean_rate_bNNN`, `threshold_bNNN`, `fpga_temp_vs_time_bNNN`; with a `"current"` input in the run's `wd_channel_map` (`WD_ROLE_TABLE`) also `proton_current_counts` and `proton_current_seconds` (board 036) |
| `PIPSMMuPixMonitor` | `/Event/muquad_twc`; `/Event/mutrig_cal` (optional, only with `PSM_TIMEWALK`) | — | `L<n>_chip<vid>_xy` and `L<n>_xy` per MuPix chip and plane, `hits_per_chip`, `tot_vs_chip`, `L<n>_mult`, and from the L1/L2 coincidence `dt`, `npairs`, `npartners`, `dx`, `dy`, `track_xy`, `xxp`, `yyp` (bin edges between pixel centres and slope steps: 2 pixels and 7 slope steps per bin), the same tracks on the minitwin window `xy_mt`, `xxp_mt`, `yyp_mt` (the beam-tuning feed's maps, `PSM_PHASE_SPACE_*`, stage-weighted like the `_w` views), and the same tracks on fixed axes `track_xy_expanded`, `xxp_central`, `yyp_central`, plus their acceptance-weighted twins `track_xy_expanded_w`, `xxp_central_w`, `yyp_central_w`; with `PSM_TIMEWALK` the all-pairs timewalk `tw_dt_vs_tot_L<n>_<vid>`, `tw_dt_vs_stot_L<n>_<vid>`, `tw_tot_vs_stot_L<n>_<vid>` per plane and counter S1-S5 (`<vid>` 2001, 2003-2006) |
| `PIPSMSMAMonitor` | `/Event/mutrig_cal`; `/Event/rf` (optional per frame, absent on a run without an RF channel) | — | `hits_per_counter`, `tot_vs_counter`, `hits_per_event_vs_counter`, `tot`, `fine_time_vs_counter`, `counters`, the S1-S5 coincidence views `pattern`, `s1_partners`, `pattern_duplicates`, `dt_to_s1`, `dt_to_s1_wide`, `pattern_counters`; with the RF input also `rf_period`, `rf_pulses_per_gate`, `rf_offset_vs_pulse`, `rf_phase`, `rf_veto_gap`, `rf_counters` and one `rf_phase_vs_tot_<vid>` per cabled counter |
| `PIPSMAllTrackReco` (a `PIPSMSimpleTrackReco`) | `/Event/muquad_twc`, `/Event/mutrig_cal`; `/Event/rf` (optional per frame, absent on a run without an RF channel) | `/Event/exp_all_tracks` | `xy`, `xxp`, `yyp`, `nhits`, `nseed`, plus their acceptance-weighted twins `xy_w`, `xxp_w`, `yyp_w`; with the RF input also `xy_vs_s1phase`, `xxp_vs_s1phase`, `yyp_vs_s1phase` and their weighted twins `xy_vs_s1phase_w`, `xxp_vs_s1phase_w`, `yyp_vs_s1phase_w`; with `PSM_TIMEWALK` the track-only timewalk `tw_dt_vs_tot_L<n>_<vid>`, `tw_tot_vs_stot_L<n>_<vid>`, and `tw_cluster_size_L<n>`, `tw_seeds` |
| `PIPSMPatternReco` | `/Event/exp_all_tracks` | `/Event/exp_pattern` | — |
| `PIPSMComputeWeight` | `/Event/exp_all_tracks` | `/Event/exp_track_weights` | — |
| `PIPSMDelayedCoincidence` | `/Event/exp_all_tracks`, `exp_track_weights` | `/Event/exp_tagged` | `counters`, `class`, `dt`, `sb`, `stop`, `xp`, `xp_w`, `yp`, `yp_w`, `xy`, `xy_w`, `xxp`, `xxp_w`, `yyp`, `yyp_w` |

**The SMA monitor reads the SMA hits alone**, as `/Event/mutrig_cal` (the
decoder's `/Event/mutrig` in time order, see *SMA calibration*).
`PIPSMSMAMonitor` is the counter-side companion of the MuPix monitor: no pixel
hits, no data-side channel map, no tracklets, so it still says what the
scintillator counters are doing when those are what is broken. Six histograms: `hits_per_counter`, whose
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

**Four of those TES paths are this job's choice, not a default.**
`/Event/exp_all_tracks` and `/Event/exp_track_weights` are names the script
assigns and passes explicitly to the PSM algorithms; their own defaults are
`/Event/tracker_fr`, `/Event/dtar_fr` and `/Event/exp_simple_tracks`, which are
the simulation's names. `/Event/muquad_twc` (`_TES_MUQUAD_TWC`) is the timewalk
layer's output, passed to the MuPix monitor's `input` and the track reco's
`L_hits` in place of `/Event/muquad`. `/Event/mutrig_cal` (`_TES_MUTRIG_CAL`) is
the SMA calibration layer's output, passed to every SMA reader in place of
`/Event/mutrig`: the timewalk layer's and the MuPix monitor's `CounterInput`,
the SMA monitor's `input`, the track reco's `S_hits`, and the gates of
`PSMSMASeq` and `PSMRecoSeq`. The three decoder paths above them — `/Event/wd_waveform`,
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
resolved against the JSON directory of `CONDITIONS` (json mode only), the ODB
specs and `ODB_OVERRIDES` against `CONDITIONS_DIR`; an absolute path is honoured
unchanged.

### Job

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `EVT_MAX` | `-1` | `-1` is the whole file. A few thousand while tuning turns minutes into seconds. `NL_EVTMAX` in the environment wins |
| `LIGHT` | `False` | The light job, meant for pinky and not yet switched on there (see *Light mode (pinky)* below): on, it forces `WRITE_NTUPLE`, `PSM_TIMEWALK`, `PSM_SMA_WIDE_DT` and `PSM_SMA_DIAGNOSTICS` to `False` after the block, the overrides file and the environment have been read, so nothing else can turn them back on. Leave it `False` in the block: the daemon's `--light` and `process.py`'s `--light` render it into the job, and `NL_LIGHT=1` sets it for an unrendered run. Anything but a bool is rejected by `check()` |
| `OUTPUT_LEVEL` | `"INFO"` | `INFO` is what the shift log wants; `DEBUG` makes the decoder print per event and the job crawl |
| `WD_ENABLED` | `True` | Off drops the whole WaveDREAM chain (waveforms → features → RF → hits). Nothing downstream announces the missing collections |
| `PSM_DECODE` | `True` | Off leaves the `H000` banks undecoded, so `/Event/muquad`, `/Event/mutrig` and `/Event/rf` never exist |
| `PSM_RECO` | `True` | Requires `PSM_DECODE`. Off leaves the hits unreconstructed and the PSM monitoring histograms unbooked |

### Conditions

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `CONDITIONS` | `"db:service=pioneer-conditions"` | Where the constants come from: `db[:SERVICE or CONNINFO]` the conditions database, `json:DIR` the JSON containers in `DIR` (a snapshot), `json` the containers in `CONDITIONS_DIR`. `NL_CONDITIONS` wins for an unrendered run, `process.py --conditions` for a hand run. The service is expanded through `~/.pg_service.conf` before the job reaches Gaudi, and the password stays in `~/.pgpass`. See *Conditions*. A service this host does not define, or anything that does not parse, is reported by `check()` |
| `CONDITIONS_DIR` | `NL_CONDITIONS_DIR`, else `/simulation/reco_testbeam/conditions` | The ODB specs and `ODB_OVERRIDES` resolve against it in both modes, and the containers too under a bare `json`. Wrong and `check()` prints one "conditions container does not exist" line per file, naming the absolute path |
| `ODB_SPECS` | `odb/bt2026_runinfo.json`, `odb/bt2026_wavedream_daq.json`, `odb/bt2026_wavedream_scalers.json`, `odb/bt2026_isel.json` | Map subtrees of the begin-of-run ODB dump to conditions tables. Drop one and the DAQ settings the run was actually taken with reach neither the algorithms nor the provenance header |
| `ODB_PRELOAD` | `runinfo`, `wd_board_settings`, `wd_channel_settings`, `wd_scaler_names` | Resolved at `initialize()` rather than lazily, so a misconfigured job dies in the first second naming the missing ODB path instead of at the first event that needs it |
| `ODB_OVERRIDES` | `""` | Run-indexed corrections applied to a private copy of the ODB tree before any table is mapped, for a setting that was recorded wrong. `""` uses the ODB exactly as recorded. A JSON container, loaded in both modes; in db mode it is the only JSON the job reads |
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

**Proton current.** The monitor also takes the input the run's interval of
`wd_channel_map` (`WD_ROLE_TABLE`) calls `current` and counts it into the
one-bin `proton_current_counts`: each reading's rate times the time since the
previous reading taken. `proton_current_seconds` holds the board time those
counts cover, so counts / seconds is the mean rate, and `finalize()` prints
both. Stale readings never feed these two (they are re-sends of an old value),
whatever `WD_SCALER_FILL_STALE` says, and neither does a reading on which the
input is disabled; the next reading taken then covers the whole interval at
its own rate. A reading with the same board time as the previous one is a
duplicate and is skipped. A reading without a previous one, the job's first or
the first after the board clock was reset (the board time counts seconds since
configuration, and the board is reconfigured at the start of a run), is
counted with min(its own board time, the median of the job's steady
intervals), so the nominal 5 s when the job has none. The begin of a run is
therefore approximate: the time between the last reading on the old clock and
the reset is in no file, and the first readings get estimated intervals, a few
seconds per run (up to tens of % of one subrun-0 file), plus at most one
readout period after the run's last reading. The merge step uses only the
rate, counts / seconds, which these estimates bias far less (below). A run
whose map has no `current` input (every run before the current was cabled to
input 15: input 6, cabled for it earlier, never counted and is spare), or
`WD_ROLE_TABLE = ""`, books neither histogram and says so at `initialize()`.
A `WD_ROLE_TABLE` that does not resolve for the run (no interval, a bad role)
stops the job at `initialize()`, as it does for `PIWDWaveformAnalysis`.

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `WD_SCALER_MONITOR` | `True` | Off drops the module. Requires `WD_ENABLED`: only `PITMidasWaveDream` decodes the scaler banks into `/Event/wd_scalers` |
| `WD_SCALER_BOARDS` | `[36]` | Board serials that get the per-board histograms. A serial read out but not listed lands only in `readings` and is named in the end-of-job warning; a listed serial with no readings leaves its histograms empty and is warned about too |
| `WD_SCALER_TIME_BIN_S` / `WD_SCALER_TIME_MAX_S` | `5.0` / `7200.0` | Bin width and upper edge in s of the board-time axis (seconds since the board was configured). The bin equals the readout period, one reading per bin. A run past the upper edge piles into the overflow bin; `finalize()` counts those readings and says to raise the edge |
| `WD_SCALER_FILL_STALE` | `False` | Readings the frontend flagged stale are counted and skipped; `True` fills them as well |

### PSM decode

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_RF_CHANNEL` | `None` | MuTrig **raw** readout channel (`chipid*32 + channel`, consumed before the map lookup) carrying the accelerator RF gated by S1. `None` takes it from the run's interval of `mutrig_channel_map`: the one raw channel it sends to the role id 2014 (`rf`), and a run whose interval has none (any run before the board was recabled with the RF on it) has no `/Event/rf` and empty RF histograms. The decoder's `initialize()` line names the channel and where it came from. An integer 0-15 overrides the map for every file the job processes, for a file whose interval is wrong or not yet written; the map's own RF channel is then dropped and counted. The SMA monitor and the track reco are always handed `/Event/rf`, because whether a run has RF is only known at `initialize()` |
| `PSM_CURRENT_CHANNEL` | `None` | Same raw-id convention and rule, the proton-current pulse on the SMA, role id 2015 (`proton_current`). A run whose interval marks it books `histograms/musip/current`; the proton current left the SMA when the NIM copies were cabled, so later runs book none and the merge step normalises them by the WaveDREAM scaler (see below). An integer 0-15 overrides the map; `check()` rejects it equal to `PSM_RF_CHANNEL` |
| `PSM_QUAD_PIXEL_PITCH` | `0.08` | MuPix pitch in mm; the local hit position is `(col + 0.5) * pitch`, so a wrong pitch scales every position and every slope |
| `PSM_SMA_COARSE_SHIFT` | `None` | The SMA word's coarse field is the time in ns shifted right by this, and it has differed between run ranges (3, i.e. 8 ns ticks, then 15, then 14). `None` takes the run's value from the `sma_coarse_shift` table in `bt2026_psm_readout_map.json`; a run no interval covers stops the job at `initialize()` rather than guessing. An integer here overrides the table, for a run whose shift has been measured (psm-analysis `sma-tot-vs-wd/mupix_phase.py RUN --time-check`) but not yet entered. Wrong, and every counter hit and RF pulse lands at a time no MuPix hit shares: the tracklets lose their L pairs while the SMA monitor's RF plots still look fine. Outside 0-18 is rejected by `check()` |
| `PSM_SMA_DIAGNOSTICS` | `True` | Books the decoder's raw-word SMA diagnostics under `histograms/musip/sma_*` (the list is in the table above). Off, they are simply absent; the decoder's own default is off so that other jobs using it do not grow them. Read them as counts: the live fraction is `sma_live_time` bin 1 / (bin 1 + bin 2), and for a subrun run the gaps *between* subruns are in no file, so the merged value is slightly high. Below coarse shift 12 the SMA time wraps (2.1 s at shift 3), so a frame span or gap longer than about 1.07 s folds back and is not caught by the decoder's `smaDiagMaxMs` cut. `sma_fine_coarse_diff` is coarse minus fine over the shared bits in ns: the latch offset of the two fields sits near 0 (up to ~16 us at shift 3), a flipped fine bit b at ±2^b ns; `sma_fine_vs_coarse` calls a word a mismatch beyond ±20 us (`smaDiagLatchToleranceNs`) and names the fine bits that differ |
| `PSM_SMA_FINE_OFFSETS` | `True` | The decoder's SMA fine-time correction, applied before the times are built: the S2 k * 2048 ns offset per frame (with a per-word resolution), the RF words snapped per word on the decoder's RF channel (from the map, or `PSM_RF_CHANNEL`), and the S5 t/2 field repaired. Writes `/Event/sma_time_state` and `histograms/musip/sma_fineoffset_*`. Off restores the times from before the correction. It changes the hits, so `LIGHT` does not switch it off |
| `PSM_SMA_SKIP_STALE_FIRST_FRAME` | `True` | Frame 0 of subrun 0 holds a stale replay of the previous run. On, the decoder drops the first H000 bank when the input file is subrun 0 (the second number in the name, `run00790_00000`); other subruns, and a name with no subrun (the banner warns), are left alone. Changes the hits, so `LIGHT` does not switch it off |
| `PSM_PIXEL_MASK` | `True` | Drops MuPix pixel words on hot pixels: the run's interval of `mupix_pixel_mask` in `bt2026_psm_readout_map.json`, a list of (detector id, column, row). The decoder counts what it drops in `histograms/musip/mupix_masked_hits` (per chip) and `mupix_masked_hits_per_pixel` and in its finalize line `N pixel word(s) on the M masked pixel(s) dropped`. The shipped table masks **nothing** on [0, open); a noisy-pixel study adds an interval through `python -m pioneer.conddb.mupix_mask` (see *Masking hot pixels* below). A run the table does not resolve for — the container missing from the job, or an interval closed without a replacement — stops the job at `initialize()` rather than running unmasked. `False` decodes every pixel word and books neither histogram. Noise bursts are not treated by the mask |
| `PSM_PIXEL_MASK_TAG` | `None` | Tag of `mupix_pixel_mask` to read; `None` is the table's default tag. A trial mask under its own tag is tried by naming it here; anything but `None` or a non-empty string is rejected by `check()` |
| `PSM_SMA_NIM_PAIRING` | `True` | `PIPSMSMACalibration` pairs each counter's TOT word with its NIM copy (S1L..S5L, ids 2021, 2023-2026) into one hit, for every counter whose copy the run's `mutrig_channel_map` interval cables **and** the `sma_time_alignment` table has an offset for. A cabled copy without an offset is histogrammed only and its words stay out of `/Event/mutrig_cal`. The shipped table has no NIM offsets, so the output stays the time-ordered TOT hits until they are measured; harmless on runs without copies. `False` drops the NIM words and applies no offsets. See *SMA calibration*. Changes the hits, so `LIGHT` does not switch it off; not a bool is rejected by `check()` |
| `PSM_SMA_PAIR_WINDOW_NS` | `20.0` | Largest aligned \|t_NIM − t_TOT\| of a pair (inclusive). Too narrow and walk or jitter splits real pairs into a TOT-only and a NIM-only hit; too wide and a NIM word pairs with a neighbouring particle's TOT word (about one RF period, 20 ns, apart). `check()` wants a number in (0, 1000] |
| `PSM_SMA_TIME_SOURCE` | `"tot"` | Time of a paired hit: `"tot"` (the TOT word's leading edge, which walks with amplitude) or `"nim"` (the NIM copy's CFD time). `"nim"` can reorder hits. A NIM-only hit always has the NIM time. Anything else is rejected by `check()` |
| `PSM_SMA_NIM_ONLY_TOT` | `1.0` | ToT (raw SMA units) a NIM-only hit is written with. It has to stay above `PSM_LAYER_THR` (0.2) for the hit to fire its layer; the real ToT is unknown (`/Event/sma_hits` keeps −1 and the `totSubstituted` flag). `check()` wants 0-255 |
| `PSM_SMA_OFFSET_OVERRIDE_NS` | `{}` | **Development and quick tests only.** `{detector id: ns}` replacing the `sma_time_alignment` value of that id for this job, each one logged as a warning; an id that is no counter's stops the job. Production constants go into the conditions table. `check()` wants a dict of int → number |
| `PSM_QUAD_TIME_BIN_NS` | `8.0` | Hardware fact — MuPix counts in 8 ns. Change it only if the DAQ clock changes. There is no MuTrig counterpart: since the trigger encoding, `PITMidasMusip` reports that time in ns directly and `trigTimeBinWidth` is gone |

### PSM geometry

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_GEOMETRY_BASE` | `"GEOCOND:psm_geometry"` | The layer `PIGeometrySvc` builds the `GeoHeader` from. Empty and the decoder throws on hit one; anything but a `GEOCOND:<table>` form is rejected, because that is the only form this job takes |
| `PSM_GEOMETRY_MAPS` | `["MUPIX:mupix_chip_map", "MUTRIG:mutrig_channel_map"]` | Raw-readout-id → detector-id maps. The `NAME` side must match the decoder's `muPixMap`/`muTrigMap` defaults; without them the decoder cannot turn a chip id or `chipid*32+channel` into a detector id and throws naming the raw id on the first hit |
| `PSM_GEOMETRY_TRANS` | `["COND:isel"]` | Adds the XY-stage translation read from `/Equipment/XYTable`. A run whose ODB has no XYTable equipment fails at `initialize()` with "source absent" rather than silently using a stale stage position; set `[]` and `PSM_WEIGHT_STRATEGY = 0` to process one (the acceptance weights need the stage position) |
| `PSM_GEOMETRY_FILES` | `bt2026_psm_geometry.json`, `bt2026_psm_readout_map.json` | Supply the base table and the two map tables. Empty with a `GEOCOND` base is a hard indexing error at startup |
| `PSM_GEOMETRY_TAG` | `None` | Tag of the base geometry table (`PIGeometrySvc.GeometryTag`); `None` is the table's default, `bt2026-v4`, which puts a **provisional** 0.32 mm gap (2 pixels per side) between the four MuPix chips of each quad, an assumed value from the in-beam wedge study of 2026-09-25 that is to be measured after the beamtime. `"bt2026-v3"` reprocesses with the chips edge to edge, as files made before 2026-09-25 were. Only the geometry table is pinned, the two maps keep their default tags. Anything but `None` or a non-empty string is rejected by `check()` |

### MuPix monitor

The low-level MuPix check, and the only part of the job that reads the MuPix
hits **alone**: no scintillator hits, no channel map, no tracklets. It reads
`/Event/muquad_twc`, the timewalk layer's output: the hits of `/Event/muquad`,
corrected, in time order (see *MuPix timewalk correction* below). With the
shipped empty constants that is the decoder's hits unchanged, and in the same
order whenever the decoder's frame is time-ordered (every frame checked so far).
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
| `PSM_MUPIX_PIXELS_PER_BIN` | `1` | Pixels per bin of every hit map. 1 is one bin per pixel — 256 x 250 per chip and 516 x 504 per plane on bt2026-v4 (the chip gaps are four empty bins), about 4 MB of histogram, and the granularity at which a dead column or a hot pixel is visible. `n` divides the bin count of every map by `n²` |
| `PSM_MUPIX_DT_RANGE_NS` / `PSM_MUPIX_DT_BINS` | `204.0` / `51` | Half-width and bins of the `dt` histogram. 204 over 51 bins puts each 8 ns MuPix tick at a bin **centre**; a round 200 puts it on a bin edge, where ROOT's edge convention splits the coincidence peak across two bins |
| `PSM_MUPIX_SLOPE_RANGE_MRAD` | `0.0` | Half-width in mrad of the x'/y' axes of this module's `xxp` and `yyp`, rounded up to whole bins of 7 slope steps (18.67 mrad). `0` derives the full geometric acceptance of the two planes at the lever arm, so nothing a pair can produce reaches an overflow bin: ±1390.7 / ±1353.3 mrad in 149 / 145 bins on bt2026-v4. The same tracks on the minitwin window (`PIPSMAllTrackReco`'s phase-space axes) are `xxp_mt` / `yyp_mt` |
| `PSM_MUPIX_EXPANDED_RANGE_MM` | `41.6` | Half-width in mm of the fixed x/y axes of `track_xy_expanded`, `xxp_central` and `yyp_central` (260 bins, 0.32 mm = 4 pixels). It covers the standard five-point scan at +-17 mm (`PSM_POSITIONS_MM`) and the +-20 mm 3x3 grid, plus the 20.64 mm half-width of a plane with its chip gaps (40.64 mm), rounded up to a whole number of 0.32 mm bins. The monitor shifts the axis by a quarter pixel (0.02 mm), so a half-pixel stage offset such as 17 mm puts no pixel centre on a bin edge. It is fixed rather than taken from the plane footprint so that every run of a stage scan books the same axes and the runs merge bin by bin. Too small, and tracks go to the overflow bins; `initialize()` warns when the L1 footprint is not inside it |
| `PSM_MUPIX_CENTRAL_SLOPE_MRAD` | `100.0` | Rough half-width in mrad of the x'/y' axes of `xxp_central` and `yyp_central`. A slope from two pixel planes only takes whole multiples of one pixel over the lever arm (2.67 mrad on bt2026), so the monitor books one bin per step, each step at a bin **centre**, and rounds this up to a whole number of steps: 100 gives 77 bins over ±102.67 mrad. A fixed bin width would show a comb of alternately full and empty bins instead |
| `PSM_MUPIX_ALL_PAIRS` | `0` | Pairs every L2 hit inside the window instead of only the one nearest in time. Each extra pair is a combinatorial ghost carrying a slope no particle had, so this is a diagnostic for a busy run, not a production setting. `npartners` reports the ambiguity either way |
| `PSM_TIMEWALK` | `True` | The MuPix timewalk against the scintillators, in two samples (see "MuPix timewalk" below). Here it sets the monitor's `CounterInput` to `/Event/mutrig_cal` (the time-ordered SMA hits, see *SMA calibration*), which is read as an **optional** input: `PSMMuPixSeq` stays gated on `/Event/muquad` alone, and a frame without SMA hits skips only the timewalk fills (the count is logged at finalize). It also sets the monitor's `ConditionsTable` to the PSM channel map, from which it takes the counters S1-S5 (the channel-map file is then loaded even with `PSM_RECO` off), and turns on `PIPSMAllTrackReco`'s track-only histograms (`Timewalk`, off by default in the algorithm), and the correction layer's `twc_dt_vs_tot_*` (below; the layer's `CounterInput` and `ChannelMapTable` are set the same way, whether or not the MuPix monitor runs). Off, none of the three sets is booked; the layer still books `twc_hits` |
| `PSM_TIMEWALK_DT_MIN` / `PSM_TIMEWALK_DT_MAX` / `PSM_TIMEWALK_DT_BINS` | `-150.0` / `450.0` / `300` | The dt axis [min, max) ns of every timewalk histogram, passed to all three algorithms (`TimewalkDtMin/Max/Bins` of the monitor, the track reco and the correction layer), so the layer's `twc_dt_vs_tot_raw_L<n>` stays the monitor's `tw_dt_vs_tot_L<n>_2001` bin for bin. 2 ns bins over [−150, 450) hold the prompt edge near −90 ns and the walk tail of the lowest ToTs to about +400 ns; a narrower axis cuts that tail out of the recalibration fit. Max not above min, or bins not an integer 1-8192, is rejected by `check()` |

**MuPix timewalk.** Per plane `L1`/`L2` and counter S1-S5, with dt =
t(pixel) − t(Sn) on 300 bins of 2 ns over [−150, 450) (`PSM_TIMEWALK_DT_*`) and the pixel ToT in
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

### MuPix timewalk correction

`PIPSMMuPixTimewalkCorrection` writes the decoder's MuPix hits
(`/Event/muquad`) to `/Event/muquad_twc` with each pixel's time moved by its
chip's walk at its ToT, t → t − W_chip(ToT), and puts them in time order: the
same hits, corrected, in time order. The MuPix hits of a frame have so far
always come out of the decoder in time order (the readout sorts them; the
decoder does not enforce it), but the correction moves times by up to a few
hundred ns, so a corrected frame has to be re-sorted, and doing it here once is
what lets every reader take the hits as time-ordered. W is the fitted peak of
dt = t(pixel) − t(S1) against the pixel ToT, the chip's constant offset from S1
included, so after the correction the pixel times line up with S1 at every ToT.
The constants are the run's interval of `mupix_timewalk` in
`bt2026_psm_readout_map.json`, one curve per chip (detector id) with the ToT
held inside the chip's `[tot_min, tot_max]`. The shipped table is **empty** on
[0, open), and an empty table leaves every time as it was, so the output equals
`/Event/muquad` field for field, and in the same order whenever the raw frame is
time-ordered (every frame of the runs checked so far), and until someone writes
constants (see *Recalibrating the timewalk* below) every downstream number is
what it was before the layer existed. With constants the order of a frame's
hits can differ from `/Event/muquad`, so compare the two collections as sets of
hits, not index by index. Ties keep their readout order. Scintillator hits are
not touched; the raw `/Event/muquad` stays on the TES and in the RNTuple.

**Where it runs.** In a sequencer of its own, `PSMTimewalkSeq`, after the
decoder and the SMA calibration layer (`PSMSMACalSeq`, whose output is its
`CounterInput`) and gated on `/Event/muquad`, whenever `PSM_DECODE` is on. It is
deliberately not a member of `PSMMuPixSeq`: the track reco in `PSMRecoSeq` reads
its output too, and has to find it with `PSM_MUPIX_MONITOR` off. The decoder
writes `/Event/muquad` and `/Event/mutrig` together, so every event that passes
`PSMMuPixSeq`'s gate (`/Event/muquad`) or `PSMRecoSeq`'s (`/Event/mutrig_cal`)
already holds `/Event/muquad_twc`. `PSM_TIMEWALK_CORRECTION = False` still
schedules the layer, uncorrected and without reading the table, so the
consumers read one path whatever the setting.

With constants loaded, **everything downstream of the layer sees corrected
times**: the MuPix monitor's L1/L2 `dt` and its coincidence tracks, its
`tw_*` (whose S1 plots then equal the layer's `twc_dt_vs_tot_cor_L<n>`), and the
track reco's L window, clusters, tracks and `tw_*`. The layer's `_raw`
histograms are the before picture, and they are what a recalibration fits.

**Crosstalk ghosts move out of the window.** A ghost (a ToT ≤ 3 pixel copied
from a real hit a few tens of rows away on the same chip) carries its source's
raw time, but the correction moves it by W at its own low ToT, about 230 ns
more than its source. Corrected, ghosts sit near −230 ns against S1, outside
their source's L window [−100, +160), and in L1−L2 pairs involving a ToT ≤ 3
pixel they make side peaks near ±240 ns, outside the monitor's ±40 ns window.
On run 459 (subruns 18-35, `PSM_DROP_CROSSTALK_GHOSTS` off) the ambiguous
seeds of `PIPSMAllTrackReco/tw_seeds` drop from 1578 to 709, the same as the
ghost drop gives without the correction; with the correction the ghost drop
removes only 1 + 2 clusters. The ghost rule in `PIPSMRecoCore` has no time
condition, so this is a side effect of the correction, not of the rule. At
these rates no-L1 and no-L2 do not rise, but at a higher rate a shifted ghost
could land alone in an earlier seed's window: watch no-L1, no-L2 and
ambiguous in `tw_seeds` when the rate goes up.

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_TIMEWALK_CORRECTION` | `True` | Applies the run's `mupix_timewalk` constants. A run the table does not resolve for (the container missing from the job, an interval closed without a replacement, two active intervals overlapping it), or constants that break a rule (array lengths, an unknown form, a non-finite parameter, a bad ToT range, a detector id twice or not in `mupix_chip_map`), stops the job at `initialize()` rather than running uncorrected. A chip missing from a non-empty table is not an error: its hits pass through, counted in `twc_hits` and named in a warning. `False` writes the hits uncorrected without reading the table; the monitor and the reco then see the raw times, in the decoder's (time) order |
| `PSM_TIMEWALK_CORRECTION_TAG` | `None` | Tag of `mupix_timewalk` to read; `None` is the table's default tag (`bt2026-timewalk`). A trial set of constants under its own tag is tried by naming it here; anything but `None` or a non-empty string is rejected by `check()` |

**Histograms**, under `PIPSMMuPixTimewalkCorrection/`:

* `twc_hits`, always: hits corrected, hits passed through (on a chip without
  constants, which is every hit with the empty table), events without
  counters (no `/Event/mutrig_cal`), events. The finalize line says the same.
* with `PSM_TIMEWALK`, the all-pairs timewalk against S1 before and after the
  correction: every pixel hit against every S1 hit of the readout frame, dt =
  t(pixel) − t(S1) on 300 bins of 2 ns over [−150, 450), against the pixel
  ToT (32 bins, one per 256 ns count). Per chip `twc_dt_vs_tot_raw_<vid>` and
  `twc_dt_vs_tot_cor_<vid>`, per plane `twc_dt_vs_tot_raw_L<n>` and
  `twc_dt_vs_tot_cor_L<n>`. S1 is the channel map's `S1Channel`, the same
  table the monitor and the reco take it from, and the planes are numbered as
  the monitor numbers them. `twc_dt_vs_tot_raw_L<n>` is the monitor's
  `tw_dt_vs_tot_L<n>_2001` of an uncorrected job bin for bin; with the empty
  table `_cor` equals `_raw`. Corrected, the `_cor` band should sit flat near
  dt = 0 at every ToT: a residual slope or offset means the constants do not
  fit this run. Every plot is a count, so subruns merge by summing.

### SMA calibration

`PIPSMSMACalibration` reads the decoder's SMA hits (`/Event/mutrig`) and writes
`/Event/mutrig_cal`: the hits aligned, each counter's TOT word and NIM copy
merged into one hit, in time order (the decoder writes a frame's hits in
readout order, which interleaves the channels). Its output is defined as "the
decoder's SMA hits, calibrated, in time order", and every SMA reader in the job
reads it: the timewalk layer's and the MuPix monitor's `CounterInput`, the SMA
monitor, the track reco's `S_hits`, and the gates of `PSMSMASeq` and
`PSMRecoSeq`.

**Two channels per counter.** A counter can reach the SMA twice: its TOT-box
output (the TOT word, ids 2001, 2003-2006) and a low-threshold NIM (CFD) logic
copy with its own id (S1L..S5L = 2021, 2023-2026), cabled per run in
`mutrig_channel_map`. The layer pairs a counter when its copy is cabled **and**
`sma_time_alignment` (`bt2026_psm_readout_map.json`, offsets per id relative to
S1's TOT word) has an offset for the copy:
- every counter hit is aligned to t − offset (a TOT id without a row: 0);
- on S3 the late words (ToT ≥ 128) and the echoes at a word's trailing edge are
  marked `echo` and kept out of the pairing (written as TOT-only hits);
- TOT and NIM words are matched one to one within `PSM_SMA_PAIR_WINDOW_NS`,
  closest |dt| first;
- a paired hit is the TOT word with the time `PSM_SMA_TIME_SOURCE` picks; a
  TOT-only hit is the TOT word; a NIM-only hit is the NIM word with its
  counter's TOT id, its aligned time and ToT `PSM_SMA_NIM_ONLY_TOT`, so
  downstream it is a hit like any other (clusters, ToT sums, stop layer).
NIM ids never reach `/Event/mutrig_cal`. A cabled copy without an offset is
"uncalibrated": its words are histogrammed and counted, not paired, not
written. The shipped table lists the TOT ids at 0 and **no NIM row**, so until
the NIM offsets are measured the output is exactly the time-ordered TOT hits,
which is also what every run without NIM copies gets. `PSM_SMA_NIM_PAIRING =
False` drops the NIM words and applies no offsets.

**The sidecar** `/Event/sma_hits` (PIPSMSMAHits) is index-parallel to
`/Event/mutrig_cal`: per hit both aligned times (NaN where a word is missing),
the TOT word's ToT and the NIM width (−1 where missing), the raw indices of both
words in `/Event/mutrig`, the ToT written and a flag word: `hasTot`, `hasNim`,
`nimExpected`, `incomplete` (one of two expected words missing), `timeFromNim`,
`totSubstituted` (NIM-only), `multiCandidate` (a word had another candidate in
the window), `inTotShadow` (NIM-only inside a TOT pulse of the counter: pile-up
in its dead time), `nearFrameEdge` (within one window of the frame's first or
last SMA hit) and `echo`. With the raw `/Event/mutrig` it rebuilds
`/Event/mutrig_cal` exactly, so it is kept in every `PSM_SMA_CAL_NTUPLE` mode.

**Histograms** under `histograms/PIPSMSMACalibration/`, per counter whose copy is
cabled, calibrated or not (table above). Two are for finding an offset:
`dt_raw_<id>` (±200 ns, the nearest TOT word) and `dt_wide_<id>` (every TOT word
within 2^19 ns of a sample of NIM words spread over each frame, up to 8192 pairs
per counter and frame, 256 ns bins over the whole fine-field span). A NIM channel whose fine field carries an offset (the
decoder folds any value mod 2^20 into [−2^19, 2^19)) peaks in `dt_wide` and not
in `dt_raw`. The finalize line per counter gives the paired, TOT-only and
NIM-only counts and fractions and the median aligned dt.

**Calibrating a new copy** (the manual path; no daemon involved): run the job by
hand on one subrun (`python -m pioneer.nearline.process`), read the offset off
`dt_wide_<id>` / `dt_aligned_<id>` (or the psm-analysis `sma-nim-pairing`
calibration CLI), try it with `PSM_SMA_OFFSET_OVERRIDE_NS = {<NIM id>: <ns>}` in a
copy of the job, and when the pair fraction looks right add the NIM row to
`sma_time_alignment` in a new interval (back up the conditions DB first).

**Where it runs.** In a sequencer of its own, `PSMSMACalSeq`, straight after the
decoder and gated on `/Event/mutrig`, whenever `PSM_DECODE` is on, and before
`PSMTimewalkSeq`. The order matters: the timewalk layer reads
`/Event/mutrig_cal` as an optional input, so a calibration layer scheduled after
it would leave the `twc_dt_vs_tot_*` histograms empty without any error. It is
not a member of `PSMSMASeq` for the same reason the timewalk layer is not a
member of `PSMMuPixSeq`: the other readers have to find its output with the SMA
monitor off.

**The raw `/Event/mutrig` stays in readout order**, on the TES and in the
RNTuple. The raw-stream analyses in psm-analysis (`scint-efficiency`,
`sma-raw-check`) rely on that order, which is why the time-ordered hits are a
second collection rather than a replacement. `PSM_SMA_CAL_NTUPLE` (see
*Output*) decides which of the two the RNTuple keeps; its default `"raw"` drops
`/Event/mutrig_cal`, because the raw collection plus the always-kept
`/Event/sma_hits` sidecar rebuild it hit for hit.

### SMA monitor

The counter-side twin of the MuPix monitor, and the only other part of the job
that reads one decoded collection **alone** — the SMA hits, as the time-ordered
`/Event/mutrig_cal` of *SMA calibration* above; no pixel hits, no data-side
channel map, no tracklets. A run whose tracklet reco reconstructs
nothing still tells you which counters took hits and what their ToT looked like.

The counter axis is not configured here: `initialize()` asks `PIGeometrySvc` for
the raw `MUTRIG` map, takes the distinct detector ids out of it and logs the
index it built, raw channels and all. `ParkedVid` is set to `2002`, the Degrader
id the map parks every uncabled channel on; that index carries the idle FEB's
own words and is therefore excluded from the ToT judgements below. Change the
parked id in the map and this number has to follow it.

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_SMA_MONITOR` | `True` | Off drops the whole module. Requires `PSM_DECODE`: only the musip decoding tool produces `/Event/mutrig`, from which the SMA calibration layer makes the `/Event/mutrig_cal` the monitor reads, and the monitor takes the raw `MUTRIG` map from the `PIGeometrySvc` that `PSM_DECODE` creates |
| `PSM_SMA_HITS_PER_EVENT_MAX` | `20000` | Top of the per-counter hits-per-event axis; everything above it lands in the last bin. 20,000 is the H000 bank cap, so nothing a frame can hold is clipped; a busy counter exceeds a few hundred hits per frame |
| `PSM_SMA_DEGENERATE_TOT_SHARE` | `0.95` | Share of a cabled counter's hits at its commonest ToT value above which `finalize()` calls it degenerate. One value repeated is a pulser or a stuck field, not a spectrum. Lower it and a genuinely narrow spectrum starts warning |
| `PSM_SMA_MARKER_TOT_SHARE` | `0.5` | Share at ToT 0 or 255 above which a cabled counter is called marker-dominated. Those two values are the idle FEB's own words, so a counter made mostly of them is not seeing its TOT box |
| `PSM_SMA_WIDE_DT` | `True` | Fills `dt_to_s1_wide`: every counter hit minus every S1 hit within ±2^18 ns in 1024 ns bins, the view in which a counter time off by a whole 2^16 or 2^17 ns (a flipped fine-time bit) shows up. The pairs grow as S1 hits times other hits, so a busy run makes it expensive. `False` sets the monitor's `MaxWidePairsPerFrame` to 0: the histogram is still booked but stays **empty**, and bin 2 of `pattern_counters` ("left out of wide dt") then counts every frame paired to S1 that has at least one wide pair (a frame with none passes the cap of 0 and is not counted). The narrow `dt_to_s1` is unaffected. Anything but a bool is rejected by `check()`. `LIGHT` switches it off |

Both shares are judgements about **cabled** counters only. The parked index is
expected to be all 0 and 255 and is never reported for it.

**RF phase.** The job also hands the monitor
`/Event/rf` (`RFInput`); it exists on runs whose map (or `PSM_RF_CHANNEL`) gives an RF channel. The SMA sees the accelerator RF only through S1's gate:
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
RF histograms. On a run without an RF channel the RF histograms are booked and
stay empty.

### PSM reco

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `PSM_CHANNEL_MAP_FILE` | `"bt2026_psm_channel_map.json"` | Container holding the data-side channel map the tracklet reco reads |
| `PSM_CHANNEL_MAP_TABLE` / `PSM_CHANNEL_MAP_TAG` | `"psm_channel_map"` / `""` | The table supplies the **whole** map, which is why no per-channel job option is set here; one that were set would override the table |
| `PSM_SEED_ON` | `-1` | Negative is unseeded: the earliest scintillator hit not already absorbed starts a new tracklet, so an isolated delayed pulse (the muon from a stopped pion) forms its own tracklet instead of being lost |
| `PSM_REQUIRE_L_HITS` | `0` | Requiring exactly one L1 and one L2 pixel cluster discards every delayed tracklet, because delayed pulses have no tracker hits |
| `PSM_SEED_ON_L` | `0` | Source runs only: seed tracklets on L1 tracker hits and pair each with the nearest L2 hit inside `PSM_LPAIR_WINDOW_NS`. A source on the tracker makes L1/L2 coincidences with no scintillator involved, and both scintillator-seeded modes attach L hits only to a scintillator cluster, so they reconstruct nothing from such a run. On in beam running it throws away the scintillator seed that defines a particle |
| `PSM_LPAIR_WINDOW_NS` | `40.0` | L1 to L2 half-window in ns for that mode. The two-plane correlation from a source is much broader than the tracker time resolution, so this is generous on purpose; inert while `PSM_SEED_ON_L` is `0` |
| `PSM_L_WINDOW_BEFORE_NS` / `PSM_L_WINDOW_AFTER_NS` | `100.0` / `160.0` | A scintillator cluster at `t` takes its L1/L2 hits from `[t - before, t + after)` (`thrMupix` / `thrMupixUpper`). Measured with the SMA and MuPix times on one base, t(MuPix) − t(S1) has a sharp edge at −90 ns, peaks at −52 ns and has a timewalk tail to about +150 ns, so the window opens just before the edge and closes past the tail. Too narrow and prompt tracklets lose their L pair; too wide and more of them see a second hit on one plane and are flagged ambiguous. The S-S clustering window is separate (`PSM_SCINT_WINDOW_NS`). An empty window is rejected by `check()` |
| `PSM_SCINT_WINDOW_NS` | `5.0` | The S-S clustering window in ns (`thrScint`): in the unseeded mode a cluster takes the scintillator hits in `[t_seed, t_seed + window)`. One particle's S1-S5 hits must end up in one cluster. The SMA counters are not time-aligned yet (S3 and S5 come out about 2 ns before S1, S2 and S4, and SMA times are whole ns), so the algorithm's default of 2 ns splits most particles into two or three clusters. Each cluster takes the same L1/L2 pair (about half of all L pairs become copies), and the cluster holding S1 lacks the layers that went to the other one, so its stop layer and S5 veto are wrong. At 5 ns about 95 % of beam particles reach S5 instead of about 16 %, and the copies drop from ~54 % to ~15 % of L pairs (what is left comes from late hits 5-25 ns after the particle). The S1-gated phase-space histograms and the MuPix monitor do not change. `check()` rejects a window that is not positive or that reaches `PSM_DELAYED_WINDOW_NS[0]`, where it would absorb the delayed pulse into its prompt cluster. Revisit once the SMA channels carry time offsets |
| `PSM_L_CLUSTER_DIST_MM` | `0.12` | The L hits of each plane inside the L window are clustered by distance (`lClusterDistMm`): two hits at most this far apart in mm, global x/y, are linked (single linkage, 1e-6 mm slack, no time condition beyond the window), and exactly one cluster per plane makes the L pair, placed at the mean of the cluster's pixel centres; more than one cluster on a plane flags the tracklet `lAmbiguous`. 0.12 takes the eight touching pixels at the 0.08 mm pitch (edge 0.08, corner 0.113 mm), also across a chip boundary, and nothing further. `0` switches the clustering off: then a second hit of a plane, even the neighbouring pixel of the same particle, makes the tracklet ambiguous, which is how the reco worked before. A plane with more than 64 hits in the window (the algorithm's `lClusterMaxHits`) is ambiguous without being clustered, which bounds the pairwise work |
| `PSM_DROP_CROSSTALK_GHOSTS` | `False` | Before the one-cluster-per-plane test, drop MuPix crosstalk ghosts (`dropCrosstalkGhosts`): a cluster whose largest ToT is at most 3 and that has a pixel of higher ToT of another cluster of the window on the same chip, at most one column and 40-43, 81-85 or 122-127 rows away. Most of the ambiguity left after the clustering is this. Needs the `PIGeometrySvc` of `PSM_DECODE`, from which each hit's column and row are recovered; `check()` refuses it without. Off until decided |
| `PSM_AGGREGATE` | `1` | Fills the phase-space histograms inside the algorithm while the data is in memory. This is what makes the job a monitoring job rather than a converter |
| `PSM_AGGREGATE_PROMPT_ONLY` | `1` | Restricts that filling to prompt-like tracklets: an unambiguous L1/L2 pair **and** at least one prompt-channel hit, the prompt channel being whatever `PromptChannel = -1` in the channel map resolves to (S1 on bt2026). Under a seeded configuration the seed already guarantees both and this changes nothing. In the unseeded mode this job runs (`PSM_SEED_ON = -1`) it is what keeps `PIPSMAllTrackReco`'s TH3s meaning "prompt tracks": off, an isolated delayed pulse forms its own tracklet with no L pair, its position is a sentinel, and it piles into the overflow bins of every phase-space plot |
| `PSM_AGGREGATE_OWNERS_ONLY` | `1` | The MuPix hits are not consumed: every scintillator cluster takes the L1/L2 hits of its own window, so one pair can sit on several tracklets of a frame (a particle's late hits, a decay pulse inside the prompt's window). The reco marks one owner per pair, a pair being the same lead pixel on L1 and on L2 (`lPairOwner` 1 / 0, `lPairShares` = how many carry it; both −1 in files written before the flags): a tracklet holding a prompt-channel hit (S1) before one without, then the best match of t(L1) − t(S1) to `PSM_LPAIR_OWNER_OFFSET_NS` (the seed time standing in for t(S1) without an S1 hit), then the earliest seed. With this on (`AggregateOwnersOnly`) only owners fill the phase-space histograms, so a pair fills once; with the prompt gate on it removes only pairs carried by two or more S1 tracklets (0.01–0.4 % of entries). A particle whose lead pixel differs between two windows still fills twice. Anything but 0 or 1 is rejected by `check()` |
| `PSM_LPAIR_OWNER_OFFSET_NS` | `0.0` | Expected t(L1) − t(S1) in ns used to pick that owner (`lPairOwnerOffsetNs`), on the time base of the MuPix collection the reco reads (`/Event/muquad_twc`). With the timewalk correction (`mupix_timewalk` constants, every run from 467 on) the corrected times line up with S1: the peak is −3…−1 ns on both planes (runs 528, 920), hence 0. A run without constants passes the raw times through, which peak 30–60 ns earlier (rate dependent); set the measured peak there. A value that is not a finite number is rejected by `check()` |
| `PSM_DISTANCE_L12` | `30.0` | L1 → L2 lever arm in mm, used to turn `x2 − x1` into a slope. Must match the telescope as built or every angle is scaled wrong. Source of truth: `beamline-simulation/psm/psm_scan_config.py` `DIST_L12_MM` |
| `PSM_DELAYED_WINDOW_NS` | `(20.0, 70.0)` | The π → µ tag window in ns (τ = 26 ns), `[MIN, MAX)` |
| `PSM_REQUIRE_SEED_HIT` | `1` | Requires the **prompt** half of a coincidence to have a prompt-channel hit of its own. Off, any tracklet inside the window can play the prompt role — including, in the unseeded mode, a delayed pulse that formed its own tracklet — and `counters`/`class` then count pairs no particle made. The delayed half is selected by `PSM_DELAYED_WINDOW_NS` and `PSM_S5_THR`, not by this |
| `PSM_LAYER_THR` / `PSM_S5_THR` | `0.2` / `0.2` | Stopping-layer and through-going thresholds. MeV in simulation, but **raw MuTrig ToT on data** until a ToT-to-MeV calibration exists, so both need retuning the first time real hits arrive |
| `PSM_POSITIONS_MM` | `(0,0), (17,17), (-17,17), (-17,-17), (17,-17)` | Telescope stage positions `(dx, dy)` in mm for the acceptance weighting — XY-stage coordinates, the same numbers as `/Equipment/XYTable`. The algorithms apply isel's `(-x, y)` translation themselves, so these are not pre-negated. Source of truth: `psm_scan_config.py` `POSITIONS_MM` |
| `PSM_WEIGHT_MARGIN_MM` | `2.0` | Fiducial erosion in mm applied to each stage position's L1/L2 plane footprint before the containment test, so a track just inside or outside a plane edge is not double-counted or lost between neighbouring scan positions |
| `PSM_WEIGHT_STRATEGY` | `1` | `0`: every tracklet gets weight 1. `1`: weight `0` unless the track is inside this run's own window at both L1 and L2, else `1/N`, `N` the number of `PSM_POSITIONS_MM` windows containing the track at both L1 and L2, each window being the conditions footprint (`PIGeometrySvc`) of the L1/L2 plane moved from this run's own stage position to that config position and eroded by `PSM_WEIGHT_MARGIN_MM`, less the gaps between the MuPix chips there (a track through a position's chip gap is one that position cannot see). Over the runs of a scan the weights a trajectory would receive then sum to 1 wherever at least one run can see it; a run at none of the positions (within 0.01 mm) counts its own window as one more and warns that its weights will not sum to 1 with the scan. `2`: also require containment at the track's stop-layer depth (`PIPSMAllTrackReco` only — the MuPix monitor has no scintillators to define a stop layer and is capped at `min(strategy, 1)`). Strategies `1` and `2` need `PSM_GEOMETRY_TRANS` to include `"COND:isel"`, or every run is treated as sitting at the design position; `check()` flags both requirements |
| `PSM_PHASE_SPACE_BINS` | `320` | Bins per axis of the tagged `xy`/`xxp`/`yyp` TH2Ds and of the MuPix monitor's `xy_mt`/`xxp_mt`/`yyp_mt` (with `PSM_PHASE_SPACE_POS_RANGE_MM` and `_SLOPE_RANGE_MRAD`). Must be a positive multiple of 64 or `check()` refuses to start: 320 = 5 x 64, so the histogram rebins onto the minitwin's 64-bin maps without splitting a bin. Source of truth: `beamline-simulation/psm/psm_scan_config.py` `NBINS_2D` |
| `PSM_PHASE_SPACE_POS_RANGE_MM` | `37.0` | Half-width of the x/y axis in mm, applied to both PSM algorithms. This is the minitwin det10 window (`psm_scan_config.py` `X_WINDOW`), not a display choice — move it and the histograms stop being model input. The algorithm's own default, 2.5, is a single-position zoom |
| `PSM_PHASE_SPACE_SLOPE_RANGE_MRAD` | `950.0` | Half-width of the x'/y' axis in **mrad** (`psm_scan_config.py` `A_WINDOW`), likewise on both algorithms. The 1D `xp`/`yp` spectra keep their own narrower `SlopeRange`: they are the shift zoom, not model input |

**S1 RF phase of a tracklet.** With `PSM_DECODE`, `PIPSMAllTrackReco`
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
`h.GetZaxis().SetRange(90, 104); h.Project3D("yx")`. The same tracklets fill
`xxp_vs_s1phase` (x at L1, x' in mrad, S1 RF phase) and `yyp_vs_s1phase` (y, y',
phase) on `xxp`'s and `yyp`'s x/x' and y/y' axes and the same phase axis, so the
phase space of any phase window is a projection of those in the same way. Each
of the three has a weighted twin, `xy_vs_s1phase_w`, `xxp_vs_s1phase_w` and
`yyp_vs_s1phase_w`, filled with the same scan-acceptance weight as `xy_w` (see
`PSM_WEIGHT_STRATEGY`), so a stage scan's phase-resolved phase space can be summed
the way `xy_w` is. The six are about 26 MB of histogram in memory (the weighted
ones carry Sumw2) and add little to the file, being mostly empty.

### Output

| setting | default | what goes wrong if it is wrong |
|---|---|---|
| `WRITE_NTUPLE` | `True` | Off is a pure monitoring pass; the histogram file is unaffected. See "Output size" |
| `NTUPLE_RULES` | `[]` | Ordered `keep <glob>` / `drop <glob>` rules over TES paths, later rules winning. A path no rule matches is **kept**, so empty persists everything and a newly registered collection is never lost by omission |
| `PSM_TWC_NTUPLE` | `"corrected"` | Which MuPix hit collections the RNTuple keeps: `"both"` (`_Event_muquad` and `_Event_muquad_twc`), `"corrected"` (appends `drop /Event/muquad` to the rules) or `"raw"` (appends `drop /Event/muquad_twc`). The rule goes last, so it wins over `NTUPLE_RULES`, and only with `PSM_DECODE` on (a rule matching nothing is warned about). The default keeps the hits the track reco read; the raw times follow from them and the run's `mupix_timewalk` constants, or from reprocessing the MIDAS file. `"both"` costs about 25 % more file on a busy beam subrun (see *Output size*). With the shipped empty constants the two are equal field for field (and in the same order when the raw frame is time-ordered); with constants the corrected one is time-ordered on the corrected times, so its order can differ. Any other value is rejected by `check()` |
| `PSM_SMA_CAL_NTUPLE` | `"raw"` | Which SMA hit collections the RNTuple keeps: `"both"` (`_Event_mutrig` and `_Event_mutrig_cal`), `"calibrated"` (appends `drop /Event/mutrig`) or `"raw"` (appends `drop /Event/mutrig_cal`). Appended after the `PSM_TWC_NTUPLE` rule, so it too wins over `NTUPLE_RULES`, and only with `PSM_DECODE` on. After it, in every mode, comes `keep /Event/sma_hits`: the pairing sidecar is always written, since it records how each hit was formed and, with `_Event_mutrig`, rebuilds `_Event_mutrig_cal`. `"raw"` is the default for that reason; `"calibrated"` drops the readout-order collection (both words of every counter) the raw-stream analyses need. The TES always holds both. Any other value is rejected by `check()` |

## Conditions

`CONDITIONS` picks where the constants come from. There are two modes:

| mode | `CONDITIONS` | what the conditions service is given |
|---|---|---|
| **db** (default) | `"db:service=pioneer-conditions"`, or `db`, `db:NAME`, `db:host=H port=P dbname=D user=U` | `PgConnections` = one explicit conninfo; `JsonFiles` = `ODB_OVERRIDES` only, when set |
| **json** | `"json:DIR"`, or `json` for `CONDITIONS_DIR` | `JsonFiles` = the five `bt2026_*.json` containers from that directory (plus `ODB_OVERRIDES`); no database |

The ODB specs (`ODB_SPECS`, under `CONDITIONS_DIR/odb/`) are read in both modes.

**db mode.** Every host names the database by the libpq service
`pioneer-conditions` (read-only role `cond_viewer`), defined in the user's
`~/.pg_service.conf`, with the password in `~/.pgpass`. The job never passes the
service name to Gaudi. It expands it first (`pioneer.conddb.pgservice`) into the
explicit `host= port= dbname= user=` string, drops any password, and hands that
over. The reason is provenance: the C++ layer records only what it can parse
out of the string it is given, so a bare service name would reach every
ConditionsHeader as `host=<default>`. The service file is read the way libpq
reads it (first group and first key win, no spaces around `=`, a missing
`$PGSERVICEFILE` is an error), and a result without a host or a dbname is
refused. `db:` or `json:` with nothing after the colon is an error, not the
default. The output files record
`postgresql://host=... port=... dbname=conditions#<table>` as each table's
source. The job connects once, at `initialize()`, and makes no queries while
events are processed.

**An unreachable database fails the job at `initialize()`**, after about 20 s
(two attempts, each with libpq's 10 s `connect_timeout`). The job never falls
back to JSON by itself, because old constants that nobody chose are worse than
a failed job. The recovery is by hand: *Conditions DB down* below.

**json mode** is the job as it was before the database: the same five
containers, the same layer order, bit-identical outputs. `json:DIR` is how a
shifter processes from a snapshot while the database is down. Snapshots of the
database, exported as the same five containers, go to
`~/bt2026/conddb-snapshots/<UTC time>/`, with `latest` pointing at the newest
(written hourly by cron, and after every writer's `--write`, by
`python -m pioneer.conddb.snapshot`; see *Snapshots and backups* in
[`../conddb/README.md`](../conddb/README.md)).
`json` alone reads the git copies in `CONDITIONS_DIR`, which is also the dev
setup on a laptop without a database.

In either mode the startup banner prints the source, `[nearline] conditions db
host=... port=... dbname=...` or `[nearline] conditions json <dir>`, and the
conditions service prints one line per table saying which layer served it
(`layer=db source=postgresql://...` or `layer=json source=json://...`).

Several layers can be loaded at once, and **they never merge**: whichever layer owns
a table serves all of it.

| precedence | layer | `PIConditionsSvc` property | what it holds |
|---|---|---|---|
| highest | JSON containers | `JsonFiles` | json mode: the shipped `bt2026_*.json`. Either mode: `ODB_OVERRIDES` |
| middle | PostgreSQL | `PgConnections` | db mode: the conditions database |
| lowest | ODB | `OdbTables` | subtrees of the begin-of-run ODB dump, mapped by the spec files in `ODB_SPECS` |

Because the JSON layer comes first, a container passed in db mode would win over
the database for every table it holds. That is why db mode passes none, except
`ODB_OVERRIDES`, which is a deliberate local override.

db mode also refuses, in `check()`, the two ways of asking for containers it
would otherwise quietly not read: `WD_CONDITIONS_FILES`, `PSM_GEOMETRY_FILES` or
`PSM_CHANNEL_MAP_FILE` changed from their committed values (in the block, an
overrides file or a rendered copy), and a `CONDITIONS_DIR` that holds
`bt2026_*.json` containers without being a `reco_testbeam/conditions` checkout
(a scratch copy of the constants, pointed at with `NL_CONDITIONS_DIR` the way
it was done before the database). Both messages say to use json mode instead:
`CONDITIONS = "json:DIR"`, `NL_CONDITIONS=json:DIR`, or `process.py --conditions
json:DIR`.

`Preload` resolves `ODB_PRELOAD` (plus `wd_timebase` when WaveDREAM is on) at
`initialize()`, so a table nothing can supply fails the job in the first second.

The tables, and the container that carries each one in json mode (the database
holds the same twelve tables under the same names):

| container | tables it supplies |
|---|---|
| `bt2026_wavedream_timebase.json` | `wd_timebase` |
| `bt2026_wavedream_calibration.json` | `wd_rf`, `wd_time_alignment`, `wd_energy_calibration`, `wd_channel_map` |
| `bt2026_psm_geometry.json` | `psm_geometry` |
| `bt2026_psm_readout_map.json` | `mupix_chip_map`, `mutrig_channel_map`, `sma_coarse_shift`, `mupix_pixel_mask`, `mupix_timewalk` |
| `bt2026_psm_channel_map.json` | `psm_channel_map` |
| `odb/bt2026_runinfo.json` | `runinfo` |
| `odb/bt2026_wavedream_daq.json` | `wd_board_settings`, `wd_channel_settings` |
| `odb/bt2026_wavedream_scalers.json` | `wd_scaler_names` |
| `odb/bt2026_isel.json` | `isel` — only read when `PSM_GEOMETRY_TRANS` contains `COND:isel` |

`WD_ROLE_TABLE` names `wd_channel_map`, the cabling table
(schema `wd_channel_map`, tag
`wd036-run114`, one interval per cabling change). The per-channel trigger levels
do **not** come with it: they are a DAQ *setting*, not a cabling *decision*, so
`WD_CHANNEL_SETTINGS_TABLE` reads `wd_channel_settings` from the ODB layer
(`odb/bt2026_wavedream_daq.json`), which is why that table is in `ODB_PRELOAD`.
Where a recorded level is `0` — a channel the DAQ never configured — the
algorithm falls back per role, to `WD_SCINT_THR_FALLBACK_V` on a scintillator
and `WD_NIM_THR_FALLBACK_V` on a NIM copy, and names every such channel once at
`initialize()`.

The loader, the export and the inspection tools are in
[`../conddb/`](../conddb/) (`json2pg.py`, `condtool.py`; see its README). A JSON
output file and a database output file of the same constants carry the same
sha256 per table, because the hash covers the constants and not where they
came from, so files from both modes merge.

**The run number is never configured.** `PIMidasSelector` publishes the
begin-of-run ODB dump as `"ODBHeader"` in `PIHeaderSvc` during its own
`initialize()`, and `PIConditionsSvc` reads `/Runinfo/Run number` out of that
header during **its** `initialize()` to pick intervals of validity. That is why
the `ExtSvc` order in the script is load-bearing: reorder it and every table
resolves for run 0.

`PromptChannel = -1` in `bt2026_psm_channel_map.json` is not a missing value:
a negative prompt channel means `PIPSMRecoCore` uses `S1Channel`.

## Running it

On pinky and piana, source the environment script of this repository. It sets
up ROOT, Gaudi, MIDAS, the reco install and this repository's `python/` the
same way on both hosts (piana: the stack `software/install.sh` built, see
[`../../../software/README.md`](../../../software/README.md)), and strips an
active conda env from the shell:

```bash
source <this repo>/software/env.sh
```

Elsewhere, start the analysis container and source the environment inside it:

```bash
cd <your testbeam-env checkout> && ./start-midas-container.sh
docker exec -it testbeam-midas bash
source /software/setup_container_env.sh
pushd /software/root/install && source bin/thisroot.sh && popd
source /simulation/docker/setenv.sh
export PYTHONPATH=/workdir/beamtime2026_pie5/python:$PYTHONPATH
```

### Processing a file

```bash
python -m pioneer.nearline.process /workdir/scratch/online/run00175.mid.lz4 \
  --out-dir /workdir/scratch/nearline
```

That is the daemon's flow without the daemon or the run database, and it is
what to reach for when a run has to be processed by hand. It reads the
conditions database, as the daemon does, through the `pioneer-conditions`
service of the user running it (`~/.pg_service.conf` and `~/.pgpass`; inside
`testbeam-midas` add `-e PGSERVICEFILE=/workdir/scratch/conddb/pg_service.conf
-e PGPASSFILE=/workdir/scratch/conddb/pgpass` to the `docker exec`). It renders
`nearline_job.py` for that one file and runs `gaudirun.py` on the result, so it
leaves the **same three artefacts the daemon leaves** (two with `--light`,
which writes no RNTuple), named after the part of the file name before the
first dot:

```
/workdir/scratch/nearline/run00175.py           the complete job that ran
/workdir/scratch/nearline/run00175.root         the RNTuple
/workdir/scratch/nearline/run00175_hists.root   the histograms
```

The histogram file is the full set the daemon writes, the MuPix timewalk
(`PIPSMMuPixMonitor/tw_*`, `PIPSMAllTrackReco/tw_*` and the correction layer's
`PIPSMMuPixTimewalkCorrection/twc_*`, with `PSM_TIMEWALK` on) and the clustered
L pairs (`PSM_L_CLUSTER_DIST_MM`) included: this is the way to remake them for
one subrun file by hand.

`--evt-max N` truncates, `--render-only` writes the `.py` and stops so you can
edit it before running it, `--job PATH` renders some other copy of the job
file, and `--light` renders the light job, the one a daemon started with
`--light` runs (next section). `--conditions` picks the source:

| `--conditions` | constants from |
|---|---|
| (not given) | `NL_CONDITIONS` if set, else the job's `CONDITIONS`: the database |
| `db` | the service `pioneer-conditions` |
| `db:NAME` | another libpq service, e.g. `db:pioneer-conditions-admin` |
| `db:"host=H port=P dbname=D user=U"` | a server named directly (no password here, it goes in `~/.pgpass`) |
| `json:DIR` | the JSON containers in `DIR`, e.g. `json:~/bt2026/conddb-snapshots/latest` |
| `json` | the JSON containers in `CONDITIONS_DIR` (the git copies) |

A service the host does not define stops the command before anything runs,
with the file it searched and the `--conditions json:` line to use instead. If `gaudirun.py` is not on `PATH` it exits 2 and prints the
`source` lines above instead of a Gaudi import traceback.

**Reproducing a run is running its `.py`:** `gaudirun.py run00175.py`. The
rendered file names its own input, output, event limit, conditions source and
conditions directory, so it ignores every `NL_*` variable and re-processes
the same file the same way whatever the shell around it says. That is true of a
daemon's rendered file and a `process` one alike — they come out of the same
renderer, and differ only in `rendered_at`, `rendered_by` and `job_id`. The
conditions source is resolved when the file is rendered: a service becomes the
explicit `host= port= dbname= user=` it named at that moment (never the service
name, never a password), and a `json:` directory becomes an absolute path with
its links resolved, so `json:~/bt2026/conddb-snapshots/latest` records the
snapshot it read and not the link, which moves on. The password is not in the
file: a re-run reads `~/.pgpass` (or `PGPASSWORD`) of whoever runs it.
`NL_CONDITIONS` and `NL_CONDITIONS_DIR` **of the shell that renders** are baked
in at that moment, exactly as the daemon bakes in its own, so set them before
the `process` command rather than before the `gaudirun.py` that re-runs it.

### Light mode (pinky)

Light mode is a **light** nearline job for pinky, so that it keeps up with the
data: on one beam subrun the full job took 14.8 s of CPU and the light job 5.1 s,
about a third. Light mode is the `LIGHT` setting, and it switches off four things:

| setting forced to `False` | what a light run does not have |
|---|---|
| `WRITE_NTUPLE` | the RNTuple: a light run writes the `.py` and the `_hists.root` only |
| `PSM_TIMEWALK` | the timewalk histograms: `PIPSMMuPixMonitor/tw_*`, `PIPSMAllTrackReco/tw_*` and the correction layer's `twc_dt_vs_tot_*`. The timewalk **correction** itself still runs (`PSM_TIMEWALK_CORRECTION` is not touched), so `/Event/muquad_twc` and everything read from it are the same as in the full job |
| `PSM_SMA_WIDE_DT` | the wide SMA dt plot: `PIPSMSMAMonitor/dt_to_s1_wide` is booked but empty |
| `PSM_SMA_DIAGNOSTICS` | the decoder's SMA raw-word diagnostics, `histograms/musip/sma_*` |

Everything else — the WaveDREAM chain, the MuPix and SMA monitors, the tracklet
reco and the phase-space histograms the tuning loop and the merge read — is
the same as in the full job. A light histogram file is therefore a valid input
to the merge and to the tuning loop.

How it is selected:

* **the daemon** — `daemon.py --light`. The flag is per daemon process: every
  nearline job that daemon starts is rendered light. It is written into the
  daemon's `/Programs/<client>/Start command`, so a restart from the MIDAS
  Programs page keeps it; the first time, start the daemon by hand with
  `--light` so that the Start command it writes carries it. At startup the
  daemon sends a MIDAS message saying which job it runs.
* **by hand** — `python -m pioneer.nearline.process <midas file> --out-dir DIR
  --light` renders and runs exactly what the light daemon would.
* **unrendered** — `NL_LIGHT=1` with the environment form below.

A rendered file records the choice in its `light` field (`"1"` or `"0"`) and
ignores `NL_LIGHT`, like every other `NL_*` variable, and the banner prints
`[nearline] light on, switched off: ...` or `[nearline] light off`.

In the run database a light job registers `<filebase>_hists.root` instead of
`<filebase>.root` (the RNTuple it does not write); see *Via the daemon*.

**Do not switch light mode on at pinky yet.** Today pinky is the only host that
runs nearline jobs. piana runs none: its RNTuples are pinky's, copied over by
`scripts/sync-from-daq.sh` in psm-nearline-website-2026. Several things read
what light mode drops — the website's run header digest, `/api/runs/{run}/odb`
(which the runplan quick scans use) and the conditions views read the RNTuple,
and the timewalk refit reads the timewalk histograms — so a light pinky leaves
them with nothing for every subrun it processes. Light mode can go on at pinky
only once piana, or another host, runs the full job for every subrun.

How that host gets its jobs is still to be designed. A second daemon on piana
claiming from the same nearline queue is **not** a way to do it: both daemons
would claim from one queue, so each subrun would go to one of them, and the
ones pinky took would still have no RNTuple.

### Masking hot pixels

The hot-pixel mask is a conditions table, and the nearline job reads it from
the conditions database. Masking a run is three hand-run steps, each of which
leaves something to look at. They write to the **database**, as the
`pioneer-conditions-admin` service (`cond_admin`) of whoever runs them, so that
service must be in their `~/.pg_service.conf` with its password in `~/.pgpass`.

1. **Find the pixels.** psm-analysis `mupix-timewalk/noisy_pixels.py RUN ...`
   (inside `testbeam-midas`) writes `noisy_pixels.json`, whose
   `recommended.pixel_mask.pixels` is the mask it recommends, and one
   `runNNNNN/noisy_pixels_runNNNNN.json` per run with the `hot.pixels` records.
2. **Put them into the database** (same `PYTHONPATH` as above). Dry run first;
   it reads the table from the database and prints what changes, the intervals
   after, the pixels with their detector ids, and the diff of the table:

   ```bash
   python -m pioneer.conddb.mupix_mask --db add \
     /workdir/scratch/mupix-timewalk/noisy-pixels/noisy_pixels.json \
     --run-start 459 --last-run 459 --split \
     --comment "hot pixels of run 459 (ThHigh/ThLow 0x7a/0x79)"
   # then exactly the same with --write at the end
   python -m pioneer.conddb.mupix_mask --db show --run 459
   python -m pioneer.conddb.mupix_mask --db check
   ```

   `--db` (before `add`) is what makes it the database; with no value it is
   `service=pioneer-conditions-admin`. `--write` loads the whole table in one
   transaction and is refused if somebody changed the table since the tool
   read it (read again and redo it). After the load the tool refreshes the JSON
   snapshot (`~/bt2026/conddb-snapshots/`) by itself. The study names chips by
   their raw id; the tool converts them to detector ids through
   `mupix_chip_map` at the study's run. `--split` is needed while the range
   lies inside the shipped empty [0, open) interval: that interval is kept but
   deactivated, and its parts outside the new range come back with the empty
   payload. Where the range overlaps an interval that already masks pixels,
   `--split` also needs `--union` (keep those pixels and add the new ones) or
   `--replace-mask` (drop them there; it prints how many per interval).
   `check` runs the decoder's rules over every run.
3. **Check one file** with the job as the daemon runs it (the database, no
   `--conditions`):

   ```bash
   python -m pioneer.nearline.process /workdir/scratch/online/run00459_00000.mid.lz4 \
     --out-dir /workdir/scratch/mask-check
   ```

   The log names the interval (`Resolved table=mupix_pixel_mask ... iov=[459, 460)
   ... layer=db`) and ends with `N pixel word(s) on the M masked pixel(s)
   dropped, by chip`; `histograms/musip/mupix_masked_hits` holds the same
   counts, and the masked pixels are empty in the
   `PIPSMMuPixMonitor/<plane>_chip<vid>_xy` maps.

That is all: the next file the daemon processes reads the new interval, with
nothing to commit and nothing to redeploy. The git copy of the table
(`reco_testbeam/conditions/bt2026_psm_readout_map.json`) is an export of the
database and is refreshed from it (`../conddb/README.md`, "Workflow: the
database first"); it is never edited by hand first.

**Trying a mask without writing it** (optional, for a dev check): copy the
conditions directory to scratch, run the same `add` **without `--db`** and with
`--conditions /workdir/scratch/mask-trial/conditions ... --write`, which edits
that copy, and process a file against the copy:

```bash
python -m pioneer.nearline.process /workdir/scratch/online/run00459_00000.mid.lz4 \
  --out-dir /workdir/scratch/mask-trial \
  --conditions json:/workdir/scratch/mask-trial/conditions
```

Without `--db` and without `--conditions`, `--write` edits the git-tracked
container, which no nearline job reads unless it is run in json mode. On pinky
that would change nothing the daemon does and leave the checkout dirty, so
the next `git pull` refuses: do not do it there.

### Recalibrating the timewalk

The timewalk constants are a conditions table too, and the job fills the
histograms they are fitted from, so a recalibration is four hand-run steps.
The fit reads the `_raw` histograms, which hold the uncorrected times whatever
constants the processing applied, so any processing of the run with
`PSM_TIMEWALK` on will do, corrected or not. Step 3 writes to the database as
`pioneer-conditions-admin`, as for the mask.

1. **Process the files** the constants are to come from, as in *Processing a
   file* above (or take the daemon's outputs). Each subrun's `_hists.root`
   carries `PIPSMMuPixTimewalkCorrection/twc_dt_vs_tot_raw_<vid>` per chip.
   About 18 subruns of a beam run are needed; 10 are too few for some
   chips. Check the per-chip views in the website's browse tree first.
2. **Fit the walk per chip** (same `PYTHONPATH` as above). The tool needs
   numpy, scipy, iminuit and uproot (PyROOT is used if uproot is missing),
   plus matplotlib for `--plots`. The `testbeam-midas` python has all of
   them; on the laptop host use `~/miniconda3/envs/beamtune-psm/bin/python`.

   ```bash
   python -m pioneer.conddb.mupix_timewalk fit \
     /workdir/scratch/nearline/run00459_000{00..17}_hists.root \
     --out /workdir/scratch/twc-trial/fit_run00459.json \
     --plots /workdir/scratch/twc-trial/plots
   ```

   It sums the histograms over the files, fits the dt peak in each ToT column
   and then the walk curve to the peaks, and writes the parameters, their
   errors, the χ² and the column peaks to the JSON. PNGs (one per chip) are
   written only with `--plots DIR`. A chip with fewer than 5 good columns is
   refused and named. `add` leaves it out, so its hits pass through
   uncorrected. Look at the PNGs before going on.
3. **Put the constants into the database.** Dry run first, then the same with
   `--write`:

   ```bash
   python -m pioneer.conddb.mupix_timewalk --db add \
     /workdir/scratch/twc-trial/fit_run00459.json \
     --run-start 459 --last-run 459 --split \
     --comment "timewalk of run 459, fitted from subruns 0-17"
   # then exactly the same with --write at the end
   python -m pioneer.conddb.mupix_timewalk --db show --run 459
   python -m pioneer.conddb.mupix_timewalk --db check
   ```

   As for the mask, `--db` makes it the database, `--write` loads the whole
   table in one transaction (refused if the table changed since the read) and
   then refreshes the snapshot, and `--split` is needed while the range lies
   inside the shipped empty [0, open) interval. Constants do not merge, so a
   range that overlaps an interval already holding constants also needs
   `--replace` (together with `--split`); an empty interval does not need it.
4. **Check one file** with the job as the daemon runs it:

   ```bash
   python -m pioneer.nearline.process /workdir/scratch/online/run00459_00020.mid.lz4 \
     --out-dir /workdir/scratch/twc-check
   ```

   The log names the interval (`Resolved table=mupix_timewalk ... iov=[459, 460)
   ... layer=db`), prints each chip's curve with its W at ToT 0, at its
   `tot_max` and at 31, and ends with
   `N MuPix hit(s) timewalk-corrected, 0 passed through without constants`.
   `PIPSMMuPixTimewalkCorrection/twc_dt_vs_tot_cor_L1`/`_L2` should show a
   flat band near dt = 0 where `_raw_L1`/`_L2` bend, and the RNTuple's
   `_Event_muquad_twc.fObsTime` now differs from `_Event_muquad.fObsTime`
   (and a frame's hits may come in a different order, since the output is
   time-ordered on the corrected times).
   Prefer a file that did not go into the fit.

As for the mask, the daemon's next job reads the new interval; nothing is
committed or redeployed, and the git container is refreshed from the database.
**Trying constants without writing them** works as for the mask: `add` without
`--db`, with `--conditions <scratch copy> ... --write`, then `process.py
--conditions json:<scratch copy>`; never without `--db` against pinky's own
checkout. The walk depends on the pixel threshold, so a change of the MuPix
thresholds is a reason to refit and to start a new interval.

### Deploy order

`nearline_job.py` sets the decoder's `applyPixelMask` (and
`smaDiagnostics`) unconditionally, and a `PITMidasMusip` built before those
properties existed rejects them, so every job fails at configuration. It also
imports and schedules `PIPSMMuPixTimewalkCorrection` and `PIPSMSMACalibration`,
which a reco_testbeam build without them does not have (the job then fails at
the import of `pi_psmalg_expConf`), and the layer reads `mupix_timewalk`, which an older
`conditions/` does not carry (the job then stops at `initialize()`). When
updating a machine, pull and rebuild reco_testbeam (the library and its
`conditions/`, which must carry `mupix_pixel_mask` and `mupix_timewalk`)
**before** pulling beamtime2026_pie5.

The database default needs three things on the host **before** the job file
that has it is pulled: a build with the PostgreSQL layer (libpq found when
cmake ran, so `PI_COND_HAVE_PG` is among the `shared` compile flags; a build
without it fails every job at `initialize()` with "PgConnections is set but
this build has no PostgreSQL layer"), the `pioneer-conditions` service in
`~/.pg_service.conf` of the user the daemon runs as, and that user's password
line in `~/.pgpass`. Check all three with one hand run before the daemon starts:
`python -m pioneer.nearline.process <recent subrun> --out-dir /tmp/nl-check`
must end with rc 0 and its log must show `layer=db` lines.

Restart the nearline daemon right after pulling beamtime2026_pie5. A running
daemon keeps the `render.py`, `jobs.py` and `daemon.py` it started with, but reads
`nearline_job.py` from disk for every job, so until the restart it renders the
new job file with the old code. The job file is written to survive that where it
can — a daemon from before light mode leaves the `light` placeholder unfilled,
and the job reads that as the full job — but an old daemon does not know
`--light`, and the next change may not be as forgiving. The conditions source is
such a change: a daemon started before it fills a `pg` placeholder the job no
longer has, so it refuses to render and every job it takes fails to start
(MIDAS message "Job N failed to start: ... has no placeholder for: pg"). It
does not quietly render a JSON job. Restart it.

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
| `NL_LIGHT` | no | `1` sets `LIGHT` (the light job), `0` clears it; anything else is rejected by `check()` |
| `NL_CONDITIONS` | no | overrides `CONDITIONS`, e.g. `json:/workdir/scratch/snap` or `db:pioneer-conditions-admin`. Also read by the renderer (the daemon, `process.py` without `--conditions`) and baked in |
| `NL_CONDITIONS_DIR` | no | overrides `CONDITIONS_DIR`. Also baked in by the renderer |
| `NL_OVERRIDES` | no | a small Python file `exec`'d over the settings, for a variant job |

**Every variable in that table is read only by an unrendered job file.** A
rendered one ignores all seven — including `NL_OVERRIDES`, which the renderer
does not fold in either — so an overrides file is an interactive mechanism and
nothing else. A variant that has to be reproducible is made by editing the
settings block, or by editing a rendered copy and running that. Light mode is
the exception in a rendered copy: what counts there is the `light` field of
`_RENDERED` (`"1"` or `"0"`), which is applied after the settings block, so
editing `LIGHT` in the copy's settings block has no effect.

**Anything you find yourself setting more than once belongs in the settings
block, not in the environment.** The environment is for the one-off; the block
is the record of how this experiment processes its data.

### The tuning loop by hand

The daemon takes proposals from the beam-tuning service and sends back what
the DAQ measured (`tuning.py`, see "The tuning loop" below). The same two
steps run by hand, for when the daemon is down or a step has to be retaken:

```bash
cd /home/pinky/bt2026/beamtime2026_pie5/python      # the environment the daemon runs in
python -m pioneer.nearline.tuning schedule --dry-run # show the run the current proposal would give
python -m pioneer.nearline.tuning schedule           # write it to the run database
python -m pioneer.nearline.tuning post --run <N> --dry-run   # show the context of MIDAS run N
python -m pioneer.nearline.tuning post --run <N>             # send it
```

Both commands connect to MIDAS as `NearlineTuning`, read their settings from
`/Nearline/config` and share `/Nearline/MiniTwin` with the daemon, so a step
taken by hand is not taken again by the daemon, and a step posted by hand is
closed for both. Options, after the command name:

| option | what it does |
|---|---|
| `--dry-run` | print the rows that would be scheduled, or the JSON that would be sent; nothing is written or sent |
| `--url URL` | the service, instead of `/Nearline/config/MiniTwin URL` |
| `--force` | `schedule` only: run even though `MiniTwin enable` is on |
| `--since N` | `schedule` only: ask for a proposal newer than `N` instead of the last one seen. One less than a proposal id retakes that proposal |
| `--output-path DIR` | the nearline output tree, instead of `/Nearline/config/Output path` |
| `--no-odb` | do not connect to MIDAS: defaults only, nothing remembered. For a look on a machine without the experiment |
| `--midas-host`, `--midas-expt` | as for the daemon; default from `MIDAS_SERVER_HOST` / `MIDAS_EXPT_NAME` |

`schedule` exits 0 when it wrote (or would write) a run, 1 when the service
has no newer proposal, 2 on an error. `post` exits 0 when the service took the
context and 2 otherwise; unlike the daemon it does not keep a context it could
not deliver, so run it again once the service answers. `post` reads the beam
header of the first subrun file through ROOT when the PIONEER dictionaries are
loaded, else through uproot; the inline maps need ROOT, and without it the
context goes out with its files only.

**Before enabling the loop**, run `post --dry-run` on a recent run in the
daemon's exact environment (same user, same shell setup, same `PYTHONPATH`):

```bash
python -m pioneer.nearline.tuning post --run <recent run> --dry-run
```

It must print a context with 28 knobs and every subrun file. It reads the
beam header the way the daemon will: through ROOT when the PIONEER
dictionaries are loaded, else through uproot; with neither it fails with a
message naming what is missing, and so would every post of the daemon.

`schedule` refuses to run while `/Nearline/config/MiniTwin enable` is on,
because the daemon polls the same service and could take the same proposal:
set it to `n` first (it is how a step is taken while the daemon's loop is
paused), or pass `--force`. `--dry-run` only reads and always runs.

## Conditions DB down

The shifter's page for when nearline jobs fail because the conditions database
cannot be read. It assumes the DAQ is idle (no run is being taken) and that you
are logged in on pinky as the user the nearline daemon runs as (`pinky`). The
database is `conditions` on pinky's PostgreSQL server, and the job reads it
through the service `pioneer-conditions`. The job never falls back to older
constants by itself (see *Conditions*), so until someone acts, every new subrun
stays unprocessed.

### 1. Recognise it

* On the MIDAS **RunDB** page, or the website's run page, the nearline jobs of
  new subruns turn `FAILED`, each about 20 s after it started (2 s when the
  server refuses outright).
* The daemon announces its conditions source when it starts (MIDAS message
  `Nearline daemon: conditions from the database host=localhost port=5432
  dbname=conditions`); a start message that says anything else is a problem of
  its own (see *Via the daemon*).
* The failing job prints the lines below. There is no log per job: the run's
  nearline log, `/home/pinky/nearline/runNNNNN/runNNNNN_nearline.log`, is shared
  by every subrun job of that run, each job empties it when it starts and jobs
  running in parallel write into it together, so it may show another subrun or
  a mixture. To see one subrun's failure cleanly, run its rendered job again,
  which fails the same way within about 20 s:
  `gaudirun.py /home/pinky/nearline/runNNNNN/runNNNNN_SSSSS.py` (in the daemon's
  environment, see step 3). The lines (from a test against the laptop's
  database container; on pinky the host is `localhost`):

  ```
  [nearline] conditions db host=testbeam-pgdb port=5999 dbname=conditions
  PIConditionsSvc     FATAL Conditions: could not connect (host=testbeam-pgdb port=5999 dbname=conditions): connection to server at "testbeam-pgdb" (172.18.0.3), port 5999 failed: Connection refused
  ServiceManager      ERROR Unable to initialize Service: PIConditionsSvc
  ApplicationMgr      ERROR Application Manager Terminated with error code 1
  ```

  The text after `failed:` says why: `Connection refused` (nothing listens on the
  port, the server is down), `timeout expired` (the host does not answer),
  `password authentication failed` (`~/.pgpass`), `database "conditions" does
  not exist`, `no pg_hba.conf entry` (server configuration).
* If instead the MIDAS messages say `Job N failed to start: libpq service
  'pioneer-conditions' is not defined in any service file; searched: ...` (or
  another error about the service file), the jobs never started, and the daemon
  marked their rows `FAILED` straight away: the service file of the daemon's
  user is missing or broken. The daemon already said so when it started. That
  is a setup problem, not an outage; restore `~/.pg_service.conf` (the expert
  has the contents), restart the daemon, and go to step 4.
* If the whole PostgreSQL server is down, the **run database** (`pioneer`, same
  server) is down with it: the RunDB page reads "Run database: unreachable", the
  daemon sends `Nearline Error ...` MIDAS messages and claims no new jobs at all
  (they stay `PENDING`). Only the jobs that were already running go `FAILED`.

### 2. Check the database

```bash
systemctl status postgresql                          # "active (running)"?
psql service=pioneer-conditions -c 'select 1'        # prints a row with 1?
psql service=pioneer-conditions -c 'select name from cond_tables'   # the tables, not empty
```

* `systemctl` says `inactive`, `failed` or `activating`: the server is down.
  Call the DAQ expert; restarting it needs sudo
  (`sudo systemctl restart postgresql`, then `systemctl status postgresql`
  again). The run database comes back with it.
* The server is running but `psql` fails: the message names the cause, as in
  step 1. Call the expert with that message.
* Both work: the database is fine now (it may have been restarted meanwhile).
  Go to step 4 and requeue.

### 3. Process a file by hand while the database is down

Only for the subruns someone needs to look at now; everything else waits for
step 4. The latest snapshot of the database, exported as JSON containers, is at
`~/bt2026/conddb-snapshots/latest` (written by the snapshot tool; see
`../conddb/README.md`). A new snapshot is written only when the database
changed, so an old date is fine. What matters is that the hourly runs before
the outage succeeded:

```bash
ls -l ~/bt2026/conddb-snapshots/latest                # where it points, and when
tail -3 ~/bt2026/conddb-snapshots/cron.log            # no FAILED line from before the outage
```

Then, in the environment the daemon runs in (same `PATH`, `PYTHONPATH` and
`NL_CONDITIONS_DIR`). `NL_CONDITIONS_DIR` is whatever the daemon was started
with, and every job it rendered records it: read it from any recent rendered
job of the run, and export the same value.

```bash
grep -m1 '"conditions_dir"' /home/pinky/nearline/runNNNNN/runNNNNN_00000.py
export NL_CONDITIONS_DIR=<the value it shows>   # empty there means the job's default
```

Then:

```bash
cd /home/pinky/bt2026/beamtime2026_pie5/python
python -m pioneer.nearline.process /home/pinky/online/runNNNNN_SSSSS.mid.lz4 \
  --out-dir /home/pinky/nearline/runNNNNN \
  --conditions json:~/bt2026/conddb-snapshots/latest
```

It writes `runNNNNN_SSSSS.py`, `.root` and `_hists.root` there, exactly where
the daemon would have put them, so the website shows the subrun. The log's
`[nearline] conditions json /home/pinky/bt2026/conddb-snapshots/<UTC time>`
line, and the `"conditions"` field of the `.py`, record which snapshot was used.
A snapshot holds the constants as they were when it was taken; a constant
written to the database after it is not in it. The run database is not told
about a hand run: the job's row stays `FAILED` until step 4.

### 4. Requeue the failed jobs once the database is back

There is no requeue command. A job is requeued by setting its row back to
`PENDING`, which the daemon then claims like a new one. First list what failed
(the run numbers are MIDAS run numbers; the password of the run database role
`readonly` is `readonly`, see `docs/DEPLOY-pinky-rundb.md`):

```bash
psql -h localhost -U readonly -d pioneer -c "
  SELECT j.id, r.midas_run_number, j.status
  FROM state.postproc_job j JOIN state.midas_run r ON r.id = j.midas_run_id
  WHERE j.job_type = 'nearline' AND j.status = 'FAILED'
    AND r.midas_run_number BETWEEN <first run> AND <last run>
  ORDER BY j.id"
```

Check the list: these are the jobs that failed at initialize and the ones that
failed to start. (A row left `CLAIMED` belongs to a daemon that stopped in the
middle of starting a job; requeue it the same way, with `'CLAIMED'` in place of
`'FAILED'`, only while the daemon is stopped.) Then requeue those rows, as the
role the daemon writes statuses with (`bot`, password `bot`, `daemon.py:37-38`;
`readonly` cannot write):

```bash
psql -h localhost -U bot -d pioneer -c "
  UPDATE state.postproc_job j SET status = 'PENDING'
  FROM state.midas_run r
  WHERE r.id = j.midas_run_id AND j.job_type = 'nearline'
    AND j.status = 'FAILED'
    AND r.midas_run_number BETWEEN <first run> AND <last run>"
```

The daemon picks them up within a few seconds, as many at a time as
`/Nearline/config/Num parallel jobs`. Subruns processed by hand in step 3 are
processed again, now from the database, and their files are overwritten; that
is intended, because the database is the record. Watch the first few turn
`DONE` on the RunDB page, and check one log for `layer=db` lines.

If the database will stay down for a long time, the expert can run the daemon
from the snapshot instead: restart it with
`NL_CONDITIONS=json:$HOME/bt2026/conddb-snapshots/latest` in its environment (see
*Via the daemon*), then requeue as above. Every file it produces then records
the snapshot directory. Undo it (unset, restart) as soon as the database is back.

## Via the daemon

`GaudiJob.format_config_file()` calls `render_job()` on `nearline_job.py` and
writes the result into the **output** directory as `<filebase>.py`;
`build_command()` is then just `gaudirun.py <that file>`. The rendered file is
not a shim around the job — it *is* the job, every setting and every line of
assembly included, with the twelve placeholders filled:

| field | what the daemon puts there |
|---|---|
| `in_file`, `out_file` | absolute paths; `evt_max` is `-1`, so a stray `NL_EVTMAX` in the daemon's environment cannot truncate a run |
| `conditions` | the conditions source, resolved at render time: `db:` and the explicit, password-free conninfo the job's default service (or the daemon's `NL_CONDITIONS`) names in the daemon user's `~/.pg_service.conf`, or `json:` and an absolute directory, or `json` |
| `conditions_dir` | `NL_CONDITIONS_DIR` **of the daemon's environment**, baked in at render time. Empty means the job's own default |
| `rendered_at`, `rendered_by` | UTC timestamp to the second, and `user@host` |
| `job_source`, `job_git` | the job file it was rendered from, and `git describe --always --dirty` of it — so a file says which version of the job made it, dirty tree included |
| `job_id`, `run_id` | the run database's own ids for this piece of work |
| `light` | `"1"` when the daemon was started with `--light`, else `"0"` (see *Light mode (pinky)*) |

The last three rows are also printed by the banner at startup — `[nearline]
rendered <when> by <who> job=<id> run=<id>` and `[nearline] source <path> @
<describe>` — so a log says as much about its provenance as the file does.
Because the rendered file reads nothing from the environment, it stays valid
after the daemon is restarted, after the conditions tree moves, and on a
machine that never had the daemon's variables set: it is the record of what
processed that run, and re-running it is `gaudirun.py run00175.py`.

The file the daemon registers in the run database's `state.file_list` for
each job is the one the job writes: `<filebase>.root` (the RNTuple) for the
full job, `<filebase>_hists.root` for the light one. The database splits a name
on its first dot, so both rows have `fileext` `root` and differ in `filebase`
(`runNNNNN_SSSSS` against `runNNNNN_SSSSS_hists`). The merge job and the tuning
loop read both kinds: a filebase already ending in `_hists` names the histogram
file itself (`hists_file_name()` in `render.py`).

The daemon's options:

| option | default | what it does |
|---|---|---|
| `--midas-client` | `NearlineDaemon` | MIDAS client name; also the `/Programs/<client>` entry the Start command is written to |
| `--midas-host` | `$MIDAS_SERVER_HOST` or `localhost` | MIDAS server |
| `--midas-expt` | `$MIDAS_EXPT_NAME`, or the only experiment in `$MIDAS_EXPTAB` | MIDAS experiment |
| `-j`, `--jobs` | none | nearline jobs in parallel, written to `/Nearline/config/Num parallel jobs` when given. Without it the ODB value stands; the first start, which creates `/Nearline/config`, writes `3` |
| `--light` | off | run the light job (pinky); see *Light mode (pinky)*. Recorded in the Start command |

The Start command carries `--midas-client`, `--midas-host`, `--midas-expt` and,
when given, `--light`, but never `-j`. The number of parallel jobs is
`/Nearline/config/Num parallel jobs`, and the daemon writes it only on the first
start (`-j`, or `3` without it) and whenever it is started by hand with `-j`. A
restart from the MIDAS Programs page, or by hand without `-j`, keeps whatever
the ODB says, so set it there to change it for good.

**`dry_run_all_jobs = True` at `jobs.py:18`** means every job today only prints
its command and sleeps. It has to be flipped to `False` for an end-to-end test.
Note that `format_config_file()` renders even in dry-run, so the `.py` appears
next to the outputs either way.

The daemon process needs `gaudirun.py` on `PATH`, the build's generated `Conf`
modules on `PYTHONPATH`, `NL_CONDITIONS_DIR` pointing at a checkout of
`reco_testbeam/conditions`, and, for the database, the `pioneer-conditions`
service in its user's `~/.pg_service.conf` with the password in `~/.pgpass`.
It needs no `NL_CONDITIONS`: the database is the job's own default, so a daemon
restarted from the MIDAS Programs page, which has none of the `NL_*` variables,
still reads the database. On pinky the repo is at
`/home/pinky/bt2026/beamtime2026_pie5` and **there is no `/simulation`**, so the
default `CONDITIONS_DIR` is wrong there: `NL_CONDITIONS_DIR` is not optional,
because the ODB specs are read from it in both modes. It is baked into every
file the daemon renders, which is the other half of the reason a pinky-rendered
job re-runs correctly anywhere the conditions tree is at that path.

To run the daemon's jobs from JSON for a while (the rollback), start it with
`NL_CONDITIONS=json:<dir>` in its environment; each job it renders then records
`json:<dir>`. Unset it and restart to go back to the database.

**At startup the daemon announces its conditions source** as a MIDAS message
(and on stdout), resolved exactly as every job it renders will resolve it:
`Nearline daemon: conditions from the database host=... port=... dbname=...
(the job's default)`. Anything else is sent as a MIDAS **error**: a JSON source
(`... WARNING: conditions from JSON <dir>, NOT from the database
(NL_CONDITIONS in the daemon's environment) ...`), which usually means an
`NL_CONDITIONS` leaked into the environment the daemon was started from, or a
source that does not resolve (a missing service), in which case every job would
fail to start. Read that message after every restart.

A render that fails (for example a service the daemon's user does not define)
makes the job fail to start: the daemon sends a MIDAS error "Job N failed to
start: ..." and marks the job's row `FAILED`, so it shows and is requeued like
any failed job (see *Conditions DB down*, step 4). Before, such a row stayed
`CLAIMED` for good.

## The tuning loop

`tuning.py` is the daemon's side of the loop with the beam-tuning service; the
daemon calls it and the command line above runs the same functions.

1. **A proposal becomes one run.** Every mainloop iteration, while `MiniTwin
   enable` is on, the daemon asks the service for a proposal newer than the
   last one seen. A new one is written as its row of the `MiniTwin updates`
   table times one `target_position` config (`MiniTwin target config`, the
   stage centre by default), in a sequence with `on_complete = mt_add`. The
   run requests the events the proposal's `run.stop` asks for
   (`{"kind": "events", "value": N}`, at most `MiniTwin max events`), else
   10^6, with a message saying why. No five-point scan, no merge. Each knob of the proposal goes into the
   row under its run-database column, from the service's `knobs.columns`
   (`GET /v1/config`); while that map cannot be fetched, is empty, or lacks a
   knob of the proposal, the proposal is not taken (one MIDAS error, asked
   again every iteration). Knob names are never used as columns.
2. **The run is taken.** The sequencer runs it; the daemon's nearline jobs
   process every subrun as usual. The run database marks the sequence
   `RUNSDONE` once the run and every nearline job of it are `DONE`.
3. **The context is posted.** The daemon claims the sequence and posts its
   context at once (after `MiniTwin post delay` seconds when that is set
   above 0, to let the service's file mirror catch up; the daemon keeps
   working meanwhile). The context lists every subrun's
   `run<N>/<filebase>_hists.root` (a light job's row already names that file),
   with `MiniTwin local prefix` replaced by `MiniTwin remote prefix` (the
   service reads piana's mirror of the output tree), role `hist_root`; the
   three MuPix maps (`miniTwinInterface.miniTwin_histograms`: x-x', y-y',
   x-y) summed with ROOT over those subruns' files on this machine, rebinned
   to 64 x 64 and sent inline -- they are the measurement, so the loop does
   not wait for the mirror -- with the axes read from the histograms and
   `names`, `source` ("daemon"), `n_files` and `rebin` saying where they come
   from. When they cannot be made (no ROOT, unreadable files, empty maps,
   inconsistent axes) the context goes out with its files only and a MIDAS
   error: the step's measurement then depends on the mirror; the
   knobs (Demand) and readback (Measured) of the type 1/4/5 channels in the
   first subrun's `beamline` header; the step (`responds_to`, `step_id`,
   `attempt`, `plan`) when the sequence is the one the active step was
   scheduled in. The sequence becomes `DONE` and the step is closed only once
   the service has taken the context. A context the service cannot be reached
   for stays queued in the daemon and is retried, with the sequence left
   `CLAIMED`. The step a sequence belongs to is taken when the daemon claims
   it and recorded under `/Nearline/MiniTwin/Pending/<seq id>/` (removed once
   delivered), so a proposal taken while it waits out the post delay, or a
   restart, does not strip its `responds_to`. The queue is in memory: a
   restarted daemon puts every `CLAIMED` `mt_add` sequence whose step belongs
   to the current proposal (or is the active step) back into the post delay
   and posts it again (the service recognises a repeat by its context id);
   any other `CLAIMED` sequence is named in one MIDAS message and left for
   `post --run N`. A context the service refuses (a 4xx about its content) is
   dropped: the sequence is `FAILED`, with a MIDAS error and a `failed`
   report. So is a context that could not be built, one that failed 20 times
   while the service answered other calls (after 5 failures in a row it is
   moved behind the others), and one pushed out of a full queue. A context
   naming a knob the service does not know, for instance after the service
   switched to another beam file, is refused with a permanent 400 and
   dropped this way; post it again by hand once the beam files agree.
4. **Progress is reported.** While a step is active the daemon posts
   `beamtune.daq/v1` reports to `POST /v1/daq`, at most every 10 s and only
   when something changed: `scheduled`, `running` (events sent / requested),
   `nearline` (subruns done / total), `posted`, `failed` with the reason, and
   `paused`. A failure to report is logged and never stops the daemon.

**Reply check:** the id of every context the service takes is kept in
`/Nearline/MiniTwin/Last context id`. A new proposal carries `in_reply_to`,
the context the service fed its backend before computing it, and what became
of it (`outcome`). The daemon compares the two: `ok` when they agree, `none`
when the proposal answers no context (a kick, or a service without the field),
`mismatch` otherwise, with a MIDAS warning naming both. Outcome `retake` gives
an info message, `failed` (the step was given up) an error, `off_plan` a
warning. The result goes into the `scheduled` report as `reply`
(`expected`, `got`, `outcome`, `ok`) and into `schedule --dry-run`. It never
stops a proposal from being scheduled: the service decides what runs next.

**Exposure:** every context carries `measurement.exposure`, so the service can
normalise rates by run time when the proton-current normalisation is missing:
`{"seconds", "wd_events", "per_run": [{"run", "seconds", "wd_events", "bor",
"eor", "time_source", "complete"}], "source": {"seconds", "wd_events"}}`. The
daemon records the active step's run in its own transitions: at the start
(sequence 600, after the frontends reset their statistics at 500)
`/Runinfo/Start time binary` and `/Equipment/WDWaveforms/Statistics/Events
sent`, at the stop (sequence 900) `/Runinfo/Stop time binary` and the same
counter, under `/Nearline/MiniTwin/Active step/` (keys below). A run's
`seconds` is stop minus start, from begin to end of run, so it includes any
time the run was paused; `wd_events` is the counter at stop minus at start;
`time_source` is `odb`. Only a run the daemon did not record (it was down at
a transition, or `post --run N` of an older run) takes its times from the
earliest BOR and latest EOR rows of `logs.slow_control` in the run database
(`time_source` `run_db`), with no events; that table has no index on the
live database, so the lookup is cancelled after 5 s (the index on
`(midas_run_number, reason)` in `rundb/db_viewer.sql` would make it fast).
A run with subrun files left out of the context has `complete` false and
null `seconds` and `wd_events`, so the exposure only describes data in the
histograms. A value that is missing, not positive seconds, or a counter that
went down is null, and a total over runs with a null is null; what is
missing is said in one MIDAS info message and the context is posted
regardless.

**Pause:** set `/Nearline/config/MiniTwin enable` to `n`. It is read every
iteration: the daemon stops asking for proposals and reports `paused` once. A
run already scheduled is still taken and its context still posted. Set it back
to `y` to resume.

**Restart:** the last proposal id and the active step are kept in the ODB
under `/Nearline/MiniTwin`, so a restarted daemon neither schedules the
outstanding proposal again nor forgets which step the run in flight belongs
to. The proposal id is stored before the run is written: a proposal whose
scheduling failed (MIDAS error, `failed` report) is not retried by itself;
retake it with `schedule --since <id - 1>`. The daemon only ever raises the
stored id; `--since` lowers it for that one command, not in the ODB. If the
service's own last proposal is below the stored id (it was restarted with a
new state directory), its proposals are ignored and the daemon says so once,
as a MIDAS error naming both numbers; set `/Nearline/MiniTwin/Last proposal id`
to the service's number by hand to take them. A running daemon takes a value
lowered by hand at once (MIDAS message "Last proposal id lowered by hand ...");
no restart is needed.

**A failed run or nearline job:** the run database sets the sequence
`FAILED`, the daemon reports `failed`, and the step stays active; nothing is
posted. Reprocess the failed subrun (re-queue its nearline job, or run
`process.py` on it). Once every nearline job of the run is `DONE` the run
database moves the sequence on to `RUNSDONE` and the daemon posts it with its
step as usual; if it stays `FAILED`, post it by hand with
`python -m pioneer.nearline.tuning post --run <N>`, which sends the step and
closes the sequence. A `FAILED` sequence can also move back to `RUNSDONE` on
its own when a later job of its run (backup, remote copy, cleanup) changes
status, and is then posted again; that is expected, and the service
recognises a repeated context by its id.

| ODB key | default | what it is |
|---|---|---|
| `/Nearline/config/MiniTwin URL` | `http://127.0.0.1:8420` | the service (read once at start-up) |
| `/Nearline/config/MiniTwin updates` | `pim1_epics` | the config table a proposal's row goes to |
| `/Nearline/config/MiniTwin enable` | `y` | the pause switch, read every iteration |
| `/Nearline/config/MiniTwin target config` | `1` | `config.target_position` id of the one run per proposal (id 1 = seq 1, the centre (0, 0)) |
| `/Nearline/config/MiniTwin local prefix` | `/home/pinky/nearline/` | start of a file path as the daemon writes it |
| `/Nearline/config/MiniTwin remote prefix` | `/home/pioneer/nearline/histograms/` | what replaces it in a posted path |
| `/Nearline/config/MiniTwin max events` | `10000000` | most events a proposal's `run.stop` may request; more is an error and capped |
| `/Nearline/config/MiniTwin post delay` | `0` | seconds between claiming a finished sequence and posting it (`post` by hand does not wait). The default was 60 before; an ODB that already has the key keeps its value, so set it to 0 by hand to stop waiting |
| `/Nearline/MiniTwin/Last proposal id` | `0` | newest proposal id seen |
| `/Nearline/MiniTwin/Active step/Proposal id` | `0` | proposal of the run in flight; `0` = none |
| `/Nearline/MiniTwin/Active step/Step id`, `Attempt`, `Plan` | `""`, `-1`, `""` | the proposal's plan step; empty / `-1` = not known |
| `/Nearline/MiniTwin/Active step/Seq id` | `0` | the run-database sequence of the run in flight |
| `/Nearline/MiniTwin/Active step/Recorded run` | `0` | MIDAS run the next four keys belong to; `0` = none. All five are reset with every new step |
| `/Nearline/MiniTwin/Active step/Run start`, `Run stop` | `0.0`, `0.0` | Unix time of the run's start and stop transitions (`/Runinfo/Start`, `Stop time binary`); `0` = not known |
| `/Nearline/MiniTwin/Active step/Events at BOR`, `Events at EOR` | `-1`, `-1` | WaveDREAM events sent at the start (after the reset) and at the stop; `-1` = not known |
| `/Nearline/MiniTwin/Last context id` | `""` | context id of the last context the service took |
| `/Nearline/MiniTwin/Pending/<seq id>/Proposal id`, `Step id`, `Attempt`, `Plan`, `Recorded run`, `Run start`, `Run stop`, `Events at BOR`, `Events at EOR` | — | the step a claimed sequence belongs to, until its context is delivered |

The keys this loop added are created with their defaults when the daemon
starts and are never overwritten.

## What fails early, on purpose

`check()` runs before a single Configurable is touched and reports **every**
problem it finds in one message, rather than the first. What it cannot see is
whether the database answers: that is found at `initialize()` (about 20 s),
where an unreachable server fails the job (*Conditions DB down*).

1. `NL_MIDAS` or `NL_OUT` unset — the message shows **both** ways in: the interactive `NL_MIDAS=... NL_OUT=... gaudirun.py nearline_job.py`, and `python -m pioneer.nearline.process <midas file> --out-dir DIR`, which needs no environment at all. (A rendered file cannot reach this one: its paths are filled in.)
2. `NL_MIDAS` does not exist on disk.
3. The directory of `NL_OUT` is not an existing directory.
4. A resolved conditions container or ODB spec does not exist — one line per file, naming the absolute path. In db mode only the ODB specs and `ODB_OVERRIDES` are files. Next to it: `CONDITIONS` does not parse, or names a service no service file defines (the message lists the files searched and gives the `process.py --conditions json:<snapshot dir>` line), or, unrendered, `pioneer` is not on `PYTHONPATH` to expand the service. In db mode also: a container setting changed from its committed value, or a `CONDITIONS_DIR` holding `bt2026_*.json` that is not a `reco_testbeam/conditions` tree (use json mode for either). No message echoes a conninfo: only host, port and dbname are ever printed.
5. Both halves off (`WD_ENABLED` and `PSM_DECODE`): nothing would decode.
6. `PSM_RECO` without `PSM_DECODE`: nothing would produce `/Event/muquad` and `/Event/mutrig`.
7. `PSM_DECODE` with an empty `PSM_GEOMETRY_BASE`: no `GeoHeader`, and the decoder throws on hit one.
8. `PSM_GEOMETRY_BASE` not in `GEOCOND:<table>` form.
9. `PSM_MUPIX_MONITOR` without `PSM_DECODE`: nothing would produce `/Event/muquad`, and the monitor takes the chip footprints from the `PIGeometrySvc` that `PSM_DECODE` creates.
10. `PSM_MUPIX_PIXELS_PER_BIN` below 1: it is how many pixels share one bin of a hit map.
11. `PSM_MUPIX_DT_RANGE_NS` or `PSM_MUPIX_DT_BINS` not positive: a half-width and a bin count.
12. `PSM_MUPIX_WINDOW_NS` not positive: a non-positive half-window pairs nothing at all.
13. `PSM_MUPIX_EXPANDED_RANGE_MM` or `PSM_MUPIX_CENTRAL_SLOPE_MRAD` not positive: both are half-widths of symmetric axes.
14. `PSM_SMA_MONITOR` without `PSM_DECODE`: nothing would produce `/Event/mutrig`, from which the SMA calibration layer makes the `/Event/mutrig_cal` the monitor reads, and the monitor takes the raw `MUTRIG` channel map from the `PIGeometrySvc` that `PSM_DECODE` creates.
15. `PSM_SMA_HITS_PER_EVENT_MAX` below 1: it is the top of an axis counting hits per event.
16. `PSM_SMA_DEGENERATE_TOT_SHARE` or `PSM_SMA_MARKER_TOT_SHARE` outside `(0, 1]`: both are shares of one counter's hits.
17. `PSM_SMA_COARSE_SHIFT` neither `None` nor an integer 0-18: above 18 the coarse field no longer pins the fine field's wrap.
18. `PSM_PIXEL_MASK_TAG` neither `None` nor a non-empty string: it names a tag of `mupix_pixel_mask`.
19. `PSM_TIMEWALK_CORRECTION_TAG` neither `None` nor a non-empty string: it names a tag of `mupix_timewalk`.
20. `PSM_TWC_NTUPLE` not one of `"both"`, `"corrected"`, `"raw"`.
21. `PSM_SMA_CAL_NTUPLE` not one of `"both"`, `"calibrated"`, `"raw"`; `PSM_SMA_PAIR_WINDOW_NS` not a number in (0, 1000]; `PSM_SMA_TIME_SOURCE` not `"tot"` or `"nim"`; `PSM_SMA_NIM_ONLY_TOT` not a number 0-255; `PSM_SMA_OFFSET_OVERRIDE_NS` not a dict of int → number.
22. `LIGHT`, `PSM_PIXEL_MASK`, `PSM_TIMEWALK`, `PSM_TIMEWALK_CORRECTION`, `PSM_SMA_WIDE_DT` or `PSM_SMA_NIM_PAIRING` not a bool (`NL_LIGHT` or a rendered `light` other than `1`/`0` ends up here): a string such as `"False"` is true in Python and would switch the setting on.
23. `PSM_TIMEWALK` on and `PSM_TIMEWALK_DT_MIN`/`_MAX`/`_BINS` not an axis: max not above min, or bins not an integer 1-8192.
24. `PSM_RF_CHANNEL` or `PSM_CURRENT_CHANNEL` neither `None` nor an integer 0-15, or the two equal: the SMA word's channel field is 4 bits, and the decoder takes the RF channel first, so the current pulses would become RF pulses.
25. A `GEOCOND` base with an empty `PSM_GEOMETRY_FILES` (json mode): nothing supplies the table it names.
26. `COND:isel` in `PSM_GEOMETRY_TRANS` without `bt2026_isel.json` in `ODB_SPECS`.
27. json mode, `PSM_PIXEL_MASK` on (with `PSM_DECODE`) and a `PSM_GEOMETRY_FILES` without `bt2026_psm_readout_map.json`: nothing would supply the `mupix_pixel_mask` table, and the decoder stops at `initialize()`.
28. json mode, `PSM_TIMEWALK_CORRECTION` on (with `PSM_DECODE`) and a `PSM_GEOMETRY_FILES` without `bt2026_psm_readout_map.json`: nothing would supply the `mupix_timewalk` table, and the correction layer stops at `initialize()`.
29. `PSM_WEIGHT_STRATEGY` not `0`, `1` or `2`.
30. `PSM_WEIGHT_STRATEGY >= 1` without both `PSM_DECODE` and `PSM_GEOMETRY_BASE`: no `PIGeometrySvc` to take the L1/L2 plane footprints from.
31. `PSM_WEIGHT_STRATEGY >= 1` without `COND:isel` in `PSM_GEOMETRY_TRANS`: every run would be treated as sitting at the design stage position.
32. Exactly one of `WD_ALIGN_TABLE` / `WD_ECAL_TABLE` set: `PIWDCalibrator` needs both.
33. `WD_ENABLED` with an empty `WD_RF_TABLE`: `PIWDRFPhase` runs first in `WDAnalysisSeq` and `PIWDWaveformAnalysis` reads `/Event/wd_rf_phase`, so the RF table cannot be empty.
34. json mode, `WD_ROLE_TABLE` set with an empty `WD_CONDITIONS_FILES`: nothing would supply the `wd_channel_map` table.
35. `WD_CHANNEL_SETTINGS_TABLE` set with an empty `ODB_SPECS`: only the begin-of-run ODB dump serves `wd_channel_settings`.
36. `WD_CAL_CHANNELS` not a subset of `WD_CHANNELS`: they would have no features to calibrate.
37. `WD_SCALER_MONITOR` without `WD_ENABLED`: nothing would produce `/Event/wd_scalers`.
38. `WD_SCALER_TIME_BIN_S` not positive or not below `WD_SCALER_TIME_MAX_S`, or a serial listed twice in `WD_SCALER_BOARDS`.
39. `WD_RF_REFINE` on with `WD_RF_REFINE_POINTS` below 2: a scan needs at least 2 points.
40. `PSM_PHASE_SPACE_BINS` not a positive multiple of 64: the phase-space histograms would not rebin onto the 64-bin minitwin export exactly.
41. `PSM_PHASE_SPACE_POS_RANGE_MM` or `PSM_PHASE_SPACE_SLOPE_RANGE_MRAD` not positive: both are half-widths of a symmetric axis.
42. `PSM_L_WINDOW_BEFORE_NS` and `PSM_L_WINDOW_AFTER_NS` giving an empty L-hit window: no tracklet would get an L pair.
43. `PSM_DROP_CROSSTALK_GHOSTS` on without `PSM_DECODE` and a `PSM_GEOMETRY_BASE`: the ghost rule recovers each hit's column and row from the chip placement the `PIGeometrySvc` serves.
44. `OUTPUT_LEVEL` not one of `DEBUG`, `ERROR`, `INFO`, `WARNING`.
45. `PSM_GEOMETRY_TAG` neither `None` nor a non-empty string: it names a tag of the geometry table.

## The phase-space histograms are minitwin input

The PSM phase-space histograms are not free-form monitoring plots. They are the
input the minitwin model consumes, so their axes are a contract, held by the
three `PSM_PHASE_SPACE_*` settings and by `check()`:

| histogram | what fills it | binning | rebin to the model's 64 |
|---|---|---|---|
| `PIPSMDelayedCoincidence/{xy,xxp,yyp}` (+ `_w`) | tagged prompts | 320 x 320 | `Rebin2D(5, 5)`, exact |
| `PIPSMAllTrackReco/{xy,xxp,yyp}` (+ `_w`) | every prompt-like tracklet | 64 x 64 x 6 (stop layer) | none needed; sum the stop axis away |
| `PIPSMMuPixMonitor/{xy,xxp,yyp}_mt` | every L1/L2 MuPix coincidence, stage-weighted like the `_w` views (sum of w, Sumw2; `PSM_WEIGHT_STRATEGY`, `PSM_POSITIONS_MM`, `PSM_WEIGHT_MARGIN_MM`): the maps the beam-tuning feed sends, `miniTwinInterface.miniTwin_histograms` | 320 x 320 | rebin 5, exact (`serialise_hist`) |

All three are on the minitwin det10 window — x, y over +-37 mm and x', y' over
+-950 mrad — which is `beamline-simulation/psm/psm_scan_config.py`
(`X_WINDOW`, `A_WINDOW`) and `minitwin/data/axes_v8.yaml`. The offline producer
`analysis/josh/psm_scan_hists.py` books the same 320 bins on the same windows,
so a nearline histogram and an offline one are comparable bin for bin.

`counters` carries the exposure: bin 1 `n_frames`, bin 2 `n_prompt`, bin 3
`n_tagged`, mirroring the `counters` histogram that offline producer writes. The
bins are deliberately unlabelled — `PIHistogramSvc` hands an algorithm the
per-slot clone, never the prototype it merges into, so a label set at
`initialize()` would reach one slot only. `n_frames` counts events that passed
`PSMRecoSeq`'s `/Event/mutrig_cal` gate, which is a smaller number than the offline
`df.Count()` over every rec entry.

**x' and y' are `1000 * (x2 - x1) / PSM_DISTANCE_L12`, in mrad, everywhere.**
`PIPSMSimpleTrackReco` used to fill `(x1 - x2) / d` in radians, mirrored and a
factor of 1000 off from every other consumer — the nearline site's cubes,
`PIPSMDelayedCoincidence`, `PIPSMRecoCore` and `psm_scan_config.py`. That is
fixed, so the two `xxp` histograms in this job now mean the same thing and only
differ in their selection and binning.

## The merge step normalises by the proton current

`combine_files.py` sums the histograms of each run's sub-runs, divides each
run by the proton current delivered while the SMA was live, and adds the runs
together. The normalisation comes from one source for the whole join:

- **WaveDREAM** when every run being joined has
  `histograms/PIWDScalerMonitor/proton_current_counts`,
  `.../proton_current_seconds` and
  `histograms/PIPSMSMACalibration/sma_live_seconds` non-empty: the mean
  scaler rate (counts / seconds) times the SMA live time (the summed spans
  of the SMA readout frames). The printed line gives each run's three
  numbers and its factor;
- else **SMA** when every run has `histograms/musip/current` (the pulses of
  the SMA proton-current channel, counted only inside recorded frames);
- else nothing.

Both sources are rate x SMA live time of the same ~220 kHz signal, so factors
from the two mean the same thing and stay comparable with joins made before
the current left the SMA. A join still never mixes them; the line it prints
names the source. Without a source it prints one warning and every run stays
raw counts (factor 1), so the runs remain comparable with each other but not
per proton. A run lacks a source when its map has no such input (no WaveDREAM
`current` input before the current was cabled to it, no SMA current channel
once it left the SMA), when a sub-run's file lacks one of its histograms (a
note names the file, whichever sub-run it is), when one is empty
(`PSM_DECODE = False`, `WD_SCALER_MONITOR = False`), or when its files predate
`sma_live_seconds` (those fall back to the SMA pulses where they have them).
Only the current histograms of the source used are kept in the output, by
`combine_runs` and `merge_sub_runs` alike.
`MergeJob` feeds it the `<filebase>_hists.root` files
(`jobs.py`), and the loop adding runs together handles any number of runs.
It builds that list from the run's nearline `root` rows in the run database,
which a full job registers as `<filebase>.root` and a light job as
`<filebase>_hists.root`; both map to the same histogram file
(`merge_input_files()` in `jobs.py`), and a file named by more than one row —
a subrun processed again, or by both kinds of job — is listed once, because
the merge adds up what it is given.

## Output size

`PIAOutputStream` persists **everything** registered on the TES, raw DRS traces
included, so the RNTuple comes out roughly the size of the MIDAS input: the
20k-event lab run 193 gives a 248 MB RNTuple next to a 19 kB histogram file,
and writing it dominates the job's time. `WD_CHANNELS` is not a lever on that:
the waveforms are persisted either way, and widening it from 6 to 16 channels
adds about 1 % to the file.
`WRITE_NTUPLE = False` is a pure monitoring pass and the right
setting for a shift display; the histogram file is unaffected. The light job
(`LIGHT`) is that pass plus three histogram sets switched off.
The histogram file is small but no longer negligible on a PSM run: the six
320-bin phase-space TH2Ds are 7.4 MB uncompressed between them, and 3000 events
of `fake_run00913_mutrig.mid` measured 172 kB on disk (33 kB before they were
widened), because the arrays are mostly exact zeros and compress hard. Each Hive
slot clones every prototype, so that 7.4 MB is per slot — irrelevant under the
sequential event loop this job runs, but not free if it ever goes concurrent.
`PIPSMMuPixMonitor` adds about 6.4 MB uncompressed on top of that — 4.1 MB of it
one bin per pixel over eight chips and two planes, which is the price of seeing
a single dead column, and `PSM_MUPIX_PIXELS_PER_BIN` is the knob that gives it
back. The three fixed-axis track plots are about 0.4 MB of the rest; the three
`_mt` maps on the 320-bin minitwin grid add 1.2 MB, and the pixel-matched axes of
`track_xy`, `xxp` and `yyp` (258 x 252, 258 x 149, 252 x 145 instead of 128 x 128)
about 0.37 MB. It compresses at least as hard as the phase space does: on a 400-event
slice of run 166 the whole histogram file went from 48 kB to 101 kB with the
module on. The per-plane `L<n>_mult` axes account for most of the rest, and they
run to 16383 hits per event on purpose — a MuPix readout frame is not one
particle, and run 165 puts over 6000 L2 hits in a single frame.
The MuPix hits are written once by default, timewalk-corrected
(`_Event_muquad_twc`). How much they weigh depends on the MuPix rate: on a
quiet subrun of run 459 one copy was 223 kB of a 61 MB file, on a busy one of
run 790 about 34 MB of a 146 MB file. `PSM_TWC_NTUPLE = "both"` adds the raw
`_Event_muquad`, and costs more than the copy's own pages suggest: RNTuple
stores a page that repeats an earlier one byte for byte only once, which the two
copies used to share for every column the correction does not touch, but the
corrected copy is now time-ordered on the corrected times, so its pages no
longer repeat the raw ones (+25 % file on that busy subrun).
The SMA hits are written once by default, raw (`_Event_mutrig`):
`PSM_SMA_CAL_NTUPLE = "both"` adds the time-ordered `_Event_mutrig_cal`, a
second collection of the same hits and about the same size.
`NTUPLE_RULES = ["drop *", "keep /Event/wd_hits", ...]` keeps a shrunken file,
and selection happens once at `initialize()`, so the rules cost nothing per
event. There is nothing to tune in the writer itself: it is fixed at ZSTD-1
(`Compression = 501`, about 40 % less CPU than ROOT's default ZSTD-5 for about
8 % more disk) with one reused entry for the whole job.
