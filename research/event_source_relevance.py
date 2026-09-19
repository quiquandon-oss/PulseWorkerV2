"""
PR-3: Real-World Event x V1 Source Relevance -- read-only proof-of-concept.

=====================================================================
Objective
=====================================================================

PR-1 and PR-2 evaluate every PR3 event against every one of the 21 V1
sources, even though a given event may have had nothing topically to
do with a given source. This module investigates whether the missing
relationship -- SOURCE RELEVANT TO EVENT, as distinct from SOURCE
ALIGNED WITH BTC -- can be established from evidence that already
exists at $0, without inventing a taxonomy or an NLP model.

It does NOT decide whether a source is useful, does NOT rank sources,
and does NOT produce a weight recommendation of any kind.

=====================================================================
Golden rules enforced
=====================================================================

- $0, read-only: no network call is made by this module (see "Why this
  module does not live-fetch RSS" below). No LLM, no paid service.
- No schema, no migration, no new table, no production write. This
  module only ever READS `research_event_evidence`/`research_events`
  (both via a caller-supplied connection) -- it never calls
  `evidence_collector.collect_evidence_for_event()`.
- No V1/V2/Worker file is imported or referenced.
- No source ranking, no KEEP/INCREASE/DECREASE, no BUILD_REQUEST.
- BTC reaction is NEVER used as a reason for relevance -- this module
  does not import `event_source_reaction.py` or
  `controlled_event_reaction.py` at all, by design, so that dependency
  cannot even accidentally leak in.
- PR4 evidence remains event-level. No article is ever attributed to a
  source merely because that source had an extreme value -- see
  "Why source-value is never part of the relevance bridge" below.

=====================================================================
Why this module does NOT live-fetch RSS
=====================================================================

`evidence_collector.py` (PR4) is a real, $0, already-approved RSS
collector, and in principle "retrieve the event-level evidence
available around the event" could mean running it fresh. This PR
deliberately does NOT do that: (a) this session's own outbound network
policy blocks exactly these feed hosts (confirmed directly: a test
request to `cointelegraph.com` was rejected by the egress proxy with a
403 before this module was written -- not assumed, checked), and (b)
even where network is available, triggering fresh collection is a
materially different, larger action (real HTTP requests against five
external hosts, up to 50 events x 5 feeds) than a read-only analysis
pass, and was not the scope explicitly authorized here. Instead, this
module reads whatever `research_event_evidence` rows ALREADY exist in
production (read-only), which is the direct, honest answer to
"is the existing free evidence sufficient" -- see Section 10 of the
required report. Production currently has 0 `research_event_evidence`
rows (confirmed via a fresh read-only count in this session) -- the
real run of this module reflects that honestly rather than fabricating
evidence to exercise the methodology.

=====================================================================
Why source-value is never part of the relevance bridge
=====================================================================

A source's own reading (e.g. `regulatory=82`) shows that the source
represented SOMETHING at that time. It does not show that the reading
corresponds to any specific real-world event -- that would require
independent evidence the source's own value cannot supply about
itself. This module's relevance bridge therefore NEVER inspects
`source_value` when deciding RELEVANT/POSSIBLY_RELEVANT/NOT_ESTABLISHED
/INSUFFICIENT_EVIDENCE (see `classify_relevance()` -- it takes evidence
and topic affinity, never a value). `source_value`/`source_observation_ts`
are still reported in the event-level diagnostic (the task requires
them), but purely as descriptive context alongside the independently-
derived relevance verdict, never as an input to it.

=====================================================================
Source interpretation (data-derived, not an invented taxonomy)
=====================================================================

The 21 V1 source keys (data-derived via `source_analysis.discover_
sources()`, unchanged from PR-1) were inspected against PR4's own
`FEEDS` dict, which already assigns four named topic categories to
real feed URLs: "crypto" (CoinTelegraph), "macro" (Investing.com
Economy), "geopolitics" (BBC World News), "regulatory" (CoinDesk / The
Block, both prefixed "regulatory_"). Comparing V1's 21 source key
STRINGS against those four category names (deterministic substring/
exact matching, never NLP, never invented labels) finds:

  EXACT match to a PR4 evidence category:
    "geopolitics" -> "geopolitics" (exact)
    "regulatory"  -> "regulatory"  (exact, matches both regulatory_* feeds)
    "cryptonews"  -> "crypto"      (contains "crypto")
  PARTIAL (prefix) match, disclosed as such, never treated as exact:
    "macrogeo"    -> "macro"       (starts with "macro"; the remaining
                                     "geo" suffix is NOT separately
                                     credited towards "geopolitics" --
                                     a source gets at most one topic)

  NO deterministic match to any PR4 evidence category (17 of 21):
    fng, funding, longshort, global, gold, hypefunding, nasdaq,
    ninemag, oil, onchain, sosovalue, sp500, strc, usd, yield10y.
  By their own naming these are quantitative/market-derived indicators
  (fear-greed index, funding rate, long/short positioning ratio,
  global market cap, spot commodity/FX/equity-index prices, on-chain
  metrics, an ETF-flow tracker) -- not news-narrative sources -- and
  this module reports, honestly, that NO article-evidence bridge can
  be deterministically established for them. This is a discovered
  fact about the data, not a judgment about whether those sources are
  useful.

This mapping is intentionally small and mechanical. If it is wrong or
incomplete, that is itself useful, disclosed information (Section 12 /
"evidence limitations") -- this module does not attempt to guess a
better one.

=====================================================================
Event interpretation
=====================================================================

PR3's five detectors are market-derived (BTC price/regime/volatility
patterns, V2 prediction failure streaks, V1-vs-BTC divergence) -- they
are NOT independently-identified real-world events. This module treats
every PR3 event strictly as a MARKET EVENT WINDOW requiring real-world
evidence reconstruction, never as a real-world event in its own right.
`V2_FAILURE_CLUSTER` events additionally describe V2's own prediction
behavior, not a market or information event at all -- flagged
separately (`is_internal_model_event`) and never treated as a
candidate for external-evidence relevance.

=====================================================================
Temporal rule (Section 9 / "CRITICAL TEMPORAL RULE")
=====================================================================

An evidence row can only support RELEVANT/POSSIBLY_RELEVANT when
`publication_ts <= event_ts` (PRE_EVENT) or within evidence_collector's
own SAME_WINDOW tolerance (`classify_relation()`, reused unchanged).
Evidence with `publication_ts > event_ts` beyond that tolerance is
retained and reported as `POST_EVENT_CONTEXT` and NEVER used to
establish relevance -- constructively tested (a post-event-only
evidence set must yield the same relevance verdict as no evidence at
all, never RELEVANT/POSSIBLY_RELEVANT).
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import event_detector as ed  # noqa: E402
import evidence_collector as ec  # noqa: E402
import evidence_temporal as et  # noqa: E402
import source_analysis as sa  # noqa: E402

RELEVANCE_LABELS = ("RELEVANT", "POSSIBLY_RELEVANT", "NOT_ESTABLISHED", "INSUFFICIENT_EVIDENCE")
TEMPORAL_STATUSES = ("PRE_EVENT_EVIDENCE", "SAME_WINDOW_EVIDENCE", "POST_EVENT_CONTEXT", "NO_EVIDENCE")

# Deterministic, code-derived source -> PR4-evidence-category affinity.
# See module docstring's "Source interpretation" section for the exact
# derivation. `None` means: no deterministic bridge exists for this
# source's nature (a discovered fact, not a judgment).
SOURCE_TOPIC_AFFINITY = {
    "geopolitics": ("geopolitics", "EXACT"),
    "regulatory": ("regulatory", "EXACT"),
    "cryptonews": ("crypto", "SUBSTRING"),
    "macrogeo": ("macro", "PREFIX"),
}

# PR4's own FEEDS dict, inverted feed_url -> topic category. Built here
# by reading evidence_collector.FEEDS directly (never duplicated by
# hand), so this can never silently drift from PR4's real feed set.
EVIDENCE_FEED_URL_TO_TOPIC = {
    feed_url: ("regulatory" if key.startswith("regulatory") else key)
    for key, (feed_url, _publisher) in ec.FEEDS.items()
}

INTERNAL_MODEL_EVENT_CATEGORIES = ("V2_FAILURE_CLUSTER",)


def source_topic_affinity(source_key):
    """Returns (topic, match_type) or (None, None). See module
    docstring."""
    return SOURCE_TOPIC_AFFINITY.get(source_key, (None, None))


def evidence_topic(evidence_row):
    """Deterministic: the evidence's own feed_url, looked up against
    PR4's own FEEDS dict (never a new mapping). Returns None if the
    feed_url is not one of PR4's five known feeds (should not happen
    for real PR4-collected rows, but never assumed)."""
    return EVIDENCE_FEED_URL_TO_TOPIC.get(evidence_row.get("feed_url"))


# =====================================================================
# Step 1: events -- reused from PR3 exactly as PR-1 already does
# =====================================================================

def collect_events(conn, start_ts, end_ts):
    """Identical detection + fingerprint-dedup logic as PR-1's own
    `event_source_reaction.collect_pr3_events()`, reproduced here
    (rather than imported) ONLY so this module has zero import-time
    dependency on `event_source_reaction.py` / `controlled_event_
    reaction.py` -- enforcing, structurally, that BTC-reaction code can
    never leak into a relevance decision (see module docstring's
    Golden Rules section). The detection calls themselves are PR3's
    own, unchanged functions."""
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
        event["is_internal_model_event"] = event["category"] in INTERNAL_MODEL_EVENT_CATEGORIES
    return deduped


# =====================================================================
# Step 2: source snapshot -- identical as-of shape as PR-1/resolver.py
# =====================================================================

def snapshot_v1_source(conn, event_ts, source_key):
    row = conn.execute(
        "SELECT ts, sources_json FROM history WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
        (event_ts,),
    ).fetchone()
    if row is None:
        return {"source_value": None, "source_observation_ts": None, "status": "NO_HISTORY_BEFORE_EVENT"}
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
        "status": "OK" if value is not None else "SOURCE_MISSING_AT_OBSERVATION",
    }


# =====================================================================
# Step 3: PR4 evidence lookup -- read-only, production research_event_
# evidence table, joined to a live-detected event by EXACT event_ts
# match against the persisted research_events table (session-local
# event_ids from collect_events() never correspond to production's own
# autoincrement event_id, so ts is the only safe join key available).
# =====================================================================

def fetch_evidence_for_event_ts(conn, event_ts):
    """Read-only. Looks up the persisted `research_events` row with
    this EXACT event_ts (if any), then its `research_event_evidence`
    rows (if any). Returns an empty list if either the event was never
    persisted or has no evidence -- both are honest, expected outcomes
    given production's current sparse state, never an error."""
    persisted = conn.execute(
        "SELECT event_id FROM research_events WHERE event_ts = ?", (event_ts,)
    ).fetchone()
    if persisted is None:
        return []
    persisted_event_id = persisted[0]
    rows = conn.execute(
        "SELECT evidence_id, feed_url, article_url, publisher, publication_ts, "
        "collection_ts, headline, keyword_score, evidence_relation, content_hash "
        "FROM research_event_evidence WHERE event_id = ? ORDER BY publication_ts ASC",
        (persisted_event_id,),
    ).fetchall()
    columns = ["evidence_id", "feed_url", "article_url", "publisher", "publication_ts",
               "collection_ts", "headline", "keyword_score", "evidence_relation", "content_hash"]
    return [dict(zip(columns, row)) for row in rows]


