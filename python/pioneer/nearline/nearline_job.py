"""Gaudi options file exec'd by gaudirun.py. NOT a module -- do not import it.

One MIDAS file in, one RNTuple and one histogram file out, both detector systems
in one pass. A joined run file carries WaveDREAM banks (WDEH, DRSV, DRST and the
serial-keyed scaler banks) next to musip banks (H000); the names are disjoint by
agreement, registered in midas_files/wavedream-scalar-readout/docs/REGISTRY.md.

    +-- PIMidasSelector ------- one .mid/.mid.lz4, every event, no bank filter
    |
    +-- PIMidasDecoder -------- PITMidasWaveDream  -> /Event/wd_*
    |                           PITMidasMusip      -> /Event/muquad, mutrig, rf
    |
    +-- PSMSMACalSeq ---------- gated on /Event/mutrig; runs whenever PSM_DECODE
    |     PIPSMSMACalibration  -> /Event/mutrig_cal, the decoder's SMA hits
    |                             aligned, each counter's TOT word and NIM
    |                             copy paired into one hit, in time order
    |                             (/Event/mutrig is readout order); every SMA
    |                             reader below reads it. /Event/sma_hits, the
    |                             sidecar index-parallel to it: both times,
    |                             both widths, raw indices, pairing flags
    |
    +-- PSMTimewalkSeq -------- gated on /Event/muquad; runs whenever PSM_DECODE
    |     PIPSMMuPixTimewalkCorrection -> /Event/muquad_twc, each pixel time
    |                             minus its chip's walk at its ToT, in time
    |                             order (the same hits as /Event/muquad with
    |                             the shipped empty table); the MuPix monitor
    |                             and the track reco read it
    |
    +-- PIWDSettingsSummary --- top level, no waveform needed -> WDSettingsHeader
    |
    +-- WDAnalysisSeq --------- gated on /Event/wd_waveform
    |     PIWDRFPhase          -> /Event/wd_rf_phase
    |     PIWDWaveformAnalysis -> /Event/wd_features   (consumes wd_rf_phase)
    |     PIWDCalibrator       -> /Event/wd_hits
    |
    +-- WDScalerSeq ----------- gated on /Event/wd_scalers
    |     PIWDScalerMonitor    -> histograms only: rate, threshold and FPGA
    |                             temperature per board from the scaler events,
    |                             and the proton-current counts the merge
    |                             step normalises by
    |
    +-- PSMMuPixSeq ----------- gated on /Event/muquad, reads /Event/muquad_twc
    |     PIPSMMuPixMonitor    -> histograms only: a hit map per MuPix chip and
    |                             per plane, and x/x', y/y' from an L1/L2 time
    |                             coincidence with no scintillator involved;
    |                             with PSM_TIMEWALK also reads
    |                             /Event/mutrig_cal (optional per frame) for
    |                             the all-pairs pixel-vs-S1..S5 timewalk
    |
    +-- PSMSMASeq ------------- gated on /Event/mutrig_cal
    |     PIPSMSMAMonitor      -> histograms only: rate, ToT and fine time per
    |                             counter, with no pixel hits or tracklets;
    |                             also reads /Event/rf (optional per frame;
    |                             absent on a run with no RF channel) for the
    |                             S1-gated RF phase and RF phase vs ToT per
    |                             counter
    |
    +-- PSMRecoSeq ------------ gated on /Event/mutrig_cal, which is also the
    |                             S hits; L hits /Event/muquad_twc
    |     PIPSMSimpleTrackReco   -> /Event/exp_all_tracks   (+ histograms);
    |                             with PSM_DECODE also reads /Event/rf for
    |                             each tracklet's S1 RF phase
    |     PIPSMPatternReco       -> /Event/exp_pattern
    |     PIPSMComputeWeight     -> /Event/exp_track_weights
    |     PIPSMDelayedCoincidence-> /Event/exp_tagged       (+ histograms)
    |
    +-- PIAOutputStream ------- RNTuple "rec" ; PIHistogramSvc -> *_hists.root

Two ways in, one code path:

    interactive  NL_MIDAS=... NL_OUT=... gaudirun.py nearline_job.py
    rendered     pioneer.nearline.render fills the placeholders in the block
                 below and writes the result next to the outputs as
                 <filebase>.py -- the daemon does this through
                 pioneer.nearline.jobs.GaudiJob, a shifter through
                 python -m pioneer.nearline.process <midas file>.

Rendered is the same file, so it is the same code path; there is no second
options file any more. The rendered copy is the complete job: gaudirun.py runs
it standalone, it ignores every NL_* variable, and re-running it reproduces the
run it came from no matter what the caller's environment says.

The settings are the block below; the prose explaining them is in README.md.
"""

import math
import os
from pathlib import Path

from Configurables import EvtDataSvc, EvtPersistencySvc, Gaudi__Sequencer
from Gaudi.Configuration import *
from shared.PiGaudiSharedSvcConf import (PIAOutputStream, PIConditionsSvc,
                                         PIDataModelSvc, PIHeaderSvc, PIHistogramSvc)
from reco_testbeam.pi_testbeam_servicesConf import PIGeometrySvc
from pi_midas.PIONEER_MIDAS_READERConf import (PIMidasSelector, PIMidasConversionSvc,
                                               PIMidasDecoder, PITMidasMusip,
                                               PITMidasWaveDream)
from reco_testbeam.pi_wdalgConf import (PIWDCalibrator, PIWDRFPhase, PIWDScalerMonitor,
                                        PIWDSettingsSummary, PIWDWaveformAnalysis)
from reco_testbeam.pi_psmalg_expConf import (PIPSMComputeWeight, PIPSMDelayedCoincidence,
                                             PIPSMMuPixMonitor, PIPSMMuPixTimewalkCorrection,
                                             PIPSMPatternReco, PIPSMSMACalibration,
                                             PIPSMSMAMonitor, PIPSMSimpleTrackReco)

# ===== RENDERED BY THE DAEMON (do not edit; the checked-in file carries placeholders) =====
# pioneer.nearline.render fills these with string.Template and writes the result next to
# the outputs as <filebase>.py. Left unfilled, they still read as their own placeholder
# text, which means "take the environment" and is how an interactive run works. A rendered
# file ignores every NL_* variable instead: the file beside the outputs is the whole record
# of what processed that run, including where its constants came from -- the conditions
# source as the renderer resolved it (a database as an explicit, password-free libpq
# conninfo, never a service name, or a JSON directory) and the conditions directory.
#
# Every placeholder sits inside a string literal, so the checked-in file is valid Python and
# runs unrendered. The render step substitutes safely and then refuses to write unless all
# twelve names below are gone, so only these twelve are special: a dollar sign written
# anywhere else in this file survives verbatim and can never break the daemon. Doubling one
# is what makes the render step collapse it to a single character.
_RENDERED = {
    "in_file": "${in_file}", "out_file": "${out_file}", "evt_max": "${evt_max}",
    "conditions_dir": "${conditions_dir}", "conditions": "${conditions}",
    "rendered_at": "${rendered_at}", "rendered_by": "${rendered_by}",
    "job_source": "${job_source}", "job_git": "${job_git}",
    "job_id": "${job_id}", "run_id": "${run_id}", "light": "${light}",
}
# Which of the two ways in this file is. rendered_at is the field the renderer always
# fills, and an unsubstituted placeholder still begins with the dollar sign that opens
# it, so that one character decides. chr(36) IS that dollar sign, written that way so
# that the only dollar signs in the unrendered file are the twelve placeholders above.
RENDERED = not _RENDERED["rendered_at"].startswith(chr(36))

# The conditions directory when nobody says otherwise. This is the bind mount inside the
# testbeam-midas container; on a host without /simulation (pinky) the daemon's
# NL_CONDITIONS_DIR is mandatory and is what a rendered file carries.
_DEFAULT_CONDITIONS_DIR = os.path.join(os.environ.get("PIONEERSYS"), "reco_testbeam/conditions")

# ===== SETTINGS =====
# --- Job -------------------------------------------------------------------
# Events to process; -1 is the whole file. NL_EVTMAX in the environment wins.
EVT_MAX = -1
# The light job, about a third of the CPU of the full one: histograms only. Meant for
# pinky once another host runs the full job (README, "Light mode"). On, it switches
# off WRITE_NTUPLE, PSM_TIMEWALK,
# PSM_SMA_WIDE_DT and PSM_SMA_DIAGNOSTICS after everything else has been read, so it
# wins over the block below and over an overrides file. The MuPix timewalk
# CORRECTION is not touched. The daemon's --light and process.py's --light render it
# in; NL_LIGHT=1 in the environment sets it for an interactive run.
LIGHT = False
# Gaudi verbosity for the whole job: "DEBUG", "INFO", "WARNING" or "ERROR".
OUTPUT_LEVEL = "INFO"
# Master switch for the WaveDREAM half: waveforms -> features -> RF -> hits.
WD_ENABLED = True
# Decode the musip H000 banks into /Event/muquad, /Event/mutrig and /Event/rf.
PSM_DECODE = True
# Run the tracklet chain on the decoded musip hits. Requires PSM_DECODE.
PSM_RECO = True
# --- Conditions ------------------------------------------------------------
# Where the constants come from, one of:
#   "db:service=NAME"  the conditions database, by libpq service name (~/.pg_service.conf,
#                      password in ~/.pgpass); "db:NAME" and bare "db" (pioneer-conditions)
#                      mean the same. "db:host=H port=P dbname=D user=U" names it directly.
#   "json:DIR"         the JSON containers below, from DIR (a snapshot of the database)
#   "json"             the JSON containers below, from CONDITIONS_DIR
# The database is the default. An unreachable server fails the job at initialize and
# the job never falls back to JSON by itself; the runbook "Conditions DB down" in
# README.md is what a shifter does then. The service name is expanded into the explicit
# conninfo before the job reaches Gaudi, so every ConditionsHeader names the real host.
# NL_CONDITIONS in the environment wins for an unrendered run.
CONDITIONS = "db:service=pioneer-conditions"
# Directory the ODB specs and ODB_OVERRIDES resolve against, whatever CONDITIONS says,
# and the JSON containers too under a bare "json".
CONDITIONS_DIR = os.environ.get("NL_CONDITIONS_DIR", _DEFAULT_CONDITIONS_DIR)
# Specs mapping subtrees of the begin-of-run ODB dump to conditions tables.
ODB_SPECS = ["odb/bt2026_runinfo.json", "odb/bt2026_wavedream_daq.json",
             "odb/bt2026_wavedream_scalers.json", "odb/bt2026_isel.json"]
