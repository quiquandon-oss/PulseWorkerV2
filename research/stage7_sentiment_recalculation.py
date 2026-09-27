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
   as read-only market context (`fetch_v1_macro_context`). This is
   CONTEXT ONLY: V1's 21 macro sources are never merged into
   contributing_evidence_ids or any dedup/contribution decision below,
   because none of them carries a per-event value -- describing them as
   part of the "contributing set" would be false. Anywhere this module
   or its callers describe the "full source set," that means Stage 6's
   collected news evidence plus Stage 7's own validated response
   sources (see point 4) -- V1's composite is reported separately,
   alongside, never folded in.
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
4. (Adversarial-review remediation) A VALIDATED response's own
   self-reported `sources` (stage7_research_responses.sources_json) are
   normalized by `normalize_response_sources()` -- explicit rules reject
   a source with no verifiable url, an unparseable/missing publication
   date, or a publication date after the event's own historical
   cutoff -- and the survivors are merged into the SAME
   dedupe_evidence()/contributing_evidence_ids pass as Stage 6's own
   evidence rows, never a second, parallel, uncounted pile. This keeps
   the sentiment ASSESSMENT (point 3, a label) and the EVIDENCE that
   assessment was based on (this point, rows) as two distinct concerns:
   an unvalidated response contributes neither; a validated one
   contributes both, but through separate code paths, so a bug in one
   can never silently corrupt the other.
"""

import hashlib
import json
from datetime import datetime, timezone

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


def _parse_source_publication_ts(raw):
    """Best-effort, FAIL-CLOSED parse of a Stage 7 source's self-reported
    publication date/time into ms-epoch. Accepts an already-numeric
    ms-epoch value, or an ISO-8601 date/datetime string -- a naive
    (timezone-less) string is treated as UTC explicitly, never the
    server's local timezone, so this is deterministic regardless of
    where it runs. Returns None on anything else (missing, empty,
    unparseable, wrong type); callers must treat None as "cannot verify
    this predates the historical cutoff" and exclude the source --
    never assume an unverifiable date is safe."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return None


def normalize_response_sources(response_id, sources, historical_cutoff_ts):
    """Stage 7 Step I adapter: turns a VALIDATED research response's own
    self-reported `sources` (stage7_research_responses.sources_json --
    free-form {url, publisher, publication_date, claim} dicts a human
    typed in, never itself a research_event_evidence row) into the same
    shape dedupe_evidence()/compute_event_sentiment() already operate
    on, so Stage 7's genuinely new evidence is merged into the SAME
    dedup/contribution/exclusion pass Stage 6's own rows go through --
    never a second, parallel, uncounted pile that contributing_evidence_
    ids/excluded_evidence never mention.

    Callers must only invoke this for a VALIDATED response -- an
    unvalidated (PENDING/REJECTED) response's sources must never reach
    this function at all; see compute_event_sentiment's own gating.

    Each surviving source gets a stable synthetic id
    "stage7-source-<response_id>-<index>" -- a STRING, deliberately
    distinguishable at a glance from Stage 6's own INTEGER evidence_id
    values in contributing_evidence_ids/excluded_evidence, so provenance
    (self-reported-by-AI-and-human-validated vs. collected-by-Stage-6)
    is never ambiguous downstream. A rejected source keeps this same id
    scheme in its own excluded-record, so every source -- kept or not --
    is individually addressable.

    Rejection rules, explicit and checked in this order (a source
    failing more than one only reports the first):
    - missing/blank url -> "missing a verifiable url" (Step G's own
      requirement: "verifiable URLs/publishers/dates").
    - publication_date missing or unparseable -> "publication date is
      missing or unparseable" (fails closed: an unverifiable date can
      never be assumed to predate the cutoff).
    - publication_ts strictly after historical_cutoff_ts -> "published
      after the historical cutoff" (never let information from after
      the event enter a historical assessment).

    content_hash is computed from the normalized (trimmed, lowercased)
    URL ONLY, never from the claim text -- two independently-reported
    sources making the SAME claim from DIFFERENT urls must both survive
    as independent corroboration; only a literal repeated url (the same
    article cited twice) is treated as a duplicate.

    Returns (normalized, excluded): `normalized` is the list of dedupe_
    evidence()-ready dicts that passed every rule (still subject to
    dedupe_evidence()'s own cross-row dedup afterward, e.g. two Stage 7
    sources citing the identical url); `excluded` is
    [{"evidence_id", "reason"}], the same shape compute_event_
    sentiment()'s own excluded_evidence already uses.
    """
    normalized = []
    excluded = []
    for i, raw in enumerate(sources or []):
        synthetic_id = f"stage7-source-{response_id}-{i}"
        if not isinstance(raw, dict):
            excluded.append({"evidence_id": synthetic_id, "reason": "malformed source record (not an object)"})
            continue
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            excluded.append({"evidence_id": synthetic_id, "reason": "missing a verifiable url"})
            continue
        publication_ts = _parse_source_publication_ts(raw.get("publication_date"))
        if publication_ts is None:
            excluded.append({"evidence_id": synthetic_id, "reason": "publication date is missing or unparseable"})
            continue
        if historical_cutoff_ts is not None and publication_ts > historical_cutoff_ts:
            excluded.append({"evidence_id": synthetic_id, "reason": "published after the historical cutoff"})
            continue
        normalized.append({
            "evidence_id": synthetic_id,
            "content_hash": hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest(),
            "origin": "stage7_response_source",
            "response_id": response_id,
            "article_url": url.strip(),
            "publisher": raw.get("publisher") if isinstance(raw.get("publisher"), str) else None,
            "publication_ts": publication_ts,
            "claim": raw.get("claim") if isinstance(raw.get("claim"), str) else None,
        })
    return normalized, excluded