def classify_evidence_temporal_status(evidence_row, event_ts, same_window_tolerance_ms=ec.SAME_WINDOW_TOLERANCE_MS):
    """Reuses evidence_collector.classify_relation() UNCHANGED (PRE_EVENT/
    SAME_WINDOW/POST_EVENT), remapped to this PR's four-way vocabulary.
    publication_ts <= event_ts (outside the same-window tolerance) is
    PRE_EVENT_EVIDENCE; within tolerance either side is SAME_WINDOW_
    EVIDENCE; strictly after (beyond tolerance) is POST_EVENT_CONTEXT."""
    relation = ec.classify_relation(evidence_row["publication_ts"], event_ts)
    return {"PRE_EVENT": "PRE_EVENT_EVIDENCE", "SAME_WINDOW": "SAME_WINDOW_EVIDENCE",
            "POST_EVENT": "POST_EVENT_CONTEXT"}[relation]


# =====================================================================
# Step 4: relevance classification -- evidence + topic affinity +
# temporal compatibility ONLY. Never source_value, never BTC reaction.
# =====================================================================

def classify_relevance(source_key, evidence_rows, event_ts):
    """Descriptive, explainable, deterministic. Returns
    {"result", "reason", "evidence_ids", "temporal_statuses"}.
    """
    if not evidence_rows:
        return {"result": "INSUFFICIENT_EVIDENCE",
                "reason": "no research_event_evidence rows exist for this event",
                "evidence_ids": [], "temporal_statuses": []}

    topic, match_type = source_topic_affinity(source_key)
    annotated = []
    for row in evidence_rows:
        temporal_status = classify_evidence_temporal_status(row, event_ts)
        annotated.append({"row": row, "temporal_status": temporal_status, "topic": evidence_topic(row)})

    # Post-event-only evidence NEVER establishes relevance -- filtered
    # out before any topic comparison is even attempted (Section 9).
    predictive_eligible = [a for a in annotated if a["temporal_status"] != "POST_EVENT_CONTEXT"]
    all_ids = [a["row"]["evidence_id"] for a in annotated]

    if topic is None:
        return {"result": "NOT_ESTABLISHED",
                "reason": f"source '{source_key}' has no deterministic topical bridge to any PR4 "
                          "evidence category (quantitative/market-derived source, not a news source)",
                "evidence_ids": all_ids,
                "temporal_statuses": sorted({a["temporal_status"] for a in annotated})}

    if not predictive_eligible:
        return {"result": "NOT_ESTABLISHED",
                "reason": "evidence exists but only as POST_EVENT_CONTEXT -- cannot establish "
                          "pre-event relevance from post-event information",
                "evidence_ids": all_ids,
                "temporal_statuses": sorted({a["temporal_status"] for a in annotated})}

    exact_pre_event_matches = [a for a in predictive_eligible
                                if a["topic"] == topic and a["temporal_status"] == "PRE_EVENT_EVIDENCE"]
    if exact_pre_event_matches and match_type == "EXACT":
        return {"result": "RELEVANT",
                "reason": f"pre-event evidence from a feed topically matching source '{source_key}' "
                          f"('{topic}', {match_type} match) exists strictly before event_ts",
                "evidence_ids": [a["row"]["evidence_id"] for a in exact_pre_event_matches],
                "temporal_statuses": ["PRE_EVENT_EVIDENCE"]}

    any_topic_matches = [a for a in predictive_eligible if a["topic"] == topic]
    if any_topic_matches:
        return {"result": "POSSIBLY_RELEVANT",
                "reason": f"topically matching evidence exists ('{topic}', {match_type} match) but "
                          "only as SAME_WINDOW evidence, or via a partial/prefix source-topic match "
                          "rather than an exact one",
                "evidence_ids": [a["row"]["evidence_id"] for a in any_topic_matches],
                "temporal_statuses": sorted({a["temporal_status"] for a in any_topic_matches})}

    return {"result": "NOT_ESTABLISHED",
            "reason": f"pre-event/same-window evidence exists, but none of it topically matches "
                      f"source '{source_key}' ('{topic}')",
            "evidence_ids": [a["row"]["evidence_id"] for a in predictive_eligible],
            "temporal_statuses": sorted({a["temporal_status"] for a in predictive_eligible})}


