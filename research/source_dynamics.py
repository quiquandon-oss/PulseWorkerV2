"""
Experiment 5, Part 3: deterministic sentiment/evidence dynamics.

Classifies a chronological sequence of ONE source's archived values
into NOISE / PERSISTENCE / BULLISH_TREND / BEARISH_TREND /
ACCELERATION / DECELERATION / REVERSAL, and separately provides
CONTRADICTION detection (two directions disagreeing) and
CROSS_SOURCE_CONFIRMATION (independent sources agreeing within a time
window) plus a diminishing-returns repetition scorer for repeated
coverage of the same underlying story.

Deterministic only -- no AI, no learned model, no network access at
all (this module takes plain Python values in and returns plain
dicts out; it never touches a database connection). Every threshold is
a named, documented constant (same discipline as source_analysis.py's
STABILITY_EPSILON / STRONG_REDUNDANCY_THRESHOLD), not a magic number
inline.

No-lookahead is a structural property of this module, not a runtime
check: every function here takes an already-bounded sequence the
caller assembled (e.g. via sentiment_archive.get_archive_range(...,
end_ts=as_of_ts)) -- there is no way to hand this module a value from
"the future" without the caller doing so explicitly, and
experiment5_agent.py's own OBSERVE step is the one place that bound is
actually enforced against a real timestamp.

Sentiment dynamics vs. price outcomes are kept structurally separate:
nothing in this module reads btc_price or any outcome field. Comparing
a sentiment classification against a realized price direction is
experiment5_agent.py's job (evaluate_pending_decisions), never this
module's.
"""

NOISE_BAND = 3.0
# |change| below this, on a 0-100 source scale, is not treated as a
# directional move at all. Chosen from real archived sources_json
# values inspected during the forensic phase (e.g. fng moving 69->71
# within a few hours is a real, small, non-noteworthy fluctuation; a
# move like fng 51->71 or etfflows 96->0, also real and observed in the
# same data, is unambiguously not noise). 3.0 sits comfortably below the
# smallest real moves treated as meaningful in that inspection and
# comfortably above single-point rounding jitter.

PERSISTENCE_MIN_RUN = 3
# This many consecutive same-direction observations (each individually
# still allowed to be inside NOISE_BAND) before calling it "persistent"
# rather than noise -- smallest run that isn't a single blip.

TREND_MIN_RUN = 3
# Consecutive same-direction moves, EACH individually exceeding
# NOISE_BAND, before calling it a trend rather than mere persistence.

REPETITION_DECAY_BASE = 0.5
# Each additional mention of the SAME underlying story (already grouped
# by the caller -- this module does not itself decide what counts as
# "the same story") contributes REPETITION_DECAY_BASE times the
# previous mention's contribution. The first mention (index 0) always
# contributes full weight 1.0. This is the literal mechanism satisfying
# "repeated evidence must have diminishing incremental value" / "do not
# just increase a source's weight because it repeats the same message."
# 0.5 is a deliberately simple, conservative halving -- not fit to any
# data (there is not yet enough archived history to fit one) -- chosen
# so a 2nd mention still counts for something real (0.5) while a 5th
# mention contributes under 0.07, an explicit, documented placeholder
# pending real evidence, same epistemic stance as this project's other
# "reasonable initial parameter, not empirically tuned" constants
# (e.g. MOMENTUM_BLEND_WEIGHT_V1 in worker.js).

CONFIRMATION_WINDOW_MS = 6 * 3600000
# Two sources' directional moves count as confirming each other if
# within this many ms of each other. 6h, not evidence_collector.py's
# tighter 1h SAME_WINDOW_TOLERANCE_MS, because source VALUES in
# `history`/the archive are sampled at an irregular, sometimes multi-hour
# cadence (confirmed directly from real archived timestamps during the
# forensic phase) -- a 1h window would spuriously call two genuinely
# simultaneous real-world moves "not confirming" merely due to polling
# jitter between refresh cycles.


def classify_sequence(values):
    """values: list of (ts, value) tuples, chronological, already
    bounded by the caller. Returns {classification, run_length,
    direction}. classification is one of INSUFFICIENT_DATA / NOISE /
    PERSISTENCE / BULLISH_TREND / BEARISH_TREND."""
    if len(values) < 2:
        return {"classification": "INSUFFICIENT_DATA", "run_length": 0, "direction": None}

    diffs = [values[i][1] - values[i - 1][1] for i in range(1, len(values))]

    # persistence_run counts the full trailing same-sign run, regardless
    # of each step's magnitude. trend_run counts only the trailing
    # SUB-run where every step so far exceeds NOISE_BAND -- once a
    # sub-threshold step is seen, trend_still_active goes False for the
    # rest of the loop (trend_run stops growing) but persistence_run
    # keeps counting through the whole same-sign run. This is what lets
    # a string of small-but-consistent moves (each individually inside
    # NOISE_BAND) still accumulate into PERSISTENCE rather than being
    # capped at run_length=1 by a premature exit.
    trailing_sign = None
    trend_run = 0
    persistence_run = 0
    trend_still_active = True
    for d in reversed(diffs):
        sign = 1 if d > 0 else (-1 if d < 0 else 0)
        if sign == 0:
            break
        if trailing_sign is None:
            trailing_sign = sign
        if sign != trailing_sign:
            break
        persistence_run += 1
        if trend_still_active and abs(d) > NOISE_BAND:
            trend_run += 1
        else:
            trend_still_active = False

    if trailing_sign is not None and trend_run >= TREND_MIN_RUN:
        return {
            "classification": "BULLISH_TREND" if trailing_sign > 0 else "BEARISH_TREND",
            "run_length": trend_run,
            "direction": trailing_sign,
        }
    if trailing_sign is not None and persistence_run >= PERSISTENCE_MIN_RUN:
        return {"classification": "PERSISTENCE", "run_length": persistence_run, "direction": trailing_sign}
    return {"classification": "NOISE", "run_length": persistence_run, "direction": trailing_sign}


