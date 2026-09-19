"""
PR-1: Event x V1 Source x BTC Reaction -- read-only historical
reconstruction proof-of-concept.

=====================================================================
Objective
=====================================================================

Tests whether existing production data (PR3's event detectors, the V1
`history.sources_json` sentiment sources, and `btc_data`) can actually
support the intended final research loop: for each of the 21 V1
sentiment sources, what did it represent around a real, deterministically
-detected market event, and was that followed by measurable, correctly
-timed BTC behaviour?

This module does NOT: recommend a weight change, compute KEEP/INCREASE/
DECREASE verdicts, produce a BUILD_REQUEST, write to any table, propose
a schema, or schedule anything. It is a read-only diagnostic.

=====================================================================
Golden rules enforced
=====================================================================

- $0: pure computation over data already fetched from D1 elsewhere in
  this project's established pattern -- no network call, no LLM call,
  no paid API of any kind exists in this module.
- No V1/V2/Worker file is imported or referenced.
- No schema/migration proposed or applied.
- No write of any kind -- no `persist_*` function exists in this module.
- No automatic weight recommendation -- see `summarize_by_source()`'s
  own docstring; it counts alignment outcomes, it does not rank or
  score sources against each other.
- Read-only.
- Temporal sequence (a source's value existed before the event; the
  event existed before the BTC reaction) is NEVER treated as evidence
  of an incremental reaction by itself. See "Two separate concepts"
  below.

=====================================================================
Two separate concepts, never collapsed (per the build authorization's
explicit Section 7 instruction)
=====================================================================

1. TEMPORAL SEQUENCE: does a V1 source observation exist at/before
   event_ts, and does a BTC price point exist after event_ts? This is
   established by `snapshot_v1_source()` and `resolve_horizon_reaction()`
   independently of any return magnitude or direction.
2. EVIDENCE OF INCREMENTAL REACTION: does the source's own direction
   (relative to its own historical median -- see below) agree with the
   direction BTC actually moved, by more than the empirically-observed
   noise floor for that horizon? This is `classify_alignment()`'s job,
   and it is computed ONLY after temporal sequence is already
   established -- but establishing #1 never implies #2. An event-source
   pair can have perfect temporal sequence and still be classified
   NO_MEASURABLE_REACTION or INSUFFICIENT_EVIDENCE for #2. This module
   never claims a source "caused" or "predicted" BTC's move merely
   because BTC moved afterward (Golden Rule 7 / Section 8).

=====================================================================
Why per-source direction uses "above/below its own historical median"
=====================================================================

Unlike the V1 composite (`history.score`), which already has an
established bullish/bearish-extreme convention in this codebase
(`event_detector.FROZEN_V1_BULLISH_EXTREME`/`FROZEN_V1_BEARISH_EXTREME`),
none of the 21 individual raw sources has a project-established
bullish/bearish threshold -- they sit on different native scales (some
0-100, some signed deltas; see `source_analysis.py`'s own Section 6
finding). Rather than inventing 21 new arbitrary thresholds, this
module compares each event's source reading against that SAME source's
own median value across the analysis window -- a self-normalizing,
scale-invariant convention. This median is a BATCH descriptive
statistic computed once over the whole window (the same disclosure
category as PR5d's `empirical_magnitude_threshold()`/
`empirical_staleness_threshold()`): it is NEVER information available
at any single event_ts, and is used only to interpret which side of
its own typical range a reading sits, not as a prediction-time-
available signal.

=====================================================================
Why the noise floor is empirical, not invented
=====================================================================

A resolved BTC return of, say, +0.01% should not be treated as
"aligned" or "not aligned" with a source's direction -- it is noise.
Rather than inventing a fixed percentage floor, this module computes,
per horizon, the 25th percentile of |return_pct| across every
GOOD/APPROXIMATE-quality-resolved event at that horizon (the same
"P25 of absolute return" convention `movement_distribution.py`
(PR5b) already established for a different series) -- reused here as
a documented convention, not a new number pulled from nowhere.

=====================================================================
Why horizon resolution quality exists (Section 5's explicit instruction)
=====================================================================

`btc_data`'s real cadence is NOT constant (empirically confirmed:
irregular/bursty in the earlier ~2/3 of a typical analysis window,
settling into a clean ~3h cadence, matching the production
`0 */3 * * *` cron, in the most recent portion -- see the PR
description for the exact real numbers from this run). A naive
"nearest available future price" lookup (the same shape
`outcome_engine.py`'s own `RESOLVED` flag uses) would silently accept
a price point from hours after a claimed short horizon as if it
resolved that horizon -- exactly the failure mode already identified
and avoided in this project's FNG-24h readiness work. This module
instead requires the matched point to fall within a disclosed fraction
of the REQUESTED horizon past the true target (`GOOD` <=25% short,
`APPROXIMATE` <=75% short, `POOR` beyond that, `NO_DATA` if nothing
exists in (event_ts, event_ts+horizon] at all), and always reports the
REAL elapsed time used alongside the requested horizon label -- never
silently relabeling a 6-hour-late match as if it resolved a 15-minute
horizon.

No causal claim, no coefficient optimization, no automatic conclusion
beyond what is stated above.
"""

