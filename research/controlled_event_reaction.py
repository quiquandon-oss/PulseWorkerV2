"""
PR-2: Controlled Event x Source x BTC Reaction analysis.

=====================================================================
Objective
=====================================================================

PR-1 established that a source's own direction (relative to its own
historical median) can be compared against BTC's post-event reaction,
producing a descriptive ALIGNED/NOT_ALIGNED/MIXED/NO_MEASURABLE_
REACTION/INSUFFICIENT_EVIDENCE verdict per event x source. That
verdict says nothing about whether the observed post-event BTC move
was itself distinguishable from BTC simply continuing whatever it was
already doing before the event.

This module answers exactly that, and ONLY that:

    "Does the apparent post-event reaction survive when we control for
    BTC's pre-event momentum?"

It does NOT decide whether a source is useful, does NOT rank sources,
does NOT compute incremental information beyond OTHER V1 sources (a
different, harder question this module does not attempt), and does
NOT produce a weight recommendation of any kind.

=====================================================================
Golden rules enforced
=====================================================================

- $0, read-only: pure computation over PR-1's already-fetched data and
  the same production tables PR-1 already reads. No network, no LLM,
  no paid service of any kind.
- No schema, no migration, no new table, no production write.
- No V1/V2/Worker file is imported or referenced.
- No source ranking, no KEEP/INCREASE/DECREASE, no BUILD_REQUEST.
- Never converts temporal alignment into "source effectiveness" -- see
  "Five distinct concepts" below.
- Never attributes PR4 evidence to a source (PR4 is not touched here
  at all).
- The whole-window source median PR-1 computes is reused UNCHANGED and
  is still, explicitly, never information available at any single
  event_ts (same disclosure as PR-1).

=====================================================================
Five distinct concepts -- explicitly which ones this PR does and does
NOT establish (Section 7's required distinction)
=====================================================================

1. TEMPORAL SEQUENCE (PR-1, reused unchanged): a source observation
   exists at/before event_ts; a BTC price point exists after it.
2. EVENT RELEVANCE: whether a given PR3 event was genuinely "about"
   a given source at all. NOT established by this PR (or PR-1). Every
   event is compared against every available source regardless of
   topical relevance -- this is disclosed as a limitation, not solved.
3. DIRECTIONAL ALIGNMENT (PR-1's `alignment` field, reused verbatim,
   never recomputed here): does the source's own direction agree with
   BTC's raw post-event reaction.
4. CONTROLLED / RESIDUAL REACTION (NEW in this PR): does BTC's post-
   event reaction, once a simple, deterministic continuation-of-prior-
   momentum baseline is subtracted, still show a measurable move. This
   is a property of the EVENT'S BTC reaction, not of any one source --
   it never depends on which source is being looked at.
5. INCREMENTAL INFORMATION / EVENTUAL WEIGHT EFFECTIVENESS: whether a
   source adds information beyond what OTHER V1 sources already
   represent, and whether its current V1 weight is justified. NOT
   addressed by this PR at all -- that requires comparing sources
   against each other and against V1's actual composite formula,
   neither of which this PR does.

Concept 4 is genuinely new; concepts 1-3 are PR-1's, imported and
reused unmodified; concept 5 remains entirely future work.

=====================================================================
Why the continuation baseline is a fixed scaling rule, not a model
=====================================================================

For a given horizon H, the "what would we expect if BTC just kept
doing what it was already doing" baseline is computed by taking the
ALREADY-COMPUTED pre-event trailing return (PR-1's own
`compute_pre_event_return()`, over its own fixed 24h lookback, using
only prices at or before event_ts) and linearly scaling it to H's
duration:

    baseline_return_pct(H) = pre_event_return_pct * (H_ms / lookback_ms)

This is deterministic and temporally safe (every input is at or before
event_ts) and involves no fitted parameter, no regression, no lookback
window selection beyond the one PR-1 already uses -- it is explicitly
NOT a predictive model, per the build authorization's explicit
instruction. It answers only "is the observed post-event move bigger
or smaller than naive extrapolation of the pre-event trend," nothing
more.

`residual_reaction_pct(H) = post_event_return_pct(H) - baseline_return_pct(H)`

=====================================================================
Why the noise floors are empirical, computed fresh, never hardcoded
=====================================================================

Two separate empirical P25-of-absolute-value floors are computed, per
horizon, over the CURRENT real dataset (never hardcoded from a
previous run):
  - the RAW reaction noise floor: PR-1's own `noise_floor_by_horizon`,
    reused unchanged, decides whether the raw post-event move is
    measurable at all before a control comparison is even attempted.
  - the RESIDUAL noise floor: the same P25-of-|value| convention,
    applied fresh to the distribution of residuals actually observed
    in this run, decides whether a residual is distinguishable from
    typical noise in the residual series itself (a different scale
    than raw returns, so it needs its own floor, computed the same way
    -- not the same numeral reused blindly).

=====================================================================
Control classification (descriptive only -- never causal language)
=====================================================================

  INSUFFICIENT_EVIDENCE: the raw reaction did not resolve at usable
    quality, or no pre-event return exists to build a baseline from.
  NO_MEASURABLE_REACTION: the raw post-event reaction itself is below
    PR-1's own empirical noise floor (checked BEFORE any control
    comparison is attempted -- a reaction too small to interpret at
    all cannot meaningfully be "above" or "below" a baseline either).
  CONTINUATION_CONSISTENT: the raw reaction is measurable, but the
    residual (reaction minus continuation baseline) is within the
    residual noise floor -- the observed move is not distinguishable
    from simple continuation of the pre-event trend.
  REACTION_ABOVE_CONTROL / REACTION_BELOW_CONTROL: the residual
    clears the residual noise floor, positive or negative respectively
    -- purely a magnitude/sign statement about the residual, never a
    claim that the event or a source CAUSED or PREDICTED anything.

=====================================================================
Primary reporting horizon (for the flat event-level table only)
=====================================================================

PR-1 empirically found 6h/12h/24h to be the only genuinely resolvable
horizons at current cadence (recalculated fresh by this module too --
never hardcoded from PR-1's own prior run). For the flat per-event x
per-source table, one horizon must be chosen per row; this module
prefers the LONGEST defensible horizon that actually resolved at
usable quality for that specific event (`PRIMARY_HORIZON_PRIORITY =
("24h", "12h", "6h")`) -- more information, and consistent with PR-1's
own worked example, which also reported 24h alongside 6h. The full,
un-collapsed per-horizon detail (all 8 candidate horizons, including
the ones PR-1 found poorly supported) remains available in
`build_controlled_reaction_dataset()`'s own return value for anyone
who wants it -- the flat table is a reporting convenience, not the
only data this module computes.
"""

