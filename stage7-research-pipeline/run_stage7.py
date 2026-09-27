#!/usr/bin/env python3
"""
Stage 7 -- AI-Assisted Internet Research & Full-Source Sentiment
Recalculation. Scheduled orchestration script.

RESEARCH-ONLY WRITE SURFACE: stage7_research_requests, stage7_event_
sentiment (both new, migration 0016). Never writes research_events,
research_event_evidence (Stage 6's own tables), history, btc_data,
predictions, challenger_predictions, selection_decisions, or any V1/V2
production table. stage7_research_responses is written only by a human
via the Worker's POST /api/research-lab/stage7-register-response
endpoint (worker.js) -- never by this scheduled script, which only
READS responses to decide whether a validated one now resolves a
pending request.

Mirrors exp009-event-source-evidence/run_experiment.py's own structure
deliberately: same dual D1 access pattern (wrangler CLI for reads,
Cloudflare's D1 REST API with bound parameters for writes), same
in-memory sqlite mirror technique, same fail-loud-write-nothing-on-error
discipline. run_d1/d1_api_query/build_local_mirror are DUPLICATED here
rather than imported from exp009's script, matching this project's own
explicit, established convention (see that script's d1_api_query
docstring: "each experiment script is self-contained and independently
auditable").

Zero new event/evidence/relevance/reaction methodology: event_detector.py,
evidence_collector.py, event_source_reaction.py, controlled_event_
reaction.py, event_source_relevance.py, and event_source_evidence_join.py
are all reused completely UNCHANGED, exactly as exp009 already reuses
them. The only genuinely new logic is in research/stage7_evidence_
sufficiency.py, research/stage7_sentiment_recalculation.py, and
research/stage7_github_publisher.py (each independently unit-tested,
zero DB/network dependency).

Does NOT call any AI API. Publishing a research request is a GitHub
commit (existing repo, existing ambient GITHUB_TOKEN via actions/checkout
-- see .github/workflows/stage7-research-pipeline.yml), never a call to
Claude/ChatGPT/Gemini/Grok. The actual internet research is Olivier's,
run manually outside this script (Step G).
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
import stage7_evidence_sufficiency as suff  # noqa: E402
import stage7_sentiment_recalculation as sr  # noqa: E402
import stage7_github_publisher as pub  # noqa: E402

DATABASE_NAME = "sentiment-history"
# Same non-secret identifiers every other experiment script already uses.
CLOUDFLARE_ACCOUNT_ID = "f58e761fbc8e62dc404d8684290af264"
D1_DATABASE_ID = "f91ca980-b886-423a-bd6f-f3baea46d181"
WINDOW_MS = ed.MAX_WINDOW_MS
LOOKBACK_BUFFER_MS = ed.LOOKBACK_BUFFER_MS
# The same 5-day + 1-day retry-eligibility window live_evidence_pipeline.py
# already uses for whether Stage 6 will even attempt evidence collection
# for an event -- Stage 7 processes exactly the events Stage 6 itself
# still considers live, reusing that boundary rather than inventing a
# different one.
MAX_EVENT_AGE_FOR_STAGE7_MS = 6 * 24 * 3600000


def run_d1(sql: str):
    result = subprocess.run(
        ["wrangler", "d1", "execute", DATABASE_NAME, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    return parsed[0]["results"] if parsed and parsed[0].get("results") is not None else []


def d1_api_query(sql: str, params: list):
    """Identical implementation/rationale to exp009's own d1_api_query --
    see that script's docstring for the full history of why a bound
    parameter is required here (D1's ~100,000-byte SQL statement-length
    ceiling)."""
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
    """Identical shape/rationale to exp009's own build_local_mirror --
    duplicated per this project's established per-experiment convention."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "score INTEGER, technical_score INTEGER, sources_json TEXT, gold_regime TEXT)"
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
            "INSERT INTO history (ts, score, technical_score, sources_json, gold_regime) VALUES (?, ?, ?, ?, ?)",
            (r["ts"], r.get("score"), r.get("technical_score"), r.get("sources_json"), r.get("gold_regime")),
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