# Tables resolved at initialize(), so a misconfigured job dies in the first second.
ODB_PRELOAD = ["runinfo", "wd_board_settings", "wd_channel_settings", "wd_scaler_names"]
# Run-indexed corrections applied to a private copy of the ODB tree; "" is none. A JSON
# container, and it is loaded in both modes: in db mode it is the one JSON file the job
# reads, and it wins over the database for the table it holds.
ODB_OVERRIDES = ""
# Write the resolved ODB tables into the output as a WDSettingsHeader.
SETTINGS_SUMMARY = True
# --- WaveDREAM -------------------------------------------------------------
# Board-local channels the waveform analysis produces features for; a channel left
# out has no leading edge and no charge anywhere downstream. All 16 inputs, because
# the board can transmit all of them (DRSChannelTxEnable 0x3ffff) and the logic
# copies are physics -- pi-stop and delayed-mu live there. A channel the board does
# not transmit costs nothing: no waveform arrives, so no feature is made, which is
# why listing all 16 is safe against a board configured for fewer. Where all 16 do
# arrive, widening 6 -> 16 costs about 1% of the RNTuple and a few percent of the
# job; the waveforms dominate the file and they are stored either way.
#
# Note this also widens the channel-agnostic ppamp, ppamp_vs_channel and le_time
# histograms from the scintillators to every cabled channel, so they now mix ~10 mV
# counter pulses with ~800 mV logic levels. ppamp_vs_channel is per channel and
# reads better for it; the two 1D ones become bimodal.
WD_CHANNELS = list(range(16))
# Channels PIWDCalibrator turns into physical hits; must be a subset of the above.
WD_CAL_CHANNELS = [0, 1, 2, 3, 4]
# Samples at the start of the trace averaged into the baseline.
WD_BASELINE_SAMPLES = 50
# Constant fraction of the pulse extremum defining the leading edge time.
WD_CF_FRACTION = 0.5
# Charge integration window in ns around the leading edge, (edge-PRE, edge+POST).
WD_INTEGRATE_PRE_NS = 5.0
WD_INTEGRATE_POST_NS = 40.0
# Monitoring histogram axes: amplitude upper edge in V, time upper edge in ns.
# WD_AMP_MAX bounds the PEAK-TO-PEAK excursion; the per-pulse amplitude below
# is a different, smaller quantity and has its own axis.
WD_AMP_MAX = 1.0
WD_TIME_MAX = 1100.0
# What each channel is cabled to (scint/nim/rf/current/spare), which is what
# decides who gets per-channel histograms and who is counted as a counter.
# "" books only the channel-agnostic histograms.
WD_ROLE_TABLE = "wd_channel_map"
WD_ROLE_TAG = ""
# Per-channel discriminator levels, from the begin-of-run ODB dump. Signed
# volts, and negative on bt2026 because the pulses are negative-going.
WD_CHANNEL_SETTINGS_TABLE = "wd_channel_settings"
# |amplitude| in V counting as a pulse where the recorded level is 0, i.e. the
# channel was never configured. A scintillator and a NIM level are an order
# apart, so one fallback for both would be wrong for one of them.
WD_SCINT_THR_FALLBACK_V = 0.024
WD_NIM_THR_FALLBACK_V = 0.1
# Refuse the hardware levels when the gain chain is not unity: TriggerLevel is
# referenced after TriggerGain, the recorded amplitude comes through
# FrontendGain. bt2026 runs both at unity, so this stays off and only warns.
WD_STRICT_THRESHOLDS = False
# Charge axis in V ns, sized from the measured pulse spectrum with headroom above
# its tail. The minimum is below zero because a window with no pulse integrates to
# a small negative number, and raising it to 0 buries the pedestal in the underflow
# bin. Re-measure when the gain chain or the counters change.
WD_CHARGE_MIN = -0.5
WD_CHARGE_MAX = 3.0
# |amplitude| axis in V. This is the extremum relative to the baseline, several
# times smaller than the peak-to-peak excursion WD_AMP_MAX bounds, so it gets its
# own axis; the value keeps the MIP peak around the middle of it.
WD_PULSE_AMP_MAX = 0.3
# Baseline axis in V. It reaches well below zero on purpose: a baseline far from
# the pedestal is a pulse landing inside the baseline window, and seeing that tail
# is the whole point of the plot. Tighten it and the pathology becomes underflow.
WD_BASELINE_MIN_V = -0.5
WD_BASELINE_MAX_V = 0.1
# Baseline-RMS axis in V, an order of magnitude above the noise floor: that
# resolves the floor in the low bins and still leaves the tail -- the small
# percentage of traces with a pulse inside the baseline window -- on the axis.
WD_BASELINE_RMS_MAX_V = 0.02
# RF phase bins; 48 matches the nearline site's energy_vs_rfphase so the
# histogram and the derived cube can be compared directly.
WD_PHASE_BINS = 48
# Upper edge in V of the RF fit-residual histogram, which is the one plot saying
# whether the fitted sine actually describes the trace. The residual sits near the
# noise floor when the frequency in wd_rf is right and grows quickly when it is not,
# so the axis is deliberately tight: widen it and a bad fit stops standing out,
# narrow it and the interesting tail disappears into the overflow bin. Monitoring
# only -- no pulse, phase or hit is rejected on this number.
WD_RF_RESIDUAL_MAX = 0.05
# Containers holding the DRS4 timebase and the three calibration tables; two files
# defining the same table name is a hard error, so replace a file, never stack one.
# json mode only; the database serves the same tables.
WD_CONDITIONS_FILES = ["bt2026_wavedream_timebase.json", "bt2026_wavedream_calibration.json"]
# RF channel and frequency. REQUIRED whenever WD_ENABLED: PIWDRFPhase runs first
# in WDAnalysisSeq and PIWDWaveformAnalysis consumes /Event/wd_rf_phase, so ""
# no longer drops the algorithm -- check() rejects it.
WD_RF_TABLE = "wd_rf"
# Calibration table names; either one "" drops PIWDCalibrator and /Event/wd_hits.
WD_ALIGN_TABLE = "wd_time_alignment"
WD_ECAL_TABLE = "wd_energy_calibration"
# Pin one conditions tag (a VERSION of a table, not a time period) for all three.
WD_TAG = ""
# Re-scan the RF frequency per event instead of trusting the wd_rf constant.
# Off in production: the point of the table is that the frequency is a known,
# provenanced per-run number. Turn it on to DERIVE that number for a new run --
# the fitted value lands in _Event_wd_rf_phase.frequency_hz -- or to diagnose
# drift. It costs 2 x WD_RF_REFINE_POINTS extra fits per event.
WD_RF_REFINE = False
WD_RF_REFINE_POINTS = 41
# Half-width of the coarse refinement scan, as a fraction of the nominal.
WD_RF_REFINE_SPAN = 0.01
# --- WaveDREAM scalers -----------------------------------------------------
# Histogram the scaler readout: per board, rate vs board time and run-average
# rate per scaler index, the discriminator thresholds and the FPGA temperature.
# The scaler frontend writes its readings in events of their own that carry no
# waveforms, so this runs in its own sequencer gated on /Event/wd_scalers.
# Requires WD_ENABLED, whose PITMidasWaveDream decodes the scaler banks.
WD_SCALER_MONITOR = True
# Board serials to book histograms for. A serial read out but not listed is
# counted in histograms/PIWDScalerMonitor/readings only, and reported at the end.
WD_SCALER_BOARDS = [36]
# Time-axis bin width and upper edge in s of board time (seconds since the board
# was configured). The bin matches the frontend's 5 s readout period, one reading
# per bin; a reading past the upper edge lands in the overflow and is reported.
WD_SCALER_TIME_BIN_S = 5.0
WD_SCALER_TIME_MAX_S = 7200.0
# Also fill readings the frontend flagged stale; off, they are counted and skipped.
WD_SCALER_FILL_STALE = False
# --- PSM decode ------------------------------------------------------------
# MuTrig RAW readout channels (chipid*32+channel, read before the map lookup)
# carrying the RF gated by S1 and the proton current. None (the default) leaves
# the decoder to take each from the run's interval of mutrig_channel_map in
# bt2026_psm_readout_map.json, which marks them with the role ids 2014 (rf) and
# 2015 (proton_current); a run whose interval marks no RF has no /Event/rf, one
# that marks no current no histograms/musip/current. An integer overrides the
# map for every run this job processes, for a file whose interval is wrong or
# not yet written. "off" switches the role off whatever the map says (no
# /Event/rf, resp. no musip/current; the map's channel is dropped and counted).
# The proton current left the SMA when the NIM copies were cabled (the
# WaveDREAM scaler counts it), and raw channel 7 has carried a counter since
# then (S3L, later S3 TOT): the old PSM_CURRENT_CHANNEL = 7 would consume every
# one of its words as a current pulse before the map is read.
PSM_RF_CHANNEL = None
PSM_CURRENT_CHANNEL = None
# MuPix pixel pitch in mm; a wrong pitch scales every position and every slope.
PSM_QUAD_PIXEL_PITCH = 0.08
# MuPix timestamp bin width in ns. There is no MuTrig counterpart any more: the
# trigger encoding reports its time in ns directly, so PITMidasMusip dropped
# trigTimeBinWidth along with the 50 ps timestamp it used to scale.
PSM_QUAD_TIME_BIN_NS = 8.0
# The SMA trigger word's coarse field is the time in ns shifted right by a number
# that has differed between run ranges (3, i.e. 8 ns ticks, then 15, then 14).
# None takes the run's value from the sma_coarse_shift table in
# bt2026_psm_readout_map.json, and a run no interval of it covers stops the job
# at initialize. Set an integer here only to process a run whose shift is known
# (measured with psm-analysis sma-tot-vs-wd/mupix_phase.py RUN --time-check) but
# not yet in the table; a wrong value puts every counter hit and RF pulse at a
# time no MuPix hit shares.
PSM_SMA_COARSE_SHIFT = None
# Raw-word diagnostics of the SMA stream, booked by the decoder under
# histograms/musip/sma_*: words per channel, words per frame, frame span and the
# gap between frames (live time), the coarse-minus-fine time difference per
# channel and the fine-bit occupancy. Counts only, so subruns merge. Off leaves
# them unbooked; the decoder's own default is off, so any other job using it
# (a MuPix debug job, say) does not grow them.
PSM_SMA_DIAGNOSTICS = True
# The decoder's SMA fine-time correction: the S2 k * 2048 ns offset found per
# frame (with a per-word resolution), the RF words snapped per word, and the S5
# t/2 field repaired, all before the times are built. Writes the product
# /Event/sma_time_state and the histograms/musip/sma_fineoffset_* histograms.
# Off restores the times the decoder gave before the correction existed. It
# changes the hits, so light mode does not switch it off.
PSM_SMA_FINE_OFFSETS = True
# Part of the fine-time correction above: the low-threshold NIM copies (S1L..S5L,
# ids 2021, 2023-2026, wherever the run's mutrig_channel_map cables them) can carry
# an arbitrary per-frame fine-time lag against S1. The decoder measures it per frame
# from the copy's words that share a coarse field with an S1 word and removes it
# down to the copy's nominal delay below, but only when it lies further than 50 ns
# from it (a real cable delay survives); a frame without S1 (a source run) is never
# corrected by a guess. Recorded per frame in /Event/sma_time_state (m_lagNs).
# False leaves the copies' times as the words have them. Changes the hits, so not a
# light switch.
PSM_SMA_NIM_LAG = True
# {NIM copy id: nominal delay from S1 in ns} for the lag above; an id not listed is
# 0. Empty: S4L's ch 10 copy is a logic output with several delays to S4 and stays
# uncalibrated, and S3L sits within a few ns of S1.
PSM_SMA_NIM_NOMINAL_DELAY_NS = {}
# The detector ids the lag above is measured and removed for (the decoder's
# fineOffsetLagVids): the NIM copies S1L..S5L. An id the run's map does not cable
# is inactive. A TOT id may be added if its channel turns out to carry the same
# per-frame fault, e.g. 2004 (S3 TOT, on the channel the S3L copy used before
# every counter got a NIM copy). Not the S1 reference (2001), the voted S2 (2003),
# the halved S5 (2006) or a role marker (2014, 2015). Changes the hits, so not a
# light switch.
PSM_SMA_LAG_VIDS = [2021, 2023, 2024, 2025, 2026]
# Frame 0 of subrun 0 holds a stale replay of the previous run. With this on, the
# decoder drops the first H000 bank of the job when the input file is subrun 0
# (the second number in the file name, run00790_00000); other subruns and a name
# without a subrun are left alone. Changes the hits, so not a light switch.
PSM_SMA_SKIP_STALE_FIRST_FRAME = True
# After a run stop the readout writes a short burst of SMA frames whose clocks are
# frozen, so one frame spans hundreds of seconds, and the pairing of its hits is
# quadratic. The decoder drops a whole H000 bank (its SMA and MuPix hits alike) whose
# SMA time span exceeds this many ms; the count is musip/sma_dropped_banks. A real
# frame spans milliseconds. 0 switches the cut off. Changes the hits, so not a light
# switch.
PSM_SMA_MAX_FRAME_SPAN_MS = 1000.0
# The same for a bank in which fewer than this share (0..1) of the SMA words have
# a fine time consistent with their coarse field; a bank of garbage words fails it.
# 0 switches the cut off. Changes the hits, so not a light switch.
PSM_SMA_MIN_FINE_COARSE_SHARE = 0.0
# Drop the MuPix pixel words of hot pixels: the run's interval of the
# mupix_pixel_mask table in bt2026_psm_readout_map.json, counted per chip in
# histograms/musip/mupix_masked_hits and per pixel in mupix_masked_hits_per_pixel.
# The shipped table masks nothing on [0, open); a noisy-pixel study adds intervals
# through python -m pioneer.conddb.mupix_mask. A run the table does not resolve for
# stops the job at initialize. False decodes every pixel word, hot or not.
PSM_PIXEL_MASK = True
# Tag of mupix_pixel_mask to read; None reads the table's default tag.
PSM_PIXEL_MASK_TAG = None
# Drop the MuPix pixel hits whose ToT (time over threshold, 256 ns counts, 0..31) is
# at or below this value, in the decoder, before the timewalk layer, the MuPix
# monitor and the track reco see them. Low ToT (0-2) is mostly crosstalk ghosts,
# noise bursts and hot pixels. musip/mupix_tot_vs_chip is filled before the cut, so
# the dropped population stays visible there. -1 keeps every hit; an integer in
# [-1, 31].
PSM_MUPIX_TOT_CUT = 2
# High-ToT copies of the MuPix monitor's stage-weighted maps (xy_hitot_mt,
# xxp_hitot_mt, yyp_hitot_mt, track_xy_expanded_hitot_w, xxp_central_hitot_w,
# yyp_central_hitot_w). They take the same L1/L2 pairs with the same weights as
# the maps without hitot, but only the pairs whose L1 hit AND L2 hit both have a
# ToT (256 ns counts, 0..31) at or above this value. The decoder has already
# dropped every hit with ToT <= PSM_MUPIX_TOT_CUT, so every hit the monitor sees
# has ToT >= PSM_MUPIX_TOT_CUT + 1: at or below that value the six maps are bin
# for bin copies of their originals (the job prints a WARNING), and they select
# something only above it. -1 books and fills none of the six; an integer in
# [-1, 31]. The website and the minitwin feed do not read these maps: they appear
# only in the histogram file.
PSM_MUPIX_HIGH_TOT_MIN = 13
# --- SMA calibration layer: TOT + NIM pairing -------------------------------
# PIPSMSMACalibration (/Event/mutrig -> /Event/mutrig_cal and /Event/sma_hits) pairs
# each counter's TOT word with its low-threshold NIM copy (S1L..S5L, ids 2021,
# 2023-2026) into one hit. A counter is paired when the run's mutrig_channel_map
# interval cables its NIM copy AND the sma_time_alignment table
# (bt2026_psm_readout_map.json) has an offset for that copy; a cabled copy without an
# offset is histogrammed only (histograms/PIPSMSMACalibration/) and its words stay out
# of /Event/mutrig_cal. The shipped table has no NIM offsets, so until they are
# measured the output is the TOT hits in time order, as before. NIM ids never reach
# /Event/mutrig_cal: a NIM-only hit carries its counter's TOT id. False drops the NIM
# words and applies no offsets at all. Changes the hits, so not a light switch.
PSM_SMA_NIM_PAIRING = True
# Largest |t_NIM - t_TOT| of a pair, aligned times, ns (inclusive).
PSM_SMA_PAIR_WINDOW_NS = 20.0
# Time of a paired hit: "tot" (the TOT word's leading edge) or "nim" (the NIM copy's
# CFD time, free of the TOT walk). A NIM-only hit always has the NIM time.
PSM_SMA_TIME_SOURCE = "tot"
# ToT (raw SMA units) given to a NIM-only hit, one with no TOT word: above
# PSM_LAYER_THR, so it fires its layer like any other hit.
PSM_SMA_NIM_ONLY_TOT = 1.0
# DEVELOPMENT AND QUICK TESTS ONLY: {detector id: offset in ns} replacing the
# sma_time_alignment value of that id for this job, e.g. {2024: -153522.0} to try a
# NIM copy whose offset is not yet in the table. Each one is logged as a warning.
# Leave it empty in production: constants belong in the conditions table, where the
# rendered job and every reprocessing find them.
PSM_SMA_OFFSET_OVERRIDE_NS = {}
# --- PSM geometry ----------------------------------------------------------
# Base layer PIGeometrySvc builds the GeoHeader from, as "GEOCOND:<table>".
PSM_GEOMETRY_BASE = "GEOCOND:psm_geometry"
# Raw-readout-id -> detector-id maps as "NAME:table"; the NAME side must match the
# decoder's muPixMap/muTrigMap property defaults.
PSM_GEOMETRY_MAPS = ["MUPIX:mupix_chip_map", "MUTRIG:mutrig_channel_map"]
# Extra transform layers on the base; ["COND:isel"] adds the XY-stage translation
# read from /Equipment/XYTable in the ODB. A run whose ODB has no XYTable fails
# at initialize() with "source absent"; set [] and PSM_WEIGHT_STRATEGY = 0 to
# process one of those (the acceptance weights need the stage position).
PSM_GEOMETRY_TRANS = ["COND:isel"]
# Containers supplying the base table and the two map tables above (json mode only).
PSM_GEOMETRY_FILES = ["bt2026_psm_geometry.json", "bt2026_psm_readout_map.json"]
# Tag of the base geometry table; None reads the table's default tag, which is
# bt2026-v4: the MuPix chips of each quad 0.32 mm apart, a PROVISIONAL value from
# the in-beam wedge study, to be measured after the beamtime. "bt2026-v3" is the
# same geometry with the chips edge to edge. Only the geometry table is pinned;
# the two maps keep their own default tags.
PSM_GEOMETRY_TAG = None
# --- MuPix monitor ---------------------------------------------------------
# The low-level MuPix check: a hit map per chip and per plane, and tracks made
# from an L1/L2 time coincidence alone. It reads the MuPix hits and nothing
# else (/Event/muquad_twc, the timewalk layer's output: the hits of
# /Event/muquad, corrected, in time order) -- no
# scintillators, no channel map, no tracklets -- so it still says
# what the pixel planes are doing when the parts it is checking are broken.
# Requires PSM_DECODE, whose PIGeometrySvc supplies every chip footprint,
# the plane membership and the L1 -> L2 lever arm.
PSM_MUPIX_MONITOR = True
# Symmetric L1/L2 half-window in ns. Loose on purpose: the MuPix stamp is an
# 8 ns count and the two planes are different chips, so a window at the
# resolution of the clock throws real pairs away. Set it from the run's own
# histograms/PIPSMMuPixMonitor/dt, which is filled over the wider range below.
PSM_MUPIX_WINDOW_NS = 40.0
# Pixels per bin of every hit map. 1 is one bin per pixel: 256 x 250 per chip
# and 516 x 504 per plane on bt2026-v4 (512 x 500 of pixels, four empty bins
# across each gap between the chips), about 4 MB of histogram in total, and the
# granularity a dead column or a hot pixel is visible at. Setting it to n
# divides the bin count of every map by n squared.
PSM_MUPIX_PIXELS_PER_BIN = 1
# Half-width in ns and bins of the dt histogram, scanned independently of the
# coincidence window. 204 over 51 bins puts each 8 ns MuPix tick at a bin
# CENTRE; a round 200 puts it on a bin edge, where ROOT's edge convention
# splits the coincidence peak between two bins.
PSM_MUPIX_DT_RANGE_NS = 204.0
PSM_MUPIX_DT_BINS = 51
# Half-width in mrad of the x'/y' axes of this module's xxp and yyp, rounded up
# to whole bins (the monitor's TrackSlopeStepsPerBin slope steps, 18.67 mrad).
# 0 derives the full geometric acceptance of the two planes at the measured
# lever arm, so nothing a pair can produce lands in an overflow bin. The same
# tracks on PIPSMAllTrackReco's phase-space window are xxp_mt / yyp_mt.
PSM_MUPIX_SLOPE_RANGE_MRAD = 0.0
# Half-width in mm of the fixed x/y axes of track_xy_expanded, xxp_central and
# yyp_central. It covers the standard five-point scan (PSM_POSITIONS_MM, +-17 mm)
# and the +-20 mm 3x3 grid, plus the 20.64 mm half-width of a plane with the
# 0.32 mm gap between its chips (40.64 mm),
# rounded up to 41.6 = 130 x 0.32 so the monitor's 260 bins stay 0.32 mm (four
# pixels) wide; the monitor shifts the axis by a quarter pixel so a half-pixel
# stage offset such as 17 mm puts no pixel on a bin edge. It is a fixed number
# rather than the plane footprint so that every run of a stage scan books the
# same axes and the scan's runs merge bin by bin. Changing it without
# ExpandedBins changes the bin width.
PSM_MUPIX_EXPANDED_RANGE_MM = 41.6
# Rough half-width in mrad of the x'/y' axes of xxp_central and yyp_central,
# the beam core on a finer axis than the full acceptance above. The monitor
# rounds it up to a whole number of slope steps (one pixel over the lever arm),
# one step per bin: 100 gives 77 bins over +-102.67 mrad.
PSM_MUPIX_CENTRAL_SLOPE_MRAD = 100.0
# Pair every L2 hit inside the window instead of only the one nearest in time.
# Each extra pair is a combinatorial ghost carrying a slope no particle had,
# so this is a diagnostic for a busy run, not a production setting.
PSM_MUPIX_ALL_PAIRS = 0
# MuPix timewalk against the counters S1-S5: t(pixel) - t(Sn) against the pixel
# ToT, against the Sn ToT, and pixel ToT against Sn ToT, per plane L1/L2 and
# counter. Two samples: every (pixel, Sn) pair of a readout frame in the MuPix
# monitor (tw_* under PIPSMMuPixMonitor, dt in [-150, 450) ns, ToT vs ToT in the
# prompt window [-100, 450) ns), and the pixels of the clustered L pair of each
# track holding an S1 hit against the Sn hit nearest that S1 within +-50 ns
# (tw_* under PIPSMAllTrackReco, filled only with PSM_AGGREGATE on). The monitor
# reads /Event/mutrig_cal for this; a frame without it skips the timewalk only.
# It also books the correction layer's raw-vs-corrected histograms (below).
PSM_TIMEWALK = True
# The dt axis [min, max) ns in bins of every timewalk histogram: the monitor's
# and the track reco's tw_* and the correction layer's twc_*. One axis for all
# three, so twc_dt_vs_tot_raw_L<n> stays the monitor's tw_dt_vs_tot_L<n>_<S1>
# bin for bin and the recalibration CLI reads the layer's histograms on the
# axis it expects. 2 ns bins over [-150, 450) hold the prompt edge near
# -90 ns and the walk tail of the lowest ToTs to about +400 ns.
PSM_TIMEWALK_DT_MIN = -150.0
PSM_TIMEWALK_DT_MAX = 450.0
PSM_TIMEWALK_DT_BINS = 300
# --- MuPix timewalk correction ---------------------------------------------
# PIPSMMuPixTimewalkCorrection writes the hits of /Event/muquad to
# /Event/muquad_twc with each pixel's time moved by its chip's walk at its ToT,
# t - W_chip(ToT), W being the fitted peak of t(pixel) - t(S1) against the pixel
# ToT, offset included, so the corrected pixel times line up with S1: the same
# hits, corrected, in time order (the correction moves times by up to a few
# hundred ns, so it re-sorts them). The constants are the run's interval of the
# mupix_timewalk table in bt2026_psm_readout_map.json; the shipped table is
# empty on [0, open), which leaves every time as it was, so the output equals
# /Event/muquad hit for hit whenever the raw frame is in time order (every
# frame of the runs checked so far; one that is not comes out sorted). The
# MuPix monitor and the track reco read /Event/muquad_twc. The layer runs
# whenever PSM_DECODE is on, whatever the consumers are set to. A run the table
# does not resolve for stops the job at initialize. False still runs the layer,
# uncorrected and without reading the table, so the consumers always read one
# path.
PSM_TIMEWALK_CORRECTION = True
# Tag of mupix_timewalk to read; None reads the table's default tag.
PSM_TIMEWALK_CORRECTION_TAG = None
# --- SMA monitor -----------------------------------------------------------
# The low-level check on the SMA time-over-threshold readout: how many hits each
# counter takes, and what their ToT looks like. It reads /Event/mutrig_cal (the
# SMA calibration layer's time-ordered copy of /Event/mutrig) and nothing
# else -- no MuPix hits, no data-side channel map, no tracklets -- so it
# still says what the counters are doing when the parts that depend on them are
# broken. Requires PSM_DECODE, whose PIGeometrySvc also serves the raw MUTRIG
# map the counter axis is built from.
PSM_SMA_MONITOR = True
# Top of the per-counter hits-per-event axis; anything above it lands in the
# last bin. A readout frame holds at most 20,000 words (the H000 bank cap), and
# a busy counter routinely exceeds a few hundred hits per frame, so the axis
# runs to the bank cap rather than clipping a counter that is merely busy.
PSM_SMA_HITS_PER_EVENT_MAX = 20000
# A cabled counter whose commonest ToT value takes at least this share of its
# hits is reported as degenerate at finalize. One value repeated is a pulser or
# a stuck field, not a spectrum.
PSM_SMA_DEGENERATE_TOT_SHARE = 0.95
# Share of a cabled counter's hits at ToT 0 or 255 above which it is reported as
# marker-dominated. Those two values are the idle FEB's own words, so a counter
# made mostly of them is not seeing its TOT box.
PSM_SMA_MARKER_TOT_SHARE = 0.5
# Fill dt_to_s1_wide, every counter hit minus every S1 hit within +-2^18 ns in
# 1024 ns bins: the plot in which a counter time off by a whole 2^16 or 2^17 ns (a
# flipped fine-time bit) shows up. Its pairs grow as S1 hits times other hits, so
# on a busy run it is not cheap. False leaves the histogram booked and empty, and
# bin 2 of pattern_counters then counts every frame as left out of it.
PSM_SMA_WIDE_DT = True
# A frame whose S1 pairs within the narrow dt window would exceed this many is left
# out of dt_to_s1 and dt_to_s1_wide, and counted in narrow_pairs_left_out: a bound
# on the quadratic pairing of a frame with far too many hits. 0 means no limit. Not
# a light switch.
PSM_SMA_MAX_NARROW_PAIRS = 0
# --- PSM reco --------------------------------------------------------------
# Container holding the data-side channel map the tracklet reco reads (json mode only).
PSM_CHANNEL_MAP_FILE = "bt2026_psm_channel_map.json"
# Channel-map table and tag; the table supplies the WHOLE map, so no per-channel job option is set.
PSM_CHANNEL_MAP_TABLE = "psm_channel_map"
PSM_CHANNEL_MAP_TAG = ""
# Seeding channel for the clustering; negative is unseeded, so an isolated delayed
# pulse forms its own tracklet instead of being lost.
PSM_SEED_ON = -1
# Require exactly one L1 and one L2 hit; delayed pulses have no tracker hits.
PSM_REQUIRE_L_HITS = 0
# For SOURCE runs: seed tracklets on L1 tracker hits instead of on
# scintillator hits, pairing each with the nearest L2 hit within
# PSM_LPAIR_WINDOW_NS. A source sitting on the tracker makes L1/L2 coincidences
# with no scintillator involved, and both S-seeded modes attach L hits only to
# a scintillator-seeded cluster, so they reconstruct nothing at all from such a
# run. Leave it off for beam running, where the scintillator seed is what
# defines a particle.
PSM_SEED_ON_L = 0
# L1 <-> L2 half-window in ns for the mode above. The two-plane correlation
# from a source is much broader than the tracker's time resolution, so this is
# generous on purpose; scan it rather than trusting the default.
PSM_LPAIR_WINDOW_NS = 40.0
# Window in ns in which a scintillator cluster takes its L1/L2 hits:
# [t_S - PSM_L_WINDOW_BEFORE_NS, t_S + PSM_L_WINDOW_AFTER_NS). Measured with the
# SMA and MuPix times on one base, t(MuPix) - t(S1) has a sharp edge at -90 ns,
# peaks at -52 ns and has a timewalk tail to about +150 ns over a flat background,
# so the window opens just before the edge and closes past the tail. The
# algorithm's own defaults (8 before, thrScint = 2 after) sit entirely on the
# near side of the edge. The S-S clustering window is separate:
# PSM_SCINT_WINDOW_NS below.
PSM_L_WINDOW_BEFORE_NS = 100.0
PSM_L_WINDOW_AFTER_NS = 160.0
# S-S clustering window in ns (thrScint): in the unseeded mode a cluster takes the
# scintillator hits in [t_seed, t_seed + window). One particle's S1-S5 hits must
# land in one cluster, but the SMA counters are not time-aligned yet (S3 and S5
# come out about 2 ns before S1, S2 and S4, and SMA times are whole ns), so the
# algorithm's default of 2 splits most particles into two or three clusters. Each
# of them takes the same L1/L2 pair (about half of all L pairs are copies), and
# the cluster holding S1 is missing the layers that went to the other one, so its
# stop layer and S5 veto are wrong. 5 ns keeps the particle together; it stays
# far below PSM_DELAYED_WINDOW_NS[0], so a delayed pulse is never absorbed into
# its prompt cluster. Revisit once the SMA channels carry time offsets.
PSM_SCINT_WINDOW_NS = 5.0
# The L hits of each plane inside that window are clustered: two hits at most
# this far apart in mm (global x/y, single linkage) are one cluster, and exactly
# one cluster per plane makes the L pair, at the mean of the cluster's pixel
# centres. 0.12 takes the eight touching pixels at the 0.08 mm pitch (edge 0.08,
# corner 0.113), also across a chip boundary, and nothing further; on beam data
# that holds ~90% of the same-particle excess of same-plane hit pairs. 0 turns
# the clustering off: a second hit of a plane then makes the tracklet ambiguous,
# neighbouring pixels of one particle included.
PSM_L_CLUSTER_DIST_MM = 0.12
# Before the one-cluster-per-plane test, drop MuPix crosstalk ghosts: a cluster
# of ToT <= 3 on the chip of a higher-ToT pixel of the same window, at most one
# column and 40-43, 81-85 or 122-127 rows away (found in beam data; most
# of the remaining ambiguity). Off until decided.
PSM_DROP_CROSSTALK_GHOSTS = False
# Fill the phase-space histograms inside the algorithm; this is the monitoring.
PSM_AGGREGATE = 1
# Restrict those histograms to prompt-like tracklets: only a tracklet with an
# unambiguous L1/L2 pair and at least one prompt-channel hit is filled, the prompt
# channel being whatever PromptChannel = -1 in the channel map resolves to (S1 on
# bt2026). Under the seeded configuration this changes nothing, because the seed
# already guarantees both. In the unseeded mode this job runs (PSM_SEED_ON = -1) it
# is what keeps the all-tracks TH3s meaning "prompt tracks": without it an isolated
# delayed pulse forms its own tracklet with no L pair, its position is a sentinel,
# and it piles up in the overflow bins of every phase-space plot. Turn it off only
# to look at everything the clustering made, and expect those overflows.
PSM_AGGREGATE_PROMPT_ONLY = 1
# The MuPix hits are not consumed: every scintillator cluster takes the L1/L2 hits
# of its own window, so one pair can sit on several tracklets of a frame (a
# particle's late hits, a decay pulse inside the prompt's window). The reco marks
# one owner per pair, a pair being the same lead pixel on L1 and on L2
# (lPairOwner / lPairShares in the tracklets): a tracklet holding a prompt-channel
# hit (S1) before one without, then the best match of t(L1) - t(S1) to
# PSM_LPAIR_OWNER_OFFSET_NS (the seed time standing in for t(S1) without an S1
# hit), then the earliest seed. With this on, only owners fill the phase-space
# histograms, so a pair fills once; with the prompt gate on it removes only pairs
# carried by two or more S1 tracklets.
PSM_AGGREGATE_OWNERS_ONLY = 1
# Expected t(L1) - t(S1) in ns for choosing that owner, on the time base of the
# MuPix collection the reco reads (/Event/muquad_twc). With the timewalk
# correction (mupix_timewalk constants, every run from 467 on) the corrected
# times line up with S1: the peak is -3..-1 ns on both planes (runs 528, 920),
# so 0. A run without constants passes the raw times through, which peak
# 30-60 ns earlier (rate dependent); set the measured peak for such a run.
PSM_LPAIR_OWNER_OFFSET_NS = 0.0
# L1 -> L2 lever arm in mm, used to turn (x2 - x1) into a slope.
PSM_DISTANCE_L12 = 30.0
# Delayed-coincidence window in ns for the pi -> mu tag, [MIN, MAX). 18 ns is where
# the SMA dead time after the prompt hit ends (lower adds echo pulses of the prompt);
# 115 ns stops below the S2/S3 words that follow a hit by ~135 ns. Only safe with
# the delayed-candidate rules below: without PSM_DELAYED_TOT_MIN the 0-ToT pseudo
# clusters at +43 / +83 ns fill the wider window.
PSM_DELAYED_WINDOW_NS = (18.0, 115.0)
# Bins of the dt and sb histograms over that window: one per ns, so it must equal
# the window width in ns (check() enforces it; change both together).
PSM_DELAYED_DT_BINS = 97
# Delayed candidates. The delayed summed ToT (s1tot + ... + s5tot of the candidate
# cluster, exp_tagged delayedTot) must lie in [PSM_DELAYED_TOT_MIN,
# PSM_DELAYED_TOT_MAX). The lower edge removes the 0-ToT clusters and the low-ToT
# pulses whose dt spectrum is no 26 ns exponential (ToT 6-9 carry no real decays:
# removing them loses no WD-confirmed tag); the upper edge removes the long-ToT S3
# words that are not decays. [10, 30) is the band of the tuned tag. None as the
# maximum: no upper edge.
PSM_DELAYED_TOT_MIN = 10.0
PSM_DELAYED_TOT_MAX = 30.0
# 1: a delayed candidate must hit the prompt's stop plate and no other plate (its
# layer pattern above PSM_LAYER_THR is the stop plate's bit alone); a prompt with no
# layer above threshold then has no candidate. The muon of a pi -> mu decay stops
# within ~1 mm, so it lights only the plate the pion stopped in.
PSM_DELAYED_STOP_PLATE_ONLY = 1
# Prompt selection by the S1 RF phase (the prompt tracklet's s1rfphase), ns,
# [MIN, MAX); a prompt without a valid phase then fails. The pion peak moves with
# the momentum and the tune, so it has to be measured per setting before it goes
# in here (e.g. (95.0, 98.5) at 165 MeV/c). None: off, every prompt.
PSM_PROMPT_RF_WINDOW_NS = None
# Stop layers (1..5 = S1..S5, 0 = no layer above threshold) whose prompts count
# as tagged: the n_tagged counter, the tagged phase-space maps (xy, xxp, yyp and the
# _w twins) and the dt, sb, far and dt_all histograms. The exp_tagged rows keep every
# stop layer. S2 is included, but it gives almost no tags until the S2 chain-offset
# decoder fix exists (the decoder misplaces the S2 words of a busy channel, so the
# decay pulse is not seen: efficiency ~0 %). With the fix alone about 8 % at 33 %
# strict purity; with the fix and the SMA time offsets table about 24 % / 71 %,
# like S4 (27 % / 81 %). Until then S2 adds far-window accidentals but no signal:
# subtract accidentals per stop layer. An empty tuple: every layer.
PSM_TAGGED_STOP_LAYERS = (2, 3, 4)
# Far accidental window in ns, [MIN, MAX) before the prompt: the candidates in it
# are counted per prompt (exp_tagged nFar) and histogrammed ("far", every
# candidate, the tagged stop layers pooled). Scaled by the signal width over this
# width it estimates the UNCORRELATED accidentals of the signal window only: the
# decay positron of the prompt's own muon adds a correlated, slowly falling term
# of about the same size at 18-115 ns, so a far-subtracted count is not
# background-free. Subtract per stop layer (dt_all_stop): a layer blind after its
# own hit (S2 for now) fills the far window but not the signal. The mirrored sideband (-MAX, -MIN] of the
# window under-counts the accidentals: the dead time after a hit and the blind
# S2 channel deplete the region just before the prompt. Must lie below
# -PSM_DELAYED_WINDOW_NS[1]. None: off.
PSM_FAR_WINDOW_NS = (-1000.0, -200.0)
# Every-pair dt histograms "dt_all" (tagged stop layers) and "dt_all_stop" (by stop
# layer, every layer) over this window in ns, 1 ns bins: every candidate of every
# S5-vetoed prompt under the same rules, not only the earliest. The pion lifetime
# fit and the flat accidental level come from these. None: off.
PSM_PAIR_WINDOW_NS = (-1000.0, 500.0)
# 1: fill "stop_phase", stop layer x S1 RF phase (1 ns bins over 0-131, bin 131 =
# no valid phase) of every prompt with a prompt-channel hit, through-going ones
# included, so the pion window and the RF sidebands can be chosen offline.
PSM_STOP_PHASE_HIST = 1
# Require the PROMPT half of a coincidence to have a prompt-channel hit of its own.
# Off, any tracklet in the window can play the prompt role -- including, in the
# unseeded mode, a delayed pulse that formed its own tracklet -- and the tag then
# counts pairs no particle made. The delayed half is selected by the window and by
# PSM_S5_THR, not by this.
PSM_REQUIRE_SEED_HIT = 1
# Layer-hit energy threshold and the S5 through-going threshold; raw MuTrig ToT on data, so retune.
PSM_LAYER_THR = 0.2
PSM_S5_THR = 0.2
# Telescope stage positions (dx, dy) in mm for the acceptance weighting -- XY-stage
# coordinates, the same numbers as /Equipment/XYTable. The algorithms apply isel's
# (-x, y) translation themselves; do not pre-negate these.
PSM_POSITIONS_MM = [(0.0, 0.0), (17.0, 17.0), (-17.0, 17.0), (-17.0, -17.0), (17.0, -17.0)]
# Fiducial erosion in mm of each stage position's L1/L2 plane footprint before the
# containment test: a position only counts for a track that sits at least this far
# inside its planes, so a small error in the footprint or the stage position does
# not decide whether a track near an edge counts once or twice.
PSM_WEIGHT_MARGIN_MM = 2.0
# Weighting strategy. 0: every tracklet gets weight 1. 1: weight 0 unless the
# track is inside this run's own window at both L1 and L2, else 1/N, N the
# number of PSM_POSITIONS_MM windows containing it at both L1 and L2, where
# each window is the conditions footprint (PIGeometrySvc) of the L1/L2 plane
# moved from this run's own stage position to that config position and eroded
# by PSM_WEIGHT_MARGIN_MM, less the gaps between the MuPix chips there (a track
# through a position's chip gap is one that position cannot see). Over the runs of a scan the weights a trajectory
# would receive then sum to 1 wherever at least one run can see it. A run at
# none of PSM_POSITIONS_MM (within 0.01 mm) counts its own window as one more
# position and warns that its weights will not sum to 1 with the scan.
# 2: also require containment at the track's stop-layer depth
# (PIPSMAllTrackReco only; the MuPix monitor has no scintillators to define a
# stop layer and is capped at min(strategy, 1)).
# Strategies 1 and 2 need PSM_GEOMETRY_TRANS to include "COND:isel", or every
# run is silently treated as sitting at the design position.
PSM_WEIGHT_STRATEGY = 1
# Phase-space histogram axes: PIPSMDelayedCoincidence's tagged xy/xxp/yyp and
# their weighted twins, PIPSMAllTrackReco's xy/xxp/yyp TH3s and their weighted
# twins xy_w/xxp_w/yyp_w, the MuPix monitor's stage-weighted xy_mt/xxp_mt/yyp_mt
# (the maps the beam-tuning feed reads, miniTwinInterface.miniTwin_histograms), and (on the
# MuPix monitor's own axes, not these ranges) track_xy_expanded/xxp_central/
# yyp_central and their weighted twins
# track_xy_expanded_w/xxp_central_w/yyp_central_w. These are not free
# monitoring knobs on the reco side, they are the minitwin det10 input
# contract -- the histograms this job writes rebin onto the model's [3, 64, 64]
# maps with no interpolation. Source of truth for all three numbers is
# beamline-simulation/psm/psm_scan_config.py (X_WINDOW, A_WINDOW, NBINS_2D),
# which the offline producer analysis/josh/psm_scan_hists.py books against and
# beam-tuning-client/beamtune/adapters/psm_maps.py reads back.
#
# 320 = 5 x 64, so the rebin onto the 64-bin export splits no bin; check() below
# rejects a count that is not a positive multiple of 64 for exactly that reason.
# The all-tracks TH3s keep their own 64 bins -- already the export grid -- since
# a 320-bin TH3 would be ~3 MB each; only the tagged TH2Ds get the fine grid.
PSM_PHASE_SPACE_BINS = 320
# Position axis half-width in mm: x and y at L1, GLOBAL coordinates. The 2.5 the
# algorithm defaults to was a single-position zoom that put every off-axis track
# into the overflow bin.
PSM_PHASE_SPACE_POS_RANGE_MM = 37.0
# Slope axis half-width in mrad. Both algorithms compute
# 1000 * (x2 - x1) / PSM_DISTANCE_L12, the minitwin paraxial convention, so only
# the axis moved. The 1D xp/yp spectra keep their own narrower range on purpose:
# they are the shift-display zoom, not a minitwin input.
PSM_PHASE_SPACE_SLOPE_RANGE_MRAD = 950.0
# --- Output ----------------------------------------------------------------
# Write the "rec" RNTuple. Off for a pure monitoring pass; histograms are unaffected.
WRITE_NTUPLE = True
# Ordered "keep <glob>" / "drop <glob>" rules over TES paths, later rules winning;
# empty persists everything, so a new collection is never lost by omission.
# (/Event/sma_time_state, the SMA fine-offset state, is small: one entry per H000
# bank, so it is kept.)
NTUPLE_RULES = []
# Which MuPix hit collections the RNTuple keeps: "both" (/Event/muquad and the
# corrected /Event/muquad_twc), "corrected" (drops /Event/muquad) or "raw"
# (drops /Event/muquad_twc). Appended to NTUPLE_RULES as the last rule, so it
# wins. On a busy beam subrun each copy is about a quarter of the file, and
# since the corrected copy is time-ordered its unchanged columns no longer
# repeat the raw pages byte for byte, so the writer cannot store them once:
# "both" costs about 25 % more file. The default keeps the hits the reco read;
# the raw times follow from them and the run's mupix_timewalk constants, or
# from reprocessing the MIDAS file.
PSM_TWC_NTUPLE = "corrected"
# Which SMA hit collections the RNTuple keeps: "both" (/Event/mutrig and the
# calibrated /Event/mutrig_cal), "calibrated" (drops /Event/mutrig) or "raw"
# (drops /Event/mutrig_cal). Appended to NTUPLE_RULES after PSM_TWC_NTUPLE, so
# it wins too. With PSM_SMA_HITS_NTUPLE below the sidecar /Event/sma_hits, with
# the raw /Event/mutrig, rebuilds /Event/mutrig_cal hit for hit (raw word, id,
# time, ToT), so "raw" loses nothing. "raw" by default: the raw-stream analyses
# need /Event/mutrig in its readout order, and it keeps both words of every
# counter, so "calibrated" is for when nobody needs either; the sidecar's raw
# indices then point into the dropped /Event/mutrig (the job warns).
PSM_SMA_CAL_NTUPLE = "raw"
# Keep the calibration layer's sidecar /Event/sma_hits in the RNTuple: the only
# record of how each hit was paired (both times, both widths, flags, raw word
# indices). It costs about 14-20 % of the file on a beam subrun, and until a NIM
# offset is in sma_time_alignment it holds nothing /Event/mutrig and a time sort
# do not. True adds no rule, so NTUPLE_RULES still decide (a "drop *" drops it);
# False appends "drop /Event/sma_hits" last. The TES always has it.
PSM_SMA_HITS_NTUPLE = True
# ===== END OF SETTINGS =====