def compute_input_fingerprint(event_id, contributing_evidence_ids, evidence_sufficiency, response_id):
    # sorted(..., key=str): contributing_evidence_ids now mixes Stage 6's
    # own INTEGER evidence_id values with Stage 7's own STRING synthetic
    # ids ("stage7-source-<response_id>-<index>") -- Python 3 raises
    # TypeError comparing int to str directly, so this sorts by each
    # id's string form purely for a stable, deterministic fingerprint
    # order; it is never used as a display or business-logic ordering.
    payload = json.dumps(
        {"event_id": event_id, "evidence_ids": sorted(contributing_evidence_ids, key=str),
         "sufficiency": evidence_sufficiency, "response_id": response_id},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_event_sentiment(event, evidence_rows, per_source_interpretation, evidence_sufficiency,
                             v1_macro_context, validated_response=None, previous_sentiment_id=None,
                             historical_cutoff_ts=None):
    """
    event: dict with event_id (required), event_ts (used as the default
        historical_cutoff_ts when none is passed explicitly).
    evidence_rows: research_event_evidence rows for this event (Stage 6's
        own, unchanged) -- the FULL set, not pre-filtered by the caller.
    per_source_interpretation: dict source_key -> classify_event_source_
        interpretation() result (event_source_evidence_join.py, reused
        verbatim, computed by the caller -- this function never
        re-derives it).
    evidence_sufficiency: one of stage7_evidence_sufficiency.
        SUFFICIENCY_STATUSES.
    v1_macro_context: fetch_v1_macro_context()'s own return value --
        reported separately, NEVER merged into contributing_evidence_ids
        (see this module's own header comment, point 1).
    validated_response: None, or a dict with at least
        {"response_id", "validation_status", "findings": {"sentiment_assessment": ...},
        "sources": [...]} -- a numeric sentiment is only ever set when
        validation_status == "VALIDATED", and `sources` is only ever
        normalized/merged into the evidence set under that exact same
        condition (see normalize_response_sources). An unvalidated
        response's sources never reach dedupe_evidence at all.
    historical_cutoff_ts: ms-epoch; defaults to event["event_ts"] when
        omitted (the same value stage7_github_publisher already uses as
        a request's own historical_cutoff_ts). Explicit param rather
        than an always-implicit read of event["event_ts"], so a caller
        (or a test) can pin the cutoff independently of the event dict.

    Returns a dict matching stage7_event_sentiment's own columns.
    Never calculates using only newly-discovered (Stage 7) evidence --
    contributing_evidence_ids always includes every deduped Stage-6-and-
    Stage-7 row passed in, per Step I's explicit instruction.
    """
    sentiment_label = None
    sentiment_score = None
    response_id = None
    response_sources_excluded = []
    all_evidence_rows = list(evidence_rows)

    if validated_response is not None and validated_response.get("validation_status") == "VALIDATED":
        response_id = validated_response.get("response_id")
        assessment = validated_response.get("findings", {}).get("sentiment_assessment")
        if assessment in SENTIMENT_SCALE:
            sentiment_label = assessment
            sentiment_score = SENTIMENT_SCALE[assessment]
        cutoff = historical_cutoff_ts if historical_cutoff_ts is not None else event.get("event_ts")
        normalized_sources, response_sources_excluded = normalize_response_sources(
            response_id, validated_response.get("sources"), cutoff
        )
        all_evidence_rows = all_evidence_rows + normalized_sources

    deduped_rows, duplicate_groups = dedupe_evidence(all_evidence_rows)
    contributing_evidence_ids = [r["evidence_id"] for r in deduped_rows]
    excluded_evidence = response_sources_excluded + [
        {"evidence_id": eid, "reason": f"duplicate content_hash of evidence_id {group['kept_evidence_id']}"}
        for group in duplicate_groups.values()
        for eid in group["dropped_evidence_ids"]
    ]

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
