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
    +-- PIWDSettingsSummary --- top level, no waveform needed -> WDSettingsHeader
    |
    +-- WDAnalysisSeq --------- gated on /Event/wd_waveform
    |     PIWDRFPhase          -> /Event/wd_rf_phase
    |     PIWDWaveformAnalysis -> /Event/wd_features   (consumes wd_rf_phase)
    |     PIWDCalibrator       -> /Event/wd_hits
    |
    +-- PSMMuPixSeq ----------- gated on /Event/muquad
    |     PIPSMMuPixMonitor    -> histograms only: a hit map per MuPix chip and
    |                             per plane, and x/x', y/y' from an L1/L2 time
    |                             coincidence with no scintillator involved
    |
    +-- PSMRecoSeq ------------ gated on /Event/mutrig
    |     PIPSMSimpleTrackReco   -> /Event/exp_all_tracks   (+ histograms)
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
from reco_testbeam.pi_wdalgConf import (PIWDCalibrator, PIWDRFPhase,
                                        PIWDSettingsSummary, PIWDWaveformAnalysis)
from reco_testbeam.pi_psmalg_expConf import (PIPSMComputeWeight, PIPSMDelayedCoincidence,
                                             PIPSMMuPixMonitor, PIPSMPatternReco,
                                             PIPSMSimpleTrackReco)

# ===== RENDERED BY THE DAEMON (do not edit; the checked-in file carries placeholders) =====
# pioneer.nearline.render fills these with string.Template and writes the result next to
# the outputs as <filebase>.py. Left unfilled, they still read as their own placeholder
# text, which means "take the environment" and is how an interactive run works. A rendered
# file ignores every NL_* variable instead: the file beside the outputs is the whole record
# of what processed that run, including the conditions directory and database connection
# the renderer's environment supplied.
#
# Every placeholder sits inside a string literal, so the checked-in file is valid Python and
# runs unrendered. The render step substitutes safely and then refuses to write unless all
# eleven names below are gone, so only these eleven are special: a dollar sign written
# anywhere else in this file survives verbatim and can never break the daemon. Doubling one
# is what makes the render step collapse it to a single character.
_RENDERED = {
    "in_file": "${in_file}", "out_file": "${out_file}", "evt_max": "${evt_max}",
    "conditions_dir": "${conditions_dir}", "pg": "${pg}",
    "rendered_at": "${rendered_at}", "rendered_by": "${rendered_by}",
    "job_source": "${job_source}", "job_git": "${job_git}",
    "job_id": "${job_id}", "run_id": "${run_id}",
}
# Which of the two ways in this file is. rendered_at is the field the renderer always
# fills, and an unsubstituted placeholder still begins with the dollar sign that opens
# it, so that one character decides. chr(36) IS that dollar sign, written that way so
# that the only dollar signs in the unrendered file are the eleven placeholders above.
RENDERED = not _RENDERED["rendered_at"].startswith(chr(36))

# Where conditions containers live when nobody says otherwise. This is the bind mount
# inside the testbeam-midas container; on a host without /simulation (pinky) the daemon's
# NL_CONDITIONS_DIR is mandatory and is what a rendered file carries.
_DEFAULT_CONDITIONS_DIR = os.path.join(os.environ.get("PIONEERSYS"), "reco_testbeam/conditions")

# ===== SETTINGS =====
# --- Job -------------------------------------------------------------------
# Events to process; -1 is the whole file. NL_EVTMAX in the environment wins.
EVT_MAX = -1
# Gaudi verbosity for the whole job: "DEBUG", "INFO", "WARNING" or "ERROR".
OUTPUT_LEVEL = "INFO"
# Master switch for the WaveDREAM half: waveforms -> features -> RF -> hits.
WD_ENABLED = True
# Decode the musip H000 banks into /Event/muquad, /Event/mutrig and /Event/rf.
PSM_DECODE = True
# Run the tracklet chain on the decoded musip hits. Requires PSM_DECODE.
PSM_RECO = True
# --- Conditions ------------------------------------------------------------
# Directory every bare container name below is resolved against.
CONDITIONS_DIR = os.environ.get("NL_CONDITIONS_DIR", _DEFAULT_CONDITIONS_DIR)
# Campaign database, libpq conninfo strings. FORTHCOMING DEFAULT: once served, the
# constants come from here and the JSON files become local overrides.
# NL_PG wins; password in PGPASSWORD.
PG_CONNECTIONS = []
# Specs mapping subtrees of the begin-of-run ODB dump to conditions tables.
ODB_SPECS = ["odb/bt2026_runinfo.json", "odb/bt2026_wavedream_daq.json",
             "odb/bt2026_wavedream_scalers.json", "odb/bt2026_isel.json"]
