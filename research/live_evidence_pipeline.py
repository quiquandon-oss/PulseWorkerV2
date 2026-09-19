"""
PR-6: Free scheduled Event Detection + Evidence Collection pipeline.

=====================================================================
Objective
=====================================================================

The PR4 diagnosis (session preceding this PR) found, with direct
evidence from this repository's own git history, that PR4's collector
is correct and reachable, but was only ever run ONCE, manually,
~27-29 days after the events it targeted -- long after free RSS feeds
had aged the relevant articles out of retention. The fix is not a code
change to PR3 or PR4 (both are reused here completely UNCHANGED); it
is closing the timing gap: run detection and collection close together,
on a recurring, free schedule.

    BTC/V1/V2 data (D1, read-only)
        |
        v
    PR3 event detection (event_detector.py, UNCHANGED)
        |
        v
    genuinely-new-event filter (fingerprint against research_events)
        |
        v
    persist newly detected events -> research_events
        |
        v
    (if young enough) PR4 evidence collection (evidence_collector.py,
     UNCHANGED) -> research_event_evidence
        |
        v
    execution summary (plain data; the caller decides how to print it)

=====================================================================
Golden rules enforced
=====================================================================

- $0: no paid API, no paid news/search, no paid LLM, no new Cloudflare
  Cron Trigger (the account's cron budget is documented elsewhere in
  this project as already exhausted) -- scheduling is GitHub Actions
  `schedule:` only, the same free mechanism already used by
  `export-learning-data.yml`.
- PR3/PR4 logic is reused verbatim. This module contains ZERO event-
  detection or evidence-matching logic of its own -- only orchestration
  (fetch -> detect -> filter -> persist -> collect -> summarize).
- All I/O (D1 reads/writes) is INJECTED as plain functions
  (`d1_query_fn`, `d1_execute_fn`), exactly mirroring
  `evidence_collector.py`'s own `fetcher` injection pattern. This
  module itself makes no network call, no subprocess call, and no
  direct database connection -- fully unit-testable with fakes, same
  discipline as every other research/ module in this project.
- No V1/V2/Worker file is imported, read, or referenced.
- No source ranking, no weight recommendation, no BUILD_REQUEST -- this
  module has nothing to do with source evaluation at all; it only
  keeps the event/evidence ledger populated.

=====================================================================
The two safeguards this PR exists to enforce (per the build
authorization's explicit "Critical safeguard" section)
=====================================================================

1. NEW EVENT -> collect evidence immediately:
   `filter_genuinely_new()` compares each freshly-detected event's
   fingerprint against the FULL set already in `research_events`
   (small, bounded table -- an unbounded SELECT here is the same
   accepted convention already used for `research_hypotheses`/
   `research_analyses` elsewhere in this project). An event whose
   fingerprint already exists is never re-persisted and never
   re-submitted for evidence collection -- this is what prevents
   duplicate processing on every scheduled run.

2. OLD EVENT -> do not repeatedly attempt historical RSS recovery:
   TWO independent, redundant guards, not just one:
     a) `DETECTION_WINDOW_MS` (3 days) bounds the candidate window
        itself -- an event more than 3 days old is never even
        detected as a candidate by a given run, by construction.
     b) `is_eligible_for_evidence_collection()` / `MAX_EVENT_AGE_
        FOR_EVIDENCE_MS` (5 days, intentionally a little wider than
        (a) to tolerate a missed run) is a SEPARATE, explicit check
        applied to every genuinely-new event before evidence
        collection is attempted, independent of how it was detected.
        An event that fails this check is still persisted to
        `research_events` (the ledger of what happened is valuable
        regardless), but evidence collection is skipped and reported
        as `SKIPPED_TOO_OLD` in the execution summary -- never
        silently attempted, never silently dropped.
   Together these mean: even on this pipeline's very first-ever run
   (or after a long outage), it can NEVER repeat the exact mistake the
   PR4 diagnosis found (a ~29-day-late retroactive collection attempt).
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
import event_detector as ed  # noqa: E402
import evidence_collector as ec  # noqa: E402

# ---------------------------------------------------------------------
# Constants -- both bounds disclosed and justified in the module
# docstring's "safeguards" section above. Reused, not reinvented:
# DETECTION_LOOKBACK_BUFFER_MS is PR3's OWN LOOKBACK_BUFFER_MS, not a
# new number.
# ---------------------------------------------------------------------
DETECTION_WINDOW_MS = 3 * 24 * 3600000          # candidate events must be <= 3 days old
DETECTION_LOOKBACK_BUFFER_MS = ed.LOOKBACK_BUFFER_MS  # PR3's own 8-day requirement, reused
MAX_EVENT_AGE_FOR_EVIDENCE_MS = 5 * 24 * 3600000  # belt-and-suspenders guard, see docstring

_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", ".ai", "migrations")
_EVIDENCE_TABLE_DDL = open(os.path.join(_MIGRATIONS_DIR, "0007_research_event_evidence.sql")).read()


# =====================================================================
# Step 1: local mirror construction (pure -- takes already-fetched rows)
# =====================================================================

def build_local_mirror(btc_rows, history_rows, prediction_rows):
    """Builds an in-memory sqlite mirror from real D1 rows, in the exact
    shape event_detector.py/evidence_collector.py already expect. Pure:
    no I/O of any kind."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL)")
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                  "score INTEGER NOT NULL, sources_json TEXT, technical_score INTEGER, gold_regime TEXT)")
    conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                  "target_ts INTEGER, horizon_hours INTEGER NOT NULL, p_up REAL, realized_up INTEGER, "
                  "realized_return REAL, model_version TEXT, git_commit_sha TEXT)")
    for r in btc_rows:
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (r["ts"], r["btc_price"]))
    for r in history_rows:
        conn.execute("INSERT INTO history (ts, score, sources_json) VALUES (?, ?, ?)",
                      (r["ts"], r["score"], r.get("sources_json")))
    for r in prediction_rows:
        conn.execute("INSERT INTO predictions (ts, horizon_hours, p_up, realized_up) VALUES (?, ?, ?, ?)",
                      (r["ts"], r["horizon_hours"], r.get("p_up"), r.get("realized_up")))
    conn.commit()
    return conn


