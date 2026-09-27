"""
Stage 7 -- Steps C.3 and I: full-source event sentiment recalculation.

=====================================================================
Read this before changing anything below (disclosed methodology gap)
=====================================================================

The task this module implements asks Stage 7 to "recalculate event
sentiment using the existing V1 methodology" over "the complete
eligible source set." Verified directly against the real code before
writing a line of this module:

- V1's ONLY sentiment methodology (CryptoPulse/index.html's
  `computeComposite()` + `COMPOSITE_SOURCES_DEFAULTS`) is a weighted
  average over a FIXED set of 21 macro/market indicators (Fear & Greed,
  funding rate, on-chain metrics, etc.). It has no defined input for a
  specific news article, and no notion of "this event's evidence set"
  at all -- it produces one continuous market-wide score, not a
  per-event one.
- Stage 6's own modules (`event_source_relevance.py`,
  `event_source_evidence_join.py`) are explicit, repeatedly, that they
  compute RELEVANCE and ALIGNMENT classifications only -- "does NOT
  decide whether a source is useful", "No LLM", "no source ranking,
  no weight recommendation". Neither module, nor
  `research_event_evidence` itself, carries a positive/negative
  polarity value for any evidence row (`keyword_score` is a stubbed-out
  no-op today -- see `evidence_collector.score_headline_optional()`).

There is therefore NO existing numeric "event sentiment" concept
anywhere in V1 or Stage 6 to literally "reuse." Recomputing one from
evidence counts or keyword text here would be inventing a new scoring
formula this codebase's own prior review has twice explicitly declined
to add ("don't make PR4's evidence layer a V1 scoring layer").

What this module does instead, keeping faith with "reuse existing
methodology, never invent a new one silently":
1. Reads V1's OWN real, already-computed composite (`history.score` /
   `sources_json`) nearest the event -- verbatim, never recomputed --
   as read-only market context (`fetch_v1_macro_context`).
2. Reuses `classify_event_source_interpretation()` verbatim (imported,
   not reimplemented) for the per-source evidence-based assessment.
3. Sets a numeric `sentiment_score`/`sentiment_label` ONLY from a
   VALIDATED human-reviewed AI research response's own explicit
   POSITIVE/NEGATIVE/MIXED/INDETERMINATE judgment (Step G.6/H) -- on
   Stage 7's OWN disclosed 0/50/100 scale, explicitly NOT V1's
   composite formula. Absent such a response, `sentiment_label` and
   `sentiment_score` are both None -- an explicit "no defensible
   assessment yet" state, never a fabricated neutral/default value,
   even when evidence_sufficiency is SUFFICIENT.
"""

import hashlib
import json

FORMULA_VERSION = "stage7-v1"

# Stage 7's OWN scale, used ONLY to represent a validated AI research
# response's own directional judgment numerically. Never applied to
# evidence/relevance data directly.
SENTIMENT_SCALE = {"POSITIVE": 100.0, "NEGATIVE": 0.0, "MIXED": 50.0, "INDETERMINATE": None}


def fetch_v1_macro_context(conn, event_ts):
    """Reads V1's own already-computed composite nearest (at or before)
    event_ts -- the exact query pattern event_source_relevance.
    snapshot_v1_source() already uses, reused here rather than
    reimplemented, extended to return the full row (not just one
    source's value) since Stage 7's macro context is the WHOLE
    composite + its source snapshot, not one source in isolation."""
    row = conn.execute(
        "SELECT ts, score, technical_score, sources_json FROM history WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
        (event_ts,),
    ).fetchone()
    if row is None:
        return {"available": False, "reason": "no history row at or before event_ts"}
    ts, score, technical_score, sources_json = row
    try:
        sources = json.loads(sources_json) if sources_json else {}
    except (TypeError, ValueError):
        sources = {}
    return {
        "available": True,
        "observation_ts": ts,
        "composite_score": score,
        "technical_score": technical_score,
        "sources_snapshot": sources,
    }