import statistics
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
import event_source_reaction as esr  # noqa: E402

CONTINUATION_BASELINE_LOOKBACK_MS = esr.PRE_EVENT_LOOKBACK_MS  # reused, not reinvented
CONTROL_NOISE_PERCENTILE = esr.NOISE_FLOOR_PERCENTILE          # same P25 convention as PR-1
PRIMARY_HORIZON_PRIORITY = ("24h", "12h", "6h")                # longest-first, defensible horizons only
CONTROL_CLASSIFICATIONS = (
    "CONTINUATION_CONSISTENT", "REACTION_ABOVE_CONTROL", "REACTION_BELOW_CONTROL",
    "NO_MEASURABLE_REACTION", "INSUFFICIENT_EVIDENCE",
)


# =====================================================================
# Step 1: continuation baseline + residual (event-level, source-independent)
# =====================================================================

def compute_continuation_baseline(pre_event_result, horizon_ms, lookback_ms=CONTINUATION_BASELINE_LOOKBACK_MS):
    """Deterministic, temporally-safe scaling rule -- see module
    docstring. Never a fitted model; every input is at or before
    event_ts (inherited from PR-1's own `compute_pre_event_return()`)."""
    if pre_event_result["status"] != "OK":
        return {"status": "INSUFFICIENT_EVIDENCE", "baseline_return_pct": None}
    rate_per_ms = pre_event_result["return_pct"] / lookback_ms
    return {"status": "OK", "baseline_return_pct": rate_per_ms * horizon_ms}


def compute_residual_reaction(post_event_reaction, baseline):
    if post_event_reaction["status"] != "OK" or baseline["status"] != "OK":
        return {"status": "INSUFFICIENT_EVIDENCE", "residual_pct": None}
    return {"status": "OK", "residual_pct": post_event_reaction["return_pct"] - baseline["baseline_return_pct"]}


def classify_control(post_event_reaction, residual, reaction_noise_floor, residual_noise_floor):
    """Descriptive-only classification. See module docstring's
    'Control classification' section for the exact rule and why no
    causal language is ever used."""
    if post_event_reaction["status"] != "OK" or post_event_reaction["quality"] not in ("GOOD", "APPROXIMATE"):
        return {"result": "INSUFFICIENT_EVIDENCE",
                "reason": "post-event reaction not resolved at usable quality"}
    if reaction_noise_floor is not None and abs(post_event_reaction["return_pct"]) < reaction_noise_floor:
        return {"result": "NO_MEASURABLE_REACTION",
                "reason": "raw post-event reaction below the empirical noise floor"}
    if residual["status"] != "OK":
        return {"result": "INSUFFICIENT_EVIDENCE",
                "reason": "continuation baseline unavailable -- no pre-event return"}
    r = residual["residual_pct"]
    if residual_noise_floor is not None and abs(r) < residual_noise_floor:
        return {"result": "CONTINUATION_CONSISTENT", "residual_pct": r}
    return {"result": "REACTION_ABOVE_CONTROL" if r > 0 else "REACTION_BELOW_CONTROL", "residual_pct": r}