# The spellings of LIGHT in a rendered file and in NL_LIGHT, and the settings light
# mode switches off.
_LIGHT_VALUES = {"1": True, "0": False}
_LIGHT_SWITCHES = ("WRITE_NTUPLE", "PSM_TIMEWALK", "PSM_SMA_WIDE_DT", "PSM_SMA_DIAGNOSTICS")

# Where the input, the output, the event limit, light mode, the conditions source and the
# conditions directory come from. This runs before the container lists below, which
# resolve against them, and before check(), which reports on all of them.
if RENDERED:
    # A rendered file is a record, so it reads nothing from the environment: no
    # NL_MIDAS, NL_OUT, NL_EVTMAX, NL_CONDITIONS, NL_CONDITIONS_DIR and no overrides file.
    # Re-run it under any environment at all and it processes the same input with the
    # same settings into the same outputs, which is what makes it worth keeping.
    NL_MIDAS = _RENDERED["in_file"]
    NL_OUT = _RENDERED["out_file"]
    EVT_MAX = int(_RENDERED["evt_max"])
    # The renderer writes "1" or "0"; anything else stays a string for check() to reject.
    # One exception: a daemon started before light mode existed keeps its old
    # renderer in memory, which fills every field but this one, while it reads this
    # file from disk for each job. Its jobs arrive with the light placeholder still
    # in them, and for that renderer every job is the full one, so that reads as "0".
    _light = _RENDERED["light"]
    if _light.startswith(chr(36)):
        _light = "0"
    LIGHT = _LIGHT_VALUES.get(_light, _light)
    # Empty means the renderer had no NL_CONDITIONS_DIR, so the default stands. The
    # rendered file must still name one directory, because a run reprocessed against
    # a different conditions tree is a different run.
    CONDITIONS_DIR = _RENDERED["conditions_dir"] or _DEFAULT_CONDITIONS_DIR
    # As the renderer resolved it: "db:" and an explicit, password-free conninfo (no
    # service name, so the file says which server it read even after ~/.pg_service.conf
    # changes), "json:" and an absolute directory, or a bare "json".
    CONDITIONS = _RENDERED["conditions"]
    # An overrides file is an interactive convenience and is deliberately not part
    # of a rendered job: it would be a second file the record depends on, and the
    # point of the record is that it depends on nothing. Note the renderer does NOT
    # fold one in, so NL_OVERRIDES set while rendering has no effect at all -- a
    # variant job is made by editing the settings block, or the rendered copy.
    NL_OVERRIDES = ""