# Tables resolved at initialize(), so a misconfigured job dies in the first second.
ODB_PRELOAD = ["runinfo", "wd_board_settings", "wd_channel_settings", "wd_scaler_names"]
# Run-indexed corrections applied to a private copy of the ODB tree; "" is none.
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
# --- PSM decode ------------------------------------------------------------
# MuTrig RAW readout channels (chipid*32+channel, read before the map lookup)
# carrying the RF and the beam current; fake-MIDAS SMA cabling, hardware cabling
# replaces both. None drops /Event/rf resp. histograms/musip/current.
PSM_RF_CHANNEL = 5
PSM_CURRENT_CHANNEL = 6
# MuPix pixel pitch in mm; a wrong pitch scales every position and every slope.
PSM_QUAD_PIXEL_PITCH = 0.08
# MuPix timestamp bin width in ns. There is no MuTrig counterpart any more: the
# trigger encoding reports its time in ns directly, so PITMidasMusip dropped
# trigTimeBinWidth along with the 50 ps timestamp it used to scale.
PSM_QUAD_TIME_BIN_NS = 8.0
# --- PSM geometry ----------------------------------------------------------
# Base layer PIGeometrySvc builds the GeoHeader from, as "GEOCOND:<table>".
PSM_GEOMETRY_BASE = "GEOCOND:psm_geometry"
# Raw-readout-id -> detector-id maps as "NAME:table"; the NAME side must match the
# decoder's muPixMap/muTrigMap property defaults.
PSM_GEOMETRY_MAPS = ["MUPIX:mupix_chip_map", "MUTRIG:mutrig_channel_map"]
# Extra transform layers on the base; ["COND:isel"] adds the XY-stage translation
# read from /Equipment/XYTable in the ODB.
PSM_GEOMETRY_TRANS = []
# Containers supplying the base table and the two map tables above.
PSM_GEOMETRY_FILES = ["bt2026_psm_geometry.json", "bt2026_psm_readout_map.json"]
# --- MuPix monitor ---------------------------------------------------------
# The low-level MuPix check: a hit map per chip and per plane, and tracks made
# from an L1/L2 time coincidence alone. It reads /Event/muquad and nothing
# else -- no scintillators, no channel map, no tracklets -- so it still says
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
# and 512 x 500 per plane on bt2026, about 4 MB of histogram in total, and the
# granularity a dead column or a hot pixel is visible at. Setting it to n
# divides the bin count of every map by n squared.
PSM_MUPIX_PIXELS_PER_BIN = 1
# Half-width in ns and bins of the dt histogram, scanned independently of the
# coincidence window. 204 over 51 bins puts each 8 ns MuPix tick at a bin
# CENTRE; a round 200 puts it on a bin edge, where ROOT's edge convention
# splits the coincidence peak between two bins.
PSM_MUPIX_DT_RANGE_NS = 204.0
PSM_MUPIX_DT_BINS = 51
# Half-width in mrad of this module's own x'/y' axes. 0 derives the full
# geometric acceptance of the two planes at the measured lever arm, so nothing
# a pair can produce lands in an overflow bin. Set it to
# PSM_PHASE_SPACE_SLOPE_RANGE_MRAD to read these next to PIPSMAllTrackReco's
# phase space instead, and expect the tails outside that window to pile up.
PSM_MUPIX_SLOPE_RANGE_MRAD = 0.0
# Pair every L2 hit inside the window instead of only the one nearest in time.
# Each extra pair is a combinatorial ghost carrying a slope no particle had,
# so this is a diagnostic for a busy run, not a production setting.
PSM_MUPIX_ALL_PAIRS = 0
# --- PSM reco --------------------------------------------------------------
# Container holding the data-side channel map the tracklet reco reads.
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
# L1 -> L2 lever arm in mm, used to turn (x2 - x1) into a slope.
PSM_DISTANCE_L12 = 30.0
# Delayed-coincidence window in ns for the pi -> mu tag, [MIN, MAX).
PSM_DELAYED_WINDOW_NS = (20.0, 70.0)
# Require the PROMPT half of a coincidence to have a prompt-channel hit of its own.
# Off, any tracklet in the window can play the prompt role -- including, in the
# unseeded mode, a delayed pulse that formed its own tracklet -- and the tag then
# counts pairs no particle made. The delayed half is selected by the window and by
# PSM_S5_THR, not by this.
PSM_REQUIRE_SEED_HIT = 1
# Layer-hit energy threshold and the S5 through-going threshold; raw MuTrig ToT on data, so retune.
PSM_LAYER_THR = 0.2
PSM_S5_THR = 0.2
# Telescope stage positions (dx, dy) in mm for the acceptance weighting.
PSM_POSITIONS_MM = [(0.0, 0.0), (17.0, 17.0), (-17.0, 17.0), (-17.0, -17.0), (17.0, -17.0)]
# Weighting strategy; 0 gives every tracklet weight 1.
PSM_WEIGHT_STRATEGY = 0
# Phase-space histogram axes: PIPSMDelayedCoincidence's tagged xy/xxp/yyp and
# their weighted twins, and the position/slope windows of PIPSMAllTrackReco's
# TH3s. These are not free monitoring knobs, they are the minitwin det10 input
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
NTUPLE_RULES = []
# ===== END OF SETTINGS =====