def select_primary_horizon(post_event_btc_reactions, priority=PRIMARY_HORIZON_PRIORITY):
    for h in priority:
        r = post_event_btc_reactions[h]
        if r["status"] == "OK" and r["quality"] in ("GOOD", "APPROXIMATE"):
            return h
    return None


# =====================================================================
# Top-level orchestration -- reuses PR-1's dataset builder unmodified
# =====================================================================

def build_controlled_reaction_dataset(conn, start_ts, end_ts):
    """Read-only. Never writes. Reuses
    `event_source_reaction.build_event_source_reaction_dataset()`
    UNCHANGED for events, source snapshots, raw reactions, and PR-1's
    own alignment verdict -- this module only adds the control layer
    on top."""
    base = esr.build_event_source_reaction_dataset(conn, start_ts, end_ts)
    reaction_noise_floor = base["noise_floor_by_horizon"]

    per_event_pre_event = {}
    per_event_reactions = {}
    for r in base["results"]:
        per_event_pre_event.setdefault(r["event_id"], r["pre_event_btc_return"])
        per_event_reactions.setdefault(r["event_id"], r["post_event_btc_reactions"])

    residuals_by_horizon = defaultdict(list)
    control_by_event_horizon = {}
    for event_id, pre_event in per_event_pre_event.items():
        reactions = per_event_reactions[event_id]
        for h, ms in esr.CANDIDATE_HORIZONS_MS.items():
            baseline = compute_continuation_baseline(pre_event, ms)
            residual = compute_residual_reaction(reactions[h], baseline)
            control_by_event_horizon[(event_id, h)] = {"baseline": baseline, "residual": residual}
            if residual["status"] == "OK":
                residuals_by_horizon[h].append(abs(residual["residual_pct"]))

    residual_noise_floor = {}
    for h, vals in residuals_by_horizon.items():
        if vals:
            vals_sorted = sorted(vals)
            idx = int(len(vals_sorted) * CONTROL_NOISE_PERCENTILE)
            residual_noise_floor[h] = vals_sorted[min(idx, len(vals_sorted) - 1)]

    final_results = []
    for r in base["results"]:
        event_id = r["event_id"]
        reactions = r["post_event_btc_reactions"]
        row_controls = {}
        for h in esr.CANDIDATE_HORIZONS_MS:
            cbh = control_by_event_horizon[(event_id, h)]
            classification = classify_control(
                reactions[h], cbh["residual"], reaction_noise_floor.get(h), residual_noise_floor.get(h),
            )
            row_controls[h] = {"baseline": cbh["baseline"], "residual": cbh["residual"], "classification": classification}
        final_results.append({**r, "control_by_horizon": row_controls})

    return {
        "window": base["window"],
        "events": base["events"],
        "source_keys": base["source_keys"],
        "source_medians": base["source_medians"],
        "reaction_noise_floor_by_horizon": reaction_noise_floor,
        "residual_noise_floor_by_horizon": residual_noise_floor,
        "results": final_results,
    }


# =====================================================================
# A. Event-level table (flat, one primary horizon per row)
# =====================================================================

def build_event_level_table(dataset):
    rows = []
    for r in dataset["results"]:
        reactions = r["post_event_btc_reactions"]
        primary_h = select_primary_horizon(reactions)
        pre_event_return = r["pre_event_btc_return"].get("return_pct")
        if primary_h is None:
            rows.append({
                "event_id": r["event_id"], "event_ts": r["event_ts"], "event_category": r["event_category"],
                "source_key": r["source_key"], "source_value": r["source_value"],
                "source_observation_ts": r["source_observation_ts"],
                "pre_event_return": pre_event_return,
                "post_event_return": None, "requested_horizon": None, "actual_elapsed_ms": None,
                "resolution_quality": None, "pr1_alignment": r["alignment"]["result"],
                "control_baseline_pct": None, "residual_pct": None,
                "control_classification": "INSUFFICIENT_EVIDENCE",
            })
            continue
        reaction = reactions[primary_h]
        control = r["control_by_horizon"][primary_h]
        rows.append({
            "event_id": r["event_id"], "event_ts": r["event_ts"], "event_category": r["event_category"],
            "source_key": r["source_key"], "source_value": r["source_value"],
            "source_observation_ts": r["source_observation_ts"],
            "pre_event_return": pre_event_return,
            "post_event_return": reaction["return_pct"],
            "requested_horizon": primary_h, "actual_elapsed_ms": reaction["elapsed_ms"],
            "resolution_quality": reaction["quality"], "pr1_alignment": r["alignment"]["result"],
            "control_baseline_pct": control["baseline"].get("baseline_return_pct"),
            "residual_pct": control["residual"].get("residual_pct"),
            "control_classification": control["classification"]["result"],
        })
    return rows


