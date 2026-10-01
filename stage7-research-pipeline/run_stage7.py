#!/usr/bin/env python3
"""
Stage 7 -- AI-Assisted Internet Research & Full-Source Sentiment
Recalculation. Human-triggered orchestration script (workflow_dispatch
only -- see .github/workflows/stage7-research-pipeline.yml; no
`schedule:` trigger exists or should ever be added).

CONFIRMED HUMAN-CONTROLLED OPERATING MODEL: this script never creates a
research request and never decides to recalculate sentiment on its own
initiative. Each run does exactly three things, none of which requires
or waits on a human mid-run:
  1. PROPOSE -- assesses currently-eligible events and writes any newly
     insufficient/conflicting one as a row in stage7_research_candidates
     (status PROPOSED). A human reviews these in the app and explicitly
     selects which to research; see worker.js's createStage7ResearchRequests().
  2. PUBLISH/RETRY -- for requests a human has already created (via the
     Worker, status PENDING_RESEARCH) or that previously failed to
     publish (FAILED_RETRYABLE), attempts to commit the already-generated
     prompt file to GitHub for durability. A human can review/copy the
     prompt directly from the app without waiting for this step.
  3. RECALCULATE -- for a response a human has both VALIDATED and
     explicitly flagged via "Trigger recalculation"
     (recalculation_requested_ts IS NOT NULL), recomputes that event's
     Stage 7 sentiment. Merely registering/validating a response does
     NOT, by itself, queue a recalculation.

RESEARCH-ONLY WRITE SURFACE: stage7_research_candidates, stage7_research_
requests, stage7_event_sentiment (all new, migrations 0016/0017). Never
writes research_events, research_event_evidence (Stage 6's own tables),
history, btc_data, predictions, challenger_predictions, selection_
decisions, or any V1/V2 production table. stage7_research_requests is
ALSO written directly by the Worker (candidate selection -> request
creation, and the recalculation-trigger flag) -- see worker.js's
createStage7ResearchRequests()/triggerStage7Recalculation(); this script
only ever UPDATEs a request it did not itself create (publish/retry) or
INSERTs a stage7_event_sentiment row, never a stage7_research_requests
row directly. stage7_research_responses is written only by a human via
the Worker's POST /api/research-lab/stage7-register-response endpoint
(worker.js) -- never by this scheduled script, which only READS
responses to decide whether an explicitly-flagged, validated one now
resolves a pending request.

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

STAGING-ONLY, FAIL-CLOSED D1 TARGET (Copilot-audit remediation): this
script has no hardcoded database to talk to. validate_staging_target()
reads STAGE7_TARGET_ACCOUNT_ID / STAGE7_TARGET_DATABASE_NAME /
STAGE7_TARGET_DATABASE_ID / CLOUDFLARE_API_TOKEN from the environment and
checks them against the one expected staging target (pulseworker-v2-
staging) BEFORE main() issues its first remote D1 call -- a missing,
empty, or mismatched value (including a value that names production's own
sentiment-history database) aborts immediately, never falls back to a
default. Both run_d1() (wrangler CLI) and d1_api_query() (Cloudflare REST
API) read the SAME validated config object, never independent constants.
There is no production mode in this script at all -- see the workflow
file's own branch guard and STAGE7_STAGING_CLOUDFLARE_API_TOKEN secret for
the other half of this isolation.
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

# The ONLY staging target this script will ever accept. Named "EXPECTED_"
# rather than "DATABASE_NAME"/"D1_DATABASE_ID" etc. so it can never be
# mistaken for -- or accidentally used as -- a live default: the actual
# target used by run_d1()/d1_api_query() always comes from _TARGET_CONFIG,
# populated only after validate_staging_target() confirms the environment
# matches these values exactly.
EXPECTED_STAGING_ACCOUNT_ID = "f58e761fbc8e62dc404d8684290af264"
EXPECTED_STAGING_DATABASE_NAME = "pulseworker-v2-staging"
EXPECTED_STAGING_DATABASE_ID = "5458d504-2778-49ae-bd25-7751f1c49d50"

# Named explicitly so validate_staging_target() can reject a configured
# target that matches production BY NAME, with a clear error message,
# rather than merely failing the "doesn't match staging" check.
PRODUCTION_DATABASE_NAME = "sentiment-history"
PRODUCTION_DATABASE_ID = "f91ca980-b886-423a-bd6f-f3baea46d181"

REQUIRED_TARGET_ENV_VARS = (
    "STAGE7_TARGET_ACCOUNT_ID",
    "STAGE7_TARGET_DATABASE_NAME",
    "STAGE7_TARGET_DATABASE_ID",
    "CLOUDFLARE_API_TOKEN",
)


class StagingTargetConfig:
    """The single validated D1 target run_d1() and d1_api_query() both
    read from -- never two independently-sourced constants that could
    drift apart. Only ever constructed by validate_staging_target()."""

    def __init__(self, account_id, database_name, database_id, api_token):
        self.account_id = account_id
        self.database_name = database_name
        self.database_id = database_id
        self.api_token = api_token


def validate_staging_target(env=None):
    """Fail-closed staging-target validation. Must be called, and must
    succeed, before ANY remote D1 query or write -- including the
    schema-state check main() otherwise issues first. Reads
    STAGE7_TARGET_ACCOUNT_ID, STAGE7_TARGET_DATABASE_NAME,
    STAGE7_TARGET_DATABASE_ID, and CLOUDFLARE_API_TOKEN from `env`
    (defaults to os.environ).

    Raises RuntimeError -- never returns a partial/best-effort result --
    if any of the four is missing or empty, if the configured database
    name/id matches PRODUCTION under any name, or if the full triple
    (account_id, database_name, database_id) does not match the one
    expected staging target exactly. There is no default value for any
    of these three identifiers: an unset variable is always a hard
    failure, never a silent fallback to staging OR production.

    Returns a StagingTargetConfig on success.
    """
    env = env if env is not None else os.environ
    missing = [name for name in REQUIRED_TARGET_ENV_VARS if not (env.get(name) or "").strip()]
    if missing:
        raise RuntimeError(
            "Stage 7 refusing to run: missing/empty required staging target environment "
            f"variable(s): {', '.join(missing)}. Aborting before any remote D1 access -- "
            "see run_stage7.py's validate_staging_target()."
        )

    account_id = env["STAGE7_TARGET_ACCOUNT_ID"].strip()
    database_name = env["STAGE7_TARGET_DATABASE_NAME"].strip()
    database_id = env["STAGE7_TARGET_DATABASE_ID"].strip()
    api_token = env["CLOUDFLARE_API_TOKEN"]

    if database_name == PRODUCTION_DATABASE_NAME or database_id == PRODUCTION_DATABASE_ID:
        raise RuntimeError(
            "Stage 7 refusing to run: configured D1 target matches PRODUCTION "
            f"(database_name={database_name!r}, database_id={database_id!r}). This script "
            "must never write to the production sentiment-history database. Aborting "
            "before any remote D1 access."
        )

    expected = (EXPECTED_STAGING_ACCOUNT_ID, EXPECTED_STAGING_DATABASE_NAME, EXPECTED_STAGING_DATABASE_ID)
    if (account_id, database_name, database_id) != expected:
        raise RuntimeError(
            "Stage 7 refusing to run: configured D1 target does not match the expected "
            f"staging target. Got account_id={account_id!r}, database_name={database_name!r}, "
            f"database_id={database_id!r}; expected account_id={EXPECTED_STAGING_ACCOUNT_ID!r}, "
            f"database_name={EXPECTED_STAGING_DATABASE_NAME!r}, database_id={EXPECTED_STAGING_DATABASE_ID!r}. "
            "Aborting before any remote D1 access."
        )

    return StagingTargetConfig(account_id, database_name, database_id, api_token)


# Populated by main() via validate_staging_target() before any remote call
# -- module-level so run_d1()/d1_api_query() always read the exact same
# validated object, never two independently-resolved configs that could
# disagree. None until main() runs; run_d1()/d1_api_query() raise loudly
# if called while it is still None, rather than falling back to anything.
_TARGET_CONFIG = None

WINDOW_MS = ed.MAX_WINDOW_MS
LOOKBACK_BUFFER_MS = ed.LOOKBACK_BUFFER_MS
# The same 5-day + 1-day retry-eligibility window live_evidence_pipeline.py
# already uses for whether Stage 6 will even attempt evidence collection
# for an event -- Stage 7 processes exactly the events Stage 6 itself
# still considers live, reusing that boundary rather than inventing a
# different one.
MAX_EVENT_AGE_FOR_STAGE7_MS = 6 * 24 * 3600000


def run_d1(sql: str):
    if _TARGET_CONFIG is None:
        raise RuntimeError(
            "run_d1() called before validate_staging_target() populated the target "
            "config -- main() must validate the staging target before any remote D1 access."
        )
    result = subprocess.run(
        ["wrangler", "d1", "execute", _TARGET_CONFIG.database_name, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    return parsed[0]["results"] if parsed and parsed[0].get("results") is not None else []


def d1_api_query(sql: str, params: list):
    """Identical implementation/rationale to exp009's own d1_api_query --
    see that script's docstring for the full history of why a bound
    parameter is required here (D1's ~100,000-byte SQL statement-length
    ceiling). account_id/database_id/token all come from the SAME
    validated _TARGET_CONFIG run_d1() reads -- never independently."""
    if _TARGET_CONFIG is None:
        raise RuntimeError(
            "d1_api_query() called before validate_staging_target() populated the target "
            "config -- main() must validate the staging target before any remote D1 access."
        )
    token = _TARGET_CONFIG.api_token
    url = f"https://api.cloudflare.com/client/v4/accounts/{_TARGET_CONFIG.account_id}/d1/database/{_TARGET_CONFIG.database_id}/query"
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


def build_candidate_id(event_id, sequence=1):
    """Deterministic, not random -- same rationale as pub.build_request_id():
    a repeated propose run for the same event computes the identical id,
    which is what makes propose idempotent at the D1 layer (on top of
    idx_stage7_candidates_active_event's own uniqueness guard).

    `sequence` is 1 for an event's first-ever candidate (the original
    `stage7-cand-<event_id>` format, unchanged) and n+1 when the event already
    has n candidate rows in ANY status. A CONVERTED/STALE/DISMISSED row is kept
    forever, so re-proposing the same event (e.g. after its request was
    REJECTED, or a stale candidate whose event is eligible again) with the
    first-proposal id would collide with that retained row's PRIMARY KEY and
    crash the pipeline."""
    return f"stage7-cand-{event_id}" if sequence <= 1 else f"stage7-cand-{event_id}-{sequence}"


def build_retry_request(existing_request, event):
    """Pure. Reconstructs the exact request dict pub.publish_request_file()
    needs to (re)publish a request's file, from that row's own persisted
    columns plus the current `event` dict (event/coin/category are not
    persisted on the row itself -- see stage7_github_publisher.
    build_request_file_content()). Deliberately reuses the ORIGINAL
    created_ts/reasons/questions/missing_categories/evidence_snapshot
    exactly as first computed, never today's now_ts or freshly re-queried
    evidence -- a (re)published file must be identical to what a
    successful first attempt would have written, per evidence_snapshot_
    json's own "a stable summary at request-creation time" contract
    (migration 0016). Also passes through `prompt_text` when the row
    already has one stored (every request created via the human-controlled
    candidate flow does, worker.js's createStage7ResearchRequests()) --
    stage7_github_publisher.build_request_file_content() prefers this
    stored value over recomputing its own build_research_prompt(), which
    is what keeps the published file's prompt IDENTICAL to the one the
    human actually saw and copied, rather than a second, independently
    generated (and potentially divergent) version."""
    return {
        "request_id": existing_request["request_id"],
        "event_id": existing_request["event_id"],
        "created_ts": existing_request["created_ts"],
        "historical_cutoff_ts": existing_request["historical_cutoff_ts"],
        "sufficiency_status": existing_request["sufficiency_status"],
        "reasons": json.loads(existing_request["reasons_json"]),
        "questions": json.loads(existing_request["questions_json"]),
        "missing_categories": json.loads(existing_request["missing_categories_json"]),
        "event": event,
        "evidence_snapshot": json.loads(existing_request["evidence_snapshot_json"]),
        "prompt_text": existing_request.get("prompt_text"),
    }


def retry_stage7_request_publish(existing_request, event, now_ts):
    """Publishes (or retries publishing) a single request's file on this
    run, UPDATING that same row in place -- never INSERTing a new
    stage7_research_requests row (the partial unique index on
    (event_id) WHERE status NOT IN ('INTEGRATED','REJECTED') would reject
    one anyway while this row stays non-terminal, so this is belt-and-
    braces, not the only guard). Handles two distinct origins identically,
    since the underlying operation (publish + record the outcome) is the
    same either way:
    - PENDING_RESEARCH: a human created this request via the Worker/
      candidate flow (worker.js's createStage7ResearchRequests()) and it
      has never been attempted at all yet (publish_attempts starts at 0
      for these rows) -- this is that FIRST attempt.
    - FAILED_RETRYABLE: a previous attempt (of either origin) failed and
      is eligible for another try, per pub.decide_retry_outcome()'s own
      MAX_PUBLISH_ATTEMPTS gate.

    Goes through the exact same pub.publish_request_file() the original
    legacy auto-publish path used -- so the same branch guard (never
    main/master) and the same idempotent/never-overwrite disk-content
    checks apply identically here. The `WHERE ... AND status IN (...)` on
    the UPDATE is a defensive no-op guard against acting on a row that
    concurrently left that state (e.g. a human registered a response
    against it between this function's caller reading it and this call).

    Returns True if this attempt published successfully (including an
    idempotent "already published, unchanged" outcome), False otherwise.
    See pub.decide_retry_outcome() for the attempt-count/give-up logic."""
    request = build_retry_request(existing_request, event)
    publish_result = pub.publish_request_file(request, repo_dir=os.getcwd())
    new_status, new_attempts = pub.decide_retry_outcome(
        existing_request["publish_attempts"], publish_result["published"]
    )
    d1_api_query(
        "UPDATE stage7_research_requests SET status = ?, updated_ts = ?, publish_attempts = ?, "
        "github_path = ?, github_published_ts = ?, github_publish_error = ? "
        "WHERE request_id = ? AND status IN ('PENDING_RESEARCH', 'FAILED_RETRYABLE')",
        [new_status, now_ts, new_attempts,
         publish_result["path"] if publish_result["published"] else None,
         now_ts if publish_result["published"] else None,
         publish_result["error"],
         existing_request["request_id"]],
    )
    return publish_result["published"]


# Columns this script needs beyond 0016's tables, per the migration that adds
# them. A table-level check alone cannot see a missing COLUMN -- without this
# a staging database stuck at 0016/0017 would fail deep inside a query with an
# opaque "no such column", or (worse) silently skip recalculation bookkeeping.
REQUIRED_STAGE7_COLUMNS = {
    "stage7_research_requests": {
        "candidate_id": "0017", "prompt_text": "0017", "recalculation_requested_ts": "0017",
        "recalculation_status": "0018", "recalculation_started_ts": "0018",
        "recalculation_completed_ts": "0018", "recalculation_error": "0018",
        "recalculation_attempts": "0018", "recalculation_sentiment_id": "0018",
    },
    "stage7_research_responses": {"raw_response_text": "0017", "human_confirmed_ts": "0018"},
}

# A request whose recalculation a human asked for and that this script has not
# yet finished. NULL covers a request flagged before 0018 existed. FAILED is
# deliberately absent: a failed recalculation is never retried automatically
# (that would loop on a permanent error); a human re-requests it, which sets
# the status back to REQUESTED.
RECALC_ACTIVE_STATUSES = (None, "REQUESTED", "RUNNING")
MAX_RECALC_ERROR_CHARS = 500


# Tables introduced after 0016. Checked together with the columns so a database that is missing the candidates
# table (migration 0017) fails with an actionable message instead of a raw D1 "no such table" mid-run.
REQUIRED_STAGE7_LATER_TABLES = {"stage7_research_candidates": "0017"}


def find_missing_stage7_tables(existing_table_names):
    """Pure. [(table, migration)] for every post-0016 Stage 7 table that is absent."""
    present = set(existing_table_names)
    return [(t, m) for t, m in REQUIRED_STAGE7_LATER_TABLES.items() if t not in present]


def find_missing_stage7_columns(present_columns_by_table):
    """present_columns_by_table: {table: set(column names)} as found in the
    database. Returns [(table, column, migration)] for every required column
    that is absent -- empty when the schema is complete."""
    missing = []
    for table, columns in REQUIRED_STAGE7_COLUMNS.items():
        present = present_columns_by_table.get(table, set())
        for column, migration in columns.items():
            if column not in present:
                missing.append((table, column, migration))
    return missing


def recalculation_needs_work(response_row):
    return response_row.get("recalculation_status") in RECALC_ACTIVE_STATUSES


def mark_recalculation_running(request_id, now_ts):
    d1_api_query(
        "UPDATE stage7_research_requests SET recalculation_status = 'RUNNING', recalculation_started_ts = ?, "
        "recalculation_attempts = recalculation_attempts + 1, recalculation_error = NULL, updated_ts = ? "
        "WHERE request_id = ? AND (recalculation_status IS NULL OR recalculation_status IN ('REQUESTED','RUNNING'))",
        [now_ts, now_ts, request_id],
    )


def mark_recalculation_completed(request_id, sentiment_id, now_ts):
    # status moves to INTEGRATION_REVIEW (a human decides what, if anything,
    # follows) unless the request is already terminal.
    d1_api_query(
        "UPDATE stage7_research_requests SET recalculation_status = 'COMPLETED', recalculation_completed_ts = ?, "
        "recalculation_sentiment_id = ?, recalculation_error = NULL, updated_ts = ?, "
        "status = CASE WHEN status IN ('INTEGRATED','REJECTED') THEN status ELSE 'INTEGRATION_REVIEW' END "
        "WHERE request_id = ?",
        [now_ts, sentiment_id, now_ts, request_id],
    )


def mark_recalculation_failed(request_id, error_text, now_ts):
    d1_api_query(
        "UPDATE stage7_research_requests SET recalculation_status = 'FAILED', recalculation_completed_ts = ?, "
        "recalculation_error = ?, updated_ts = ? WHERE request_id = ?",
        [now_ts, str(error_text)[:MAX_RECALC_ERROR_CHARS], now_ts, request_id],
    )


def main():
    global _TARGET_CONFIG
    # Must be the very first thing main() does -- strictly before the
    # schema-state check below, which is otherwise the first remote D1
    # call this script makes. Raises and aborts on any missing/malformed/
    # production-pointing configuration; see validate_staging_target()'s
    # own docstring.
    _TARGET_CONFIG = validate_staging_target()

    now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Two DELIBERATELY DIFFERENT boundaries, matching exp009-event-source-
    # evidence/run_experiment.py's own established pattern (its start_ts
    # vs. fetch_start_ts) -- found to be missing here via a real local
    # end-to-end run, which is exactly the class of bug pure unit tests
    # (which never call the real detector with a real window) cannot
    # catch. event_detector._validate_window() hard-rejects any window
    # wider than WINDOW_MS (MAX_WINDOW_MS, 90 days) -- passing it a window
    # that already has LOOKBACK_BUFFER_MS added on top (98 days) makes
    # EVERY run of this script raise ValueError and crash, unconditionally.
    # detection_start_ts (exactly WINDOW_MS wide) is what actually goes to
    # build_event_source_evidence_dataset(); fetch_start_ts (the wider,
    # lookback-inclusive bound) is used ONLY for the raw SQL SELECTs below
    # that populate the local mirror -- the detector modules themselves
    # already subtract their own LOOKBACK_BUFFER_MS internally from
    # whatever start_ts they're given, so pre-subtracting it here a
    # second time was the root cause, not a defense.
    detection_start_ts = now_ts - WINDOW_MS
    fetch_start_ts = detection_start_ts - LOOKBACK_BUFFER_MS
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
                      "exist yet in the STAGING database. Apply .ai/migrations/0016, then 0017, then 0018 to it "
                      "(a human step, never automatic), then re-run.",
            "events_considered": 0, "candidates_proposed": 0, "candidates_marked_stale": 0,
            "publish_failures": 0, "sentiment_rows_written": 0, "retries_attempted": 0, "retries_succeeded": 0,
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

    present_columns = {}
    for table in REQUIRED_STAGE7_COLUMNS:
        present_columns[table] = {
            r["name"] for r in run_d1(f"SELECT name FROM pragma_table_info('{table}')")
        }
    existing_later_tables = {
        r["name"] for r in run_d1(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            f"({','.join(repr(t) for t in REQUIRED_STAGE7_LATER_TABLES)})"
        )
    }
    missing_tables = find_missing_stage7_tables(existing_later_tables)
    missing_columns = find_missing_stage7_columns(present_columns)
    if missing_tables or missing_columns:
        raise RuntimeError(
            "Stage 7 schema is behind this script: missing "
            + ", ".join([f"table {t} (migration {m})" for t, m in missing_tables]
                        + [f"column {t}.{c} (migration {m})" for t, c, m in missing_columns])
            + ". Apply the named migration(s) to the STAGING database, in order, then re-run. Never worked around."
        )

    history_rows = run_d1(f"SELECT ts, score, technical_score, sources_json, gold_regime FROM history WHERE ts BETWEEN {fetch_start_ts} AND {end_ts} ORDER BY ts ASC")
    btc_rows = run_d1(f"SELECT ts, btc_price FROM btc_data WHERE ts BETWEEN {fetch_start_ts} AND {end_ts} ORDER BY ts ASC")
    predictions_rows = run_d1(f"SELECT ts, horizon_hours, p_up, realized_up FROM predictions WHERE ts BETWEEN {fetch_start_ts} AND {end_ts} ORDER BY ts ASC")
    research_events_rows = run_d1(f"SELECT event_id, event_ts FROM research_events WHERE event_ts BETWEEN {fetch_start_ts} AND {end_ts} ORDER BY event_ts ASC")
    event_ids = [r["event_id"] for r in research_events_rows]
    evidence_rows_all = []
    if event_ids:
        placeholders = ",".join(str(i) for i in event_ids)
        evidence_rows_all = run_d1(
            "SELECT evidence_id, event_id, feed_url, article_url, publisher, publication_ts, collection_ts, "
            f"headline, keyword_score, evidence_relation, content_hash FROM research_event_evidence WHERE event_id IN ({placeholders})"
        )

    conn = build_local_mirror(history_rows, btc_rows, predictions_rows, research_events_rows, evidence_rows_all)
    dataset = join_module.build_event_source_evidence_dataset(conn, detection_start_ts, end_ts)
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
        "SELECT event_id, request_id, status, publish_attempts, created_ts, historical_cutoff_ts, "
        "sufficiency_status, reasons_json, questions_json, missing_categories_json, "
        "evidence_snapshot_json, prompt_text FROM stage7_research_requests "
        "WHERE status NOT IN ('INTEGRATED','REJECTED')"
    )}
    # PROPOSED/SELECTED candidates already open for an event -- checked so
    # a repeated propose pass never inserts a second candidate row for the
    # same event (belt-and-braces on top of idx_stage7_candidates_active_event,
    # which would reject the INSERT anyway) and so a candidate whose event
    # already has its own non-terminal request is never re-surfaced (the
    # confirmed requirement's own "an event with an open request is not
    # proposed as a duplicate").
    existing_candidates = {r["event_id"]: r for r in run_d1(
        "SELECT event_id, candidate_id, status FROM stage7_research_candidates "
        "WHERE status IN ('PROPOSED','SELECTED')"
    )}
    # How many candidate rows (ANY status) each event already has -- the next
    # proposal's sequence number, so a retained CONVERTED/STALE/DISMISSED row
    # never collides with the new row's primary key (see build_candidate_id).
    candidate_counts = {r["event_id"]: r["n"] for r in run_d1(
        "SELECT event_id, COUNT(*) AS n FROM stage7_research_candidates GROUP BY event_id"
    )}
    # (Confirmed human-controlled operating model.) A validated response no
    # longer implicitly queues a recalculation merely by existing -- only
    # once a human has explicitly clicked "Trigger recalculation" for that
    # SPECIFIC request (worker.js's triggerStage7Recalculation(), which
    # sets recalculation_requested_ts) does this query -- and therefore
    # the whole compute_event_sentiment() call below -- ever consider it.
    # Registering a response with the validated checkbox checked is, by
    # itself, no longer sufficient to trigger a recalculation on this run.
    latest_responses = {
        r["event_id"]: r for r in run_d1(
            "SELECT req.event_id, resp.response_id, resp.request_id, resp.validation_status, "
            "resp.findings_json, resp.sources_json, req.recalculation_status "
            "FROM stage7_research_responses resp "
            "JOIN stage7_research_requests req ON req.request_id = resp.request_id "
            "WHERE resp.validation_status = 'VALIDATED' AND req.recalculation_requested_ts IS NOT NULL"
        )
    }
    # id MUST be selected: previous_sentiment_id is read from it below. It was
    # previously omitted, so previous_sentiment_id was always NULL and every
    # recalculation looked like the event's first.
    latest_sentiment = {}
    for r in run_d1("SELECT id, event_id, input_fingerprint FROM stage7_event_sentiment ORDER BY calculation_ts DESC, id DESC"):
        latest_sentiment.setdefault(r["event_id"], r)

    recalculations_attempted, recalculations_completed, recalculation_failures = 0, 0, 0
    sentiment_rows_written, publish_failures = 0, 0
    retries_attempted, retries_succeeded = 0, 0
    candidates_proposed, candidates_marked_stale = 0, 0

    for event_id, event in eligible.items():
        evidence_rows = evidence_by_event.get(event_id, [])
        relevance_results = relevance_by_event.get(event_id, {})
        interpretation_results = interpretation_by_event.get(event_id, {})
        validated = latest_responses.get(event_id)
        # A human-requested recalculation that already FAILED is never retried
        # automatically (see RECALC_ACTIVE_STATUSES) -- and must not be
        # recomputed as a side effect of the baseline pass either, or a
        # permanent error would crash every later run. It stays FAILED, visible
        # in the UI, until a human re-requests it.
        if validated is not None and validated.get("recalculation_status") == "FAILED":
            continue
        needs_work = validated is not None and recalculation_needs_work(validated)
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

        if needs_work:
            recalculations_attempted += 1
            mark_recalculation_running(validated["request_id"], now_ts)

        try:
            v1_macro_context = sr.fetch_v1_macro_context(conn, event["event_ts"])
            previous = latest_sentiment.get(event_id)
            result = sr.compute_event_sentiment(
                event, evidence_rows, interpretation_results, sufficiency_status, v1_macro_context,
                validated_response=validated_response,
                previous_sentiment_id=previous.get("id") if previous else None,
                historical_cutoff_ts=event["event_ts"],
            )

            result_sentiment_id = previous.get("id") if previous else None
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
                if needs_work:
                    fingerprint_literal = str(result["input_fingerprint"]).replace("'", "''")
                    inserted = run_d1(
                        f"SELECT id FROM stage7_event_sentiment WHERE event_id = {int(event_id)} "
                        f"AND input_fingerprint = '{fingerprint_literal}' ORDER BY id DESC LIMIT 1"
                    )
                    result_sentiment_id = inserted[0]["id"] if inserted else None
                    latest_sentiment[event_id] = {"id": result_sentiment_id, "event_id": event_id,
                                                  "input_fingerprint": result["input_fingerprint"]}
            if needs_work:
                # A validated human research response drove (or, on an
                # idempotent repeat after an interrupted run, had already
                # driven) this event's recalculation. Stage 7's workflow ends
                # here: the request moves to INTEGRATION_REVIEW and any
                # weight/code-change recommendation is separate and needs its
                # own explicit approval. The bookkeeping is applied whether or
                # not a new row was inserted, so an interrupted earlier run
                # (row written, status not updated) converges instead of being
                # skipped forever as an "idempotent repeat".
                mark_recalculation_completed(validated["request_id"], result_sentiment_id, now_ts)
                recalculations_completed += 1
        except Exception as exc:
            if not needs_work:
                raise  # a baseline (not human-requested) failure is never swallowed
            # Isolate THIS request's failure so other requests still run, but
            # record it durably and surface it in the pipeline result and exit
            # status -- never a silent skip.
            mark_recalculation_failed(validated["request_id"], f"{type(exc).__name__}: {exc}", now_ts)
            recalculation_failures += 1
            print(f"RECALCULATION FAILED for request {validated['request_id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        if sufficiency_status == "SUFFICIENT":
            continue  # resolved -- no new request needed, no retry needed

        existing_request = existing_requests.get(event_id)
        if existing_request is not None:
            # A non-terminal request is already open for this event.
            # Never create a second row (the partial unique index would
            # reject it anyway) -- attempt this SAME row's publish only
            # while it is PENDING_RESEARCH (a human created it via the
            # Worker/candidate flow and it has never been published to
            # GitHub at all yet -- see build_retry_request()'s own
            # docstring) or FAILED_RETRYABLE (a previous publish attempt,
            # for either origin, failed); any other open status (already
            # published, awaiting a response, in integration review, ...)
            # needs no action here. retry_stage7_request_publish() is
            # reused unchanged for both cases -- a "first attempt" and a
            # "retry" are the identical operation (publish + record the
            # outcome), just starting from a different publish_attempts count.
            if existing_request["status"] in ("PENDING_RESEARCH", "FAILED_RETRYABLE"):
                retries_attempted += 1
                if retry_stage7_request_publish(existing_request, event, now_ts):
                    retries_succeeded += 1
            continue

        # (Confirmed human-controlled operating model.) This event has
        # insufficient/conflicting evidence and no request is open for it
        # yet -- propose it as a CANDIDATE for a human to review and
        # explicitly select, never auto-create a request or publish
        # anything here. assessment is always populated when reaching this
        # line: validated_response was None (the only case that skips
        # computing it, which forces sufficiency_status == "SUFFICIENT",
        # already continued past this point above).
        if event_id in existing_candidates:
            continue  # already proposed (PROPOSED or SELECTED) -- never a duplicate candidate row
        candidate_id = build_candidate_id(event_id, candidate_counts.get(event_id, 0) + 1)
        candidate_fingerprint = sr.compute_input_fingerprint(
            event_id, [r["evidence_id"] for r in evidence_rows], assessment["status"], None
        )
        d1_api_query(
            "INSERT INTO stage7_research_candidates (candidate_id, event_id, proposed_ts, updated_ts, "
            "status, sufficiency_status, reasons_json, questions_json, missing_categories_json, "
            "historical_cutoff_ts, evidence_snapshot_json, input_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [candidate_id, event_id, now_ts, now_ts, "PROPOSED", assessment["status"],
             json.dumps(assessment["reasons"]), json.dumps(assessment["questions"]),
             json.dumps(assessment["missing_categories"]), event["event_ts"], json.dumps(evidence_rows),
             candidate_fingerprint],
        )
        candidates_proposed += 1

    # A PROPOSED/SELECTED candidate whose event is no longer in this run's
    # own `eligible` set (aged out past MAX_EVENT_AGE_FOR_STAGE7_MS, or a
    # concurrent change already resolved it) is marked STALE -- informational,
    # never re-surfaced as an active candidate, but never silently deleted
    # either. A CONVERTED candidate is excluded from existing_candidates
    # already (its own query only selects PROPOSED/SELECTED), so this can
    # never mark one stale merely because its event is no longer eligible.
    for stale_event_id, stale_candidate in existing_candidates.items():
        if stale_event_id not in eligible:
            d1_api_query(
                "UPDATE stage7_research_candidates SET status = 'STALE', updated_ts = ? "
                "WHERE candidate_id = ? AND status IN ('PROPOSED','SELECTED')",
                [now_ts, stale_candidate["candidate_id"]],
            )
            candidates_marked_stale += 1

    # A human-requested recalculation whose event is no longer in this run's
    # eligible window cannot be computed by this pipeline version (it only
    # rebuilds evidence for events inside MAX_EVENT_AGE_FOR_STAGE7_MS). Leaving
    # it REQUESTED forever would be a silent dead end, so it is recorded as
    # FAILED with that exact reason -- visible in the UI, never hidden.
    for stuck_event_id, stuck in latest_responses.items():
        if stuck_event_id in eligible or not recalculation_needs_work(stuck):
            continue
        recalculations_attempted += 1
        mark_recalculation_failed(
            stuck["request_id"],
            "EVENT_NOT_ELIGIBLE: the event is outside the pipeline's evidence window "
            f"({MAX_EVENT_AGE_FOR_STAGE7_MS // 86400000} days), so it cannot be recalculated by this pipeline version.",
            now_ts,
        )
        recalculation_failures += 1

    result = {
        "ok": recalculation_failures == 0, "status": "OK" if recalculation_failures == 0 else "COMPLETED_WITH_FAILURES",
        "events_considered": len(eligible),
        "candidates_proposed": candidates_proposed, "candidates_marked_stale": candidates_marked_stale,
        "publish_failures": publish_failures, "sentiment_rows_written": sentiment_rows_written,
        "retries_attempted": retries_attempted, "retries_succeeded": retries_succeeded,
        "recalculations_attempted": recalculations_attempted,
        "recalculations_completed": recalculations_completed,
        "recalculation_failures": recalculation_failures,
    }
    print(json.dumps(result))
    return result


def exit_code_for(outcome):
    """Per-request recalculation failures are isolated in main() so one bad
    request does not stop the others, but the run as a whole must still be
    visibly red -- a non-zero exit fails the workflow step."""
    return 1 if outcome.get("recalculation_failures") else 0


if __name__ == "__main__":
    sys.exit(exit_code_for(main()))
