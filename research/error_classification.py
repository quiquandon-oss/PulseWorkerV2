"""
PR5d: V2 prediction error classification against resolved BTC outcomes
and existing V1/event context.

Objective (per PR5d build authorization): for every resolved BTC
prediction, deterministically classify what KIND of miss (if any)
occurred -- WRONG_DIRECTION, CORRECT_DIRECTION_WRONG_MAGNITUDE,
MISSING_EVENT, STALE_SENTIMENT, MISLEADING_SENTIMENT,
TECHNICAL_SENTIMENT_CONFLICT, REGIME_CHANGE, UNEXPECTED_SHOCK, or
INSUFFICIENT_INFORMATION -- then report error counts/rates broken down
by coin/horizon, V1 composite bucket, PR3 event category, regime,
confidence band, model version, and chronological period.

Scope: BTC only (the `predictions` table), matching PR2/PR5a/PR5c's own
precedent of not generalizing across coins until each coin's table
shape (link_predictions/eth_predictions) is independently verified.

Read-only with respect to every production table this module queries
(`predictions`, `history`, `btc_data`, `research_events`,
`research_event_evidence`). The only function that writes anything is
persist_findings(), which INSERTs into the already-deployed
`research_analyses` table -- exercised only against an in-memory
SQLite fixture in test_error_classification.py, never invoked against
production in this PR.

No V1/V2 modification, no coefficient change, no causal claim anywhere
in this module's output -- every classification is an explicit,
versioned, DETERMINISTIC rule over already-known facts (what happened,
when), never an inference about WHY the model behaves as it does.

=====================================================================
Strict separation: observed facts vs. descriptive classification vs.
candidate hypotheses (never causal explanations)
=====================================================================

Every classify_prediction() result carries three clearly separated
parts:
  - "observed_fact": raw, uninterpreted numbers (p_up, realized_return,
    direction_correct, ...) -- never touched by the classification rule.
  - "error_type": one of the nine taxonomy labels (or None if the
    prediction was directionally correct and within the empirical
    magnitude band), assigned by a single, versioned, deterministic
    decision rule (CLASSIFICATION_RULE_VERSION) over already-known,
    already-available-at-the-relevant-time data. This is a DESCRIPTIVE
    label ("this row looks like X"), not a claim that X CAUSED the
    error.
  - "contributing_signals": the exact deterministic inputs the rule
    read to reach that label (which events overlapped the window, the
    V1 staleness gap, the technical/composite conflict flag, ...) so a
    human reviewer can audit the decision without re-deriving it.

derive_candidate_observations() goes one step further and looks for
CONCENTRATED patterns across many predictions -- but its output is
capped at the evidence-gate labels OBSERVATION / RESEARCH_HYPOTHESIS
(see EVIDENCE_GATE_STATUSES below) and every such finding repeats,
verbatim, that it is a descriptive association, not a causal
explanation and not yet a validated hypothesis. This module never
assigns MONITOR, VALIDATION_READY, BUILD_REQUEST, AWAITING_APPROVAL,
IMPLEMENTED, VALIDATED, REJECTED, or ROLLED_BACK -- those stages
require validation work this PR does not perform.

=====================================================================
No new thresholds invented -- reused-frozen or empirically-derived-
and-labeled-PROPOSED only
=====================================================================

- V1 composite bucketing reuses event_detector.py's own ALREADY-FROZEN
  FROZEN_V1_BEARISH_EXTREME (45) / FROZEN_V1_BULLISH_EXTREME (58)
  constants verbatim (imported, not duplicated) -- the same boundary
  PR3's own V1_BTC_DIVERGENCE detector uses. No new V1-bucket threshold
  is introduced.
- The magnitude boundary that separates CORRECT_DIRECTION_WRONG_
  MAGNITUDE from a directionally-correct-and-close prediction is NEVER
  hard-coded. empirical_magnitude_threshold() computes the actual
  distribution of |realized_return| among directionally-correct,
  resolved predictions in the requested window (reusing PR5b's
  movement_distribution.summarize_absolute_returns(), not
  reimplementing percentile math) and reports the median as a
  PROPOSED (not frozen) cut, alongside the full p25/p50/p75/p90
  context -- exactly mirroring movement_distribution.py's own
  propose_movement_buckets() convention (status: "PROPOSED", requires
  human review before freezing).
- The staleness gap that flags STALE_SENTIMENT is likewise never a
  hard-coded number of hours. empirical_staleness_threshold() computes
  the actual distribution of (prediction_ts - v1_observation_ts) gaps
  in-sample and proposes its 90th percentile as the cut, reported
  alongside p50/p75/p90 -- again PROPOSED, not frozen.
- REGIME_CHANGE and UNEXPECTED_SHOCK are driven entirely by PR3's own,
  UNCHANGED, public event_detector functions (detect_regime_reversals,
  detect_large_moves) -- this module calls them, never re-derives or
  re-freezes their thresholds.

=====================================================================
Temporal safety (Section 4 of the build authorization)
=====================================================================

information available at prediction_ts -> prediction -> realized
outcome at target_ts. Every signal this module uses to explain a
prediction's error is checked against that boundary explicitly:

  - V1 composite/technical_score/gold_regime come from the as-of join
    (h.ts <= p.ts, the same proven-safe correlated-subquery shape as
    resolver.py) -- by construction, never a later observation.
  - PR3 events are split into two disjoint sets per prediction:
    "overlapping the outcome window" (prediction_ts < event_ts <=
    target_ts) is used ONLY to explain what happened DURING the
    outcome period (UNEXPECTED_SHOCK / REGIME_CHANGE / MISSING_EVENT)
    -- never claimed as information the prediction could have used.
    V1_BTC_DIVERGENCE events are ADDITIONALLY always
    is_post_event_analysis=1 (PR3's own flag, read verbatim, never
    overridden) and are used here strictly as post-outcome research
    context for MISLEADING_SENTIMENT, exactly as PR3's own module
    docstring requires ("must never be presented as information
    available at T").
  - test_error_classification.py::test_no_lookahead_event_before_prediction_never_used
    and ::test_event_at_or_before_prediction_ts_excluded_from_window
    give constructive proof of this boundary, not just a comment.

  IMPORTANT (post-review clarification): empirical_staleness_threshold()
  and empirical_magnitude_threshold() are DESCRIPTIVE BATCH STATISTICS,
  computed once over every row in the requested analysis window --
  including rows chronologically AFTER the specific prediction being
  classified. That is safe and correct for what they are (a reporting
  threshold characterizing "typical" staleness/magnitude across this
  window, exactly like movement_distribution's own percentiles), but
  they must NEVER be read as information available to the original V2
  prediction at prediction_ts -- no online, real-time process could
  have computed "this window's own future median" before the window
  finished. Only the per-row inputs actually compared against these
  thresholds (v1_staleness_gap_ms, the row's own realized_return) are
  prediction-time-safe in the Section 4 sense; the thresholds
  themselves are research-time-only batch summaries.

=====================================================================
IMPORTANT (post-review clarification): winner labels are NOT
independent evidence of cause prevalence
=====================================================================

classify_prediction() reports exactly ONE error_type per prediction --
the highest-priority match in the fixed, documented, and now
explicitly tested precedence order (see classify_prediction()'s own
docstring and test_error_classification.py's priority-interaction
tests). When a prediction's window genuinely satisfies MULTIPLE
candidate causes at once (real production data shows this happens --
see the PR description), only the highest-ranked one is ever reported
as error_type; the others are NOT silently discarded -- they remain
visible via contributing_signals's own counts (e.g.
regime_reversal_events_in_window) for exactly this audit purpose -- but
they never appear as error_type and are therefore invisible to any
code that only reads the winner-label counts.

Consequence: the error_counts this module reports (WRONG_DIRECTION=N,
UNEXPECTED_SHOCK=M, ...) are WINNER-LABEL frequencies under this fixed
precedence, NOT independent estimates of how often each underlying
cause was present. A category ranked low in the precedence order
(REGIME_CHANGE, MISLEADING_SENTIMENT, TECHNICAL_SENTIMENT_CONFLICT,
STALE_SENTIMENT) can be systematically under-counted here even when it
was genuinely present, simply because something higher-ranked also
matched. Do not treat these frequencies as mutually exclusive
population-level prevalence estimates without first consulting
contributing_signals (or a future overlap analysis over all matched
categories, not just the winner) -- this PR does not build that
overlap analysis; it is intentionally out of scope here (see PR
description for the queued follow-up).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import event_detector  # noqa: E402
import movement_distribution  # noqa: E402

MAX_WINDOW_MS = 90 * 24 * 3600 * 1000  # same bound/rationale as every
# other research module in this project (resolver.py, outcome_engine.py,
# source_analysis.py, ...).

SUPPORTED_COIN = "BTC"
SUPPORTED_HORIZONS = (12, 24)  # the two horizons actually present in
# production predictions (verified empirically before writing this
# module -- see PR description; NOT assumed).

CLASSIFICATION_RULE_VERSION = "pr5d-v1"

ERROR_TYPES = (
    "WRONG_DIRECTION",
    "CORRECT_DIRECTION_WRONG_MAGNITUDE",
    "MISSING_EVENT",
    "STALE_SENTIMENT",
    "MISLEADING_SENTIMENT",
    "TECHNICAL_SENTIMENT_CONFLICT",
    "REGIME_CHANGE",
    "UNEXPECTED_SHOCK",
    "INSUFFICIENT_INFORMATION",
)

# The full lifecycle the PR5 specification defines (mirrors PR5a's
# research_hypotheses.status convention verbatim). PR5d may only ever
# assign the first two -- everything from MONITOR onward requires
# validation work explicitly out of scope here.
EVIDENCE_GATE_STATUSES = (
    "OBSERVATION",
    "MONITOR",
    "RESEARCH_HYPOTHESIS",
    "VALIDATION_READY",
    "BUILD_REQUEST",
    "AWAITING_APPROVAL",
    "IMPLEMENTED",
    "VALIDATED",
    "REJECTED",
    "ROLLED_BACK",
)
PR5D_MAX_EVIDENCE_GATE_STATUS = "RESEARCH_HYPOTHESIS"  # this module never
# assigns anything past this point in EVIDENCE_GATE_STATUSES.

# Reused verbatim from event_detector.py -- NOT duplicated as a second
# literal. Buckets V1's composite score the same way PR3's own
# V1_BTC_DIVERGENCE detector already does.
FROZEN_V1_BEARISH_EXTREME = event_detector.FROZEN_V1_BEARISH_EXTREME
FROZEN_V1_BULLISH_EXTREME = event_detector.FROZEN_V1_BULLISH_EXTREME

# Below this per-bucket sample size, a rate is still computed but
# flagged -- never hidden, never treated as a significance claim
# (Section 11: "do not introduce inferential significance labels
# without explicitly justified sample-size requirements").
MIN_SAMPLE_FOR_RATE = 30
MIN_SAMPLE_FOR_TRANSITIONS = 30

CHRONOLOGICAL_PERIOD_MS = 7 * 24 * 3600 * 1000  # weekly buckets --
# descriptive binning only, not a statistical claim.

# How close an event's timestamp must be to a persisted research_events
# row before this module treats them as "the same event" for the
# OPTIONAL PR4-evidence cross-reference (Section on EVENT LINKAGE /
# PR4 EVIDENCE). Loose on purpose: this is a best-effort lookup against
# a currently very sparse table (3 rows in production at the time this
# module was written -- see PR description), never a hard requirement.
EVIDENCE_MATCH_TOLERANCE_MS = 5 * 60 * 1000


def _validate_bounds(start_ts, end_ts):
    if start_ts is None or end_ts is None:
        raise ValueError("start_ts and end_ts are required -- there is no unbounded mode")
    if start_ts >= end_ts:
        raise ValueError(f"start_ts ({start_ts}) must be strictly before end_ts ({end_ts})")
    window = end_ts - start_ts
    if window > MAX_WINDOW_MS:
        raise ValueError(f"requested window ({window}ms) exceeds MAX_WINDOW_MS ({MAX_WINDOW_MS}ms)")


# =====================================================================
# Bounded, indexed data access (same as-of pattern as resolver.py,
# extended with the additional predictions columns PR5d needs). This
# duplicates resolver.py's JOIN shape rather than modifying that
# already-frozen, narrowly-scoped module -- the same "don't touch a
# proven module, extend via a sibling query" precedent
# outcome_engine.py's HISTORY_OUTCOME_SQL already established.
# =====================================================================

BASE_SQL = """
SELECT
  p.ts AS prediction_ts,
  p.horizon_hours AS horizon_hours,
  p.target_ts AS target_ts,
  p.p_up AS p_up,
  p.realized_up AS realized_up,
  p.realized_return AS realized_return,
  p.model_version AS model_version,
  p.git_commit_sha AS git_commit_sha,
  h.ts AS v1_observation_ts,
  h.score AS v1_composite,
  h.technical_score AS technical_score,
  h.gold_regime AS gold_regime