def group_results_by_event(dataset):
    """Pure. Groups build_event_source_evidence_dataset()'s flat
    per-(event,source) `results` rows back up BY EVENT_TS (not
    event_id): event_source_relevance.py's own collect_events() assigns
    event_id as a session-local sequence number re-derived on every run
    ("session-local event_ids from collect_events() never correspond to
    production's own autoincrement event_id, so ts is the only safe
    join key available" -- fetch_evidence_for_event_ts's own comment,
    event_source_relevance.py). Using that local id as if it were
    research_events' real primary key would silently corrupt every
    stage7_* write, so this function (and every caller) keys by event_ts
    throughout, and only resolve_real_event_ids() below ever maps to a
    real, persisted event_id. Excludes internal-model events
    (V2_FAILURE_CLUSTER) -- never a candidate for real-world evidence
    research, same rule PR59 already enforces upstream."""
    events_by_ts = {e["event_ts"]: e for e in dataset["events"] if not e["is_internal_model_event"]}
    relevance_by_ts = {ts: {} for ts in events_by_ts}
    interpretation_by_ts = {ts: {} for ts in events_by_ts}
    for row in dataset["results"]:
        ts = row["event_ts"]
        if ts not in events_by_ts:
            continue
        relevance_by_ts[ts][row["source_key"]] = {"result": row["relevance_result"]}
        interpretation_by_ts[ts][row["source_key"]] = row["event_source_interpretation"]
    return events_by_ts, relevance_by_ts, interpretation_by_ts


def select_eligible_events(events_by_ts, now_ts, max_age_ms=MAX_EVENT_AGE_FOR_STAGE7_MS):
    """Pure. Same age-eligibility shape as live_evidence_pipeline.py's
    own is_eligible_for_evidence_collection() -- Stage 7 processes an
    event only while Stage 6 itself would still consider it live."""
    return {ts: e for ts, e in events_by_ts.items() if now_ts - e["event_ts"] <= max_age_ms}


REQUIRED_STAGE7_TABLES = ("stage7_research_requests", "stage7_research_responses", "stage7_event_sentiment")


def classify_stage7_schema_state(existing_table_names):
    """Pure. (Adversarial-review remediation, finding #3.) `existing_table_
    names` is whatever a real `SELECT name FROM sqlite_master WHERE
    type='table' AND name IN (...)` query actually returned -- this
    function never queries anything itself, so it is fully testable
    without D1/wrangler/network.

    Returns:
    - "READY": all three Stage 7 tables exist -- migration 0016 is fully
      applied, safe to read/write normally.
    - "NOT_APPLIED": NONE of the three exist -- the expected, safe
      pre-migration state. The caller must skip cleanly (exit 0, a
      no-op), never raise, never fail the workflow.
    - "PARTIAL": SOME but not all three exist -- an anomalous state (a
      migration that started but didn't finish, or was hand-edited).
      This is deliberately NEVER treated the same as NOT_APPLIED or
      READY -- the caller must raise loudly, since silently proceeding
      against, or silently skipping, a half-applied schema could corrupt
      data or mask a real deployment problem.
    """
    present = set(existing_table_names) & set(REQUIRED_STAGE7_TABLES)
    if len(present) == len(REQUIRED_STAGE7_TABLES):
        return "READY"
    if len(present) == 0:
        return "NOT_APPLIED"
    return "PARTIAL"


def resolve_real_event_ids(events_by_ts, research_events_rows):
    """Pure. Maps session-local-keyed (by event_ts) events onto
    research_events' REAL persisted event_id, via the exact same
    event_ts join event_source_relevance.fetch_evidence_for_event_ts()
    already relies on as "the only safe join key available". An event
    with no matching persisted research_events row (not yet written by
    live_evidence_pipeline.py) is dropped entirely -- Stage 7 must never
    invent or guess a foreign key for stage7_research_requests.event_id
    / stage7_event_sentiment.event_id. Returns {real_event_id: event
    dict with event_id overwritten to the real id}."""
    real_id_by_ts = {r["event_ts"]: r["event_id"] for r in research_events_rows}
    return {
        real_id_by_ts[ts]: dict(event, event_id=real_id_by_ts[ts])
        for ts, event in events_by_ts.items()
        if ts in real_id_by_ts
    }