# =====================================================================
# B. Source-level summary -- descriptive counts ONLY, no ranking
# =====================================================================

def summarize_by_source_controlled(dataset):
    """Combines PR-1's own alignment counts (reused verbatim, never
    recomputed) with this PR's new control-classification counts, per
    source. Deliberately produces no score, no rank, no 'best source'
    -- every source's row is independent of every other source's row.
    """
    event_table = build_event_level_table(dataset)
    pr1_summary = esr.summarize_by_source(dataset)
    n_events = len(dataset["events"])

    control_counts = defaultdict(lambda: defaultdict(int))
    for row in event_table:
        control_counts[row["source_key"]][row["control_classification"]] += 1

    combined = {}
    for source_key in dataset["source_keys"]:
        pr1 = pr1_summary.get(source_key, {})
        controls = control_counts.get(source_key, {})
        n_available = pr1.get("source_observations_available", 0)
        combined[source_key] = {
            "events_in_universe": n_events,
            "source_observations_available": n_available,
            "coverage_pct": (n_available / n_events * 100.0) if n_events else 0.0,
            "aligned": pr1.get("ALIGNED", 0),
            "not_aligned": pr1.get("NOT_ALIGNED", 0),
            "mixed": pr1.get("MIXED", 0),
            "no_measurable_reaction": pr1.get("NO_MEASURABLE_REACTION", 0),
            "insufficient_evidence": pr1.get("INSUFFICIENT_EVIDENCE", 0),
            "controlled_comparisons_available": n_events - controls.get("INSUFFICIENT_EVIDENCE", 0),
            "continuation_consistent": controls.get("CONTINUATION_CONSISTENT", 0),
            "above_control": controls.get("REACTION_ABOVE_CONTROL", 0),
            "below_control": controls.get("REACTION_BELOW_CONTROL", 0),
            "no_measurable_reaction_control": controls.get("NO_MEASURABLE_REACTION", 0),
        }
    return combined


# =====================================================================
# C. Horizon summary -- 6h/12h/24h, descriptive only
# =====================================================================

def summarize_by_horizon(dataset, horizons=("6h", "12h", "24h")):
    """Purely descriptive, computed PER EVENT (these BTC-only
    quantities do not depend on which source is being looked at)."""
    events = dataset["events"]
    per_event_pre = {}
    per_event_reactions = {}
    per_event_control = {}
    for r in dataset["results"]:
        per_event_pre.setdefault(r["event_id"], r["pre_event_btc_return"])
        per_event_reactions.setdefault(r["event_id"], r["post_event_btc_reactions"])
        per_event_control.setdefault(r["event_id"], r["control_by_horizon"])

    summary = {}
    for h in horizons:
        n_usable_reaction = 0
        n_controlled = 0
        pre_vals, post_vals, residual_vals = [], [], []
        classification_counts = defaultdict(int)
        for event in events:
            eid = event["event_id"]
            reaction = per_event_reactions[eid][h]
            if reaction["status"] == "OK" and reaction["quality"] in ("GOOD", "APPROXIMATE"):
                n_usable_reaction += 1
                post_vals.append(reaction["return_pct"])
            pre = per_event_pre[eid]
            if pre["status"] == "OK":
                pre_vals.append(pre["return_pct"])
            control = per_event_control[eid][h]
            if control["residual"]["status"] == "OK":
                n_controlled += 1
                residual_vals.append(control["residual"]["residual_pct"])
            classification_counts[control["classification"]["result"]] += 1
        summary[h] = {
            "n_events": len(events),
            "n_usable_reaction": n_usable_reaction,
            "n_controlled_comparison": n_controlled,
            "median_pre_event_return_pct": statistics.median(pre_vals) if pre_vals else None,
            "median_post_event_return_pct": statistics.median(post_vals) if post_vals else None,
            "median_residual_pct": statistics.median(residual_vals) if residual_vals else None,
            "control_classification_distribution": dict(classification_counts),
        }
    return summary