def classify_acceleration(values):
    """Second difference of the trailing 3 points. Requires >= 3 points."""
    if len(values) < 3:
        return {"classification": "INSUFFICIENT_DATA", "second_difference": None}
    v = [p[1] for p in values[-3:]]
    d1 = v[1] - v[0]
    d2 = v[2] - v[1]
    second_diff = d2 - d1
    if abs(d1) <= NOISE_BAND and abs(d2) <= NOISE_BAND:
        return {"classification": "NOISE", "second_difference": second_diff}
    if (d1 >= 0) == (d2 >= 0) and second_diff > NOISE_BAND:
        return {"classification": "ACCELERATION", "second_difference": second_diff}
    if (d1 >= 0) == (d2 >= 0) and second_diff < -NOISE_BAND:
        return {"classification": "DECELERATION", "second_difference": second_diff}
    return {"classification": "NOISE", "second_difference": second_diff}


def detect_reversal(values):
    """A REVERSAL requires an ALREADY-ESTABLISHED trend (per
    classify_sequence over every point except the last) immediately
    followed by a non-trivial move in the opposite direction. A single
    up-tick after a single down-tick is NOISE, not a reversal, because
    classify_sequence's own TREND_MIN_RUN gate will not have called the
    prior points a trend at all."""
    if len(values) < TREND_MIN_RUN + 2:
        return {"classification": "INSUFFICIENT_DATA"}
    prior = classify_sequence(values[:-1])
    latest_diff = values[-1][1] - values[-2][1]
    if prior["classification"] in ("BULLISH_TREND", "BEARISH_TREND") and abs(latest_diff) > NOISE_BAND:
        latest_dir = 1 if latest_diff > 0 else -1
        if latest_dir != prior["direction"]:
            return {"classification": "REVERSAL", "from_direction": prior["direction"], "to_direction": latest_dir}
    return {"classification": "NO_REVERSAL"}


def detect_contradiction(direction_a, direction_b):
    """Pure, deterministic disagreement check between two already-
    established directions (+1/-1/0/None) -- e.g. one source's
    classify_sequence direction vs. another's, or sentiment vs. a
    realized price direction (the caller decides what the two inputs
    mean; this function only compares them)."""
    if direction_a is None or direction_b is None or direction_a == 0 or direction_b == 0:
        return {"classification": "INSUFFICIENT_DATA"}
    return {"classification": "CONTRADICTION" if direction_a != direction_b else "AGREEMENT"}


def repetition_decay_weight(occurrence_index):
    """occurrence_index=0 (first mention) -> weight 1.0. Each subsequent
    mention of the same story contributes REPETITION_DECAY_BASE times
    the previous one's weight."""
    if occurrence_index < 0:
        raise ValueError("occurrence_index must be >= 0")
    return REPETITION_DECAY_BASE ** occurrence_index


def score_repeated_evidence(occurrence_indices):
    """Total evidence strength for a cluster of mentions of the SAME
    underlying story, already grouped by the caller. Sum of decayed
    weights -- strictly less than counting each mention at full weight
    (diminishing returns), strictly more than counting only the first
    (independent confirmation elsewhere still matters)."""
    return sum(repetition_decay_weight(i) for i in occurrence_indices)


def cross_source_confirmation(source_events, window_ms=CONFIRMATION_WINDOW_MS):
    """source_events: list of (source_key, ts, direction) tuples, each
    ALREADY an established directional classification (e.g. from
    classify_sequence) -- this function only checks time+direction
    agreement across DISTINCT sources; it never reinterprets or
    reclassifies the inputs. Returns the largest group of >=2 distinct
    sources agreeing in direction within window_ms of each other."""
    if len(source_events) < 2:
        return {"classification": "NO_CONFIRMATION", "confirming_sources": []}
    best = []
    for src_a, ts_a, dir_a in source_events:
        if dir_a in (None, 0):
            continue
        group = {src_a}
        for src_b, ts_b, dir_b in source_events:
            if src_b == src_a:
                continue
            if dir_b == dir_a and abs(ts_b - ts_a) <= window_ms:
                group.add(src_b)
        if len(group) > len(best):
            best = sorted(group)
    if len(best) >= 2:
        return {"classification": "CROSS_SOURCE_CONFIRMATION", "confirming_sources": best}
    return {"classification": "NO_CONFIRMATION", "confirming_sources": []}
