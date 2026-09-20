#!/usr/bin/env python3
"""
Experiment 9 -- Event x Source x Real-World Evidence.

RESEARCH ONLY. Complementary to EXP-005, which asks: "does a V1 source
contain measurable information about subsequent BTC movement?" This
experiment asks a different question: "what real-world information was
a V1 source representing around a detected event, and did that
representation relate to the event's control-adjusted BTC reaction?"

This script:
  1. Fetches real, bounded (<=90 days -- research/event_detector.py's own
     MAX_WINDOW_MS, unchanged) history + btc_data + predictions +
     research_events + research_event_evidence from production D1
     (read-only).
  2. Runs research/event_source_evidence_join.py's own
     build_event_source_evidence_dataset() -- UNCHANGED, imported not
     reimplemented -- against a throwaway local sqlite mirror of that
     data. That module itself reuses controlled_event_reaction.py
     (PR57+PR58, BTC reaction) and event_source_relevance.py (PR59,
     real-world-evidence relevance), also UNCHANGED, and only joins
     their independently-computed results by (event_ts, source_key).
  3. Persists the full report into the EXISTING research_analyses table
     (schema unchanged since PR1), tagged with
     subject='EXP-009:event_source_evidence' so this experiment's own
     rows are unambiguously distinguishable from anything else that
     table may ever hold. The INSERT is executed via Cloudflare's D1
     REST API directly (d1_api_query), with the full report passed as
     a BOUND PARAMETER rather than embedded in the SQL text -- reusing,
     from the start, the parameterized-write design EXP-005 arrived at
     only after two production failures (E2BIG under wrangler
     --command, then SQLITE_TOOBIG under wrangler --file) -- see
     exp005-source-effectiveness/run_experiment.py's own d1_api_query
     docstring for the full history. EXP-005 itself is not modified by
     this script in any way.

It NEVER writes to history, btc_data, predictions, research_events,
research_event_evidence, selection_decisions, or any V1/V2 production
table. Its only write target is research_analyses, and only ever an
INSERT (never UPDATE/DELETE) -- each run is a new, independent,
timestamped observation, not a mutation of a prior one.

Zero new statistical logic and zero new event/evidence/relevance
methodology. research/event_detector.py, research/evidence_collector.py,
research/event_source_reaction.py, research/controlled_event_reaction.py,
and research/event_source_relevance.py are all reused completely
UNCHANGED. The only genuinely new logic is
research/event_source_evidence_join.py's deterministic combination of
their already-computed categorical labels (see that module's own
docstring for the full decision table) -- itself introducing no new
statistical threshold and no new significance test.

Governance: this experiment produces research descriptors only
(EVENT_SOURCE_ALIGNED / NOT_ALIGNED / REDUNDANT_POSSIBLE /
INFORMATIONAL_ONLY / MISLEADING_POSSIBLE / INSUFFICIENT_EVIDENCE). It
never claims causation, never ranks sources, and never recommends a
coefficient, weight, or production change. Any such change would
require: research evidence -> controlled candidate change -> OOS
validation -> independent audit -> human approval -> production
deployment -- exactly the same discipline already documented for
EXP-005.

Failure handling: if anything fails, this script exits non-zero and
writes NOTHING. It never falls back to a fake/partial/synthetic report.
"""
import json
import os
import subprocess
import sys
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "research")
import event_detector as ed  # noqa: E402 -- UNCHANGED, reused as-is
import event_source_evidence_join as join_module  # noqa: E402 -- UNCHANGED, reused as-is

DATABASE_NAME = "sentiment-history"
# Same non-secret identifiers exp005-source-effectiveness/run_experiment.py
# already uses -- not a credential, only CLOUDFLARE_API_TOKEN is.
CLOUDFLARE_ACCOUNT_ID = "f58e761fbc8e62dc404d8684290af264"
D1_DATABASE_ID = "f91ca980-b886-423a-bd6f-f3baea46d181"
SUBJECT = "EXP-009:event_source_evidence"
WINDOW_MS = ed.MAX_WINDOW_MS  # 90 days -- event_detector's own bound, reused not reinvented
LOOKBACK_BUFFER_MS = ed.LOOKBACK_BUFFER_MS  # 8 days -- event_detector's own
# trailing-window requirement; every detector needs price/prediction
# history strictly before start_ts to compute its own rolling statistics
# at the very first point in the requested range.