# Where the input, the output, the event limit and the two host-dependent settings
# come from. This runs before the container lists below, which resolve against
# CONDITIONS_DIR, and before check(), which reports on all of them.
if RENDERED:
    # A rendered file is a record, so it reads nothing from the environment: no
    # NL_MIDAS, NL_OUT, NL_EVTMAX, NL_PG, NL_CONDITIONS_DIR and no overrides file.
    # Re-run it under any environment at all and it processes the same input with the
    # same settings into the same outputs, which is what makes it worth keeping.
    NL_MIDAS = _RENDERED["in_file"]
    NL_OUT = _RENDERED["out_file"]
    EVT_MAX = int(_RENDERED["evt_max"])
    # Empty means the renderer had no NL_CONDITIONS_DIR, so the default stands. The
    # rendered file must still name one directory, because a run reprocessed against
    # a different conditions tree is a different run.
    CONDITIONS_DIR = _RENDERED["conditions_dir"] or _DEFAULT_CONDITIONS_DIR
    # Same list, same separator as NL_PG; empty is the normal case today.
    PG_CONNECTIONS = [c for c in _RENDERED["pg"].split(os.pathsep) if c]
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
    if os.environ.get("NL_PG"):
        PG_CONNECTIONS = [c for c in os.environ["NL_PG"].split(os.pathsep) if c]

# Measured, not guessed: 501 is ZSTD-1, about 40% less CPU than ROOT's default
# ZSTD-5 for about 8% more disk, and reusing one entry for the whole job saves
# an allocation and a free per field per event.
_NTUPLE_COMPRESSION = 501
_NTUPLE_REUSE_ENTRY = True