# =====================================================================
# Step 2: event detection -- PR3's own, UNCHANGED functions
# =====================================================================

def detect_candidate_events(conn, start_ts, end_ts):
    """Identical detector set already reused, unmodified, by
    event_source_reaction.py / event_source_relevance.py. Fingerprint-
    deduplicated within this single call (a detector rule can otherwise
    emit the same logical event twice -- see PR-1's own finding)."""
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
    return sorted(by_fingerprint.values(), key=lambda e: (e["event_ts"], e["fingerprint"]))


# =====================================================================
# Step 3: the two safeguards
# =====================================================================

def filter_genuinely_new(candidate_events, existing_fingerprints):
    """Safeguard 1 -- see module docstring."""
    return [e for e in candidate_events if e["fingerprint"] not in existing_fingerprints]


def is_eligible_for_evidence_collection(event, now_ts, max_age_ms=MAX_EVENT_AGE_FOR_EVIDENCE_MS):
    """Safeguard 2b -- see module docstring."""
    return (now_ts - event["event_ts"]) <= max_age_ms


# =====================================================================
# Step 4: D1 write-statement builders (pure string building; execution
# is always the caller's injected d1_execute_fn, never done here)
# =====================================================================

def _sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def build_insert_event_sql(event, detection_ts):
    columns = ["fingerprint", "event_ts", "detection_ts", "category", "direction", "intensity",
               "available_before_prediction", "is_post_event_analysis", "trigger_metric",
               "trigger_threshold", "trigger_version"]
    values = [event["fingerprint"], event["event_ts"], detection_ts, event["category"],
              event.get("direction"), event.get("intensity"), 1, event["is_post_event_analysis"],
              event.get("trigger_metric"), event.get("trigger_threshold"), event.get("trigger_version")]
    return (f"INSERT INTO research_events ({', '.join(columns)}) VALUES "
            f"({', '.join(_sql_literal(v) for v in values)})")


def build_insert_evidence_sql(event_id, evidence_row):
    columns = ["event_id", "feed_url", "article_url", "publisher", "publication_ts",
               "collection_ts", "headline", "keyword_score", "evidence_relation", "content_hash"]
    values = [event_id, evidence_row["feed_url"], evidence_row["article_url"], evidence_row["publisher"],
              evidence_row["publication_ts"], evidence_row["collection_ts"], evidence_row["headline"],
              evidence_row.get("keyword_score"), evidence_row["evidence_relation"], evidence_row["content_hash"]]
    return (f"INSERT INTO research_event_evidence ({', '.join(columns)}) VALUES "
            f"({', '.join(_sql_literal(v) for v in values)})")