# =====================================================================
# Top-level orchestration
# =====================================================================

def build_event_source_relevance_dataset(conn, start_ts, end_ts):
    """Read-only. Never writes. `conn` must expose BOTH the market/V1
    tables (history, btc_data, predictions) and the research_events /
    research_event_evidence tables -- exactly the shape a real
    production D1 connection has."""
    events = collect_events(conn, start_ts, end_ts)
    source_keys = sa.discover_sources(conn, start_ts, end_ts)

    results = []
    for event in events:
        evidence_rows = fetch_evidence_for_event_ts(conn, event["event_ts"])
        for source_key in source_keys:
            snap = snapshot_v1_source(conn, event["event_ts"], source_key)
            relevance = classify_relevance(source_key, evidence_rows, event["event_ts"])
            results.append({
                "event_id": event["event_id"], "event_ts": event["event_ts"],
                "event_category": event["category"],
                "is_internal_model_event": event["is_internal_model_event"],
                "source_key": source_key,
                "source_value": snap["source_value"],
                "source_observation_ts": snap["source_observation_ts"],
                "source_observation_status": snap["status"],
                "n_evidence_rows": len(evidence_rows),
                "relevance": relevance,
            })

    return {"window": {"start_ts": start_ts, "end_ts": end_ts}, "events": events,
            "source_keys": source_keys, "results": results}