else:
    # A variant job reassigns a few settings in a small file instead of editing
    # this one. Unknown names are not rejected; the banner prints the path.
    NL_OVERRIDES = os.environ.get("NL_OVERRIDES", "")
    if NL_OVERRIDES:
        exec(compile(Path(NL_OVERRIDES).read_text(), NL_OVERRIDES, "exec"), globals())

    # The environment is the one-off, so it wins over the block and the overrides file.
    NL_MIDAS = os.environ.get("NL_MIDAS", "")
    NL_OUT = os.environ.get("NL_OUT", "")
    if os.environ.get("NL_EVTMAX"):
        EVT_MAX = int(os.environ["NL_EVTMAX"])
    if os.environ.get("NL_CONDITIONS"):
        CONDITIONS = os.environ["NL_CONDITIONS"]
    # "1" or "0"; anything else is kept as the string it is, and check() rejects it.
    if os.environ.get("NL_LIGHT"):
        LIGHT = _LIGHT_VALUES.get(os.environ["NL_LIGHT"], os.environ["NL_LIGHT"])

# Light mode, applied here: after the block, the overrides file and the environment
# have all had their say, and before anything below derives from these settings.
# Each is forced off, not toggled, so a light job is the same job whatever the block
# says about them. _LIGHT_OFF names the ones that were on, for the banner.
_LIGHT_OFF = []
if LIGHT is True:
    _LIGHT_OFF = [name for name in _LIGHT_SWITCHES if globals()[name] is True]
    WRITE_NTUPLE = False
    PSM_TIMEWALK = False
    PSM_SMA_WIDE_DT = False
    PSM_SMA_DIAGNOSTICS = False

# The subrun of the input: the second number in the file name (run00790_00005 ->
# 5), None when the name has just one. NL_MIDAS is the file the job reads in both
# modes (the baked-in in_file when rendered), so this is derived from it here.
_SUBRUN_NUMBERS = "".join(c if c.isdigit() else " " for c in
                          Path(str(NL_MIDAS)).name.split(".")[0]).split()
_SUBRUN = int(_SUBRUN_NUMBERS[1]) if len(_SUBRUN_NUMBERS) > 1 else None

# Measured, not guessed: 501 is ZSTD-1, about 40% less CPU than ROOT's default
# ZSTD-5 for about 8% more disk, and reusing one entry for the whole job saves
# an allocation and a free per field per event.
_NTUPLE_COMPRESSION = 501
_NTUPLE_REUSE_ENTRY = True

# TES paths. The first six are the decoding tools' own defaults, and changing a
# tool's path property without changing these breaks the sequencer gates (and the
# SMA monitor's RF input) silently. The last four are names this job chooses and
# passes to the PSM algorithms explicitly: /Event/muquad_twc is the timewalk
# layer's output, which the MuPix monitor and the track reco read in place of
# /Event/muquad; /Event/mutrig_cal is the SMA calibration layer's output, which
# every SMA reader reads in place of /Event/mutrig, whose readout order the raw
# collection keeps; the track defaults are /Event/tracker_fr, /Event/dtar_fr and
# /Event/exp_simple_tracks, which are the simulation's names, so the assignment is
# what puts the testbeam chain on one set of paths.
_TES_WAVEFORM = "/Event/wd_waveform"
_TES_SCALERS = "/Event/wd_scalers"
_TES_MUQUAD = "/Event/muquad"
_TES_MUTRIG = "/Event/mutrig"
_TES_RF = "/Event/rf"
_TES_SMA_TIME_STATE = "/Event/sma_time_state"
_TES_MUQUAD_TWC = "/Event/muquad_twc"
_TES_MUTRIG_CAL = "/Event/mutrig_cal"
_TES_SMA_HITS = "/Event/sma_hits"
# PSM_TWC_NTUPLE -> the collection it drops from the RNTuple (None: nothing).
_TWC_NTUPLE_DROP = {"both": None, "corrected": _TES_MUQUAD, "raw": _TES_MUQUAD_TWC}
# PSM_SMA_CAL_NTUPLE -> the collection it drops from the RNTuple (None: nothing).
_SMA_CAL_NTUPLE_DROP = {"both": None, "calibrated": _TES_MUTRIG, "raw": _TES_MUTRIG_CAL}
_TES_PSM_TRACKS = "/Event/exp_all_tracks"
_TES_PSM_WEIGHTS = "/Event/exp_track_weights"
_TIMEBASE_TABLE = "wd_timebase"
_BOARD_SETTINGS_TABLE = "wd_board_settings"
_LEVELS = {"DEBUG": DEBUG, "INFO": INFO, "WARNING": WARNING, "ERROR": ERROR}


# The libpq service a bare "db" means. pioneer.nearline.render has the same constant.
_DEFAULT_SERVICE = "pioneer-conditions"


def _split_conditions(spec):
    """CONDITIONS -> ("db", conninfo) or ("json", dir or ""); ValueError otherwise.

    The same grammar as pioneer.nearline.render.split_conditions, which the
    renderer uses; written out here because a rendered job must run with nothing
    from pioneer on the path. A test holds the two to the same answers.
    """
    kind, colon, arg = str(spec).strip().partition(":")
    kind, arg = kind.strip().lower(), arg.strip()
    if colon and not arg:
        # "db:" is not "db": an empty service or directory is a mistake, not a default.
        raise ValueError(f"CONDITIONS is '{kind}:' with nothing after the colon: write "
                         f"'{kind}' for the default, or name the service or directory.")
    if kind == "db":
        if not arg:
            arg = "service=" + _DEFAULT_SERVICE
        elif "=" not in arg:
            arg = "service=" + arg
        return "db", arg
    if kind == "json":
        return "json", arg
    # The kind only: the rest of the string could hold a password.
    raise ValueError(f"CONDITIONS starts with {kind!r}: it must be 'db', 'db:SERVICE', "
                     "'db:<libpq conninfo>', 'json' or 'json:DIR'.")


def _describe_conninfo(conninfo):
    """host/port/dbname of a conninfo, as PICondPgLayer::Describe prints them; the same
    answer as pioneer.conddb.pgservice.describe for the strings the renderer writes."""
    keys = dict(t.split("=", 1) for t in str(conninfo).split() if "=" in t)
    out = "host=" + (keys.get("host") or keys.get("hostaddr") or "<default>")
    out += (" port=" + keys["port"]) if keys.get("port") else ""
    out += (" dbname=" + keys["dbname"]) if keys.get("dbname") else ""
    return out


# The conditions source, resolved. _COND_KIND is "db" or "json" (None when CONDITIONS
# does not parse), _PG_CONNINFO the explicit conninfo handed to PgConnections in db mode,
# _JSON_DIR the directory the containers resolve against in json mode. Every failure is
# kept in _COND_PROBLEM for check() to report with the rest.
_COND_KIND, _PG_CONNINFO, _JSON_DIR, _COND_PROBLEM = None, "", CONDITIONS_DIR, ""
try:
    _COND_KIND, _cond_arg = _split_conditions(CONDITIONS)
except ValueError as _exc:
    _COND_PROBLEM = str(_exc)
else:
    if _COND_KIND == "json":
        _JSON_DIR = os.path.expanduser(_cond_arg) if _cond_arg else CONDITIONS_DIR
    elif RENDERED:
        # The renderer already expanded the service and dropped any password.
        _PG_CONNINFO = _cond_arg
    else:
        try:
            from pioneer.conddb.pgservice import ServiceNotFound, resolve_conninfo
        except ImportError:
            _COND_PROBLEM = ("CONDITIONS names a database, and expanding it needs "
                             "pioneer.conddb.pgservice, which is not importable: put the "
                             "repository's python/ directory on PYTHONPATH, or use "
                             "python -m pioneer.nearline.process, which renders the job with "
                             "the conninfo already expanded.")
        else:
            try:
                _PG_CONNINFO = resolve_conninfo(_cond_arg)
            except ServiceNotFound as _exc:
                _COND_PROBLEM = (f"{_exc}. Define it in ~/.pg_service.conf (or point "
                                 "PGSERVICEFILE at a file that does), or process from a JSON "
                                 "snapshot instead: python -m pioneer.nearline.process <midas "
                                 "file> --out-dir DIR --conditions json:<snapshot dir> (README, "
                                 "\"Conditions DB down\").")
            except ValueError as _exc:
                # pgservice never echoes the string, so the message is safe to print.
                _COND_PROBLEM = f"CONDITIONS names a database that cannot be used: {_exc}"


def _cond(name):
    """A container name resolved against the JSON directory; absolute stays absolute."""
    text = str(name)
    return text if os.path.isabs(text) else os.path.join(_JSON_DIR, text)


def _odb(name):
    """An ODB spec or overrides file resolved against CONDITIONS_DIR, in either mode."""
    text = str(name)
    return text if os.path.isabs(text) else os.path.join(CONDITIONS_DIR, text)


def hist_file(path):
    """x.root -> x_hists.root; the file the nearline website reads."""
    text = str(path)
    return (text[:-len(".root")] if text.endswith(".root") else text) + "_hists.root"


# The containers this configuration will actually load, in layer order. check()
# verifies them and PIConditionsSvc below is handed these very lists. In db mode the
# overrides file is the only JSON: the database serves every other table, and a
# container passed next to it would win over it (the JSON layer comes first).
_JSON_MODE = _COND_KIND == "json"
# The committed container settings; a test holds them to the settings block. db mode
# reads none of these files, so a job that changes one -- in the block, an overrides
# file or a rendered copy -- expects something db mode would silently not do, and
# check() refuses it.
_JSON_ONLY_DEFAULTS = {
    "WD_CONDITIONS_FILES": ["bt2026_wavedream_timebase.json",
                            "bt2026_wavedream_calibration.json"],
    "PSM_GEOMETRY_FILES": ["bt2026_psm_geometry.json", "bt2026_psm_readout_map.json"],
    "PSM_CHANNEL_MAP_FILE": "bt2026_psm_channel_map.json",
}


def _stray_containers():
    """bt2026_*.json in a CONDITIONS_DIR that is not a reco_testbeam/conditions tree.

    In db mode CONDITIONS_DIR only supplies the ODB specs. A checkout of
    reco_testbeam/conditions always holds the git containers next to them, which is
    expected; any other directory holding them is somebody's copy of the constants,
    which db mode would not read.
    """
    tree = os.path.normpath(str(CONDITIONS_DIR))
    if tree.endswith(os.path.join("reco_testbeam", "conditions")) or not os.path.isdir(tree):
        return []
    return sorted(n for n in os.listdir(tree) if n.startswith("bt2026_") and n.endswith(".json"))
_JSON_FILES = []
if ODB_OVERRIDES:
    _JSON_FILES.append(_odb(ODB_OVERRIDES))
if WD_ENABLED and _JSON_MODE:
    _JSON_FILES += [_cond(f) for f in WD_CONDITIONS_FILES]
if PSM_DECODE and _JSON_MODE:
    _JSON_FILES += [_cond(f) for f in PSM_GEOMETRY_FILES]
# The channel map feeds the tracklet reco and, with the timewalk on, the MuPix
# monitor's counters S1-S5 and the S1 of the correction layer's histograms. The
# layer runs whenever PSM_DECODE does, so _TWC_TIMEWALK covers _MUPIX_TIMEWALK.
_MUPIX_TIMEWALK = bool(PSM_MUPIX_MONITOR and PSM_TIMEWALK and PSM_DECODE)
_TWC_TIMEWALK = bool(PSM_TIMEWALK and PSM_DECODE)
if (PSM_RECO or _MUPIX_TIMEWALK or _TWC_TIMEWALK) and PSM_CHANNEL_MAP_FILE and _JSON_MODE:
    _JSON_FILES.append(_cond(PSM_CHANNEL_MAP_FILE))
_ODB_TABLES = [_odb(f) for f in ODB_SPECS]