def _run_evidence_collection(real_event_id, event_ts, evidence_fetcher):
    """Runs PR4's OWN, UNCHANGED collect_evidence_for_event() against a
    throwaway local sqlite mirror (never against production directly --
    D1 is only ever touched via the caller's injected d1_execute_fn),
    then returns (counters, evidence_rows_as_dicts) for the caller to
    replicate to production."""
    local_conn = sqlite3.connect(":memory:")
    local_conn.execute("CREATE TABLE research_events (event_id INTEGER PRIMARY KEY, fingerprint TEXT, "
                        "event_ts INTEGER, detection_ts INTEGER, category TEXT)")
    local_conn.executescript(_EVIDENCE_TABLE_DDL)
    local_conn.execute("INSERT INTO research_events (event_id, event_ts) VALUES (?, ?)",
                        (real_event_id, event_ts))
    local_conn.commit()
    counters = ec.collect_evidence_for_event(local_conn, real_event_id, event_ts, fetcher=evidence_fetcher)
    columns = ["feed_url", "article_url", "publisher", "publication_ts", "collection_ts",
               "headline", "keyword_score", "evidence_relation", "content_hash"]
    rows = local_conn.execute(f"SELECT {', '.join(columns)} FROM research_event_evidence").fetchall()
    local_conn.close()
    return counters, [dict(zip(columns, row)) for row in rows]


# =====================================================================
# Top-level orchestration
# =====================================================================

def run_pipeline(d1_query_fn, d1_execute_fn, now_ts, evidence_fetcher=None):
    """The single entry point. `d1_query_fn(sql) -> list[dict]` and
    `d1_execute_fn(sql) -> None` are the ONLY I/O this function performs
    -- both injected, so this function makes zero real network/
    subprocess/database calls itself. `evidence_fetcher=None` means PR4's
    own real default fetcher (live RSS) will be used when the caller's
    injected functions are the real, production ones; tests always pass
    a fake fetcher, exactly like evidence_collector.py's own test suite.

    Returns a plain-data execution summary dict. Never raises for an
    expected "nothing new to do" outcome -- that is success, reported
    honestly, not an error.
    """
    start_ts = now_ts - DETECTION_WINDOW_MS
    fetch_start_ts = start_ts - DETECTION_LOOKBACK_BUFFER_MS

    btc_rows = d1_query_fn(
        f"SELECT ts, btc_price FROM btc_data WHERE ts >= {fetch_start_ts} AND ts <= {now_ts} ORDER BY ts ASC")
    history_rows = d1_query_fn(
        f"SELECT ts, score, sources_json FROM history WHERE ts >= {fetch_start_ts} AND ts <= {now_ts} ORDER BY ts ASC")
    prediction_rows = d1_query_fn(
        f"SELECT ts, horizon_hours, p_up, realized_up FROM predictions "
        f"WHERE ts >= {fetch_start_ts} AND ts <= {now_ts} ORDER BY ts ASC")

    summary = {
        "status": "OK",
        "window": {"start_ts": start_ts, "end_ts": now_ts, "fetch_start_ts": fetch_start_ts},
        "rows_fetched": {"btc_data": len(btc_rows), "history": len(history_rows),
                          "predictions": len(prediction_rows)},
        "candidate_events": 0, "already_known_events": 0, "newly_persisted_events": 0,
        "evidence_eligible_events": 0, "evidence_skipped_too_old_events": 0,
        "events": [],
    }

    if len(btc_rows) < 2:
        summary["status"] = "INSUFFICIENT_BTC_DATA"
        return summary

    mirror = build_local_mirror(btc_rows, history_rows, prediction_rows)
    candidates = detect_candidate_events(mirror, start_ts, now_ts)
    mirror.close()
    summary["candidate_events"] = len(candidates)

    existing_fingerprints = {r["fingerprint"] for r in d1_query_fn("SELECT fingerprint FROM research_events")}
    new_events = filter_genuinely_new(candidates, existing_fingerprints)
    summary["already_known_events"] = len(candidates) - len(new_events)

    for event in new_events:
        d1_execute_fn(build_insert_event_sql(event, now_ts))
        summary["newly_persisted_events"] += 1
        event_entry = {"fingerprint": event["fingerprint"], "category": event["category"],
                        "event_ts": event["event_ts"], "evidence": None}

        if not is_eligible_for_evidence_collection(event, now_ts):
            summary["evidence_skipped_too_old_events"] += 1
            event_entry["evidence"] = "SKIPPED_TOO_OLD"
            summary["events"].append(event_entry)
            continue

        summary["evidence_eligible_events"] += 1
        id_rows = d1_query_fn(
            f"SELECT event_id FROM research_events WHERE fingerprint = {_sql_literal(event['fingerprint'])}")
        if not id_rows:
            event_entry["evidence"] = "ERROR_EVENT_ID_NOT_FOUND_AFTER_INSERT"
            summary["events"].append(event_entry)
            continue
        real_event_id = id_rows[0]["event_id"]

        counters, evidence_rows = _run_evidence_collection(real_event_id, event["event_ts"], evidence_fetcher)
        for row in evidence_rows:
            d1_execute_fn(build_insert_evidence_sql(real_event_id, row))
        event_entry["evidence"] = counters
        summary["events"].append(event_entry)

    return summary