# TES paths. The first three are the decoding tools' own defaults, and changing a
# tool's path property without changing these breaks the sequencer gates silently.
# The last two are names this job chooses and passes to the PSM algorithms
# explicitly; their defaults are /Event/tracker_fr, /Event/dtar_fr and
# /Event/exp_simple_tracks, which are the simulation's names, so the assignment is
# what puts the testbeam chain on one set of paths.
_TES_WAVEFORM = "/Event/wd_waveform"
_TES_MUQUAD = "/Event/muquad"
_TES_MUTRIG = "/Event/mutrig"
_TES_PSM_TRACKS = "/Event/exp_all_tracks"
_TES_PSM_WEIGHTS = "/Event/exp_track_weights"
_TIMEBASE_TABLE = "wd_timebase"
_BOARD_SETTINGS_TABLE = "wd_board_settings"
_LEVELS = {"DEBUG": DEBUG, "INFO": INFO, "WARNING": WARNING, "ERROR": ERROR}


def _cond(name):
    """A container name resolved against CONDITIONS_DIR; absolute stays absolute."""
    text = str(name)
    return text if os.path.isabs(text) else os.path.join(CONDITIONS_DIR, text)


def hist_file(path):
    """x.root -> x_hists.root; the file the nearline website reads."""
    text = str(path)
    return (text[:-len(".root")] if text.endswith(".root") else text) + "_hists.root"


# The containers this configuration will actually load, in layer order. check()
# verifies them and PIConditionsSvc below is handed these very lists.
_JSON_FILES = []
if ODB_OVERRIDES:
    _JSON_FILES.append(_cond(ODB_OVERRIDES))
if WD_ENABLED:
    _JSON_FILES += [_cond(f) for f in WD_CONDITIONS_FILES]
if PSM_DECODE:
    _JSON_FILES += [_cond(f) for f in PSM_GEOMETRY_FILES]
if PSM_RECO and PSM_CHANNEL_MAP_FILE:
    _JSON_FILES.append(_cond(PSM_CHANNEL_MAP_FILE))
_ODB_TABLES = [_cond(f) for f in ODB_SPECS]


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
    if PSM_MUPIX_MONITOR and float(PSM_MUPIX_WINDOW_NS) <= 0:
        problems.append(f"PSM_MUPIX_WINDOW_NS is {PSM_MUPIX_WINDOW_NS}: it is a half-window, "
                        "so a non-positive value pairs nothing at all.")
    if PSM_DECODE and PSM_GEOMETRY_BASE and not PSM_GEOMETRY_FILES:
        problems.append("PSM_GEOMETRY_BASE is a GEOCOND layer but PSM_GEOMETRY_FILES is "
                        "empty: nothing would supply the table it names.")
    if "COND:isel" in PSM_GEOMETRY_TRANS and "bt2026_isel.json" not in {
            os.path.basename(str(p)) for p in ODB_SPECS}:
        problems.append("PSM_GEOMETRY_TRANS has 'COND:isel' but ODB_SPECS has no "
                        "bt2026_isel.json, so nothing maps the isel table.")
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
    if WD_ENABLED and WD_ROLE_TABLE and not WD_CONDITIONS_FILES:
        problems.append("WD_ROLE_TABLE is set but WD_CONDITIONS_FILES is empty: nothing "
                        "would supply the wd_channel_map table.")
    if WD_ENABLED and WD_CHANNEL_SETTINGS_TABLE and not ODB_SPECS:
        problems.append("WD_CHANNEL_SETTINGS_TABLE is set but ODB_SPECS is empty: only the "
                        "begin-of-run ODB dump serves wd_channel_settings, so the "
                        "per-channel thresholds would not resolve.")
    if WD_ENABLED and not set(WD_CAL_CHANNELS) <= set(WD_CHANNELS):
        problems.append(f"WD_CAL_CHANNELS {sorted(set(WD_CAL_CHANNELS) - set(WD_CHANNELS))} are "
                        "not in WD_CHANNELS: they would have no features to calibrate.")
    if WD_RF_REFINE and int(WD_RF_REFINE_POINTS) < 2:
        problems.append(f"WD_RF_REFINE is on but WD_RF_REFINE_POINTS is "
                        f"{WD_RF_REFINE_POINTS}; a scan needs at least 2 points.")
    if PSM_RECO and (int(PSM_PHASE_SPACE_BINS) <= 0 or int(PSM_PHASE_SPACE_BINS) % 64):
        problems.append(f"PSM_PHASE_SPACE_BINS is {PSM_PHASE_SPACE_BINS}: it must be a "
                        "positive multiple of 64, or the phase-space histograms do not "
                        "rebin onto the 64-bin minitwin export exactly (320 = 5 x 64).")
    if PSM_RECO and (float(PSM_PHASE_SPACE_POS_RANGE_MM) <= 0
                     or float(PSM_PHASE_SPACE_SLOPE_RANGE_MRAD) <= 0):
        problems.append(f"PSM_PHASE_SPACE_POS_RANGE_MM ({PSM_PHASE_SPACE_POS_RANGE_MM}) and "
                        f"PSM_PHASE_SPACE_SLOPE_RANGE_MRAD "
                        f"({PSM_PHASE_SPACE_SLOPE_RANGE_MRAD}) are half-widths of a "
                        "symmetric axis, so both must be positive.")
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
if PG_CONNECTIONS:
    condSvc.PgConnections = [str(c) for c in PG_CONNECTIONS]