def run_d1(sql: str):
    """Executes a SQL statement against production D1 via wrangler,
    exactly the same mechanism every other experiment's SELECT fetches
    already use. Only used for this script's small, fixed-shape SELECT
    fetches -- never for the large generated report (see d1_api_query)."""
    result = subprocess.run(
        ["wrangler", "d1", "execute", DATABASE_NAME, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    return parsed[0]["results"] if parsed and parsed[0].get("results") is not None else []


def d1_api_query(sql: str, params: list):
    """Executes a PARAMETERIZED SQL statement against production D1 via
    Cloudflare's D1 REST API directly (POST .../d1/database/{id}/query).

    Identical implementation to exp005-source-effectiveness/run_
    experiment.py's own d1_api_query() (duplicated deliberately, not
    imported -- each experiment script is self-contained and
    independently auditable, matching this project's established
    per-experiment convention; EXP-005's own file is never imported by,
    or modified for, this experiment).

    A bound parameter's value is transmitted as data, never parsed as
    SQL grammar, so it is not subject to D1's ~100,000-byte maximum SQL
    STATEMENT length -- confirmed empirically during EXP-005's own
    build (99,141 bytes succeeds as literal SQL text, 100,141 bytes
    fails with SQLITE_TOOBIG) and confirmed to work at real production
    scale via a real GitHub Actions run with a 230,000-byte bound
    parameter (exact_match=True). This experiment's own report can be
    large (many events x 21 sources), so it uses this proven mechanism
    from the start rather than risking the same two-step failure EXP-005
    hit.

    Uses the existing CLOUDFLARE_API_TOKEN secret and the project's
    existing, non-secret CLOUDFLARE_ACCOUNT_ID/D1_DATABASE_ID. No new
    secret, no new credential. The token is read once from the
    environment and used only in the Authorization header; it is never
    included in any log line, print statement, or exception message
    this function raises.

    Fails loudly on anything but an unambiguous success: a network
    error, a non-200 HTTP status, a top-level {"success": false}
    envelope, or a missing/malformed result shape all raise -- there is
    no partial-success path, and this function never returns normally
    without D1 itself having reported success."""
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/d1/database/{D1_DATABASE_ID}/query"
    body = json.dumps({"sql": sql, "params": params}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"D1 API request failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:2000]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"D1 API request failed: {e.reason}") from e

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"D1 API returned non-JSON response (HTTP {status})") from e

    if status != 200 or not parsed.get("success"):
        raise RuntimeError(f"D1 API request did not succeed: HTTP {status} errors={parsed.get('errors')}")

    result = parsed.get("result")
    if not isinstance(result, list) or not result:
        raise RuntimeError(f"D1 API response missing expected result list: {parsed}")

    statement_result = result[0]
    if not statement_result.get("success", True):
        raise RuntimeError(f"D1 API statement did not succeed: {statement_result}")

    return statement_result.get("results") or []


def build_local_mirror(history_rows, btc_rows, predictions_rows, research_events_rows, research_event_evidence_rows):
    """In-memory sqlite mirror containing ONLY the columns the reused
    modules actually query (event_detector.py, event_source_reaction.py,
    controlled_event_reaction.py, event_source_relevance.py) -- same
    minimal-mirror convention already used by
    research/live_evidence_pipeline.py and every other experiment's
    run_experiment.py.

    research_events.event_id is inserted with production's own real
    IDs (never AUTOINCREMENT-reassigned in the mirror), because
    research_event_evidence.event_id must still resolve to the correct
    row for event_source_relevance.fetch_evidence_for_event_ts()'s join
    to work."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "score INTEGER, sources_json TEXT, gold_regime TEXT)"
    )
    conn.execute("CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL)")
    conn.execute(
        "CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "horizon_hours INTEGER, p_up REAL, realized_up INTEGER)"
    )
    conn.execute("CREATE TABLE research_events (event_id INTEGER PRIMARY KEY, event_ts INTEGER NOT NULL)")
    conn.execute(
        "CREATE TABLE research_event_evidence (evidence_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, "
        "feed_url TEXT NOT NULL, article_url TEXT NOT NULL, publisher TEXT NOT NULL, "
        "publication_ts INTEGER NOT NULL, collection_ts INTEGER NOT NULL, headline TEXT NOT NULL, "
        "keyword_score REAL, evidence_relation TEXT NOT NULL, content_hash TEXT NOT NULL)"
    )
    for r in history_rows:
        conn.execute(
            "INSERT INTO history (ts, score, sources_json, gold_regime) VALUES (?, ?, ?, ?)",
            (r["ts"], r.get("score"), r.get("sources_json"), r.get("gold_regime")),
        )
    for r in btc_rows:
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (r["ts"], r["btc_price"]))
    for r in predictions_rows:
        conn.execute(
            "INSERT INTO predictions (ts, horizon_hours, p_up, realized_up) VALUES (?, ?, ?, ?)",
            (r["ts"], r["horizon_hours"], r["p_up"], r["realized_up"]),
        )
    for r in research_events_rows:
        conn.execute("INSERT INTO research_events (event_id, event_ts) VALUES (?, ?)", (r["event_id"], r["event_ts"]))
    for r in research_event_evidence_rows:
        conn.execute(
            "INSERT INTO research_event_evidence (evidence_id, event_id, feed_url, article_url, publisher, "
            "publication_ts, collection_ts, headline, keyword_score, evidence_relation, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["evidence_id"], r["event_id"], r["feed_url"], r["article_url"], r["publisher"],
             r["publication_ts"], r["collection_ts"], r["headline"], r.get("keyword_score"),
             r["evidence_relation"], r["content_hash"]),
        )
    conn.commit()
    return conn


INSERT_ANALYSIS_SQL = (
    "INSERT INTO research_analyses "
    "(analysis_ts, window_start_ts, window_end_ts, sample_size, subject, metric_json, "
    "multiple_testing_correction, validation_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

NOT_A_HYPOTHESIS_TEST_NOTE = json.dumps({
    "applicable": False,
    "reason": "event_source_evidence_join.py's classification is a deterministic categorical "
              "combination of already-computed labels, not a statistical hypothesis test -- no "
              "multiple-testing correction applies.",
})


def summarize_validation_status(evidence_coverage):
    """A single coarse validation_status label for the research_analyses
    row itself -- descriptive bookkeeping only, mirroring EXP-005's own
    summarize_validation_status() convention. Never a coefficient or
    production recommendation."""
    if evidence_coverage["n_real_world_events_with_any_evidence"] > 0:
        return "evidence_observed"
    return "insufficient_evidence"


def main():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = now_ms - WINDOW_MS
    fetch_start_ts = start_ts - LOOKBACK_BUFFER_MS

    history_rows = run_d1(
        f"SELECT ts, score, sources_json, gold_regime FROM history "
        f"WHERE ts >= {fetch_start_ts} AND ts <= {now_ms} ORDER BY ts ASC"
    )
    btc_rows = run_d1(
        f"SELECT ts, btc_price FROM btc_data WHERE ts >= {fetch_start_ts} AND ts <= {now_ms} ORDER BY ts ASC"
    )
    predictions_rows = run_d1(
        f"SELECT ts, horizon_hours, p_up, realized_up FROM predictions "
        f"WHERE ts >= {fetch_start_ts} AND ts <= {now_ms} AND horizon_hours IN (12, 24) "
        f"AND realized_up IS NOT NULL AND p_up IS NOT NULL ORDER BY ts ASC"
    )
    research_events_rows = run_d1(
        f"SELECT event_id, event_ts FROM research_events "
        f"WHERE event_ts >= {fetch_start_ts} AND event_ts <= {now_ms}"
    )
    research_event_evidence_rows = run_d1(
        f"SELECT evidence_id, event_id, feed_url, article_url, publisher, publication_ts, "
        f"collection_ts, headline, keyword_score, evidence_relation, content_hash "
        f"FROM research_event_evidence WHERE event_id IN "
        f"(SELECT event_id FROM research_events WHERE event_ts >= {fetch_start_ts} AND event_ts <= {now_ms})"
    )
    print(f"[exp009] fetched {len(history_rows)} history rows, {len(btc_rows)} btc_data rows, "
          f"{len(predictions_rows)} predictions rows, {len(research_events_rows)} research_events rows, "
          f"{len(research_event_evidence_rows)} research_event_evidence rows for window [{start_ts}, {now_ms}]")

    if len(history_rows) < 3 or len(btc_rows) < 3:
        print(f"[exp009] insufficient market data (history={len(history_rows)}, btc_data={len(btc_rows)}) "
              f"-- writing nothing", file=sys.stderr)
        sys.exit(1)

    mirror = build_local_mirror(history_rows, btc_rows, predictions_rows, research_events_rows, research_event_evidence_rows)
    dataset = join_module.build_event_source_evidence_dataset(mirror, start_ts, now_ms)
    mirror.close()

    # Summaries are computed over the FULL dataset (including internal-model
    # events, so their exclusion is itself visible and auditable), but the
    # persisted per-row detail is filtered to real-world events only:
    # V2_FAILURE_CLUSTER events are never a candidate for real-world-
    # evidence interpretation at all (event_source_evidence_join.py's own
    # rule -- every such row is unconditionally INSUFFICIENT_EVIDENCE with
    # zero exceptions), so persisting their full per-source detail adds
    # bulk without adding information. This is a disclosed scoping choice,
    # not a silent drop -- n_internal_model_events_excluded_from_results
    # records exactly how many were left out.
    evidence_coverage = join_module.summarize_evidence_coverage(dataset)
    by_source_interpretation = join_module.summarize_by_source_interpretation(dataset)
    btc_outcome_coverage = join_module.summarize_btc_outcome_coverage(dataset)

    real_world_events = [e for e in dataset["events"] if not e["is_internal_model_event"]]
    real_world_results = [r for r in dataset["results"] if not r["is_internal_model_event"]]

    report = {
        "window": dataset["window"],
        "events": real_world_events,
        "n_internal_model_events_excluded_from_results": len(dataset["events"]) - len(real_world_events),
        "source_keys": dataset["source_keys"],
        "results": real_world_results,
        "evidence_coverage": evidence_coverage,
        "by_source_interpretation": by_source_interpretation,
        "btc_outcome_coverage": btc_outcome_coverage,
    }
    validation_status = summarize_validation_status(evidence_coverage)

    # sample_size describes the actual persisted observation payload
    # (report["results"], i.e. real_world_results) -- NOT dataset["results"]
    # pre-filter, which still includes internal-model-event rows that are
    # deliberately excluded from what gets persisted (see the filtering
    # above). A sample_size that counted rows this analysis never actually
    # persisted would silently overstate the size of this observation --
    # research_analyses.sample_size must always equal len(the report's own
    # "results" array), the same contract EXP-005's own sample_size
    # (report["n_history_rows"], itself embedded in its own metric_json)
    # already upholds.
    params = [
        now_ms, start_ts, now_ms, len(real_world_results),
        SUBJECT, json.dumps(report), NOT_A_HYPOTHESIS_TEST_NOTE, validation_status,
    ]
    d1_api_query(INSERT_ANALYSIS_SQL, params)
    print(f"[exp009] persisted analysis: n_events={len(dataset['events'])} "
          f"n_real_world_events_with_evidence={evidence_coverage['n_real_world_events_with_any_evidence']} "
          f"n_event_source_rows={len(real_world_results)} validation_status={validation_status}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[exp009] FAILED (writing nothing): {e}", file=sys.stderr)
        sys.exit(1)