def dedupe_evidence(evidence_rows):
    """Groups evidence rows by content_hash. research_event_evidence
    already enforces UNIQUE(event_id, content_hash) at the schema level,
    so real duplicates within one event's row set should not exist --
    this is a defensive, tested guard against a future regression (a
    backfill bug, a schema change), not a response to an observed
    problem. Returns (deduped_rows keeping the first-seen row per
    content_hash, duplicate_groups: {content_hash: {"kept_evidence_id",
    "dropped_evidence_ids": [...]}})."""
    seen = {}
    duplicate_groups = {}
    deduped = []
    for row in evidence_rows:
        content_hash = row.get("content_hash")
        if content_hash is None:
            deduped.append(row)
            continue
        if content_hash in seen:
            duplicate_groups.setdefault(
                content_hash, {"kept_evidence_id": seen[content_hash], "dropped_evidence_ids": []}
            )["dropped_evidence_ids"].append(row["evidence_id"])
            continue
        seen[content_hash] = row["evidence_id"]
        deduped.append(row)
    return deduped, duplicate_groups


def compute_input_fingerprint(event_id, contributing_evidence_ids, evidence_sufficiency, response_id):
    payload = json.dumps(
        {"event_id": event_id, "evidence_ids": sorted(contributing_evidence_ids),
         "sufficiency": evidence_sufficiency, "response_id": response_id},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_event_sentiment(event, evidence_rows, per_source_interpretation, evidence_sufficiency,
                             v1_macro_context, validated_response=None, previous_sentiment_id=None):
    """
    event: dict with event_id (required).
    evidence_rows: research_event_evidence rows for this event (Stage 6's
        own, unchanged) -- the FULL set, not pre-filtered by the caller.
    per_source_interpretation: dict source_key -> classify_event_source_
        interpretation() result (event_source_evidence_join.py, reused
        verbatim, computed by the caller -- this function never
        re-derives it).
    evidence_sufficiency: one of stage7_evidence_sufficiency.
        SUFFICIENCY_STATUSES.
    v1_macro_context: fetch_v1_macro_context()'s own return value.
    validated_response: None, or a dict with at least
        {"response_id", "validation_status", "findings": {"sentiment_assessment": ...}}
        -- a numeric sentiment is only ever set when validation_status
        == "VALIDATED".

    Returns a dict matching stage7_event_sentiment's own columns.
    Never calculates using only newly-discovered (Stage 7) evidence --
    contributing_evidence_ids always includes every deduped Stage-6-and-
    Stage-7 row passed in, per Step I's explicit instruction.
    """
    deduped_rows, duplicate_groups = dedupe_evidence(evidence_rows)
    contributing_evidence_ids = [r["evidence_id"] for r in deduped_rows]
    excluded_evidence = [
        {"evidence_id": eid, "reason": f"duplicate content_hash of evidence_id {group['kept_evidence_id']}"}
        for group in duplicate_groups.values()
        for eid in group["dropped_evidence_ids"]
    ]

    sentiment_label = None
    sentiment_score = None
    response_id = None
    if validated_response is not None and validated_response.get("validation_status") == "VALIDATED":
        assessment = validated_response.get("findings", {}).get("sentiment_assessment")
        if assessment in SENTIMENT_SCALE:
            sentiment_label = assessment
            sentiment_score = SENTIMENT_SCALE[assessment]
            response_id = validated_response.get("response_id")

    fingerprint = compute_input_fingerprint(
        event["event_id"], contributing_evidence_ids, evidence_sufficiency, response_id
    )

    return {
        "event_id": event["event_id"],
        "formula_version": FORMULA_VERSION,
        "evidence_sufficiency": evidence_sufficiency,
        "sentiment_label": sentiment_label,
        "sentiment_score": sentiment_score,
        "v1_macro_context": v1_macro_context,
        "evidence_interpretation": per_source_interpretation,
        "contributing_evidence_ids": contributing_evidence_ids,
        "excluded_evidence": excluded_evidence,
        "duplicate_handling": duplicate_groups,
        "ai_research_response_id": response_id,
        "previous_sentiment_id": previous_sentiment_id,
        "input_fingerprint": fingerprint,
    }


def is_idempotent_repeat(new_result, previous_result):
    """True when repeated processing found the exact same inputs as the
    most recent prior calculation for this event -- the caller must
    then skip inserting a new stage7_event_sentiment row (Step I's own
    idempotency requirement)."""
    if previous_result is None:
        return False
    return new_result["input_fingerprint"] == previous_result.get("input_fingerprint")