# =====================================================================
# Descriptive summary -- no ranking, no score
# =====================================================================

def summarize_by_source(dataset):
    from collections import defaultdict
    per_source = defaultdict(lambda: defaultdict(int))
    n_events = len(dataset["events"])
    for r in dataset["results"]:
        stats = per_source[r["source_key"]]
        stats["events_evaluated"] += 1
        if r["source_observation_status"] == "OK":
            stats["source_observations_available"] += 1
        for status in r["relevance"]["temporal_statuses"] or ["NO_EVIDENCE"]:
            stats[status] += 1
        stats[r["relevance"]["result"]] += 1
    combined = {}
    for source_key in dataset["source_keys"]:
        s = per_source.get(source_key, {})
        combined[source_key] = {
            "events_evaluated": n_events,
            "source_observations_available": s.get("source_observations_available", 0),
            "pre_event_evidence_available": s.get("PRE_EVENT_EVIDENCE", 0),
            "same_window_evidence": s.get("SAME_WINDOW_EVIDENCE", 0),
            "post_event_context_only": s.get("POST_EVENT_CONTEXT", 0),
            "RELEVANT": s.get("RELEVANT", 0),
            "POSSIBLY_RELEVANT": s.get("POSSIBLY_RELEVANT", 0),
            "NOT_ESTABLISHED": s.get("NOT_ESTABLISHED", 0),
            "INSUFFICIENT_EVIDENCE": s.get("INSUFFICIENT_EVIDENCE", 0),
        }
    return combined