# The ORDER of this list is load-bearing. PIMidasSelector publishes the
# begin-of-run ODB dump into PIHeaderSvc as "ODBHeader" during its own
# initialize(); PIConditionsSvc reads /Runinfo/Run number out of that header
# during ITS initialize() to pick intervals of validity; PIGeometrySvc needs the
# conditions service resolved before it. Reorder and every table resolves run 0.
services = [PIHeaderSvc(), selector, PIMidasConversionSvc("ConversionSvc"),
            EvtDataSvc(), PIDataModelSvc(), condSvc]
if PSM_DECODE:
    services.append(PIGeometrySvc(Base=str(PSM_GEOMETRY_BASE),
                                  Trans=[str(t) for t in PSM_GEOMETRY_TRANS],
                                  Maps=[str(m) for m in PSM_GEOMETRY_MAPS]))
services.append(PIHistogramSvc(OutputFile=hist_file(NL_OUT)))
services.append(EvtPersistencySvc(CnvServices=["PIMidasConversionSvc/ConversionSvc"]))
audit = AuditorSvc()
audit.Auditors += [ChronoAuditor()]
services.append(audit)

tools = [PITMidasWaveDream()] if WD_ENABLED else []
if PSM_DECODE:
    musip = PITMidasMusip(quadPixelPitch=float(PSM_QUAD_PIXEL_PITCH),
                          quadTimeBinWidth=float(PSM_QUAD_TIME_BIN_NS))
    # rf_channel is what creates /Event/rf at all and current_channel what books
    # histograms/musip/current; the tool's own sentinel cannot be written from
    # Python, so "unset" means not assigning the property.
    if PSM_RF_CHANNEL is not None:
        musip.rf_channel = int(PSM_RF_CHANNEL)
    if PSM_CURRENT_CHANNEL is not None:
        musip.current_channel = int(PSM_CURRENT_CHANNEL)
    tools.append(musip)

algorithms = [PIMidasDecoder(decoders=tools)]
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
        input=_TES_MUQUAD, GeometrySvc="PIGeometrySvc",
        PixelPitch=float(PSM_QUAD_PIXEL_PITCH),
        PixelsPerBin=int(PSM_MUPIX_PIXELS_PER_BIN),
        CoincidenceWindow=float(PSM_MUPIX_WINDOW_NS),
        DtRange=float(PSM_MUPIX_DT_RANGE_NS), DtBins=int(PSM_MUPIX_DT_BINS),
        SlopeRange=float(PSM_MUPIX_SLOPE_RANGE_MRAD),
        AllPairs=int(PSM_MUPIX_ALL_PAIRS))
    algorithms.append(Gaudi__Sequencer("PSMMuPixSeq", RequireObjects=[_TES_MUQUAD],
                                       Members=[mupix_monitor]))

