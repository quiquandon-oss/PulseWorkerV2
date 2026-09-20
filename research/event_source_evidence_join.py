"""
EXP-009: Event x Source x Real-World Evidence -- research-only join.

=====================================================================
Objective
=====================================================================

EXP-005 asks: "does a V1 source contain measurable information about
subsequent BTC movement?" This module asks a different, complementary
question: "what real-world information was a V1 source representing
around a detected event, and did that representation relate to the
subsequent, control-adjusted BTC reaction?"

It does NOT decide whether a source is useful, does NOT rank sources,
does NOT compute a weight recommendation, and does NOT claim a source
"caused" or "predicted" BTC's move. It never converts a correlation
into a causal claim.

=====================================================================
Why this module exists (the actual new work)
=====================================================================

`controlled_event_reaction.py` (PR57+PR58, BTC reaction) and
`event_source_relevance.py` (PR59, real-world-evidence relevance) were
deliberately built with ZERO import dependency on each other --
event_source_relevance.py's own docstring: "this module does not
import event_source_reaction.py or controlled_event_reaction.py at
all, by design, so that dependency cannot even accidentally leak in."
That separation is correct and is preserved here: this module imports
BOTH of them, but neither of them imports this module or each other.
It only joins their independently-computed, already-finished result
rows by (event_ts, source_key) -- it recomputes nothing either module
already computed, and modifies neither.

=====================================================================
Golden rules enforced (same discipline as PR57/58/59)
=====================================================================

- $0, read-only: pure computation over data already fetched elsewhere
  in this project's established pattern. No network, no LLM, no paid
  service of any kind.
- No V1/V2/Worker file is imported or referenced.
- No schema/migration proposed here -- persistence (a new
  research_analyses row) is the calling orchestration script's job,
  not this module's.
- No write of any kind -- no `persist_*` function exists in this
  module.
- No source ranking, no KEEP/INCREASE/DECREASE, no BUILD_REQUEST.
- The classification below is a purely CATEGORICAL combination of
  labels the two reused modules ALREADY compute -- it introduces no
  new statistical threshold, no new numeric computation, and no new
  significance test. See `classify_event_source_interpretation()`.

=====================================================================
Three objects, kept explicitly separate (never collapsed)
=====================================================================

A. EVENT: a PR3 detected market/event anchor (event_source_relevance's
   own `events`, which carry `is_internal_model_event` -- reused
   verbatim, not recomputed).
B. EVIDENCE: `research_event_evidence` rows already collected around
   the event by PR60's live pipeline -- an RSS article is never itself
   treated as "the event."
C. SOURCE REPRESENTATION: what a V1 sentiment source's own value was
   at the relevant as-of observation time -- never treated as proof
   that the market-derived event detector represents real-world
   causality.

=====================================================================
classify_event_source_interpretation() -- the actual new logic
=====================================================================

Inputs are the three ALREADY-COMPUTED categorical results:
  - relevance_result (PR59): RELEVANT / POSSIBLY_RELEVANT /
    NOT_ESTABLISHED / INSUFFICIENT_EVIDENCE
  - pr1_alignment (PR57's `classify_alignment`, reused via PR58's flat
    table): ALIGNED / NOT_ALIGNED / MIXED / NO_MEASURABLE_REACTION /
    INSUFFICIENT_EVIDENCE
  - control_result (PR58's `classify_control`, the primary-horizon
    residual-vs-continuation-baseline verdict): CONTINUATION_CONSISTENT
    / REACTION_ABOVE_CONTROL / REACTION_BELOW_CONTROL /
    NO_MEASURABLE_REACTION / INSUFFICIENT_EVIDENCE

Decision rules, in priority order:

1. If PR59 could not establish ANY evidence-based read on the event
   (relevance_result == INSUFFICIENT_EVIDENCE), or if BTC's own
   reaction/control state is itself unresolved (pr1_alignment or
   control_result == INSUFFICIENT_EVIDENCE), the join question this
   module asks cannot be answered at all -> INSUFFICIENT_EVIDENCE.
2. Topically relevant (RELEVANT or POSSIBLY_RELEVANT) AND BTC's
   controlled reaction is genuinely measurable
   (REACTION_ABOVE_CONTROL / REACTION_BELOW_CONTROL):
     - pr1_alignment == ALIGNED     -> EVENT_SOURCE_ALIGNED
     - pr1_alignment == NOT_ALIGNED -> EVENT_SOURCE_MISLEADING_POSSIBLE
       (topically connected to real-world evidence, pointed the wrong
       way, and BTC genuinely moved -- "possible", never asserted, per
       the same _POSSIBLE discipline PR59 already uses for
       affinity_status).
     - pr1_alignment == MIXED or NO_MEASURABLE_REACTION -> a genuine
       disagreement between PR57's own raw-return alignment check and
       PR58's controlled view -- reported honestly as
       EVENT_SOURCE_NOT_ALIGNED rather than forced into either of the
       two more specific labels above.
3. Topically relevant AND BTC's reaction is NOT distinguishable from
   noise/continuation (NO_MEASURABLE_REACTION / CONTINUATION_CONSISTENT)
   -> EVENT_SOURCE_INFORMATIONAL_ONLY: the source captured real-world
   information connected to the event, but that information did not
   correspond to a measurable, controlled BTC move.
4. NOT topically relevant (NOT_ESTABLISHED) AND the source's own value
   nonetheless ALIGNED with a genuinely measurable, controlled BTC
   reaction -> EVENT_SOURCE_REDUNDANT_POSSIBLE: the source's numeric
   behaviour tracked the market reaction despite no direct textual
   bridge to the event's real-world evidence -- flagged as a
   *possible* sign the source is capturing information already
   reflected in price action itself (a quantitative/consequence-style
   source, per PR59's own "indirect relevance" framing), never
   asserted as proven redundancy.
5. Everything else (not topically relevant, and either no genuine
   controlled reaction or no alignment) -> INSUFFICIENT_EVIDENCE: this
   deterministic combination does not support any of the more specific
   labels above, and the honest default is to say so rather than force
   a verdict.

Never call this causal attribution. `EVENT_SOURCE_ALIGNED` states only
that a source's own direction (vs. its own historical median) matched
BTC's control-adjusted reaction while topically connected to real-
world evidence -- it never states or implies that the source, or the
real-world evidence, CAUSED that reaction.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import controlled_event_reaction as cer  # noqa: E402 -- UNCHANGED, reused as-is
import event_source_relevance as esrel  # noqa: E402 -- UNCHANGED, reused as-is

EVENT_SOURCE_INTERPRETATIONS = (
    "EVENT_SOURCE_ALIGNED",
    "EVENT_SOURCE_NOT_ALIGNED",
    "EVENT_SOURCE_REDUNDANT_POSSIBLE",
    "EVENT_SOURCE_INFORMATIONAL_ONLY",
    "EVENT_SOURCE_MISLEADING_POSSIBLE",
    "INSUFFICIENT_EVIDENCE",
)

_GENUINE_REACTION = ("REACTION_ABOVE_CONTROL", "REACTION_BELOW_CONTROL")
_NO_REACTION = ("NO_MEASURABLE_REACTION", "CONTINUATION_CONSISTENT")


def classify_event_source_interpretation(relevance_result, pr1_alignment, control_result):
    """Pure, deterministic combination of three already-computed
    categorical labels. See module docstring for the full decision
    table and rationale. Never inspects a numeric value directly --
    every input here is itself already a finished classification from
    PR57/58/59."""
    if relevance_result == "INSUFFICIENT_EVIDENCE":
        return "INSUFFICIENT_EVIDENCE"
    if pr1_alignment == "INSUFFICIENT_EVIDENCE" or control_result == "INSUFFICIENT_EVIDENCE":
        return "INSUFFICIENT_EVIDENCE"

    topically_relevant = relevance_result in ("RELEVANT", "POSSIBLY_RELEVANT")
    genuine_reaction = control_result in _GENUINE_REACTION
    no_reaction = control_result in _NO_REACTION

    if topically_relevant:
        if no_reaction:
            return "EVENT_SOURCE_INFORMATIONAL_ONLY"
        if genuine_reaction:
            if pr1_alignment == "ALIGNED":
                return "EVENT_SOURCE_ALIGNED"
            if pr1_alignment == "NOT_ALIGNED":
                return "EVENT_SOURCE_MISLEADING_POSSIBLE"
            # MIXED or NO_MEASURABLE_REACTION under PR57's own raw-return
            # view, despite PR58's controlled view finding a genuine
            # reaction -- an honest disagreement between the two
            # independently-computed views, not forced into either
            # more specific label.
            return "EVENT_SOURCE_NOT_ALIGNED"
        # control_result exhaustively belongs to _GENUINE_REACTION or
        # _NO_REACTION once INSUFFICIENT_EVIDENCE is excluded above --
        # unreachable in practice, but fail closed rather than fall
        # through silently.
        return "INSUFFICIENT_EVIDENCE"

    # NOT_ESTABLISHED topical relevance.
    if genuine_reaction and pr1_alignment == "ALIGNED":
        return "EVENT_SOURCE_REDUNDANT_POSSIBLE"
    return "INSUFFICIENT_EVIDENCE"


PRIMARY_REPORTING_HORIZONS = ("6h", "12h", "24h")  # Section 7's explicit
# requested horizons -- reported in full per-horizon detail (never
# collapsed to just one) alongside PR58's own single "best available"
# primary horizon used for the interpretation classification.


def _horizon_detail(full_reaction_row, h):
    """Extracts one horizon's reaction+control detail from PR58's own
    per-result row (post_event_btc_reactions / control_by_horizon,
    both keyed by horizon label), in the flat shape this experiment
    persists. Never recomputes anything -- pure extraction."""
    reaction = full_reaction_row["post_event_btc_reactions"][h]
    control = full_reaction_row["control_by_horizon"][h]
    return {
        "status": reaction["status"],
        "quality": reaction.get("quality"),
        "return_pct": reaction.get("return_pct"),
        "residual_pct": control["residual"].get("residual_pct"),
        "control_classification": control["classification"]["result"],
    }


def build_event_source_evidence_dataset(conn, start_ts, end_ts):
    """Read-only. Never writes. `conn` must expose history, btc_data,
    predictions, research_events, and research_event_evidence -- the
    same shape `event_source_relevance.build_event_source_relevance_
    dataset()` and `controlled_event_reaction.build_controlled_
    reaction_dataset()` already each independently require.

    Joins by (event_ts, source_key) rather than by any session-local
    event_id: both reused modules re-detect events with the identical
    PR3 detector functions and the identical fingerprint-dedup logic
    over the same (conn, start_ts, end_ts), so their event_ts sets are
    guaranteed identical for a single run of this function -- but the
    join is written defensively (falling back to INSUFFICIENT_EVIDENCE
    for the reaction side) rather than assuming this can never fail.

    Reports BTC reaction at 6h/12h/24h in full (PRIMARY_REPORTING_
    HORIZONS), in addition to PR58's own single best-available primary
    horizon (used only for the deterministic interpretation label) --
    the event-level control methodology (PR58) and the per-source
    interpretation (this module) are kept as separate fields on the
    same row, never collapsed into each other.
    """
    reaction_dataset = cer.build_controlled_reaction_dataset(conn, start_ts, end_ts)
    relevance_dataset = esrel.build_event_source_relevance_dataset(conn, start_ts, end_ts)

    reaction_flat_by_key = {
        (row["event_ts"], row["source_key"]): row
        for row in cer.build_event_level_table(reaction_dataset)
    }
    reaction_full_by_key = {
        (row["event_ts"], row["source_key"]): row
        for row in reaction_dataset["results"]
    }
    events_by_ts = {e["event_ts"]: e for e in relevance_dataset["events"]}

    joined = []
    for rel_row in relevance_dataset["results"]:
        key = (rel_row["event_ts"], rel_row["source_key"])
        reaction_row = reaction_flat_by_key.get(key)
        reaction_full = reaction_full_by_key.get(key)
        event = events_by_ts[rel_row["event_ts"]]

        relevance_result = rel_row["relevance"]["result"]
        pr1_alignment = reaction_row["pr1_alignment"] if reaction_row else "INSUFFICIENT_EVIDENCE"
        control_result = reaction_row["control_classification"] if reaction_row else "INSUFFICIENT_EVIDENCE"

        if event["is_internal_model_event"]:
            # V2_FAILURE_CLUSTER describes V2's own prediction behaviour,
            # not a market or information event -- never a candidate for
            # real-world-evidence interpretation (per PR59's own rule).
            interpretation = "INSUFFICIENT_EVIDENCE"
        else:
            interpretation = classify_event_source_interpretation(
                relevance_result, pr1_alignment, control_result
            )

        horizons = (
            {h: _horizon_detail(reaction_full, h) for h in PRIMARY_REPORTING_HORIZONS}
            if reaction_full else
            {h: {"status": "NO_DATA", "quality": None, "return_pct": None,
                 "residual_pct": None, "control_classification": "INSUFFICIENT_EVIDENCE"}
             for h in PRIMARY_REPORTING_HORIZONS}
        )

        joined.append({
            "event_id": rel_row["event_id"],
            "event_ts": rel_row["event_ts"],
            "event_category": rel_row["event_category"],
            "is_internal_model_event": event["is_internal_model_event"],
            "source_key": rel_row["source_key"],
            "source_value": rel_row["source_value"],
            "source_observation_ts": rel_row["source_observation_ts"],
            "n_evidence_rows": rel_row["n_evidence_rows"],
            "relevance_result": relevance_result,
            "relevance_affinity_status": rel_row["relevance"]["affinity_status"],
            "relevance_temporal_statuses": rel_row["relevance"]["temporal_statuses"],
            "pr1_alignment": pr1_alignment,
            "control_classification": control_result,
            "primary_horizon": reaction_row["requested_horizon"] if reaction_row else None,
            "pre_event_return": reaction_row["pre_event_return"] if reaction_row else None,
            "post_event_return": reaction_row["post_event_return"] if reaction_row else None,
            "residual_pct": reaction_row["residual_pct"] if reaction_row else None,
            "horizons": horizons,
            "event_source_interpretation": interpretation,
        })

    return {
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "events": relevance_dataset["events"],
        "source_keys": relevance_dataset["source_keys"],
        "results": joined,
    }


# =====================================================================
# Descriptive summaries only -- no ranking, no score, no recommendation
# =====================================================================

def summarize_evidence_coverage(dataset):
    """Per-event evidence coverage. n_evidence_rows is identical across
    every source row for a given event (evidence is event-level, never
    source-level -- see module docstring's conceptual separation), so
    this reduces to one entry per real (non-internal-model) event."""
    per_event = {}
    for r in dataset["results"]:
        per_event.setdefault(r["event_id"], {
            "event_ts": r["event_ts"],
            "event_category": r["event_category"],
            "is_internal_model_event": r["is_internal_model_event"],
            "n_evidence_rows": r["n_evidence_rows"],
        })
    real_world_events = [e for e in per_event.values() if not e["is_internal_model_event"]]
    n_events = len(real_world_events)
    n_with_evidence = sum(1 for e in real_world_events if e["n_evidence_rows"] > 0)
    return {
        "n_events": len(dataset["events"]),
        "n_real_world_events": n_events,
        "n_real_world_events_with_any_evidence": n_with_evidence,
        "n_real_world_events_insufficient_evidence": n_events - n_with_evidence,
    }


def summarize_by_source_interpretation(dataset):
    """Descriptive counts per source across the deterministic
    interpretation labels. No ranking, no score, no recommendation --
    same discipline PR57/58/59's own summary functions already use."""
    from collections import defaultdict
    per_source = defaultdict(lambda: defaultdict(int))
    for r in dataset["results"]:
        per_source[r["source_key"]][r["event_source_interpretation"]] += 1
    combined = {}
    for source_key in dataset["source_keys"]:
        counts = per_source.get(source_key, {})
        combined[source_key] = {label: counts.get(label, 0) for label in EVENT_SOURCE_INTERPRETATIONS}
    return combined


def summarize_btc_outcome_coverage(dataset, horizons=PRIMARY_REPORTING_HORIZONS):
    """Descriptive only: among real-world (non-internal-model) events,
    how many resolved a usable, quality-flagged post-event reaction at
    each of the three primary horizons this experiment reports. Counted
    once per EVENT (BTC reaction does not depend on which source is
    being looked at), reusing the per-horizon detail already attached
    to each joined row -- never recomputed."""
    per_event_horizons = {}
    for r in dataset["results"]:
        if r["is_internal_model_event"]:
            continue
        per_event_horizons.setdefault(r["event_id"], r["horizons"])
    n_events = len(per_event_horizons)
    coverage = {}
    for h in horizons:
        n_resolved = sum(
            1 for hz in per_event_horizons.values()
            if hz[h]["status"] == "OK" and hz[h]["quality"] in ("GOOD", "APPROXIMATE")
        )
        coverage[h] = {"n_events": n_events, "n_resolved": n_resolved}
    return coverage