def summarize_sample_bias(dataset, history_min_ts=None):
    """Section: 'VERY IMPORTANT SAMPLE-BIAS CHECK'. Reports coverage
    denominators explicitly -- never a bare percentage."""
    events = dataset["events"]
    n_events = len(events)
    n_with_source = sum(
        1 for e in events
        if any(r["event_id"] == e["event_id"] and r["source_observation_status"] == "OK"
               for r in dataset["results"])
    )
    n_with_evidence = sum(1 for e in events
                          if any(r["event_id"] == e["event_id"] and r["n_evidence_rows"] > 0
                                 for r in dataset["results"]))
    n_with_pre_event_evidence = sum(
        1 for e in events
        if any(r["event_id"] == e["event_id"] and "PRE_EVENT_EVIDENCE" in r["relevance"]["temporal_statuses"]
               for r in dataset["results"])
    )
    n_bridged = sum(
        1 for e in events
        if any(r["event_id"] == e["event_id"] and r["relevance"]["result"] in ("RELEVANT", "POSSIBLY_RELEVANT")
               for r in dataset["results"])
    )
    n_before_history = None
    if history_min_ts is not None:
        n_before_history = sum(1 for e in events if e["event_ts"] < history_min_ts)
    return {
        "total_events": n_events,
        "events_with_any_source_observation": n_with_source,
        "events_with_event_level_evidence": n_with_evidence,
        "events_with_pre_event_evidence": n_with_pre_event_evidence,
        "events_with_source_plus_evidence_bridge": n_bridged,
        "events_with_insufficient_evidence": n_events - n_with_evidence,
        "events_predating_history_retention": n_before_history,
    }