import json
import statistics
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
import event_detector as ed  # noqa: E402
import source_analysis as sa  # noqa: E402

# ---------------------------------------------------------------------
# Reused / disclosed constants -- see module docstring for each choice.
# ---------------------------------------------------------------------
PRE_EVENT_LOOKBACK_MS = 24 * 3600000  # reuses event_detector's own 24h
# convention (LARGE_MOVE/V1_BTC_DIVERGENCE both already compare 24h
# BTC windows) -- not a new number.

CANDIDATE_HORIZONS_MS = {
    "5m": 300000, "15m": 900000, "30m": 1800000, "1h": 3600000,
    "3h": 10800000, "6h": 21600000, "12h": 43200000, "24h": 86400000,
}

GOOD_QUALITY_FRACTION = 0.25        # matched point within 25% of the
APPROXIMATE_QUALITY_FRACTION = 0.75  # horizon short of the exact target
NOISE_FLOOR_PERCENTILE = 0.25       # same P25 convention as movement_distribution.py
GENUINE_HORIZON_SHARE = 0.80        # descriptive-only bar for the
# horizon-resolvability SUMMARY (see summarize_horizon_resolvability's
# own docstring) -- never used to gate an individual event's result.


# =====================================================================
# Step 1: events -- PR3's own, UNCHANGED detector functions
# =====================================================================

def collect_pr3_events(conn, start_ts, end_ts):
    """Runs PR3's five detector functions, UNCHANGED, live over the
    window (mirrors PR5d's fetch_events_for_window() precedent of never
    trusting the sparse persisted research_events table). Deduplicates
    by fingerprint (PR1 schema's own idempotency discipline: identical
    fingerprint = identical event identity, never double-counted), then
    assigns a deterministic, SESSION-LOCAL event_id (sorted by
    (event_ts, fingerprint) for a stable rerun order) -- no
    research_events row is read from or written to by this function.
    """
    all_events = []
    all_events += ed.detect_large_moves(conn, start_ts, end_ts)
    all_events += ed.detect_regime_reversals(conn, start_ts, end_ts)
    all_events += ed.detect_volatility_expansion(conn, start_ts, end_ts)
    all_events += ed.detect_v1_btc_divergence(conn, start_ts, end_ts)
    for horizon_hours in (12, 24):
        all_events += ed.detect_v2_failure_clusters(conn, "BTC", horizon_hours, start_ts, end_ts)

    by_fingerprint = {}
    for event in all_events:
        by_fingerprint[event["fingerprint"]] = event
    deduped = sorted(by_fingerprint.values(), key=lambda e: (e["event_ts"], e["fingerprint"]))
    for i, event in enumerate(deduped):
        event["event_id"] = i + 1
    return deduped


# =====================================================================
# Step 2: source snapshot -- as-of join, identical shape to resolver.py
# =====================================================================