def main():
    now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = now_ts - WINDOW_MS - LOOKBACK_BUFFER_MS
    end_ts = now_ts

    # (Adversarial-review remediation, finding #3.) Checked FIRST, before
    # any other D1 read, and via a query that must itself succeed: if this
    # raises, that is a real, unrelated D1/connectivity error and propagates
    # exactly as before (never swallowed). Only a SUCCESSFUL query that
    # finds zero of the three Stage 7 tables is treated as "migration not
    # applied yet" -- see classify_stage7_schema_state()'s own docstring.
    existing_stage7_tables = {
        r["name"] for r in run_d1(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('stage7_research_requests','stage7_research_responses','stage7_event_sentiment')"
        )
    }
    schema_state = classify_stage7_schema_state(existing_stage7_tables)
    if schema_state == "NOT_APPLIED":
        result = {
            "ok": True, "status": "SKIPPED -- MIGRATION NOT APPLIED",
            "reason": "None of stage7_research_requests/stage7_research_responses/stage7_event_sentiment "
                      "exist yet. Apply .ai/migrations/0016_stage7_research_pipeline.sql to production D1, "
                      "then re-run -- see this workflow file's own ACTIVATION SEQUENCE comment.",
            "events_considered": 0, "requests_created": 0, "publish_failures": 0, "sentiment_rows_written": 0,
        }
        print(json.dumps(result))
        return result
    if schema_state == "PARTIAL":
        raise RuntimeError(
            "Stage 7 schema is PARTIALLY applied (some but not all of stage7_research_requests / "
            "stage7_research_responses / stage7_event_sentiment exist: "
            f"found {sorted(existing_stage7_tables)}). Never treated as 'migration not applied' -- fix or "
            "complete migration 0016 manually before re-running."
        )

    history_rows = run_d1(f"SELECT ts, score, technical_score, sources_json, gold_regime FROM history WHERE ts BETWEEN {start_ts} AND {end_ts} ORDER BY ts ASC")
    btc_rows = run_d1(f"SELECT ts, btc_price FROM btc_data WHERE ts BETWEEN {start_ts} AND {end_ts} ORDER BY ts ASC")
    predictions_rows = run_d1(f"SELECT ts, horizon_hours, p_up, realized_up FROM predictions WHERE ts BETWEEN {start_ts} AND {end_ts} ORDER BY ts ASC")
    research_events_rows = run_d1(f"SELECT event_id, event_ts FROM research_events WHERE event_ts BETWEEN {start_ts} AND {end_ts} ORDER BY event_ts ASC")
    event_ids = [r["event_id"] for r in research_events_rows]
    evidence_rows_all = []
    if event_ids:
        placeholders = ",".join(str(i) for i in event_ids)
        evidence_rows_all = run_d1(
            "SELECT evidence_id, event_id, feed_url, article_url, publisher, publication_ts, collection_ts, "
            f"headline, keyword_score, evidence_relation, content_hash FROM research_event_evidence WHERE event_id IN ({placeholders})"
        )

    conn = build_local_mirror(history_rows, btc_rows, predictions_rows, research_events_rows, evidence_rows_all)
    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    events_by_ts, relevance_by_ts, interpretation_by_ts = group_results_by_event(dataset)
    eligible_by_ts = select_eligible_events(events_by_ts, now_ts)
    eligible = resolve_real_event_ids(eligible_by_ts, research_events_rows)
    real_id_by_ts = {r["event_ts"]: r["event_id"] for r in research_events_rows}
    relevance_by_event = {real_id_by_ts[ts]: v for ts, v in relevance_by_ts.items() if ts in real_id_by_ts}
    interpretation_by_event = {real_id_by_ts[ts]: v for ts, v in interpretation_by_ts.items() if ts in real_id_by_ts}
    evidence_by_event = {}
    for row in evidence_rows_all:
        evidence_by_event.setdefault(row["event_id"], []).append(row)

    existing_requests = {r["event_id"]: r for r in run_d1(
        "SELECT event_id, request_id, status FROM stage7_research_requests "
        "WHERE status NOT IN ('INTEGRATED','REJECTED')"
    )}
    latest_responses = {
        r["event_id"]: r for r in run_d1(
            "SELECT req.event_id, resp.response_id, resp.request_id, resp.validation_status, "
            "resp.findings_json, resp.sources_json "
            "FROM stage7_research_responses resp "
            "JOIN stage7_research_requests req ON req.request_id = resp.request_id "
            "WHERE resp.validation_status = 'VALIDATED'"
        )
    }
    latest_sentiment = {}
    for r in run_d1("SELECT event_id, input_fingerprint FROM stage7_event_sentiment ORDER BY calculation_ts DESC"):
        latest_sentiment.setdefault(r["event_id"], r)

    requests_created, sentiment_rows_written, publish_failures = 0, 0, 0

    for event_id, event in eligible.items():
        evidence_rows = evidence_by_event.get(event_id, [])
        relevance_results = relevance_by_event.get(event_id, {})
        interpretation_results = interpretation_by_event.get(event_id, {})
        validated = latest_responses.get(event_id)
        validated_response = None
        if validated:
            validated_response = {
                "response_id": validated["response_id"],
                "validation_status": validated["validation_status"],
                "findings": json.loads(validated["findings_json"]),
                # sources_json: the AI's own self-reported sources, only
                # ever read here for a row whose validation_status is
                # already VALIDATED (this query's own WHERE clause) --
                # normalize_response_sources() re-checks validity/cutoff
                # per source, but never re-checks validation_status itself,
                # so an unvalidated response's sources must never reach
                # this dict at all (they don't: this branch is unreachable
                # for anything but a VALIDATED row).
                "sources": json.loads(validated["sources_json"]) if validated.get("sources_json") else [],
            }

        assessment = None
        if validated_response is not None:
            sufficiency_status = "SUFFICIENT"  # a validated human research response resolves the gap
        else:
            assessment = suff.assess_evidence_sufficiency(event, evidence_rows, relevance_results, interpretation_results)
            sufficiency_status = assessment["status"]

        v1_macro_context = sr.fetch_v1_macro_context(conn, event["event_ts"])
        previous = latest_sentiment.get(event_id)
        result = sr.compute_event_sentiment(
            event, evidence_rows, interpretation_results, sufficiency_status, v1_macro_context,
            validated_response=validated_response,
            previous_sentiment_id=previous.get("id") if previous else None,
            historical_cutoff_ts=event["event_ts"],
        )

        if not sr.is_idempotent_repeat(result, previous):
            d1_api_query(
                "INSERT INTO stage7_event_sentiment (event_id, calculation_ts, formula_version, "
                "evidence_sufficiency, sentiment_label, sentiment_score, v1_macro_context_json, "
                "evidence_interpretation_json, contributing_evidence_ids_json, excluded_evidence_json, "
                "duplicate_handling_json, ai_research_response_id, previous_sentiment_id, input_fingerprint) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [event_id, now_ts, result["formula_version"], result["evidence_sufficiency"],
                 result["sentiment_label"], result["sentiment_score"], json.dumps(result["v1_macro_context"]),
                 json.dumps(result["evidence_interpretation"]), json.dumps(result["contributing_evidence_ids"]),
                 json.dumps(result["excluded_evidence"]), json.dumps(result["duplicate_handling"]),
                 result["ai_research_response_id"], result["previous_sentiment_id"], result["input_fingerprint"]],
            )
            sentiment_rows_written += 1
            if validated_response is not None and event_id in existing_requests:
                # A validated human research response just drove a fresh
                # recalculation for this event -- Stage 7's own workflow ends
                # here (Step K: only the event-level recalc is normal Stage 7
                # work; any weight/code-change recommendation is separate and
                # requires its own explicit approval). The request moves to
                # INTEGRATION_REVIEW rather than a terminal state, since only
                # a human decides whether/how this result is acted on further.
                d1_api_query(
                    "UPDATE stage7_research_requests SET status = 'INTEGRATION_REVIEW', updated_ts = ? "
                    "WHERE event_id = ? AND status NOT IN ('INTEGRATED','REJECTED')",
                    [now_ts, event_id],
                )

        if sufficiency_status == "SUFFICIENT" or event_id in existing_requests:
            continue  # no new request needed -- either resolved, or one is already open

        # assessment is always populated here: reaching this line means
        # validated_response was None (the only case that skips computing
        # it above and forces sufficiency_status == "SUFFICIENT", which
        # already continued past this point).
        request_id = pub.build_request_id(event_id)
        request = {
            "request_id": request_id, "event_id": event_id, "created_ts": now_ts,
            "historical_cutoff_ts": event["event_ts"], "sufficiency_status": assessment["status"],
            "reasons": assessment["reasons"], "questions": assessment["questions"],
            "missing_categories": assessment["missing_categories"],
            "event": event, "evidence_snapshot": evidence_rows,
        }
        publish_result = pub.publish_request_file(request, repo_dir=os.getcwd())
        d1_api_query(
            "INSERT INTO stage7_research_requests (request_id, event_id, created_ts, updated_ts, "
            "schema_version, status, sufficiency_status, reasons_json, questions_json, "
            "missing_categories_json, historical_cutoff_ts, evidence_snapshot_json, github_path, "
            "github_published_ts, github_publish_error, input_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [request_id, event_id, now_ts, now_ts, pub.SCHEMA_VERSION,
             "RESEARCH_REQUEST_PUBLISHED" if publish_result["published"] else "FAILED_RETRYABLE",
             assessment["status"], json.dumps(assessment["reasons"]), json.dumps(assessment["questions"]),
             json.dumps(assessment["missing_categories"]), event["event_ts"], json.dumps(evidence_rows),
             publish_result["path"] if publish_result["published"] else None,
             now_ts if publish_result["published"] else None, publish_result["error"],
             result["input_fingerprint"]],
        )
        requests_created += 1
        if not publish_result["published"]:
            publish_failures += 1

    result = {
        "ok": True, "status": "OK", "events_considered": len(eligible), "requests_created": requests_created,
        "publish_failures": publish_failures, "sentiment_rows_written": sentiment_rows_written,
    }
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main()