def check():
    """Every way this configuration is impossible, reported in one message."""
    problems = []
    if not NL_MIDAS or not NL_OUT:
        problems.append("NL_MIDAS and NL_OUT must both be set, as in: "
                        "NL_MIDAS=run.mid.lz4 NL_OUT=run.root gaudirun.py nearline_job.py. "
                        "The other way in needs no environment at all: "
                        "python -m pioneer.nearline.process <midas file> --out-dir DIR "
                        "renders this file with the paths filled in and runs that copy.")
    if NL_MIDAS and not os.path.exists(NL_MIDAS):
        problems.append(f"NL_MIDAS does not exist: {NL_MIDAS}")
    out_dir = os.path.dirname(os.path.abspath(NL_OUT)) if NL_OUT else ""
    if NL_OUT and not os.path.isdir(out_dir):
        problems.append(f"the directory of NL_OUT is not an existing directory: {out_dir}")
    if _COND_PROBLEM:
        problems.append(_COND_PROBLEM)
    _use_json = ("use json mode: CONDITIONS = \"json:DIR\", NL_CONDITIONS=json:DIR, "
                 "or python -m pioneer.nearline.process ... --conditions json:DIR")
    if _COND_KIND == "db":
        _changed = [n for n, v in _JSON_ONLY_DEFAULTS.items() if globals()[n] != v]
        if _changed:
            problems.append(f"{', '.join(_changed)} differ from the committed containers, but "
                            "CONDITIONS reads the database, which never opens those files; "
                            + _use_json + ".")
        _stray = _stray_containers()
        if _stray:
            problems.append(f"CONDITIONS_DIR {CONDITIONS_DIR} holds {', '.join(_stray)}, but "
                            "CONDITIONS reads the database and takes only the ODB specs from "
                            "that directory. To process those containers, " + _use_json
                            + "; to read the database, point NL_CONDITIONS_DIR at a "
                            "reco_testbeam/conditions checkout or a directory with odb/ only.")
    for path in _JSON_FILES + _ODB_TABLES:
        if not os.path.exists(path):
            problems.append(f"conditions container does not exist: {path}")
    if not WD_ENABLED and not PSM_DECODE:
        problems.append("both halves are off (WD_ENABLED, PSM_DECODE): nothing would decode.")
    if PSM_RECO and not PSM_DECODE:
        problems.append("PSM_RECO is on but PSM_DECODE is off: only the musip decoding "
                        f"tool produces {_TES_MUQUAD} and {_TES_MUTRIG}.")
    if PSM_DECODE and not PSM_GEOMETRY_BASE:
        problems.append("PSM_DECODE is on but PSM_GEOMETRY_BASE is empty: PIGeometrySvc has "
                        "nothing to build a GeoHeader from and the decoder throws on hit one.")
    if PSM_GEOMETRY_BASE and not str(PSM_GEOMETRY_BASE).startswith("GEOCOND:"):
        problems.append(f"PSM_GEOMETRY_BASE '{PSM_GEOMETRY_BASE}' is not a "
                        "'GEOCOND:<table>' layer, which is the only form this job takes.")
    if PSM_MUPIX_MONITOR and not PSM_DECODE:
        problems.append("PSM_MUPIX_MONITOR is on but PSM_DECODE is off: only the musip "
                        f"decoding tool produces {_TES_MUQUAD}, and the monitor takes the "
                        "chip footprints from the PIGeometrySvc that PSM_DECODE creates.")
    if PSM_MUPIX_MONITOR and int(PSM_MUPIX_PIXELS_PER_BIN) < 1:
        problems.append(f"PSM_MUPIX_PIXELS_PER_BIN is {PSM_MUPIX_PIXELS_PER_BIN}: it is how "
                        "many pixels share one bin of a hit map, so it must be at least 1.")
    if PSM_MUPIX_MONITOR and (float(PSM_MUPIX_DT_RANGE_NS) <= 0 or int(PSM_MUPIX_DT_BINS) < 1):
        problems.append(f"PSM_MUPIX_DT_RANGE_NS ({PSM_MUPIX_DT_RANGE_NS}) is the half-width "
                        f"of a symmetric axis and PSM_MUPIX_DT_BINS ({PSM_MUPIX_DT_BINS}) its "
                        "bin count; both must be positive.")
    if PSM_MUPIX_MONITOR and (float(PSM_MUPIX_EXPANDED_RANGE_MM) <= 0
                              or float(PSM_MUPIX_CENTRAL_SLOPE_MRAD) <= 0):
        problems.append(f"PSM_MUPIX_EXPANDED_RANGE_MM ({PSM_MUPIX_EXPANDED_RANGE_MM}) and "
                        f"PSM_MUPIX_CENTRAL_SLOPE_MRAD ({PSM_MUPIX_CENTRAL_SLOPE_MRAD}) are "
                        "half-widths of symmetric axes; both must be positive.")
    if PSM_MUPIX_MONITOR and float(PSM_MUPIX_WINDOW_NS) <= 0:
        problems.append(f"PSM_MUPIX_WINDOW_NS is {PSM_MUPIX_WINDOW_NS}: it is a half-window, "
                        "so a non-positive value pairs nothing at all.")
    if PSM_SMA_MONITOR and not PSM_DECODE:
        problems.append("PSM_SMA_MONITOR is on but PSM_DECODE is off: only the musip "
                        f"decoding tool produces {_TES_MUTRIG}, which the SMA calibration "
                        f"layer turns into the {_TES_MUTRIG_CAL} the monitor reads, and the "
                        "monitor takes the raw MUTRIG channel map from the PIGeometrySvc "
                        "that PSM_DECODE creates.")
    if PSM_SMA_MONITOR and int(PSM_SMA_HITS_PER_EVENT_MAX) < 1:
        problems.append(f"PSM_SMA_HITS_PER_EVENT_MAX is {PSM_SMA_HITS_PER_EVENT_MAX}: it is "
                        "the top of an axis counting hits per event, so it must be at "
                        "least 1.")
    if PSM_SMA_MONITOR and not (0 < float(PSM_SMA_DEGENERATE_TOT_SHARE) <= 1
                                and 0 < float(PSM_SMA_MARKER_TOT_SHARE) <= 1):
        problems.append(f"PSM_SMA_DEGENERATE_TOT_SHARE ({PSM_SMA_DEGENERATE_TOT_SHARE}) and "
                        f"PSM_SMA_MARKER_TOT_SHARE ({PSM_SMA_MARKER_TOT_SHARE}) are shares of "
                        "one counter's hits; both must be inside (0, 1].")
    if PSM_SMA_COARSE_SHIFT is not None:
        try:
            shift_ok = (int(PSM_SMA_COARSE_SHIFT) == PSM_SMA_COARSE_SHIFT
                        and 0 <= int(PSM_SMA_COARSE_SHIFT) <= 18)
        except (TypeError, ValueError):
            shift_ok = False
        if not shift_ok:
            problems.append(f"PSM_SMA_COARSE_SHIFT is {PSM_SMA_COARSE_SHIFT!r}: it must be None "
                            "(from the conditions) or an integer 0-18; above 18 the coarse "
                            "field no longer pins the fine field's 2^20 ns wrap.")
    if PSM_GEOMETRY_TAG is not None and not (isinstance(PSM_GEOMETRY_TAG, str)
                                             and PSM_GEOMETRY_TAG):
        problems.append(f"PSM_GEOMETRY_TAG is {PSM_GEOMETRY_TAG!r}: it must be None (the "
                        "table's default tag) or the name of a tag of the geometry table.")
    if PSM_PIXEL_MASK_TAG is not None and not (isinstance(PSM_PIXEL_MASK_TAG, str)
                                               and PSM_PIXEL_MASK_TAG):
        problems.append(f"PSM_PIXEL_MASK_TAG is {PSM_PIXEL_MASK_TAG!r}: it must be None (the "
                        "table's default tag) or the name of a tag of mupix_pixel_mask.")
    if not (isinstance(PSM_MUPIX_TOT_CUT, int) and not isinstance(PSM_MUPIX_TOT_CUT, bool)
            and -1 <= PSM_MUPIX_TOT_CUT <= 31):
        problems.append(f"PSM_MUPIX_TOT_CUT is {PSM_MUPIX_TOT_CUT!r}: it must be an integer in "
                        "[-1, 31] (MuPix hits with a ToT at or below it are dropped in the "
                        "decoder; -1 keeps every hit).")
    if not (isinstance(PSM_MUPIX_HIGH_TOT_MIN, int) and not isinstance(PSM_MUPIX_HIGH_TOT_MIN, bool)
            and -1 <= PSM_MUPIX_HIGH_TOT_MIN <= 31):
        problems.append(f"PSM_MUPIX_HIGH_TOT_MIN is {PSM_MUPIX_HIGH_TOT_MIN!r}: it must be an "
                        "integer in [-1, 31] (the MuPix monitor's high-ToT maps keep the pairs "
                        "whose L1 and L2 hits both have a ToT at or above it; -1 books none).")
    if PSM_TIMEWALK_CORRECTION_TAG is not None and not (
            isinstance(PSM_TIMEWALK_CORRECTION_TAG, str) and PSM_TIMEWALK_CORRECTION_TAG):
        problems.append(f"PSM_TIMEWALK_CORRECTION_TAG is {PSM_TIMEWALK_CORRECTION_TAG!r}: it "
                        "must be None (the table's default tag) or the name of a tag of "
                        "mupix_timewalk.")
    if not isinstance(PSM_TWC_NTUPLE, str) or PSM_TWC_NTUPLE not in _TWC_NTUPLE_DROP:
        problems.append(f"PSM_TWC_NTUPLE is {PSM_TWC_NTUPLE!r}: it must be one of "
                        f"{', '.join(repr(k) for k in _TWC_NTUPLE_DROP)} (which MuPix hit "
                        "collections the RNTuple keeps).")
    if not isinstance(PSM_SMA_CAL_NTUPLE, str) or PSM_SMA_CAL_NTUPLE not in _SMA_CAL_NTUPLE_DROP:
        problems.append(f"PSM_SMA_CAL_NTUPLE is {PSM_SMA_CAL_NTUPLE!r}: it must be one of "
                        f"{', '.join(repr(k) for k in _SMA_CAL_NTUPLE_DROP)} (which SMA hit "
                        "collections the RNTuple keeps).")
    for name, value in (("LIGHT", LIGHT), ("PSM_PIXEL_MASK", PSM_PIXEL_MASK),
                        ("PSM_TIMEWALK", PSM_TIMEWALK),
                        ("PSM_TIMEWALK_CORRECTION", PSM_TIMEWALK_CORRECTION),
                        ("PSM_SMA_WIDE_DT", PSM_SMA_WIDE_DT),
                        ("PSM_SMA_FINE_OFFSETS", PSM_SMA_FINE_OFFSETS),
                        ("PSM_SMA_NIM_LAG", PSM_SMA_NIM_LAG),
                        ("PSM_SMA_SKIP_STALE_FIRST_FRAME", PSM_SMA_SKIP_STALE_FIRST_FRAME),
                        ("PSM_SMA_NIM_PAIRING", PSM_SMA_NIM_PAIRING),
                        ("PSM_SMA_HITS_NTUPLE", PSM_SMA_HITS_NTUPLE)):
        if not isinstance(value, bool):
            problems.append(f"{name} is {value!r}: it must be True or False (a string such as "
                            "'False' is true in Python and would switch it on).")
    for name, value in (("PSM_SMA_MAX_FRAME_SPAN_MS", PSM_SMA_MAX_FRAME_SPAN_MS),
                        ("PSM_SMA_MIN_FINE_COARSE_SHARE", PSM_SMA_MIN_FINE_COARSE_SHARE)):
        if not (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            problems.append(f"{name} is {value!r}: it must be a number of at least 0 "
                            "(0 switches the cut off).")
    if (isinstance(PSM_SMA_MIN_FINE_COARSE_SHARE, (int, float))
            and not isinstance(PSM_SMA_MIN_FINE_COARSE_SHARE, bool)
            and PSM_SMA_MIN_FINE_COARSE_SHARE > 1):
        problems.append(f"PSM_SMA_MIN_FINE_COARSE_SHARE is {PSM_SMA_MIN_FINE_COARSE_SHARE!r}: "
                        "it is a share of the bank's SMA words, so at most 1.")
    if (not isinstance(PSM_SMA_MAX_NARROW_PAIRS, int) or isinstance(PSM_SMA_MAX_NARROW_PAIRS, bool)
            or not 0 <= PSM_SMA_MAX_NARROW_PAIRS <= 2**32 - 1):
        problems.append(f"PSM_SMA_MAX_NARROW_PAIRS is {PSM_SMA_MAX_NARROW_PAIRS!r}: it must be "
                        "an integer from 0 to 4294967295 (0 means no limit).")
    try:
        tw_axis_ok = (float(PSM_TIMEWALK_DT_MAX) > float(PSM_TIMEWALK_DT_MIN)
                      and int(PSM_TIMEWALK_DT_BINS) == PSM_TIMEWALK_DT_BINS
                      and 1 <= int(PSM_TIMEWALK_DT_BINS) <= 8192)
    except (TypeError, ValueError):
        tw_axis_ok = False
    if PSM_TIMEWALK and not tw_axis_ok:
        problems.append(f"PSM_TIMEWALK_DT_MIN/MAX/BINS ({PSM_TIMEWALK_DT_MIN!r}, "
                        f"{PSM_TIMEWALK_DT_MAX!r}, {PSM_TIMEWALK_DT_BINS!r}) are the timewalk dt "
                        "axis [min, max) in bins: max must be above min and bins an integer "
                        "1-8192, or the correction layer stops at initialize and the monitor "
                        "and the reco book no timewalk histograms.")
    def _number(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    if not (_number(PSM_SMA_PAIR_WINDOW_NS) and 0 < PSM_SMA_PAIR_WINDOW_NS <= 1000):
        problems.append(f"PSM_SMA_PAIR_WINDOW_NS is {PSM_SMA_PAIR_WINDOW_NS!r}: it must be a "
                        "number of ns above 0 and at most 1000 (the largest aligned "
                        "|t_NIM - t_TOT| of a pair; the RF period is about 20 ns).")
    if PSM_SMA_TIME_SOURCE not in ("tot", "nim"):
        problems.append(f"PSM_SMA_TIME_SOURCE is {PSM_SMA_TIME_SOURCE!r}: it must be 'tot' (the "
                        "TOT word's time) or 'nim' (the NIM copy's).")
    if not (_number(PSM_SMA_NIM_ONLY_TOT) and PSM_SMA_NIM_ONLY_TOT <= 255
            and (not _number(PSM_LAYER_THR) or PSM_SMA_NIM_ONLY_TOT > PSM_LAYER_THR)):
        problems.append(f"PSM_SMA_NIM_ONLY_TOT is {PSM_SMA_NIM_ONLY_TOT!r}: it must be a ToT "
                        f"above PSM_LAYER_THR ({PSM_LAYER_THR!r}) and at most 255 in raw SMA "
                        "units (the value a NIM-only hit is written with; at or below the layer "
                        "threshold the hit would fire no layer).")
    if not (isinstance(PSM_SMA_OFFSET_OVERRIDE_NS, dict) and all(
            isinstance(k, int) and not isinstance(k, bool) and _number(v)
            for k, v in PSM_SMA_OFFSET_OVERRIDE_NS.items())):
        problems.append(f"PSM_SMA_OFFSET_OVERRIDE_NS is {PSM_SMA_OFFSET_OVERRIDE_NS!r}: it must "
                        "be a dict {detector id (int): offset in ns (number)}, normally empty.")
    if not (isinstance(PSM_SMA_NIM_NOMINAL_DELAY_NS, dict) and all(
            isinstance(k, int) and not isinstance(k, bool) and _number(v) and abs(v) < 2**19
            for k, v in PSM_SMA_NIM_NOMINAL_DELAY_NS.items())):
        problems.append(f"PSM_SMA_NIM_NOMINAL_DELAY_NS is {PSM_SMA_NIM_NOMINAL_DELAY_NS!r}: it "
                        "must be a dict {NIM copy id (int): nominal delay from S1 in ns (number, "
                        "|ns| < 2^19)}.")
    _lag_role_ids = {2001: "the S1 reference", 2003: "the voted S2", 2006: "the halved S5",
                     2002: "the parked id, on several channels at once",
                     2014: "the RF role marker", 2015: "the proton-current role marker"}
    if not (isinstance(PSM_SMA_LAG_VIDS, (list, tuple))
            and all(isinstance(v, int) and not isinstance(v, bool) for v in PSM_SMA_LAG_VIDS)
            and len(set(PSM_SMA_LAG_VIDS)) == len(PSM_SMA_LAG_VIDS)):
        problems.append(f"PSM_SMA_LAG_VIDS is {PSM_SMA_LAG_VIDS!r}: it must be a list of distinct "
                        "detector ids (int), normally the NIM copies [2021, 2023, 2024, 2025, 2026].")
    elif any(v in _lag_role_ids for v in PSM_SMA_LAG_VIDS):
        taken = ", ".join(f"{v} ({_lag_role_ids[v]})" for v in PSM_SMA_LAG_VIDS if v in _lag_role_ids)
        problems.append(f"PSM_SMA_LAG_VIDS lists {taken}: that id has its own role in the fine-time "
                        "correction and cannot also be a lag id.")
    def _sma_channel_ok(value):
        return (value is None or value == "off"
                or (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 15))
    if not _sma_channel_ok(PSM_RF_CHANNEL):
        problems.append(f"PSM_RF_CHANNEL is {PSM_RF_CHANNEL!r}: it must be None (the RF channel "
                        "of the run's mutrig_channel_map), \"off\" or an integer SMA raw channel "
                        "0-15 (the word's 4-bit channel field), or /Event/rf holds no RF or the "
                        "wrong pulses and the SMA monitor's RF phase is meaningless.")
    if not _sma_channel_ok(PSM_CURRENT_CHANNEL):
        problems.append(f"PSM_CURRENT_CHANNEL is {PSM_CURRENT_CHANNEL!r}: it must be None (the "
                        "proton-current channel of the run's mutrig_channel_map), \"off\" or an "
                        "integer SMA raw channel 0-15, or histograms/musip/current counts the wrong "
                        "pulses.")
    if (isinstance(PSM_RF_CHANNEL, int) and PSM_RF_CHANNEL == PSM_CURRENT_CHANNEL
            and _sma_channel_ok(PSM_RF_CHANNEL)):
        problems.append(f"PSM_RF_CHANNEL and PSM_CURRENT_CHANNEL are both {PSM_RF_CHANNEL}: the "
                        "decoder takes the RF channel first, so the current pulses would become "
                        "RF pulses and the current would never be counted.")
    # The container checks below are about which JSON file supplies a table, so they
    # mean nothing in db mode, where the database serves them all.
    if _JSON_MODE and PSM_DECODE and PSM_GEOMETRY_BASE and not PSM_GEOMETRY_FILES:
        problems.append("PSM_GEOMETRY_BASE is a GEOCOND layer but PSM_GEOMETRY_FILES is "
                        "empty: nothing would supply the table it names.")
    if "COND:isel" in PSM_GEOMETRY_TRANS and "bt2026_isel.json" not in {
            os.path.basename(str(p)) for p in ODB_SPECS}:
        problems.append("PSM_GEOMETRY_TRANS has 'COND:isel' but ODB_SPECS has no "
                        "bt2026_isel.json, so nothing maps the isel table.")
    if _JSON_MODE and PSM_DECODE and PSM_PIXEL_MASK and "bt2026_psm_readout_map.json" not in {
            os.path.basename(str(p)) for p in PSM_GEOMETRY_FILES}:
        problems.append("PSM_PIXEL_MASK is on but PSM_GEOMETRY_FILES has no "
                        "bt2026_psm_readout_map.json, so nothing supplies the mupix_pixel_mask "
                        "table and the decoder stops at initialize; add it, or set "
                        "PSM_PIXEL_MASK = False to decode without a mask.")
    if (_JSON_MODE and PSM_DECODE and PSM_TIMEWALK_CORRECTION
            and "bt2026_psm_readout_map.json" not in {
                os.path.basename(str(p)) for p in PSM_GEOMETRY_FILES}):
        problems.append("PSM_TIMEWALK_CORRECTION is on but PSM_GEOMETRY_FILES has no "
                        "bt2026_psm_readout_map.json, so nothing supplies the mupix_timewalk "
                        "table and the correction layer stops at initialize; add it, or set "
                        "PSM_TIMEWALK_CORRECTION = False to pass the MuPix hits through "
                        "uncorrected.")
    if int(PSM_WEIGHT_STRATEGY) not in (0, 1, 2):
        problems.append(f"PSM_WEIGHT_STRATEGY is {PSM_WEIGHT_STRATEGY}: it must be 0 (weight "
                        "1 for every tracklet), 1 (L1/L2 window containment) or 2 (also the "
                        "track's stop-layer depth).")
    if int(PSM_WEIGHT_STRATEGY) >= 1 and not (PSM_DECODE and PSM_GEOMETRY_BASE):
        problems.append("PSM_WEIGHT_STRATEGY >= 1 needs PIGeometrySvc for the L1/L2 plane "
                        "footprints: set PSM_DECODE and PSM_GEOMETRY_BASE, or fall back to "
                        "strategy 0.")
    if int(PSM_WEIGHT_STRATEGY) >= 1 and "COND:isel" not in PSM_GEOMETRY_TRANS:
        problems.append("PSM_WEIGHT_STRATEGY >= 1 but PSM_GEOMETRY_TRANS has no 'COND:isel': "
                        "every run is then treated as sitting at the design stage position, "
                        "which is silently wrong for any run that is not.")
    wants_calib = bool(WD_ALIGN_TABLE) or bool(WD_ECAL_TABLE)
    if WD_ENABLED and wants_calib and not (WD_ALIGN_TABLE and WD_ECAL_TABLE):
        problems.append("PIWDCalibrator needs BOTH WD_ALIGN_TABLE and WD_ECAL_TABLE; "
                        "set both or clear both.")
    # Supersedes the narrower "required whenever the calibration tables are":
    # PIWDWaveformAnalysis consumes the phase too, for its monitoring, so the
    # RF algorithm now runs in every WaveDREAM job.
    if WD_ENABLED and not WD_RF_TABLE:
        problems.append("WD_RF_TABLE is empty while WD_ENABLED is on: PIWDRFPhase runs "
                        "first in WDAnalysisSeq and PIWDWaveformAnalysis reads "
                        "/Event/wd_rf_phase, so WD_RF_TABLE cannot be empty.")
    if _JSON_MODE and WD_ENABLED and WD_ROLE_TABLE and not WD_CONDITIONS_FILES:
        problems.append("WD_ROLE_TABLE is set but WD_CONDITIONS_FILES is empty: nothing "
                        "would supply the wd_channel_map table.")
    if WD_ENABLED and WD_CHANNEL_SETTINGS_TABLE and not ODB_SPECS:
        problems.append("WD_CHANNEL_SETTINGS_TABLE is set but ODB_SPECS is empty: only the "
                        "begin-of-run ODB dump serves wd_channel_settings, so the "
                        "per-channel thresholds would not resolve.")
    if WD_ENABLED and not set(WD_CAL_CHANNELS) <= set(WD_CHANNELS):
        problems.append(f"WD_CAL_CHANNELS {sorted(set(WD_CAL_CHANNELS) - set(WD_CHANNELS))} are "
                        "not in WD_CHANNELS: they would have no features to calibrate.")
    if WD_SCALER_MONITOR and not WD_ENABLED:
        problems.append("WD_SCALER_MONITOR is on but WD_ENABLED is off: only the WaveDREAM "
                        f"decoding tool produces {_TES_SCALERS}.")
    if WD_SCALER_MONITOR and not (0 < float(WD_SCALER_TIME_BIN_S) < float(WD_SCALER_TIME_MAX_S)):
        problems.append(f"WD_SCALER_TIME_BIN_S ({WD_SCALER_TIME_BIN_S}) must be positive and "
                        f"below WD_SCALER_TIME_MAX_S ({WD_SCALER_TIME_MAX_S}).")
    if WD_SCALER_MONITOR and len(set(WD_SCALER_BOARDS)) != len(WD_SCALER_BOARDS):
        problems.append(f"WD_SCALER_BOARDS {list(WD_SCALER_BOARDS)} lists a serial twice.")
    if WD_RF_REFINE and int(WD_RF_REFINE_POINTS) < 2:
        problems.append(f"WD_RF_REFINE is on but WD_RF_REFINE_POINTS is "
                        f"{WD_RF_REFINE_POINTS}; a scan needs at least 2 points.")
    _phase_space_used = PSM_RECO or PSM_MUPIX_MONITOR
    if _phase_space_used and (int(PSM_PHASE_SPACE_BINS) <= 0 or int(PSM_PHASE_SPACE_BINS) % 64):
        problems.append(f"PSM_PHASE_SPACE_BINS is {PSM_PHASE_SPACE_BINS}: it must be a "
                        "positive multiple of 64, or the phase-space histograms do not "
                        "rebin onto the 64-bin minitwin export exactly (320 = 5 x 64).")
    if _phase_space_used and (float(PSM_PHASE_SPACE_POS_RANGE_MM) <= 0
                     or float(PSM_PHASE_SPACE_SLOPE_RANGE_MRAD) <= 0):
        problems.append(f"PSM_PHASE_SPACE_POS_RANGE_MM ({PSM_PHASE_SPACE_POS_RANGE_MM}) and "
                        f"PSM_PHASE_SPACE_SLOPE_RANGE_MRAD "
                        f"({PSM_PHASE_SPACE_SLOPE_RANGE_MRAD}) are half-widths of a "
                        "symmetric axis, so both must be positive.")
    if PSM_RECO and not (float(PSM_L_WINDOW_AFTER_NS) > -float(PSM_L_WINDOW_BEFORE_NS)):
        problems.append(f"PSM_L_WINDOW_BEFORE_NS ({PSM_L_WINDOW_BEFORE_NS}) and "
                        f"PSM_L_WINDOW_AFTER_NS ({PSM_L_WINDOW_AFTER_NS}) make the L-hit window "
                        "[t - before, t + after) empty, so no tracklet would get an L pair.")
    try:
        _owner_offset_ok = math.isfinite(float(PSM_LPAIR_OWNER_OFFSET_NS))
    except (TypeError, ValueError):
        _owner_offset_ok = False
    if PSM_RECO and not _owner_offset_ok:
        problems.append(f"PSM_LPAIR_OWNER_OFFSET_NS is {PSM_LPAIR_OWNER_OFFSET_NS!r}; it must be a finite number "
                        "of ns (the expected t(L1) - t(S1) used to choose the owner of a shared L1/L2 pair).")
    if PSM_RECO and PSM_AGGREGATE_OWNERS_ONLY not in (0, 1):
        problems.append(f"PSM_AGGREGATE_OWNERS_ONLY is {PSM_AGGREGATE_OWNERS_ONLY!r}; use 1 (only the owner "
                        "of each L1/L2 pair fills the phase-space histograms) or 0 (every tracklet).")
    if PSM_RECO and not (0 < float(PSM_SCINT_WINDOW_NS) < float(PSM_DELAYED_WINDOW_NS[0])):
        problems.append(f"PSM_SCINT_WINDOW_NS ({PSM_SCINT_WINDOW_NS}) must be positive and below "
                        f"PSM_DELAYED_WINDOW_NS[0] ({PSM_DELAYED_WINDOW_NS[0]}): a wider S-S "
                        "clustering window absorbs the delayed pulse into its prompt cluster, "
                        "and the delayed-coincidence tag can no longer find it.")
    if PSM_RECO and not (0 <= float(PSM_DELAYED_TOT_MIN) < math.inf):
        problems.append(f"PSM_DELAYED_TOT_MIN ({PSM_DELAYED_TOT_MIN}) must be a finite ToT >= 0, "
                        "the lower edge of the delayed summed-ToT band.")
    if PSM_RECO and PSM_DELAYED_TOT_MAX is not None and not (
            float(PSM_DELAYED_TOT_MAX) > float(PSM_DELAYED_TOT_MIN)):
        problems.append(f"PSM_DELAYED_TOT_MAX ({PSM_DELAYED_TOT_MAX}) must be larger than "
                        f"PSM_DELAYED_TOT_MIN ({PSM_DELAYED_TOT_MIN}), or None for no upper edge: "
                        "the delayed ToT band [MIN, MAX) would take no candidate.")
    _dt_width = float(PSM_DELAYED_WINDOW_NS[1]) - float(PSM_DELAYED_WINDOW_NS[0])
    if PSM_RECO and not (isinstance(PSM_DELAYED_DT_BINS, int) and PSM_DELAYED_DT_BINS == _dt_width):
        problems.append(f"PSM_DELAYED_DT_BINS ({PSM_DELAYED_DT_BINS!r}) must be the width of "
                        f"PSM_DELAYED_WINDOW_NS in ns ({_dt_width:g}): dt and sb have one bin per ns.")
    if PSM_RECO and PSM_PROMPT_RF_WINDOW_NS is not None and not (
            float(PSM_PROMPT_RF_WINDOW_NS[0]) < float(PSM_PROMPT_RF_WINDOW_NS[1])):
        problems.append(f"PSM_PROMPT_RF_WINDOW_NS {PSM_PROMPT_RF_WINDOW_NS} is empty; give [MIN, MAX) "
                        "with MIN < MAX, or None for every prompt.")
    if PSM_RECO and not all(isinstance(x, int) and 0 <= x <= 5 for x in PSM_TAGGED_STOP_LAYERS):
        problems.append(f"PSM_TAGGED_STOP_LAYERS {PSM_TAGGED_STOP_LAYERS!r} must be stop layers 0..5 "
                        "(1..5 = S1..S5), or () for every layer.")
    if PSM_RECO and PSM_FAR_WINDOW_NS is not None and not (
            float(PSM_FAR_WINDOW_NS[0]) < float(PSM_FAR_WINDOW_NS[1]) <= -float(PSM_DELAYED_WINDOW_NS[1])):
        problems.append(f"PSM_FAR_WINDOW_NS {PSM_FAR_WINDOW_NS} must be [MIN, MAX) with MIN < MAX <= "
                        f"-PSM_DELAYED_WINDOW_NS[1] ({-float(PSM_DELAYED_WINDOW_NS[1])}): an accidental "
                        "window that reaches the mirrored sideband or the signal counts decays.")
    if PSM_RECO and PSM_PAIR_WINDOW_NS is not None and not (
            float(PSM_PAIR_WINDOW_NS[0]) < float(PSM_PAIR_WINDOW_NS[1])):
        problems.append(f"PSM_PAIR_WINDOW_NS {PSM_PAIR_WINDOW_NS} is empty; give [MIN, MAX) with "
                        "MIN < MAX, or None.")
    if PSM_RECO and PSM_DROP_CROSSTALK_GHOSTS and not (PSM_DECODE and PSM_GEOMETRY_BASE):
        problems.append("PSM_DROP_CROSSTALK_GHOSTS is on but there is no PIGeometrySvc "
                        "(PSM_DECODE, PSM_GEOMETRY_BASE): the ghost rule recovers each hit's "
                        "column and row from the chip placement it serves.")
    if OUTPUT_LEVEL not in _LEVELS:
        problems.append(f"OUTPUT_LEVEL '{OUTPUT_LEVEL}' is not one of {sorted(_LEVELS)}.")
    if problems:
        raise SystemExit("nearline_job: configuration is inconsistent:\n  - "
                         + "\n  - ".join(problems))


check()

# No bank filter on purpose: a combined job needs waveform events (DRSV), scaler
# events and musip readout events (H000), and the filter is any-of over a fixed
# bank list, so any list would drop one the first time an event id changed.
selector = PIMidasSelector("EventSelector", file=str(NL_MIDAS))
condSvc = PIConditionsSvc(JsonFiles=_JSON_FILES, OdbTables=_ODB_TABLES,
                          Preload=(list(ODB_PRELOAD) if _ODB_TABLES else [])
                          + ([_TIMEBASE_TABLE] if WD_ENABLED else []))
if _COND_KIND == "db":
    # One server. It is reached once, at initialize, and an unreachable one fails the
    # job there (about 20 s: two attempts of libpq's connect_timeout); nothing falls
    # back to the JSON containers.
    condSvc.PgConnections = [_PG_CONNINFO]

# The ORDER of this list is load-bearing. PIMidasSelector publishes the
# begin-of-run ODB dump into PIHeaderSvc as "ODBHeader" during its own
# initialize(); PIConditionsSvc reads /Runinfo/Run number out of that header
# during ITS initialize() to pick intervals of validity; PIGeometrySvc needs the
# conditions service resolved before it. Reorder and every table resolves run 0.
services = [PIHeaderSvc(), selector, PIMidasConversionSvc("ConversionSvc"),
            EvtDataSvc(), PIDataModelSvc(), condSvc]
if PSM_DECODE:
    geo_svc = PIGeometrySvc(Base=str(PSM_GEOMETRY_BASE),
                            Trans=[str(t) for t in PSM_GEOMETRY_TRANS],
                            Maps=[str(m) for m in PSM_GEOMETRY_MAPS])
    if PSM_GEOMETRY_TAG:
        geo_svc.GeometryTag = str(PSM_GEOMETRY_TAG)
    services.append(geo_svc)
services.append(PIHistogramSvc(OutputFile=hist_file(NL_OUT)))
services.append(EvtPersistencySvc(CnvServices=["PIMidasConversionSvc/ConversionSvc"]))
audit = AuditorSvc()
audit.Auditors += [ChronoAuditor()]
services.append(audit)

# What the banner adds after PSM_MUPIX_TOT_CUT: nothing once the decoder takes it.
_TOT_CUT_NOTE = " (no decode)"
# The same for PSM_MUPIX_HIGH_TOT_MIN: nothing once the monitor takes it.
_HIGH_TOT_NOTE = " (no monitor)"
# The same for the two SMA bank cuts and the monitor's narrow pair budget.
_SMA_BANK_NOTE = " (no decode)"
_SMA_PAIRS_NOTE = " (no monitor)"
tools = [PITMidasWaveDream()] if WD_ENABLED else []
if PSM_DECODE:
    musip = PITMidasMusip(quadPixelPitch=float(PSM_QUAD_PIXEL_PITCH),
                          quadTimeBinWidth=float(PSM_QUAD_TIME_BIN_NS))
    # Unset, the decoder takes the RF and current channels from the run's raw
    # map (its role ids 2014/2015) and says at initialize where each came from;
    # set, they override the map; "off" (the decoder's -2) switches the role
    # off. The RF channel is what creates /Event/rf and the current channel
    # what books histograms/musip/current.
    if PSM_RF_CHANNEL is not None:
        musip.rf_channel = -2 if PSM_RF_CHANNEL == "off" else int(PSM_RF_CHANNEL)
    if PSM_CURRENT_CHANNEL is not None:
        musip.current_channel = -2 if PSM_CURRENT_CHANNEL == "off" else int(PSM_CURRENT_CHANNEL)
    if PSM_SMA_COARSE_SHIFT is not None:
        musip.coarseShift = int(PSM_SMA_COARSE_SHIFT)
    musip.smaDiagnostics = bool(PSM_SMA_DIAGNOSTICS)
    musip.correctFineOffsets = bool(PSM_SMA_FINE_OFFSETS)
    # The correction's roles are the decoder's defaults, by detector id through
    # the run's raw map: S1 the reference, S2 voted, S5 halved, the RF role
    # snapped, the lag ids (PSM_SMA_LAG_VIDS, the NIM copies) lag channels. Only
    # the lag is a job knob.
    musip.fineOffsetLagVids = [int(v) for v in PSM_SMA_LAG_VIDS] if PSM_SMA_NIM_LAG else []
    if PSM_SMA_NIM_NOMINAL_DELAY_NS:
        musip.fineOffsetLagNominalNs = {int(k): float(v)
                                        for k, v in PSM_SMA_NIM_NOMINAL_DELAY_NS.items()}
    musip.skipFirstBank = bool(PSM_SMA_SKIP_STALE_FIRST_FRAME) and _SUBRUN == 0
    if PSM_SMA_SKIP_STALE_FIRST_FRAME and _SUBRUN is None:
        print(f"[nearline] WARNING    PSM_SMA_SKIP_STALE_FIRST_FRAME is on but "
              f"{Path(str(NL_MIDAS)).name} carries no subrun number, so no frame is skipped")
    musip.applyPixelMask = bool(PSM_PIXEL_MASK)
    if PSM_PIXEL_MASK_TAG:
        musip.pixelMaskTag = str(PSM_PIXEL_MASK_TAG)
    # Feature-detect: a reco_testbeam built before the cut existed would die at
    # option parsing on the unknown property.
    if "mupixTotCut" in PITMidasMusip.getDefaultProperties():
        musip.mupixTotCut = int(PSM_MUPIX_TOT_CUT)
        _TOT_CUT_NOTE = ""
    else:
        _TOT_CUT_NOTE = " (not applied: old reco)"
        print("[nearline] WARNING    the installed PITMidasMusip has no mupixTotCut "
              "property (reco_testbeam older than the MuPix ToT cut): every MuPix ToT "
              "is kept")
    # The bank cuts, always set so the job states them; feature-detected the same way.
    _musip_known = PITMidasMusip.getDefaultProperties()
    if "smaMaxFrameSpanMs" in _musip_known and "smaMinFineCoarseShare" in _musip_known:
        musip.smaMaxFrameSpanMs = float(PSM_SMA_MAX_FRAME_SPAN_MS)
        musip.smaMinFineCoarseShare = float(PSM_SMA_MIN_FINE_COARSE_SHARE)
        _SMA_BANK_NOTE = ""
    else:
        _SMA_BANK_NOTE = " (not applied: old reco)"
        print("[nearline] WARNING    the installed PITMidasMusip has no smaMaxFrameSpanMs / "
              "smaMinFineCoarseShare properties (reco_testbeam older than the bad-frame "
              "cuts): no SMA bank is dropped")
    tools.append(musip)

algorithms = [PIMidasDecoder(decoders=tools)]

if PSM_DECODE:
    # The SMA calibration layer, /Event/mutrig -> /Event/mutrig_cal and the
    # sidecar /Event/sma_hits, in a sequencer of its own straight after the
    # decoder and gated on the raw SMA hits. It aligns the counter hits, pairs each
    # counter's TOT word with its NIM copy where the run cables and calibrates one
    # (PSM_SMA_NIM_PAIRING), and puts the hits in time order (the decoder writes
    # them in readout order). It is not a
    # member of PSMSMASeq on purpose: the MuPix timewalk layer, the MuPix monitor
    # and the track reco read its output too, and must find it with the SMA
    # monitor switched off. It has to come before PSMTimewalkSeq, whose
    # CounterInput is /Event/mutrig_cal: in the sequential event loop the order
    # of this list is execution order, and the timewalk layer reads that input
    # as optional, so scheduled after it, it would find no SMA hits and leave
    # the twc_dt_vs_tot_* histograms empty without an error. Every event that
    # passes PSMSMASeq's or PSMRecoSeq's gate (/Event/mutrig_cal) has been
    # through it by construction.
    # The raw /Event/mutrig stays on the TES in readout order.
    sma_cal = PIPSMSMACalibration("PIPSMSMACalibration", input=_TES_MUTRIG,
                                  output=_TES_MUTRIG_CAL, hitsOutput=_TES_SMA_HITS,
                                  NimPairing=bool(PSM_SMA_NIM_PAIRING),
                                  PairWindowNs=float(PSM_SMA_PAIR_WINDOW_NS),
                                  TimeSource=str(PSM_SMA_TIME_SOURCE),
                                  NimOnlyTot=float(PSM_SMA_NIM_ONLY_TOT))
    if PSM_SMA_OFFSET_OVERRIDE_NS:
        sma_cal.OffsetOverrideNs = {int(k): float(v) for k, v in PSM_SMA_OFFSET_OVERRIDE_NS.items()}
    algorithms.append(Gaudi__Sequencer("PSMSMACalSeq", RequireObjects=[_TES_MUTRIG],
                                       Members=[sma_cal]))

    # The MuPix timewalk correction, /Event/muquad -> /Event/muquad_twc, in a
    # sequencer of its own after the SMA layer and gated on the raw hits.
    # It is not a member of PSMMuPixSeq on purpose: the track reco in PSMRecoSeq
    # reads its output too, and must find it with the MuPix monitor switched off.
    # Scheduled here it runs on every event with MuPix hits whatever the
    # consumers are set to, and since the decoder writes /Event/muquad and
    # /Event/mutrig together, every event that passes PSMMuPixSeq's gate
    # (/Event/muquad) or PSMRecoSeq's (/Event/mutrig_cal) already holds
    # /Event/muquad_twc: the same hits, corrected, in time order. Off,
    # PSM_TIMEWALK_CORRECTION still schedules it, uncorrected and without
    # reading the table, so the consumers have one input path whatever the
    # setting. The raw /Event/muquad stays on the TES.
    #
    # Its histograms, raw and corrected dt(pixel - S1) vs pixel ToT, follow
    # PSM_TIMEWALK like the monitor's and the reco's tw_*: CounterInput is the
    # time-ordered SMA hits from PSMSMACalSeq above (optional per frame, as for
    # the monitor) and S1 comes from the PSM channel map. Without them it books
    # only twc_hits.
    twc = PIPSMMuPixTimewalkCorrection(
        "PIPSMMuPixTimewalkCorrection", input=_TES_MUQUAD, output=_TES_MUQUAD_TWC,
        applyTimewalkCorrection=bool(PSM_TIMEWALK_CORRECTION), GeometrySvc="PIGeometrySvc",
        TimewalkDtMin=float(PSM_TIMEWALK_DT_MIN), TimewalkDtMax=float(PSM_TIMEWALK_DT_MAX),
        TimewalkDtBins=int(PSM_TIMEWALK_DT_BINS))
    if PSM_TIMEWALK_CORRECTION_TAG:
        twc.ConditionsTag = str(PSM_TIMEWALK_CORRECTION_TAG)
    if _TWC_TIMEWALK:
        twc.CounterInput = _TES_MUTRIG_CAL
        twc.ChannelMapTable = PSM_CHANNEL_MAP_TABLE
        if PSM_CHANNEL_MAP_TAG:
            twc.ChannelMapTag = PSM_CHANNEL_MAP_TAG
    algorithms.append(Gaudi__Sequencer("PSMTimewalkSeq", RequireObjects=[_TES_MUQUAD],
                                       Members=[twc]))

# Built once and shared by PIPSMComputeWeight, PIPSMAllTrackReco and the MuPix
# monitor, so the three algorithms' acceptance windows agree with each other.
_PSM_CONFIG_X = [float(p[0]) for p in PSM_POSITIONS_MM]
_PSM_CONFIG_Y = [float(p[1]) for p in PSM_POSITIONS_MM]

if WD_ENABLED and ODB_SPECS and SETTINGS_SUMMARY:
    algorithms.append(PIWDSettingsSummary(Tag=WD_TAG) if WD_TAG else PIWDSettingsSummary())

if WD_ENABLED:
    # RF first: PIWDWaveformAnalysis consumes /Event/wd_rf_phase, and this job
    # runs the sequential event loop, where Members order IS execution order.
    rf = PIWDRFPhase(RFTable=WD_RF_TABLE, TimebaseTable=_TIMEBASE_TABLE,
                     nbinsPhase=int(WD_PHASE_BINS), rfAmpMax=float(WD_AMP_MAX),
                     rfResidualMax=float(WD_RF_RESIDUAL_MAX),
                     refineFrequency=bool(WD_RF_REFINE),
                     refinePoints=int(WD_RF_REFINE_POINTS),
                     refineSpanFraction=float(WD_RF_REFINE_SPAN))
    if WD_TAG:
        rf.RFTag, rf.TimebaseTag = WD_TAG, WD_TAG
    if ODB_SPECS:
        rf.BoardSettingsTable = _BOARD_SETTINGS_TABLE

    ana = PIWDWaveformAnalysis(
        channels=list(WD_CHANNELS), baselineSamples=int(WD_BASELINE_SAMPLES),
        cfFraction=float(WD_CF_FRACTION), integratePreNs=float(WD_INTEGRATE_PRE_NS),
        integratePostNs=float(WD_INTEGRATE_POST_NS), ampMax=float(WD_AMP_MAX),
        timeMax=float(WD_TIME_MAX), ConditionsTable=_TIMEBASE_TABLE,
        nbinsPhase=int(WD_PHASE_BINS), pulseAmpMax=float(WD_PULSE_AMP_MAX),
        chargeMin=float(WD_CHARGE_MIN), chargeMax=float(WD_CHARGE_MAX),
        baselineMinV=float(WD_BASELINE_MIN_V), baselineMaxV=float(WD_BASELINE_MAX_V),
        baselineRmsMaxV=float(WD_BASELINE_RMS_MAX_V),
        scintThresholdFallbackV=float(WD_SCINT_THR_FALLBACK_V),
        nimThresholdFallbackV=float(WD_NIM_THR_FALLBACK_V),
        strictThresholds=bool(WD_STRICT_THRESHOLDS))
    if WD_TAG:
        ana.ConditionsTag = WD_TAG
    if ODB_SPECS:
        ana.BoardSettingsTable = _BOARD_SETTINGS_TABLE
    if WD_ROLE_TABLE:
        ana.RoleTable = WD_ROLE_TABLE
        if WD_ROLE_TAG or WD_TAG:
            ana.RoleTag = WD_ROLE_TAG or WD_TAG
    # Only the ODB layer serves the per-channel trigger levels; without it the
    # thresholds fall back per role, which the algorithm reports at initialize.
    if ODB_SPECS and WD_CHANNEL_SETTINGS_TABLE:
        ana.ChannelSettingsTable = WD_CHANNEL_SETTINGS_TABLE

    wd_members = [rf, ana]
    if WD_ALIGN_TABLE and WD_ECAL_TABLE:
        cal = PIWDCalibrator(TimeAlignTable=WD_ALIGN_TABLE, EnergyCalibTable=WD_ECAL_TABLE,
                             channels=list(WD_CAL_CHANNELS))
        if WD_TAG:
            cal.TimeAlignTag, cal.EnergyCalibTag = WD_TAG, WD_TAG
        wd_members.append(cal)
    algorithms.append(Gaudi__Sequencer("WDAnalysisSeq", Members=wd_members,
                                       RequireObjects=[_TES_WAVEFORM]))

if WD_ENABLED and WD_SCALER_MONITOR:
    # Its own sequencer: scaler events carry no waveforms, so WDAnalysisSeq's
    # gate never lets them through, and a waveform event carries no scalers.
    scaler_monitor = PIWDScalerMonitor(
        input=_TES_SCALERS, boards=[int(b) for b in WD_SCALER_BOARDS],
        timeBinS=float(WD_SCALER_TIME_BIN_S), timeMaxS=float(WD_SCALER_TIME_MAX_S),
        fillStale=bool(WD_SCALER_FILL_STALE))
    # The run's "current" input of the WaveDREAM role table is counted into
    # proton_current_counts (rate x interval, see PIWDScalerCount.hpp); the
    # rate counts / seconds times PIPSMSMACalibration's sma_live_seconds is the
    # normalisation combine_files.py prefers. A run whose table has no current
    # input, or WD_ROLE_TABLE = "", books none.
    if WD_ROLE_TABLE:
        scaler_monitor.RoleTable = WD_ROLE_TABLE
        if WD_ROLE_TAG or WD_TAG:
            scaler_monitor.RoleTag = WD_ROLE_TAG or WD_TAG
    algorithms.append(Gaudi__Sequencer("WDScalerSeq", RequireObjects=[_TES_SCALERS],
                                       Members=[scaler_monitor]))

if PSM_MUPIX_MONITOR:
    # Its own sequencer, gated on the MuPix hits and not on the scintillators:
    # this is the check that has to keep running when the scintillator half of
    # the telescope, or the tracklet reco that needs it, is what is broken.
    # DistanceL12 is deliberately not set -- the algorithm measures the lever
    # arm from the same geometry it takes the chip footprints from, so the two
    # cannot drift apart. PixelPitch is PSM_QUAD_PIXEL_PITCH because a hit map
    # binned at a different pitch than the decoder placed the hits at stops
    # being one bin per pixel.
    mupix_monitor = PIPSMMuPixMonitor(
        input=_TES_MUQUAD_TWC, GeometrySvc="PIGeometrySvc",
        PixelPitch=float(PSM_QUAD_PIXEL_PITCH),
        PixelsPerBin=int(PSM_MUPIX_PIXELS_PER_BIN),
        CoincidenceWindow=float(PSM_MUPIX_WINDOW_NS),
        DtRange=float(PSM_MUPIX_DT_RANGE_NS), DtBins=int(PSM_MUPIX_DT_BINS),
        SlopeRange=float(PSM_MUPIX_SLOPE_RANGE_MRAD),
        ExpandedPosRange=float(PSM_MUPIX_EXPANDED_RANGE_MM),
        CentralSlopeRange=float(PSM_MUPIX_CENTRAL_SLOPE_MRAD),
        AllPairs=int(PSM_MUPIX_ALL_PAIRS),
        # min(strategy, 1): the monitor has no scintillators, so it has no
        # stop-layer depth for strategy 2's extra check.
        WeightStrategy=min(int(PSM_WEIGHT_STRATEGY), 1),
        ConfigX=_PSM_CONFIG_X, ConfigY=_PSM_CONFIG_Y,
        Margin=float(PSM_WEIGHT_MARGIN_MM))
    # xy_mt / xxp_mt / yyp_mt, the maps the beam-tuning feed reads, on the same
    # minitwin grid as PIPSMDelayedCoincidence's phase space, stage-weighted with
    # WeightStrategy / ConfigX / ConfigY / Margin above (sum of w, Sumw2). Set only when the
    # installed reco_testbeam has the properties: this job pulled onto a host
    # whose reco_testbeam was not rebuilt yet would otherwise die at option
    # parsing, taking every nearline job on that host with it.
    _mupix_minitwin = dict(MinitwinBins=int(PSM_PHASE_SPACE_BINS),
                           MinitwinPosRange=float(PSM_PHASE_SPACE_POS_RANGE_MM),
                           MinitwinSlopeRange=float(PSM_PHASE_SPACE_SLOPE_RANGE_MRAD))
    _mupix_known = PIPSMMuPixMonitor.getDefaultProperties()
    if all(k in _mupix_known for k in _mupix_minitwin):
        for _k, _v in _mupix_minitwin.items():
            setattr(mupix_monitor, _k, _v)
    else:
        print("[nearline] WARNING    the installed PIPSMMuPixMonitor has no Minitwin* "
              "properties (reco_testbeam older than the *_mt maps): no "
              "PIPSMMuPixMonitor/xy_mt, xxp_mt, yyp_mt are written, so the minitwin "
              "feed and combine_files will fail on this job's files until "
              "reco_testbeam is rebuilt")
    # The six high-ToT maps (PSM_MUPIX_HIGH_TOT_MIN). Feature-detected like the
    # minitwin properties: a reco_testbeam built before them would die at option
    # parsing on the unknown property.
    if "HighTotMin" in _mupix_known:
        mupix_monitor.HighTotMin = int(PSM_MUPIX_HIGH_TOT_MIN)
        _HIGH_TOT_NOTE = ""
        # The decoder drops ToT <= its cut, so every hit here has ToT >= cut + 1
        # (>= 0 when the cut is -1 or not applied by an old decoder): a threshold
        # at or below that selects every pair. Legal, so not a check() problem.
        _tot_floor = (int(PSM_MUPIX_TOT_CUT) if not _TOT_CUT_NOTE else -1) + 1
        if 0 <= int(PSM_MUPIX_HIGH_TOT_MIN) <= _tot_floor:
            print(f"[nearline] WARNING    PSM_MUPIX_HIGH_TOT_MIN = {PSM_MUPIX_HIGH_TOT_MIN} "
                  f"is at or below the lowest ToT the decoder keeps ({_tot_floor}, "
                  f"PSM_MUPIX_TOT_CUT + 1): every pair passes, so the six "
                  f"PIPSMMuPixMonitor/*_hitot_* maps are copies of the originals")
    elif int(PSM_MUPIX_HIGH_TOT_MIN) == -1:
        # An old build writes no high-ToT maps, which is exactly what -1 asks for.
        _HIGH_TOT_NOTE = ""
    else:
        _HIGH_TOT_NOTE = " (not applied: old reco)"
        print("[nearline] WARNING    the installed PIPSMMuPixMonitor has no HighTotMin "
              "property (reco_testbeam older than the high-ToT maps): no "
              "PIPSMMuPixMonitor/*_hitot_* maps are written")
    if _MUPIX_TIMEWALK:
        # The all-pairs timewalk reads the time-ordered SMA hits as an optional
        # input: the sequencer stays gated on the MuPix hits alone, and a frame
        # with no SMA collection only skips the timewalk fills. The counters
        # S1-S5 are the channel map's, the same table the tracklet reco reads.
        mupix_monitor.CounterInput = _TES_MUTRIG_CAL
        mupix_monitor.ConditionsTable = PSM_CHANNEL_MAP_TABLE
        mupix_monitor.TimewalkDtMin = float(PSM_TIMEWALK_DT_MIN)
        mupix_monitor.TimewalkDtMax = float(PSM_TIMEWALK_DT_MAX)
        mupix_monitor.TimewalkDtBins = int(PSM_TIMEWALK_DT_BINS)
        if PSM_CHANNEL_MAP_TAG:
            mupix_monitor.ConditionsTag = PSM_CHANNEL_MAP_TAG
    # Gated on the raw hits: PSMTimewalkSeq, gated the same way, has written
    # /Event/muquad_twc for every event this lets through.
    algorithms.append(Gaudi__Sequencer("PSMMuPixSeq", RequireObjects=[_TES_MUQUAD],
                                       Members=[mupix_monitor]))

if PSM_SMA_MONITOR:
    # Its own sequencer, gated on the SMA hits and on nothing else: this is the
    # check that has to keep running when the channel map, the MuPix half of the
    # telescope, or the tracklet reco that needs both, is what is broken. The
    # counter axis is built at initialize from the raw MUTRIG map PIGeometrySvc
    # serves, so it follows the cabling of the run being processed and nothing
    # about it is set here. ParkedVid is the Degrader id that map parks every
    # uncabled channel on: the idle FEB words land there, which is why that one
    # index is left out of the ToT judgements.
    #
    # With the RF channel decoded, the monitor also pairs each S1 hit with the
    # S1-gated RF burst after it and fills the RF phase vs ToT per counter. The
    # RF* properties stay at their defaults: a 125 ns gate after each S1 hit,
    # vetoed when another S1 hit lies inside it (that hit's burst can land in
    # it), and the phase from the last RF pulse of a gate holding 2-4 pulses
    # (RFPhaseRule "last"; "dqm" is the musip DQM's four-pulse rule).
    # /Event/rf is read as an optional input: a frame in which the decoder saw
    # no RF has none.
    sma_monitor = PIPSMSMAMonitor(
        input=_TES_MUTRIG_CAL, GeometrySvc="PIGeometrySvc", RawMap="MUTRIG",
        ParkedVid=2002,
        HitsPerEventMax=int(PSM_SMA_HITS_PER_EVENT_MAX),
        DegenerateTotShare=float(PSM_SMA_DEGENERATE_TOT_SHARE),
        MarkerTotShare=float(PSM_SMA_MARKER_TOT_SHARE))
    # Always wired: whether a run has an RF channel is decided by its raw map at
    # initialize, after this file has run, and an absent /Event/rf is an empty
    # pulse list to the monitor.
    sma_monitor.RFInput = _TES_RF
    # The calibration layer's sidecar, index-parallel to /Event/mutrig_cal: its
    # NIM-only hits (a configured ToT, not a measured one) are kept out of the ToT
    # spectra and the degenerate/marker judgement.
    sma_monitor.ScintHitsInput = _TES_SMA_HITS
    # A frame is left out of dt_to_s1_wide when its wide pairs exceed this cap, so a
    # cap of 0 leaves every frame out; the property's own default (a million pairs)
    # stands otherwise.
    if not PSM_SMA_WIDE_DT:
        sma_monitor.MaxWidePairsPerFrame = 0
    # The narrow pair budget, always set; feature-detected like the decoder's cuts.
    if "MaxNarrowPairsPerFrame" in PIPSMSMAMonitor.getDefaultProperties():
        sma_monitor.MaxNarrowPairsPerFrame = int(PSM_SMA_MAX_NARROW_PAIRS)
        _SMA_PAIRS_NOTE = ""
    else:
        _SMA_PAIRS_NOTE = " (not applied: old reco)"
        print("[nearline] WARNING    the installed PIPSMSMAMonitor has no MaxNarrowPairsPerFrame "
              "property (reco_testbeam older than the bad-frame cuts): no frame is left out "
              "for its narrow pairs")
    algorithms.append(Gaudi__Sequencer("PSMSMASeq", RequireObjects=[_TES_MUTRIG_CAL],
                                       Members=[sma_monitor]))

if PSM_RECO:
    all_reco = PIPSMSimpleTrackReco(
        "PIPSMAllTrackReco", L_hits=_TES_MUQUAD_TWC, S_hits=_TES_MUTRIG_CAL,
        output=_TES_PSM_TRACKS, ConditionsTable=PSM_CHANNEL_MAP_TABLE,
        SeedOn=int(PSM_SEED_ON), requireLHits=int(PSM_REQUIRE_L_HITS),
        seedOnL=int(PSM_SEED_ON_L), thrLPair=float(PSM_LPAIR_WINDOW_NS),
        thrMupix=float(PSM_L_WINDOW_BEFORE_NS), thrMupixUpper=float(PSM_L_WINDOW_AFTER_NS),
        thrScint=float(PSM_SCINT_WINDOW_NS),
        aggregate=int(PSM_AGGREGATE),
        AggregatePromptOnly=int(PSM_AGGREGATE_PROMPT_ONLY),
        AggregateOwnersOnly=int(PSM_AGGREGATE_OWNERS_ONLY),
        lPairOwnerOffsetNs=float(PSM_LPAIR_OWNER_OFFSET_NS),
        distanceL12=float(PSM_DISTANCE_L12),
        thr=float(PSM_LAYER_THR),
        xrange=float(PSM_PHASE_SPACE_POS_RANGE_MM),
        prange=float(PSM_PHASE_SPACE_SLOPE_RANGE_MRAD),
        lClusterDistMm=float(PSM_L_CLUSTER_DIST_MM),
        dropCrosstalkGhosts=bool(PSM_DROP_CROSSTALK_GHOSTS),
        PixelPitch=float(PSM_QUAD_PIXEL_PITCH),
        Timewalk=int(bool(PSM_TIMEWALK)),
        TimewalkDtMin=float(PSM_TIMEWALK_DT_MIN), TimewalkDtMax=float(PSM_TIMEWALK_DT_MAX),
        TimewalkDtBins=int(PSM_TIMEWALK_DT_BINS),
        WeightStrategy=int(PSM_WEIGHT_STRATEGY),
        ConfigX=_PSM_CONFIG_X, ConfigY=_PSM_CONFIG_Y,
        Margin=float(PSM_WEIGHT_MARGIN_MM))
    if PSM_CHANNEL_MAP_TAG:
        all_reco.ConditionsTag = PSM_CHANNEL_MAP_TAG
    if PSM_DECODE and PSM_GEOMETRY_BASE:
        # Plane membership (which VID is L1 vs L2) from the psm_geometry table,
        # set only when PIGeometrySvc was actually created above.
        all_reco.GeometrySvc = "PIGeometrySvc"
    if PSM_DECODE:
        # Each tracklet's S1 hit gets its RF phase (s1rfphase) under the same
        # rule and defaults as the SMA monitor's rf_phase, and the prompt
        # tracklets fill xy_vs_s1phase, xxp_vs_s1phase and yyp_vs_s1phase (and
        # their scan-weighted _w twins), so a MuPix map or a phase space for any
        # phase window is a projection of them. /Event/rf is optional per
        # frame, as there.
        all_reco.RFInput = _TES_RF
        # The calibration layer's sidecar (index-parallel to S_hits) gives each
        # tracklet nimMask / incompleteMask, a bit per counter S1..S5.
        all_reco.ScintHitsInput = _TES_SMA_HITS
    weight_reco = PIPSMComputeWeight(
        input=all_reco.output, output=_TES_PSM_WEIGHTS,
        Strategy=int(PSM_WEIGHT_STRATEGY), DistanceL12=float(PSM_DISTANCE_L12),
        ConfigX=_PSM_CONFIG_X, ConfigY=_PSM_CONFIG_Y,
        Margin=float(PSM_WEIGHT_MARGIN_MM), LayerThr=float(PSM_LAYER_THR))
    if PSM_DECODE and PSM_GEOMETRY_BASE:
        # Plane footprints for the acceptance windows, set only when
        # PIGeometrySvc was actually created above; required when Strategy >= 1.
        weight_reco.GeometrySvc = "PIGeometrySvc"
    tag_reco = PIPSMDelayedCoincidence(
        input=all_reco.output, weights=weight_reco.output,
        WindowMin=float(PSM_DELAYED_WINDOW_NS[0]), WindowMax=float(PSM_DELAYED_WINDOW_NS[1]),
        LayerThr=float(PSM_LAYER_THR), S5Thr=float(PSM_S5_THR),
        RequireSeedHit=int(PSM_REQUIRE_SEED_HIT),
        RequireLPair=int(PSM_REQUIRE_L_HITS), DistanceL12=float(PSM_DISTANCE_L12),
        PhaseSpaceBins=int(PSM_PHASE_SPACE_BINS),
        PhaseSpacePosRange=float(PSM_PHASE_SPACE_POS_RANGE_MM),
        PhaseSpaceSlopeRange=float(PSM_PHASE_SPACE_SLOPE_RANGE_MRAD),
        DtBins=int(PSM_DELAYED_DT_BINS),
        DelayedTotMin=float(PSM_DELAYED_TOT_MIN),
        DelayedStopPlateOnly=int(PSM_DELAYED_STOP_PLATE_ONLY),
        TaggedStopLayers=[int(x) for x in PSM_TAGGED_STOP_LAYERS],
        StopPhaseHist=int(PSM_STOP_PHASE_HIST))
    if PSM_DELAYED_TOT_MAX is not None:
        tag_reco.DelayedTotMax = float(PSM_DELAYED_TOT_MAX)
    if PSM_PROMPT_RF_WINDOW_NS is not None:
        tag_reco.PromptPhaseMin = float(PSM_PROMPT_RF_WINDOW_NS[0])
        tag_reco.PromptPhaseMax = float(PSM_PROMPT_RF_WINDOW_NS[1])
    if PSM_FAR_WINDOW_NS is not None:
        tag_reco.FarWindowMin = float(PSM_FAR_WINDOW_NS[0])
        tag_reco.FarWindowMax = float(PSM_FAR_WINDOW_NS[1])
    if PSM_PAIR_WINDOW_NS is not None:
        tag_reco.PairWindowMin = float(PSM_PAIR_WINDOW_NS[0])
        tag_reco.PairWindowMax = float(PSM_PAIR_WINDOW_NS[1])
    algorithms.append(Gaudi__Sequencer(
        "PSMRecoSeq", RequireObjects=[_TES_MUTRIG_CAL],
        Members=[all_reco, PIPSMPatternReco(input=all_reco.output), weight_reco, tag_reco]))

for alg in algorithms:
    alg.AuditExecute = alg.AuditInitialize = alg.AuditFinalize = True

out_streams = []
if WRITE_NTUPLE:
    output = PIAOutputStream(destination=str(NL_OUT), Compression=_NTUPLE_COMPRESSION,
                             ReuseEntry=_NTUPLE_REUSE_ENTRY)
    _ntuple_rules = [str(r) for r in NTUPLE_RULES]
    # Only with PSM_DECODE: a rule matching no registered path is warned about.
    if PSM_DECODE and _TWC_NTUPLE_DROP[PSM_TWC_NTUPLE]:
        _ntuple_rules.append("drop " + _TWC_NTUPLE_DROP[PSM_TWC_NTUPLE])
    if PSM_DECODE and _SMA_CAL_NTUPLE_DROP[PSM_SMA_CAL_NTUPLE]:
        _ntuple_rules.append("drop " + _SMA_CAL_NTUPLE_DROP[PSM_SMA_CAL_NTUPLE])
    # PSM_SMA_HITS_NTUPLE: kept by default (no rule, so NTUPLE_RULES decide), or
    # dropped by a last rule.
    if PSM_DECODE and not PSM_SMA_HITS_NTUPLE:
        _ntuple_rules.append("drop " + _TES_SMA_HITS)
    if PSM_DECODE and PSM_SMA_HITS_NTUPLE and PSM_SMA_CAL_NTUPLE == "calibrated":
        print(f"[nearline] WARNING    PSM_SMA_CAL_NTUPLE = \"calibrated\" drops {_TES_MUTRIG}, "
              f"so the raw TOT/NIM indices of {_TES_SMA_HITS} point into a collection this "
              "file does not have (the sidecar's times, ToTs and flags are still valid).")
    if _ntuple_rules:
        output.SelectionRules = _ntuple_rules
    output.AuditExecute = output.AuditInitialize = output.AuditFinalize = True
    out_streams.append(output)

theManager = ApplicationMgr(EvtMax=int(EVT_MAX), OutputLevel=_LEVELS[OUTPUT_LEVEL],
                            AuditAlgorithms=True, TopAlg=algorithms, ExtSvc=services,
                            OutStream=out_streams, EvtSel=selector)

if RENDERED:
    # Provenance of this copy: who rendered it when, from which file at which commit.
    # A rendered file outlives the shell it came from, so it has to say this itself.
    print(f"[nearline] rendered   {_RENDERED['rendered_at']} by {_RENDERED['rendered_by']}"
          f" job={_RENDERED['job_id']} run={_RENDERED['run_id']}")
    print(f"[nearline] source     {_RENDERED['job_source']} @ {_RENDERED['job_git']}")
print(f"[nearline] input      {NL_MIDAS}")
print("[nearline] light      "
      + ("on, switched off: " + (" ".join(_LIGHT_OFF) or "nothing (all already off)")
         if LIGHT is True else "off"))
print(f"[nearline] rntuple    {NL_OUT if WRITE_NTUPLE else 'no RNTuple'}")
print(f"[nearline] histograms {hist_file(NL_OUT)}")
# The source, and never the user or a password. The conditions service then prints one
# "layer=" line per table naming what actually served it.
if _COND_KIND == "db":
    print(f"[nearline] conditions db {_describe_conninfo(_PG_CONNINFO)}"
          + (f" (+ JSON {' '.join(_JSON_FILES)})" if _JSON_FILES else ""))
else:
    print(f"[nearline] conditions json {_JSON_DIR}")
if _COND_KIND == "db" or _JSON_DIR != CONDITIONS_DIR:
    print(f"[nearline] odb specs  {CONDITIONS_DIR}")
if NL_OVERRIDES:
    print(f"[nearline] overrides  {NL_OVERRIDES}")
print(f"[nearline] halves     WD={WD_ENABLED} WD_SCALER_MONITOR={WD_SCALER_MONITOR}"
      f" PSM_DECODE={PSM_DECODE} PSM_RECO={PSM_RECO} PSM_MUPIX_MONITOR={PSM_MUPIX_MONITOR}"
      f" PSM_SMA_MONITOR={PSM_SMA_MONITOR} PSM_TIMEWALK={PSM_TIMEWALK}"
      f" PSM_TIMEWALK_CORRECTION={PSM_TIMEWALK_CORRECTION}"
      f" PSM_MUPIX_TOT_CUT={PSM_MUPIX_TOT_CUT}{_TOT_CUT_NOTE}"
      f" PSM_MUPIX_HIGH_TOT_MIN={PSM_MUPIX_HIGH_TOT_MIN}{_HIGH_TOT_NOTE}"
      f" PSM_SMA_WIDE_DT={PSM_SMA_WIDE_DT} PSM_SMA_DIAGNOSTICS={PSM_SMA_DIAGNOSTICS}")
print(f"[nearline] layers     "
      + (f"PIPSMSMACalibration {_TES_MUTRIG}->{_TES_MUTRIG_CAL}+{_TES_SMA_HITS}"
         f" PIPSMMuPixTimewalkCorrection {_TES_MUQUAD}->{_TES_MUQUAD_TWC}"
         if PSM_DECODE else "none (PSM_DECODE off)"))
print(f"[nearline] sma pair   PSM_SMA_NIM_PAIRING={PSM_SMA_NIM_PAIRING}"
      f" PSM_SMA_PAIR_WINDOW_NS={PSM_SMA_PAIR_WINDOW_NS} PSM_SMA_TIME_SOURCE={PSM_SMA_TIME_SOURCE}"
      f" PSM_SMA_NIM_ONLY_TOT={PSM_SMA_NIM_ONLY_TOT}"
      f" PSM_SMA_OFFSET_OVERRIDE_NS={PSM_SMA_OFFSET_OVERRIDE_NS or 'none'}")
print(f"[nearline] sma fine   PSM_SMA_FINE_OFFSETS={PSM_SMA_FINE_OFFSETS}"
      f" PSM_SMA_NIM_LAG={PSM_SMA_NIM_LAG}"
      f" PSM_SMA_LAG_VIDS={list(PSM_SMA_LAG_VIDS)}"
      f" PSM_SMA_NIM_NOMINAL_DELAY_NS={PSM_SMA_NIM_NOMINAL_DELAY_NS or 'none'}"
      f" PSM_SMA_SKIP_STALE_FIRST_FRAME={PSM_SMA_SKIP_STALE_FIRST_FRAME} subrun={_SUBRUN}")
print(f"[nearline] sma frame  PSM_SMA_MAX_FRAME_SPAN_MS={PSM_SMA_MAX_FRAME_SPAN_MS}"
      f" PSM_SMA_MIN_FINE_COARSE_SHARE={PSM_SMA_MIN_FINE_COARSE_SHARE}{_SMA_BANK_NOTE}"
      f" PSM_SMA_MAX_NARROW_PAIRS={PSM_SMA_MAX_NARROW_PAIRS}{_SMA_PAIRS_NOTE}")
print(f"[nearline] EvtMax     {EVT_MAX}")