def snapshot_v1_source(conn, event_ts, source_key):
    """AS-OF lookup: the latest `history` row with ts <= event_ts,
    identical join shape to resolver.py's own proven-safe pattern.
    Never uses a history row with ts > event_ts (no-lookahead,
    constructively tested)."""
    row = conn.execute(
        "SELECT ts, sources_json FROM history WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
        (event_ts,),
    ).fetchone()
    if row is None:
        return {"source_value": None, "source_observation_ts": None,
                "staleness_ms": None, "status": "NO_HISTORY_BEFORE_EVENT"}
    ts, sources_json = row
    try:
        sources = json.loads(sources_json) if sources_json else {}
    except (TypeError, ValueError):
        sources = {}
    value = sources.get(source_key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value = None
    return {
        "source_value": float(value) if value is not None else None,
        "source_observation_ts": ts,
        "staleness_ms": event_ts - ts,
        "status": "OK" if value is not None else "SOURCE_MISSING_AT_OBSERVATION",
    }


# =====================================================================
# Step 3: pre-event BTC control (Section 14: control for pre-existing move)
# =====================================================================

def _price_at_or_before(conn, ts):
    return conn.execute(
        "SELECT ts, btc_price FROM btc_data WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts,)
    ).fetchone()


def compute_pre_event_return(conn, event_ts, lookback_ms=PRE_EVENT_LOOKBACK_MS):
    """BTC's own trailing return INTO event_ts, using only prices at or
    before event_ts -- never a point after it. This is the control
    variable Section 14 requires: a large post-event move is not
    automatically attributed to the event/source if BTC was already
    moving beforehand."""
    anchor = _price_at_or_before(conn, event_ts)
    baseline = _price_at_or_before(conn, event_ts - lookback_ms)
    if anchor is None or baseline is None or baseline[0] >= anchor[0] or baseline[1] == 0:
        return {"status": "INSUFFICIENT_EVIDENCE", "return_pct": None}
    return {
        "status": "OK",
        "return_pct": (anchor[1] - baseline[1]) / baseline[1] * 100.0,
        "baseline_ts": baseline[0], "anchor_ts": anchor[0],
    }


# =====================================================================
# Step 4: post-event BTC reaction, multi-horizon (Section 13)
# =====================================================================

def resolve_horizon_reaction(conn, event_ts, horizon_ms, anchor_price_row):
    """Finds the LATEST btc_data point strictly after event_ts and at or
    before event_ts+horizon_ms -- never a point at/before event_ts
    (no-lookahead into the reaction itself would be meaningless; the
    point must be genuinely new). Reports resolution QUALITY (how close
    the real match is to the true target) rather than silently
    accepting any future point as if it resolved the requested horizon
    -- see module docstring."""
    target_ts = event_ts + horizon_ms
    row = conn.execute(
        "SELECT ts, btc_price FROM btc_data WHERE ts > ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (event_ts, target_ts),
    ).fetchone()
    if row is None or anchor_price_row is None:
        return {"status": "NO_DATA", "quality": None, "elapsed_ms": None,
                "gap_after_ms": None, "return_pct": None}
    matched_ts, matched_price = row
    anchor_ts, anchor_price = anchor_price_row
    elapsed_ms = matched_ts - event_ts
    gap_after_ms = target_ts - matched_ts
    if gap_after_ms <= horizon_ms * GOOD_QUALITY_FRACTION:
        quality = "GOOD"
    elif gap_after_ms <= horizon_ms * APPROXIMATE_QUALITY_FRACTION:
        quality = "APPROXIMATE"
    else:
        quality = "POOR"
    if anchor_price == 0:
        return {"status": "NO_DATA", "quality": None, "elapsed_ms": elapsed_ms,
                "gap_after_ms": gap_after_ms, "return_pct": None}
    return {
        "status": "OK", "quality": quality, "matched_ts": matched_ts,
        "elapsed_ms": elapsed_ms, "gap_after_ms": gap_after_ms,
        "return_pct": (matched_price - anchor_price) / anchor_price * 100.0,
    }


# =====================================================================
# Step 5: self-normalizing source direction (batch statistic, disclosed)
# =====================================================================

def compute_source_medians(conn, start_ts, end_ts, source_keys):
    """BATCH descriptive statistic over the whole analysis window -- see
    module docstring's disclosure. NEVER information available at any
    single event_ts."""
    _, matrix_rows, _ = sa.extract_source_matrix(conn, start_ts, end_ts)
    medians = {}
    for key in source_keys:
        values = [row["sources"].get(key) for row in matrix_rows if row["sources"].get(key) is not None]
        medians[key] = statistics.median(values) if values else None
    return medians


# =====================================================================
# Step 6: alignment -- evidence of incremental reaction, kept SEPARATE
# from temporal sequence (see module docstring)
# =====================================================================

def classify_alignment(source_value, source_median, horizon_reactions, noise_floor_by_horizon):
    """Never called unless temporal sequence (source snapshot + at least
    an attempted horizon lookup) already exists. Still returns
    INSUFFICIENT_EVIDENCE / NO_MEASURABLE_REACTION honestly rather than
    forcing ALIGNED/NOT_ALIGNED (Golden Rule 15 / Section 15)."""
    if source_value is None or source_median is None:
        return {"result": "INSUFFICIENT_EVIDENCE",
                "reason": "no source observation available", "horizons_used": []}

    usable = [(h, r) for h, r in horizon_reactions.items()
              if r["status"] == "OK" and r["quality"] in ("GOOD", "APPROXIMATE")]
    if not usable:
        return {"result": "INSUFFICIENT_EVIDENCE",
                "reason": "no horizon resolved with usable quality", "horizons_used": []}

    if source_value == source_median:
        return {"result": "INSUFFICIENT_EVIDENCE",
                "reason": "source exactly at its own median -- no directional signal",
                "horizons_used": [h for h, _ in usable]}
    source_implies_up = source_value > source_median

    measurable = []
    for h, r in usable:
        floor = noise_floor_by_horizon.get(h)
        if floor is not None and abs(r["return_pct"]) < floor:
            continue
        measurable.append((h, r))
    if not measurable:
        return {"result": "NO_MEASURABLE_REACTION",
                "reason": "all resolved horizons were below the empirical noise floor",
                "horizons_used": [h for h, _ in usable]}

    aligned_count = sum(
        1 for h, r in measurable
        if (source_implies_up and r["return_pct"] > 0) or (not source_implies_up and r["return_pct"] < 0)
    )
    total = len(measurable)
    if aligned_count > total / 2:
        result = "ALIGNED"
    elif aligned_count < total / 2:
        result = "NOT_ALIGNED"
    else:
        result = "MIXED"  # a genuine tie across horizons -- never forced either way
    return {
        "result": result, "horizons_used": [h for h, _ in measurable],
        "aligned_count": aligned_count, "total_measurable": total,
    }


# =====================================================================
# Top-level orchestration
# =====================================================================

def build_event_source_reaction_dataset(conn, start_ts, end_ts):
    """The single top-level entry point. Read-only. Never writes.
    Returns every event x source diagnostic result plus the inputs used
    to derive alignment (medians, empirical noise floor) for full
    auditability.
    """
    events = collect_pr3_events(conn, start_ts, end_ts)
    source_keys = sa.discover_sources(conn, start_ts, end_ts)
    source_medians = compute_source_medians(conn, start_ts, end_ts, source_keys)

    raw_results = []
    event_reactions = {}
    for event in events:
        anchor = _price_at_or_before(conn, event["event_ts"])
        pre_event = compute_pre_event_return(conn, event["event_ts"])
        horizon_reactions = {
            label: resolve_horizon_reaction(conn, event["event_ts"], ms, anchor)
            for label, ms in CANDIDATE_HORIZONS_MS.items()
        }
        event_reactions[event["event_id"]] = horizon_reactions
        for source_key in source_keys:
            snap = snapshot_v1_source(conn, event["event_ts"], source_key)
            raw_results.append({
                "event_id": event["event_id"], "event_ts": event["event_ts"],
                "event_category": event["category"], "event_direction": event.get("direction"),
                "is_post_event_analysis": event["is_post_event_analysis"],
                "source_key": source_key,
                "source_value": snap["source_value"],
                "source_observation_ts": snap["source_observation_ts"],
                "source_observation_status": snap["status"],
                "pre_event_btc_return": pre_event,
                "post_event_btc_reactions": horizon_reactions,
            })

    # Empirical per-horizon noise floor -- P25 of |return_pct| among
    # GOOD/APPROXIMATE-quality reactions, counted once per EVENT (not
    # once per event x source, which would just repeat the same BTC
    # number 21 times and bias the percentile toward nothing new).
    abs_returns_by_horizon = defaultdict(list)
    for event_id, horizon_reactions in event_reactions.items():
        for h, r in horizon_reactions.items():
            if r["status"] == "OK" and r["quality"] in ("GOOD", "APPROXIMATE"):
                abs_returns_by_horizon[h].append(abs(r["return_pct"]))
    noise_floor_by_horizon = {}
    for h, vals in abs_returns_by_horizon.items():
        if vals:
            vals_sorted = sorted(vals)
            idx = int(len(vals_sorted) * NOISE_FLOOR_PERCENTILE)
            noise_floor_by_horizon[h] = vals_sorted[min(idx, len(vals_sorted) - 1)]

    final_results = []
    for r in raw_results:
        alignment = classify_alignment(
            r["source_value"], source_medians.get(r["source_key"]),
            r["post_event_btc_reactions"], noise_floor_by_horizon,
        )
        final_results.append({**r, "alignment": alignment})

    return {
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "events": events,
        "source_keys": source_keys,
        "source_medians": source_medians,
        "noise_floor_by_horizon": noise_floor_by_horizon,
        "results": final_results,
    }


# =====================================================================
# Descriptive summaries only -- no recommendation, no ranking
# =====================================================================

def summarize_horizon_resolvability(dataset):
    """Purely descriptive: for each candidate horizon, what fraction of
    the REAL events in this run had a GOOD/APPROXIMATE-quality match?
    `genuinely_resolvable` is a disclosed >=80% convention for this
    summary field ONLY -- it never gates any individual event's result,
    which always carries its own real quality label regardless of this
    aggregate."""
    events = dataset["events"]
    n_events = len(events)
    per_event_reactions = {}
    for r in dataset["results"]:
        per_event_reactions.setdefault(r["event_id"], r["post_event_btc_reactions"])

    summary = {}
    for h in CANDIDATE_HORIZONS_MS:
        counts = defaultdict(int)
        for reactions in per_event_reactions.values():
            reaction = reactions[h]
            key = "NO_DATA" if reaction["status"] == "NO_DATA" else reaction["quality"]
            counts[key] += 1
        good = counts.get("GOOD", 0)
        approx = counts.get("APPROXIMATE", 0)
        good_or_approx_fraction = (good + approx) / n_events if n_events else 0.0
        summary[h] = {
            "n_events": n_events, "GOOD": good, "APPROXIMATE": approx,
            "POOR": counts.get("POOR", 0), "NO_DATA": counts.get("NO_DATA", 0),
            "good_fraction": good / n_events if n_events else 0.0,
            "good_or_approximate_fraction": good_or_approx_fraction,
            "genuinely_resolvable": good_or_approx_fraction >= GENUINE_HORIZON_SHARE,
        }
    return summary


def summarize_by_source(dataset):
    """Purely descriptive counts per source -- events evaluated, source
    observations available, and the four alignment outcomes (plus the
    honest MIXED tie category). Deliberately produces NO ranking, NO
    score, NO KEEP/INCREASE/DECREASE verdict -- see module docstring."""
    per_source = defaultdict(lambda: defaultdict(int))
    for r in dataset["results"]:
        stats = per_source[r["source_key"]]
        stats["events_evaluated"] += 1
        if r["source_observation_status"] != "OK":
            stats["no_source_observation"] += 1
            continue
        stats["source_observations_available"] += 1
        stats[r["alignment"]["result"]] += 1
    return {k: dict(v) for k, v in per_source.items()}