FROM predictions p
LEFT JOIN history h ON h.ts = (SELECT MAX(h2.ts) FROM history h2 WHERE h2.ts <= p.ts)
WHERE p.horizon_hours = ? AND p.ts >= ? AND p.ts < ?
ORDER BY p.ts ASC
"""


def fetch_resolved_predictions_with_v1_context(conn, horizon_hours, start_ts, end_ts):
    """The one bounded, indexed query this module's data layer runs.
    Both bounds are required (no default) -- mirrors resolver.py's own
    contract exactly, so a caller cannot construct an unbounded call by
    omission. horizon_hours must be one of SUPPORTED_HORIZONS.

    Returns every predictions row in the window exactly once (resolved
    or not -- resolved_up IS NULL rows are returned too, with
    realized_up/realized_return as None, never dropped), each carrying
    its as-of V1 context (None for every V1 field when no history row
    exists at or before that prediction's own ts -- missing, never
    zero, never imputed).
    """
    _validate_bounds(start_ts, end_ts)
    if horizon_hours not in SUPPORTED_HORIZONS:
        raise ValueError(
            f"unsupported horizon_hours ({horizon_hours}); must be one of {SUPPORTED_HORIZONS} "
            f"(the horizons empirically verified present in production predictions -- see PR description)"
        )
    cursor = conn.execute(BASE_SQL, (horizon_hours, start_ts, end_ts))
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def explain_base_query_plan(conn, horizon_hours, start_ts, end_ts):
    """Returns the real EXPLAIN QUERY PLAN rows for the exact same bound
    query fetch_resolved_predictions_with_v1_context() would run."""
    return conn.execute("EXPLAIN QUERY PLAN " + BASE_SQL, (horizon_hours, start_ts, end_ts)).fetchall()


# =====================================================================
# PR3 event linkage -- computed LIVE via event_detector's own, UNCHANGED
# public functions, once per analysis window (not once per prediction --
# that would be a redundant-query anti-pattern this project's other
# modules already avoid). Why live rather than reading the persisted
# research_events table: that table has only 3 rows in production (all
# LARGE_MOVE, from a single manual verification run -- see PR
# description), so joining against it as the primary source would make
# nearly every prediction spuriously MISSING_EVENT for lack of
# persistence, not for a genuine absence of a market event. Computing
# events on demand, with PR3's exact frozen rules, gives real coverage
# regardless of how populated that table happens to be right now.
# =====================================================================

def fetch_events_for_window(conn, start_ts, end_ts):
    """Calls each of PR3's public detectors exactly once for
    [start_ts, end_ts] (each detector applies its own LOOKBACK_BUFFER_MS
    internally, unchanged). Returns a dict keyed by category ->
    list of event dicts, each with at least event_ts/direction/intensity/
    is_post_event_analysis, sorted by event_ts.

    detect_v2_failure_clusters() is deliberately NOT included here: it
    is itself derived from a streak of prediction errors, so feeding it
    back in as a "cause" of an individual error would be circular. See
    v2_failure_cluster_context() below for how this module surfaces it
    instead -- as purely descriptive context, never as a classification
    input.
    """
    _validate_bounds(start_ts, end_ts)
    events_by_category = {}
    for category, events in (
        ("LARGE_MOVE", event_detector.detect_large_moves(conn, start_ts, end_ts)),
        ("REGIME_REVERSAL", event_detector.detect_regime_reversals(conn, start_ts, end_ts)),
        ("VOLATILITY_EXPANSION", event_detector.detect_volatility_expansion(conn, start_ts, end_ts)),
        ("V1_BTC_DIVERGENCE", event_detector.detect_v1_btc_divergence(conn, start_ts, end_ts)),
    ):
        events_by_category[category] = sorted(events, key=lambda e: e["event_ts"])
    return events_by_category


def v2_failure_cluster_context(conn, coin, horizon_hours, start_ts, end_ts):
    """Descriptive-only context: whether/where a V2_FAILURE_CLUSTER event
    (5 consecutive wrong predictions, PR3's own frozen rule) occurred in
    this window. Never fed into classify_prediction() as a cause --
    reported alongside the classification report purely as "this
    happened during a period PR3 already flags as a failure streak",
    which the reader may use for context, not as this module's own
    causal claim.
    """
    return event_detector.detect_v2_failure_clusters(conn, coin, horizon_hours, start_ts, end_ts)


def _events_overlapping_window(events, window_start_ts, window_end_ts):
    """Strictly (window_start_ts, window_end_ts] -- an event AT OR BEFORE
    the prediction's own ts is, by definition, not something that
    happened during the outcome window; an event exactly at target_ts is
    included (the outcome is realized at/by target_ts)."""
    return [e for e in events if window_start_ts < e["event_ts"] <= window_end_ts]


def find_evidence_for_event(conn, category, event_ts, tolerance_ms=EVIDENCE_MATCH_TOLERANCE_MS):
    """OPTIONAL PR4-evidence cross-reference (Section 16 -- never a hard
    dependency). Looks for a persisted research_events row matching
    (category, event_ts) within tolerance_ms, then, if found, for any
    research_event_evidence rows attached to it. Bounded query (a small
    ts range, indexed via idx_research_events_event_ts).

    Returns {"status": "NO_EVIDENCE_FOUND"} when no matching persisted
    event exists (expected in current production data: research_events
    has 3 rows and research_event_evidence has 0 -- see PR description)
    or when one exists but carries no evidence rows. Never fabricates
    evidence, never raises for the empty case.
    """
    event_row = conn.execute(
        "SELECT event_id FROM research_events "
        "WHERE category = ? AND event_ts >= ? AND event_ts <= ? LIMIT 1",
        (category, event_ts - tolerance_ms, event_ts + tolerance_ms),
    ).fetchone()
    if event_row is None:
        return {"status": "NO_EVIDENCE_FOUND", "reason": "no_matching_persisted_event"}
    event_id = event_row[0]
    evidence_rows = conn.execute(
        "SELECT article_url, publisher, publication_ts, headline, evidence_relation "
        "FROM research_event_evidence WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    if not evidence_rows:
        return {"status": "NO_EVIDENCE_FOUND", "reason": "event_persisted_but_no_evidence_rows",
                "event_id": event_id}
    columns = ["article_url", "publisher", "publication_ts", "headline", "evidence_relation"]
    return {"status": "OK", "event_id": event_id,
            "evidence": [dict(zip(columns, row)) for row in evidence_rows]}


# =====================================================================
# Empirically-derived (never invented) thresholds
# =====================================================================

def empirical_staleness_threshold(rows):
    """PROPOSED (never frozen) staleness cut, from the actual
    distribution of (prediction_ts - v1_observation_ts) gaps among rows
    that have V1 context at all. Returns percentiles plus a
    proposed_threshold_ms (the 90th percentile) and the sample size --
    never a hard-coded number of hours.
    """
    gaps = sorted(
        r["prediction_ts"] - r["v1_observation_ts"]
        for r in rows
        if r.get("v1_observation_ts") is not None
    )
    n = len(gaps)
    if n == 0:
        return {"status": "INSUFFICIENT_DATA", "n": 0, "proposed_threshold_ms": None}

    def pct(p):
        rank = max(0, min(n - 1, round(p * (n - 1))))
        return gaps[rank]

    return {
        "status": "PROPOSED",
        "n": n,
        "p50_ms": pct(0.50),
        "p75_ms": pct(0.75),
        "p90_ms": pct(0.90),
        "proposed_threshold_ms": pct(0.90),
        "note": "PROPOSED from this window's own data -- not frozen. Human review required before freezing.",
    }


def empirical_magnitude_threshold(rows):
    """PROPOSED (never frozen) magnitude cut separating
    CORRECT_DIRECTION_WRONG_MAGNITUDE from directionally-correct-and-
    close, from the actual |realized_return| distribution among
    directionally-correct, resolved rows in this window. Reuses PR5b's
    movement_distribution.summarize_absolute_returns() rather than
    reimplementing percentile math -- constructs outcome_engine-shaped
    rows from predictions.realized_return directly.
    """
    outcome_shaped = []
    for r in rows:
        if r.get("realized_up") is None or r.get("realized_return") is None or r.get("p_up") is None:
            continue
        direction_correct = (r["p_up"] > 0.5 and r["realized_up"] == 1) or (
            r["p_up"] <= 0.5 and r["realized_up"] == 0
        )
        if not direction_correct:
            continue
        outcome_shaped.append({
            "forward_return_pct": r["realized_return"],
            "absolute_forward_return_pct": abs(r["realized_return"]),
            "outcome_status": "RESOLVED",
        })
    summary = movement_distribution.summarize_absolute_returns(outcome_shaped)
    if summary["n"] == 0:
        return {"status": "INSUFFICIENT_DATA", "n": 0, "proposed_threshold_pct": None}
    return {
        "status": "PROPOSED",
        "n": summary["n"],
        "p25_pct": movement_distribution._percentile(
            sorted(o["absolute_forward_return_pct"] for o in outcome_shaped), 0.25
        ),
        "p50_pct": summary["abs_p50"],
        "p75_pct": summary["abs_p75"],
        "p90_pct": summary["abs_p90"],
        "proposed_threshold_pct": summary["abs_p50"],
        "note": "PROPOSED from this window's own directionally-correct sample -- not frozen. Human review required before freezing.",
    }


def v1_composite_bucket(v1_composite):
    """Reuses PR3's own frozen extremes verbatim. Returns None (never a
    fabricated bucket) when v1_composite itself is None."""
    if v1_composite is None:
        return None
    if v1_composite < FROZEN_V1_BEARISH_EXTREME:
        return "BEARISH"
    if v1_composite > FROZEN_V1_BULLISH_EXTREME:
        return "BULLISH"
    return "NEUTRAL"


def confidence_band_cuts(rows):
    """PROPOSED (never frozen) p_up confidence bands, from the actual
    distribution of |p_up - 0.5| in this window's own resolved rows.
    Returns the empirical tercile cut points (never fewer than the
    sample supports) plus the sample size."""
    distances = sorted(abs(r["p_up"] - 0.5) for r in rows if r.get("p_up") is not None)
    n = len(distances)
    if n == 0:
        return {"status": "INSUFFICIENT_DATA", "n": 0, "low_cut": None, "high_cut": None}

    def pct(p):
        rank = max(0, min(n - 1, round(p * (n - 1))))
        return distances[rank]

    return {"status": "PROPOSED", "n": n, "low_cut": pct(1 / 3), "high_cut": pct(2 / 3)}


def confidence_band(p_up, cuts):
    if p_up is None or cuts.get("low_cut") is None:
        return None
    distance = abs(p_up - 0.5)
    if distance <= cuts["low_cut"]:
        return "LOW_CONFIDENCE"
    if distance >= cuts["high_cut"]:
        return "HIGH_CONFIDENCE"
    return "MEDIUM_CONFIDENCE"


def chronological_period_bucket(ts, window_start_ts, period_ms=CHRONOLOGICAL_PERIOD_MS):
    return (ts - window_start_ts) // period_ms


# =====================================================================
# Deterministic, versioned classification
# =====================================================================

def classify_prediction(row, events_by_category, staleness_threshold_ms, magnitude_threshold_pct):
    """The single classification rule. Deterministic, ordered,
    first-match-wins -- given the same row/events/thresholds, always
    returns the same label (Section 18: deterministic rerun).

    Priority order (each documented at its own branch below):
      1. UNRESOLVED -- not an error, excluded from error statistics.
      2. INSUFFICIENT_INFORMATION -- no V1 context at all.
      3. UNEXPECTED_SHOCK -- a LARGE_MOVE happened during the outcome window.
      4. REGIME_CHANGE -- a REGIME_REVERSAL happened during the outcome window.
      5. MISSING_EVENT -- a VOLATILITY_EXPANSION happened during the outcome window.
      6. MISLEADING_SENTIMENT -- outcome window overlaps a V1_BTC_DIVERGENCE
         post-outcome event (used strictly as post-outcome context).
      7. TECHNICAL_SENTIMENT_CONFLICT -- composite and technical_score
         disagree in sign at prediction time.
      8. STALE_SENTIMENT -- the V1 observation used is older than the
         (PROPOSED, caller-supplied) staleness threshold.
      9. WRONG_DIRECTION -- default: direction was wrong, no more
         specific deterministic cause matched.
      10. CORRECT_DIRECTION_WRONG_MAGNITUDE -- direction correct, but
          |realized_return| at/above the (PROPOSED, caller-supplied)
          magnitude threshold.
      11. None -- direction correct and within the magnitude band: not
          an error.

    Returns {"observed_fact": {...}, "error_type": str|None,
             "contributing_signals": {...},
             "classification_rule_version": CLASSIFICATION_RULE_VERSION,
             "caveat": "..."}.
    """
    caveat = (
        "Descriptive classification via a deterministic rule over already-"
        "known data -- NOT a causal explanation and NOT a validated finding."
    )
    observed_fact = {
        "prediction_ts": row["prediction_ts"],
        "target_ts": row["target_ts"],
        "horizon_hours": row["horizon_hours"],
        "model_version": row["model_version"],
        "p_up": row["p_up"],
        "realized_up": row["realized_up"],
        "realized_return": row["realized_return"],
    }

    if row.get("realized_up") is None:
        return {"observed_fact": observed_fact, "status": "UNRESOLVED", "error_type": None,
                "contributing_signals": {}, "classification_rule_version": CLASSIFICATION_RULE_VERSION,
                "caveat": caveat}

    direction_correct = (row["p_up"] > 0.5 and row["realized_up"] == 1) or (
        row["p_up"] <= 0.5 and row["realized_up"] == 0
    )
    observed_fact["direction_correct"] = direction_correct

    prediction_ts = row["prediction_ts"]
    target_ts = row["target_ts"]

    if row.get("v1_observation_ts") is None:
        error_type = None if direction_correct else "INSUFFICIENT_INFORMATION"
        return {"observed_fact": observed_fact, "status": "RESOLVED", "error_type": error_type,
                "contributing_signals": {"reason": "no_v1_context_at_prediction_time"},
                "classification_rule_version": CLASSIFICATION_RULE_VERSION, "caveat": caveat}

    large_moves = _events_overlapping_window(events_by_category.get("LARGE_MOVE", []), prediction_ts, target_ts)
    regime_reversals = _events_overlapping_window(
        events_by_category.get("REGIME_REVERSAL", []), prediction_ts, target_ts
    )
    vol_expansions = _events_overlapping_window(
        events_by_category.get("VOLATILITY_EXPANSION", []), prediction_ts, target_ts
    )
    v1_divergences = _events_overlapping_window(
        events_by_category.get("V1_BTC_DIVERGENCE", []), prediction_ts, target_ts
    )

    composite = row.get("v1_composite")
    technical = row.get("technical_score")
    technical_conflict = (
        composite is not None and technical is not None
        and (composite > 50) != (technical > 50)
    )
    staleness_gap_ms = prediction_ts - row["v1_observation_ts"]
    is_stale = (
        staleness_threshold_ms is not None and staleness_gap_ms > staleness_threshold_ms
    )

    contributing_signals = {
        "large_move_events_in_window": len(large_moves),
        "regime_reversal_events_in_window": len(regime_reversals),
        "volatility_expansion_events_in_window": len(vol_expansions),
        "v1_btc_divergence_events_in_window": len(v1_divergences),
        "technical_sentiment_conflict": technical_conflict,
        "v1_staleness_gap_ms": staleness_gap_ms,
        "v1_staleness_gap_exceeds_proposed_threshold": is_stale,
    }

    if not direction_correct:
        if large_moves:
            error_type = "UNEXPECTED_SHOCK"
        elif regime_reversals:
            error_type = "REGIME_CHANGE"
        elif vol_expansions:
            error_type = "MISSING_EVENT"
        elif v1_divergences:
            error_type = "MISLEADING_SENTIMENT"
            contributing_signals["note"] = (
                "V1_BTC_DIVERGENCE is PR3's own post-outcome-only event "
                "(is_post_event_analysis=1) -- used here strictly as "
                "post-outcome research context, never as information "
                "available to the original prediction."
            )
        elif technical_conflict:
            error_type = "TECHNICAL_SENTIMENT_CONFLICT"
        elif is_stale:
            error_type = "STALE_SENTIMENT"
        else:
            error_type = "WRONG_DIRECTION"
        return {"observed_fact": observed_fact, "status": "RESOLVED", "error_type": error_type,
                "contributing_signals": contributing_signals,
                "classification_rule_version": CLASSIFICATION_RULE_VERSION, "caveat": caveat}

    # Direction correct: check magnitude.
    if row.get("realized_return") is not None and magnitude_threshold_pct is not None:
        if abs(row["realized_return"]) >= magnitude_threshold_pct:
            return {"observed_fact": observed_fact, "status": "RESOLVED",
                    "error_type": "CORRECT_DIRECTION_WRONG_MAGNITUDE",
                    "contributing_signals": contributing_signals,
                    "classification_rule_version": CLASSIFICATION_RULE_VERSION, "caveat": caveat}

    return {"observed_fact": observed_fact, "status": "RESOLVED", "error_type": None,
            "contributing_signals": contributing_signals,
            "classification_rule_version": CLASSIFICATION_RULE_VERSION, "caveat": caveat}


def classify_all(conn, horizon_hours, start_ts, end_ts):
    """Orchestrates fetch + classify for every prediction in the window,
    at one horizon (never pooled across horizons -- see
    test_multi_horizon_isolation). Fetches events and empirical
    thresholds ONCE for the whole window, not once per row.

    Every input row is represented exactly once in the output --
    UNRESOLVED rows included, never dropped.
    """
    _validate_bounds(start_ts, end_ts)
    rows = fetch_resolved_predictions_with_v1_context(conn, horizon_hours, start_ts, end_ts)
    events_by_category = fetch_events_for_window(conn, start_ts, end_ts)
    staleness = empirical_staleness_threshold(rows)
    magnitude = empirical_magnitude_threshold(rows)
    staleness_threshold_ms = staleness.get("proposed_threshold_ms")
    magnitude_threshold_pct = magnitude.get("proposed_threshold_pct")

    classifications = [
        classify_prediction(row, events_by_category, staleness_threshold_ms, magnitude_threshold_pct)
        for row in rows
    ]
    return {
        "horizon_hours": horizon_hours,
        "rows": rows,
        "classifications": classifications,
        "staleness_threshold": staleness,
        "magnitude_threshold": magnitude,
        "events_by_category": events_by_category,
    }


# =====================================================================
# Deterministic breakdowns -- counts and rates only, sample size always
# reported, no significance labels (Section 11).
# =====================================================================

def _breakdown(rows, classifications, key_fn):
    buckets = {}
    for row, c in zip(rows, classifications):
        key = key_fn(row, c)
        bucket = buckets.setdefault(key, {"n": 0, "n_resolved": 0, "error_counts": {t: 0 for t in ERROR_TYPES},
                                           "n_no_error": 0, "n_unresolved": 0})
        bucket["n"] += 1
        if c["status"] == "UNRESOLVED":
            bucket["n_unresolved"] += 1
            continue
        bucket["n_resolved"] += 1
        if c["error_type"] is None:
            bucket["n_no_error"] += 1
        else:
            bucket["error_counts"][c["error_type"]] += 1

    result = {}
    for key, bucket in buckets.items():
        n_resolved = bucket["n_resolved"]
        result[key] = {
            **bucket,
            "error_rate": (sum(bucket["error_counts"].values()) / n_resolved) if n_resolved > 0 else None,
            "sample_size_status": "OK" if n_resolved >= MIN_SAMPLE_FOR_RATE else "SMALL_SAMPLE_CAUTION",
        }
    return result


def breakdown_by_v1_bucket(rows, classifications):
    return _breakdown(rows, classifications, lambda row, c: v1_composite_bucket(row.get("v1_composite")))


def breakdown_by_regime(rows, classifications):
    return _breakdown(rows, classifications, lambda row, c: row.get("gold_regime"))


def breakdown_by_model_version(rows, classifications):
    return _breakdown(rows, classifications, lambda row, c: row.get("model_version"))


def breakdown_by_confidence_band(rows, classifications, cuts):
    return _breakdown(rows, classifications, lambda row, c: confidence_band(row.get("p_up"), cuts))


def breakdown_by_chronological_period(rows, classifications, window_start_ts):
    return _breakdown(
        rows, classifications,
        lambda row, c: chronological_period_bucket(row["prediction_ts"], window_start_ts),
    )


def breakdown_by_error_type_only(rows, classifications):
    """Total counts per error type across the whole window, with n."""
    counts = {t: 0 for t in ERROR_TYPES}
    n_resolved = 0
    n_unresolved = 0
    n_no_error = 0
    for c in classifications:
        if c["status"] == "UNRESOLVED":
            n_unresolved += 1
            continue
        n_resolved += 1
        if c["error_type"] is None:
            n_no_error += 1
        else:
            counts[c["error_type"]] += 1
    return {"n_resolved": n_resolved, "n_unresolved": n_unresolved, "n_no_error": n_no_error,
            "error_counts": counts,
            "sample_size_status": "OK" if n_resolved >= MIN_SAMPLE_FOR_RATE else "SMALL_SAMPLE_CAUTION"}


# =====================================================================
# Persistence/transitions -- descriptive counts only, no significance.
# =====================================================================

def error_transition_report(classifications):
    """Ordered by prediction_ts (classifications must already be in that
    order -- classify_all() guarantees this), counts how often each
    (previous_label, next_label) pair occurs among consecutive RESOLVED
    predictions (UNRESOLVED rows are skipped, not treated as a label).
    label is the error_type, or "NO_ERROR" when None.

    Purely descriptive counts -- no significance claim, no p-value, per
    Section 11.
    """
    resolved = [c for c in classifications if c["status"] == "RESOLVED"]
    labels = [c["error_type"] if c["error_type"] is not None else "NO_ERROR" for c in resolved]
    n_transitions = max(0, len(labels) - 1)
    if n_transitions < MIN_SAMPLE_FOR_TRANSITIONS:
        return {"status": "INSUFFICIENT_DATA", "n_transitions": n_transitions, "transition_counts": {}}

    transition_counts = {}
    for prev_label, next_label in zip(labels, labels[1:]):
        key = (prev_label, next_label)
        transition_counts[key] = transition_counts.get(key, 0) + 1

    return {
        "status": "OK",
        "n_transitions": n_transitions,
        "transition_counts": {f"{k[0]}->{k[1]}": v for k, v in transition_counts.items()},
    }


# =====================================================================
# Candidate observations / hypotheses (evidence-gate capped at
# RESEARCH_HYPOTHESIS -- never higher, per Section on EVIDENCE GATES).
# =====================================================================

# A bucket must have at least this many resolved predictions, AND the
# single most common error type in it must cover at least this share of
# resolved predictions, before it is even reported as a candidate
# OBSERVATION. Recurrence across >=2 independent breakdown dimensions
# is required before an OBSERVATION is upgraded to RESEARCH_HYPOTHESIS.
# These are reporting/aggregation thresholds (which buckets are "worth
# a human's attention"), not statistical significance claims -- no
# p-value or confidence interval is computed or implied here.
CANDIDATE_MIN_N = 30
CANDIDATE_MIN_SHARE = 0.5


def _candidate_from_breakdown(breakdown, dimension_name):
    candidates = []
    for bucket_key, bucket in breakdown.items():
        if bucket_key is None or bucket["n_resolved"] < CANDIDATE_MIN_N:
            continue
        dominant_type, dominant_count = max(bucket["error_counts"].items(), key=lambda kv: kv[1])
        if dominant_count == 0:
            continue
        share = dominant_count / bucket["n_resolved"]
        if share >= CANDIDATE_MIN_SHARE:
            candidates.append({
                "dimension": dimension_name,
                "bucket": bucket_key,
                "dominant_error_type": dominant_type,
                "n_resolved": bucket["n_resolved"],
                "share": share,
            })
    return candidates


def derive_candidate_observations(rows, classifications, window_start_ts, cuts):
    """Scans every required breakdown for a concentrated error pattern
    (Section: 'are errors concentrated in identifiable conditions?').
    Each candidate is tagged evidence_gate_status="OBSERVATION" by
    default; if the SAME dominant_error_type recurs in >=2 independent
    breakdown dimensions, all of its instances are upgraded to
    evidence_gate_status="RESEARCH_HYPOTHESIS" -- never anything higher.

    This function never claims causation and never promotes a finding
    past RESEARCH_HYPOTHESIS -- both are enforced structurally (there is
    no code path here that assigns any other EVIDENCE_GATE_STATUSES
    value), not just by convention.
    """
    all_candidates = []
    all_candidates += _candidate_from_breakdown(breakdown_by_v1_bucket(rows, classifications), "v1_composite_bucket")
    all_candidates += _candidate_from_breakdown(breakdown_by_regime(rows, classifications), "regime")
    all_candidates += _candidate_from_breakdown(breakdown_by_model_version(rows, classifications), "model_version")
    all_candidates += _candidate_from_breakdown(
        breakdown_by_confidence_band(rows, classifications, cuts), "confidence_band"
    )
    all_candidates += _candidate_from_breakdown(
        breakdown_by_chronological_period(rows, classifications, window_start_ts), "chronological_period"
    )

    dimensions_by_error_type = {}
    for c in all_candidates:
        dimensions_by_error_type.setdefault(c["dominant_error_type"], set()).add(c["dimension"])

    for c in all_candidates:
        recurring = len(dimensions_by_error_type[c["dominant_error_type"]]) >= 2
        c["evidence_gate_status"] = "RESEARCH_HYPOTHESIS" if recurring else "OBSERVATION"
        c["caveat"] = (
            "Descriptive association only -- concentration in this bucket "
            "is not a causal claim and has not been validated out-of-sample. "
            "Human review required before any status beyond RESEARCH_HYPOTHESIS."
        )

    return all_candidates


# =====================================================================
# Orchestration
# =====================================================================

def build_error_classification_report(conn, start_ts, end_ts, horizons=SUPPORTED_HORIZONS,
                                       coin=SUPPORTED_COIN):
    """The single entry point. Read-only. Never writes. Keeps every
    horizon's classification entirely separate (Section: multi-horizon
    isolation) -- nothing here pools rows across horizons.
    """
    if coin != SUPPORTED_COIN:
        raise ValueError(f"only {SUPPORTED_COIN} is supported in PR5d; got {coin!r}")
    _validate_bounds(start_ts, end_ts)

    per_horizon = {}
    for horizon_hours in horizons:
        result = classify_all(conn, horizon_hours, start_ts, end_ts)
        rows, classifications = result["rows"], result["classifications"]
        cuts = confidence_band_cuts(rows)

        per_horizon[horizon_hours] = {
            "n_predictions": len(rows),
            "overall": breakdown_by_error_type_only(rows, classifications),
            "by_v1_composite_bucket": breakdown_by_v1_bucket(rows, classifications),
            "by_regime": breakdown_by_regime(rows, classifications),
            "by_model_version": breakdown_by_model_version(rows, classifications),
            "by_confidence_band": breakdown_by_confidence_band(rows, classifications, cuts),
            "by_chronological_period": breakdown_by_chronological_period(rows, classifications, start_ts),
            "confidence_band_cuts": cuts,
            "staleness_threshold": result["staleness_threshold"],
            "magnitude_threshold": result["magnitude_threshold"],
            "transitions": error_transition_report(classifications),
            "candidate_observations": derive_candidate_observations(rows, classifications, start_ts, cuts),
            "v2_failure_cluster_context": v2_failure_cluster_context(
                conn, coin, horizon_hours, start_ts, end_ts
            ),
        }

    return {
        "coin": coin,
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "classification_rule_version": CLASSIFICATION_RULE_VERSION,
        "per_horizon": per_horizon,
        "scope_note": (
            "BTC only (predictions table). LINK/ETH (link_predictions/"
            "eth_predictions) are explicitly deferred, matching PR2/PR5a/"
            "PR5c's own precedent, not silently assumed to share BTC's shape."
        ),
        "causality_note": (
            "Every error_type and candidate_observation in this report is a "
            "DESCRIPTIVE, deterministic classification or a concentration "
            "count -- never a causal explanation, and never validated "
            "out-of-sample by this PR."
        ),
    }


# =====================================================================
# Persistence into the already-deployed research_analyses. THIS IS THE
# ONLY FUNCTION IN THIS MODULE THAT WRITES ANYTHING. Tested only against
# an in-memory sqlite3 fixture; never invoked against production in
# this PR.
# =====================================================================

def persist_findings(conn, analysis_ts, window_start_ts, window_end_ts, sample_size,
                      subject, metric_json_obj, methodology_version=CLASSIFICATION_RULE_VERSION,
                      validation_status="OBSERVATION"):
    """INSERTs one row into research_analyses (schema unchanged from PR1
    -- no migration needed; metric_json's existing free-form TEXT column
    holds methodology_version alongside the rest of the payload, exactly
    as PR5c's persist_analysis() already established).
    """
    import json
    payload = {**metric_json_obj, "methodology_version": methodology_version}
    cursor = conn.execute(
        "INSERT INTO research_analyses "
        "(analysis_ts, window_start_ts, window_end_ts, sample_size, subject, "
        " metric_json, multiple_testing_correction, validation_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            analysis_ts, window_start_ts, window_end_ts, sample_size, subject,
            json.dumps(payload), None, validation_status,
        ),
    )
    return cursor.lastrowid