if PSM_RECO:
    all_reco = PIPSMSimpleTrackReco(
        "PIPSMAllTrackReco", L_hits=_TES_MUQUAD, S_hits=_TES_MUTRIG,
        output=_TES_PSM_TRACKS, ConditionsTable=PSM_CHANNEL_MAP_TABLE,
        SeedOn=int(PSM_SEED_ON), requireLHits=int(PSM_REQUIRE_L_HITS),
        seedOnL=int(PSM_SEED_ON_L), thrLPair=float(PSM_LPAIR_WINDOW_NS),
        aggregate=int(PSM_AGGREGATE),
        AggregatePromptOnly=int(PSM_AGGREGATE_PROMPT_ONLY),
        distanceL12=float(PSM_DISTANCE_L12),
        thr=float(PSM_LAYER_THR),
        xrange=float(PSM_PHASE_SPACE_POS_RANGE_MM),
        prange=float(PSM_PHASE_SPACE_SLOPE_RANGE_MRAD))
    if PSM_CHANNEL_MAP_TAG:
        all_reco.ConditionsTag = PSM_CHANNEL_MAP_TAG
    if PSM_DECODE and PSM_GEOMETRY_BASE:
        # Plane membership (which VID is L1 vs L2) from the psm_geometry table,
        # set only when PIGeometrySvc was actually created above.
        all_reco.GeometrySvc = "PIGeometrySvc"
    weight_reco = PIPSMComputeWeight(
        input=all_reco.output, output=_TES_PSM_WEIGHTS,
        Strategy=int(PSM_WEIGHT_STRATEGY), DistanceL12=float(PSM_DISTANCE_L12),
        ConfigX=[float(p[0]) for p in PSM_POSITIONS_MM],
        ConfigY=[float(p[1]) for p in PSM_POSITIONS_MM], LayerThr=float(PSM_LAYER_THR))
    tag_reco = PIPSMDelayedCoincidence(
        input=all_reco.output, weights=weight_reco.output,
        WindowMin=float(PSM_DELAYED_WINDOW_NS[0]), WindowMax=float(PSM_DELAYED_WINDOW_NS[1]),
        LayerThr=float(PSM_LAYER_THR), S5Thr=float(PSM_S5_THR),
        RequireSeedHit=int(PSM_REQUIRE_SEED_HIT),
        RequireLPair=int(PSM_REQUIRE_L_HITS), DistanceL12=float(PSM_DISTANCE_L12),
        PhaseSpaceBins=int(PSM_PHASE_SPACE_BINS),
        PhaseSpacePosRange=float(PSM_PHASE_SPACE_POS_RANGE_MM),
        PhaseSpaceSlopeRange=float(PSM_PHASE_SPACE_SLOPE_RANGE_MRAD))
    algorithms.append(Gaudi__Sequencer(
        "PSMRecoSeq", RequireObjects=[_TES_MUTRIG],
        Members=[all_reco, PIPSMPatternReco(input=all_reco.output), weight_reco, tag_reco]))

for alg in algorithms:
    alg.AuditExecute = alg.AuditInitialize = alg.AuditFinalize = True

out_streams = []
if WRITE_NTUPLE:
    output = PIAOutputStream(destination=str(NL_OUT), Compression=_NTUPLE_COMPRESSION,
                             ReuseEntry=_NTUPLE_REUSE_ENTRY)
    if NTUPLE_RULES:
        output.SelectionRules = [str(r) for r in NTUPLE_RULES]
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
print(f"[nearline] rntuple    {NL_OUT if WRITE_NTUPLE else 'no RNTuple'}")
print(f"[nearline] histograms {hist_file(NL_OUT)}")
print(f"[nearline] conditions {CONDITIONS_DIR}")
for conninfo in PG_CONNECTIONS:
    print("[nearline] database   " + " ".join(
        t for t in str(conninfo).split() if t.startswith(("host=", "dbname="))))
if NL_OVERRIDES:
    print(f"[nearline] overrides  {NL_OVERRIDES}")
print(f"[nearline] halves     WD={WD_ENABLED} PSM_DECODE={PSM_DECODE} PSM_RECO={PSM_RECO}"
      f" PSM_MUPIX_MONITOR={PSM_MUPIX_MONITOR}")
print(f"[nearline] EvtMax     {EVT_MAX}")
