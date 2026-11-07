"""
Experiment 5: D1-query-fn-based orchestration layer, mirroring
live_evidence_pipeline.py's own established shape exactly -- this
module's only I/O is through two injected functions, `d1_query_fn(sql)
-> list[dict]` and `d1_execute_fn(sql) -> None`, so it is fully testable
with fakes and never opens a real network connection itself. The actual
wrangler-based adapter lives in scripts/experiment5-agent/run.py, a thin
I/O shim with no logic of its own (same split as
scripts/live-evidence-pipeline/run.py).

Why a local, throwaway sqlite mirror, not a live D1 connection
------------------------------------------------------------------
experiment5_agent.py / sentiment_archive.py are written against a
sqlite3.Connection-shaped interface (conn.execute(...).fetchall(),
cursor.lastrowid) because that is what every existing research/*.py
module in this project already assumes (source_analysis.py,
hypothesis_gate.py, outcome_engine.py) and what every existing test
fixture already builds. D1 over wrangler has no persistent connection
object at all -- each call is a separate subprocess round trip. This
module therefore follows live_evidence_pipeline.py's own precedent
exactly: pull a bounded window of real rows from D1 via d1_query_fn,
replay them into a local in-memory sqlite3 mirror, run the existing
pure/tested logic against that mirror, then translate whatever new rows
resulted back into explicit INSERT/UPDATE statements executed via
d1_execute_fn. Nothing here ever mutates the mirror's data before
replaying it -- rows are copied verbatim.

Idempotency against real D1
------------------------------
Because the local mirror starts empty on every run, sentiment_archive's
own idempotency check (comparing against an existing row) cannot see
what a PRIOR run already archived. This module therefore pre-fetches
the set of observation_ts values already present in the real
research_sentiment_archive table (mirroring
live_evidence_pipeline.find_existing_events_without_evidence's own
"ask D1 what's already known before doing anything" pattern) and skips
building an archive INSERT for any ts already covered -- the real
idempotency guarantee lives at this layer, not inside the throwaway
mirror.

Steady-state observation window (fix: mirror must also carry history,
not just this cycle's delta)
------------------------------------------------------------------------
agent.run_agent_cycle() observes the mirror's OWN
research_sentiment_archive table over agent.DEFAULT_OBSERVE_WINDOW_MS
(14 days) -- but until this fix, the mirror only ever received rows
this exact run newly archived (see the loop below), never rows a PRIOR
run already archived into real D1. In steady state (once the initial
backlog is archived and newly_archived is usually 0-few rows per
cycle), that left the agent observing only this cycle's tiny fresh
delta instead of the intended 14-day rolling window -- confirmed
directly against two real, unmasked production runs (the first
archived 422 rows and classified normally; 89 minutes later, the
second archived 0 new rows and returned INSUFFICIENT_ARCHIVE_DATA
despite 422 real rows sitting in production). The fix: also replay the
real, already-archived rows within the observe window (a second
bounded, indexed D1 read -- see historical_archive_rows below) into the
mirror verbatim, exactly like history_rows/btc_rows already are. This
changes only what the agent OBSERVES; it does not touch confirmation
criteria, classification, candidate-source rules, decision creation,
eligibility timing, outcome resolution, or the horizon tolerance in
experiment5_agent.py, none of which are modified by this change.

Migration status, stated plainly
-----------------------------------
research_sentiment_archive (migration 0015) and research_hypotheses
(migration 0008) are both proposed, not yet applied to production D1 --
confirmed directly (a live, read-only query against production found no
such table for research_hypotheses; migration 0015 is new in this same
change). Running scripts/experiment5-agent/run.py against production
today would therefore fail at the very first real D1 query, by design
-- this module and its wiring are built and tested now; applying either
migration is a separate, later, explicitly-authorized deployment step,
per this project's own established process (see hypothesis_gate.py's
own module docstring for the identical situation with migration 0008).
"""
import json
import sqlite3
import sys

import experiment5_agent as agent
import sentiment_archive as sa

ARCHIVE_WINDOW_MS = 30 * 24 * 3600000
# Bounded read window per run -- comfortably covers the agent's own
# DEFAULT_OBSERVE_WINDOW_MS (14 days) with margin, while staying a small,
# indexed D1 read (research_sentiment_archive.observation_ts and
# history.ts are both indexed) rather than an unbounded table scan.

BTC_DATA_WINDOW_MS = agent.DEFAULT_OBSERVE_WINDOW_MS + (25 * 3600000)
# Slightly wider than the observe window so evaluate_pending_decisions
# has enough trailing btc_data to resolve a 24h-horizon outcome for a
# decision anchored near the observe window's own start.

PIPELINE_VERSION = "experiment5-pipeline-v2"
RUN_TABLE = "experiment5_pipeline_runs"
MAX_REJECTED_SAMPLE = 10
MAX_ERROR_CHARS = 500

NEW_DECISION_DEFERRED_MARKER = "DEFERRED_TO_NEXT_RUN"
# LOCAL-mirror-only sentinel. Never written to real D1: the real INSERT for each new decision is
# built before it is applied, and always carries out_of_sample_status NULL.

ARCHIVE_COLUMNS = ["observation_ts", "sources_json", "score", "technical_score", "btc_price",
                    "gold_regime", "source_weights_version", "schema_version", "written_by",
                    "content_hash", "archived_ts"]
# The single source of truth for research_sentiment_archive's own
# (non-archive_id) column list, shared by build_insert_archive_sql, the
# historical-replay read, and the newly-archived read below -- so the
# three can never silently drift apart.


def classify_sources_json(raw):
    """Pure. How an observation's sources_json should be treated:
    'EMPTY' (None/blank/'{}': nothing to classify, kept), 'MALFORMED' (not valid JSON) or
    'NOT_AN_OBJECT' (valid JSON but not a source-id -> value mapping): both rejected, and
    'OK'. A rejected observation is EXCLUDED from the agent's input and reported -- it never
    aborts the run (get_archive_range json.loads each row), and it is never rewritten or
    deleted from D1 (append-only)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "EMPTY"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return "MALFORMED"
    if not isinstance(parsed, dict):
        return "NOT_AN_OBJECT"
    return "OK" if parsed else "EMPTY"


def decision_key(evidence_summary_json, subject):
    """Pure. The natural identity of a decision: (subject, anchor_ts). The same observation can only ever give
    one decision per subject, whatever number of times the pipeline runs. None for a payload that is not valid
    JSON / has no anchor (such a row simply cannot suppress a new decision; it is never rewritten)."""
    try:
        anchor = json.loads(evidence_summary_json)["decision"]["anchor_ts"]
    except (TypeError, ValueError, KeyError):
        return None
    return (subject, anchor) if isinstance(anchor, (int, float)) and not isinstance(anchor, bool) else None


def _sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def build_insert_archive_sql(archive_row):
    """archive_row: a dict shaped like sentiment_archive.get_archive_range()'s
    own output (from the LOCAL mirror, where archive_observation() has
    already computed content_hash etc). Builds the literal INSERT this
    module replicates to real D1."""
    values = [archive_row[c] for c in ARCHIVE_COLUMNS]
    return (f"INSERT INTO research_sentiment_archive ({', '.join(ARCHIVE_COLUMNS)}) VALUES "
            f"({', '.join(_sql_literal(v) for v in values)})")


def build_insert_decision_sql(subject, statement, source_analysis_ids_json, status,
                               evidence_summary_json, created_ts):
    columns = ["created_ts", "last_updated_ts", "subject", "statement", "source_analysis_ids",
               "status", "evidence_summary_json", "out_of_sample_status"]
    values = [created_ts, created_ts, subject, statement, source_analysis_ids_json, status,
              evidence_summary_json, None]
    return (f"INSERT INTO research_hypotheses ({', '.join(columns)}) VALUES "
            f"({', '.join(_sql_literal(v) for v in values)})")


def build_update_decision_outcome_sql(hypothesis_id, evidence_summary_json, out_of_sample_status, last_updated_ts):
    return (
        "UPDATE research_hypotheses SET "
        f"evidence_summary_json = {_sql_literal(evidence_summary_json)}, "
        f"out_of_sample_status = {_sql_literal(out_of_sample_status)}, "
        f"last_updated_ts = {_sql_literal(last_updated_ts)} "
        f"WHERE hypothesis_id = {_sql_literal(hypothesis_id)} "
        # Append-only resolution: only an UNRESOLVED Experiment 5 row may be resolved. A row someone else
        # (an overlapping run, a manual edit) already resolved keeps its FIRST outcome, and a row that is not an
        # Experiment 5 decision can never be touched even if an id were ever wrong.
        "AND subject LIKE 'experiment5:%' AND out_of_sample_status IS NULL"
    )


def _build_local_mirror(history_rows, btc_rows, archived_observation_ts, now_ts,
                         historical_archive_rows=(), diagnostics=None):
    """A fresh, throwaway in-memory sqlite3 mirror -- never the real D1
    connection. history_rows/btc_rows are already-fetched real
    production rows (list[dict], from d1_query_fn); archived_observation_ts
    is the set of ts values already present in the REAL
    research_sentiment_archive table, used only to decide which history
    rows still need archiving (see module docstring "Idempotency against
    real D1"). now_ts is this pipeline run's own actual wall-clock time
    -- passed through as archived_ts (when the observation was actually
    captured into the archive), never the observation's own ts (when the
    observation itself occurred). Conflating the two would make
    archived_ts a mere copy of observation_ts, losing its purpose: a
    later backfill/catch-up run archiving an old observation should
    record TODAY as archived_ts, not the old observation's own moment.

    historical_archive_rows: real rows already present in
    research_sentiment_archive within the agent's observe window (see
    module docstring "Steady-state observation window"), replayed
    verbatim -- same "copied, never mutated" discipline as
    history_rows/btc_rows. Every row here is, by construction of its own
    caller (run_pipeline), one whose observation_ts IS in
    archived_observation_ts, so the loop below (which only archives a
    history row when its ts is NOT in archived_observation_ts) can never
    target the same ts -- the two sets are structurally disjoint. The
    mirror's own UNIQUE index on observation_ts is kept as a hard
    backstop regardless: if that invariant were ever violated, this
    raises sqlite3.IntegrityError rather than silently duplicating a
    row, matching sentiment_archive.archive_observation()'s own
    "never silently overwrite" discipline."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE research_sentiment_archive (
        archive_id INTEGER PRIMARY KEY AUTOINCREMENT, observation_ts INTEGER NOT NULL,
        sources_json TEXT, score REAL, technical_score REAL, btc_price REAL, gold_regime TEXT,
        source_weights_version TEXT NOT NULL, schema_version TEXT NOT NULL, written_by TEXT NOT NULL,
        content_hash TEXT NOT NULL, archived_ts INTEGER NOT NULL
    )""")
    conn.execute("CREATE UNIQUE INDEX idx_archive_ts ON research_sentiment_archive(observation_ts)")
    conn.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL,
        sources_json TEXT, technical_score INTEGER, gold_regime TEXT
    )""")
    conn.execute("""CREATE TABLE btc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
    )""")
    conn.execute("""CREATE TABLE research_hypotheses (
        hypothesis_id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts INTEGER NOT NULL,
        last_updated_ts INTEGER NOT NULL, subject TEXT NOT NULL, statement TEXT NOT NULL,
        source_analysis_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OBSERVATION',
        evidence_summary_json TEXT, out_of_sample_status TEXT
    )""")

    rejected_ts = diagnostics["rejected_ts"] if diagnostics is not None else set()

    for row in historical_archive_rows:
        if classify_sources_json(row["sources_json"]) in ("MALFORMED", "NOT_AN_OBJECT"):
            rejected_ts.add(row["observation_ts"])  # stays untouched in D1; just not fed to the agent
            continue
        conn.execute(
            f"INSERT INTO research_sentiment_archive ({', '.join(ARCHIVE_COLUMNS)}) VALUES "
            f"({', '.join('?' for _ in ARCHIVE_COLUMNS)})",
            [row[c] for c in ARCHIVE_COLUMNS],
        )

    for row in history_rows:
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?, ?, ?, ?, ?)",
                     (row["ts"], row["score"], row.get("sources_json"), row.get("technical_score"), row.get("gold_regime")))
        if classify_sources_json(row.get("sources_json")) in ("MALFORMED", "NOT_AN_OBJECT"):
            rejected_ts.add(row["ts"])
            continue
        if row["ts"] not in archived_observation_ts:
            sa.archive_observation(
                conn, row["ts"], row.get("sources_json") or "{}", row["score"],
                technical_score=row.get("technical_score"), btc_price=row.get("btc_price"),
                gold_regime=row.get("gold_regime"), source_weights_version="v1-unversioned",
                written_by="experiment5-pipeline", archived_ts=now_ts,
            )
    for row in btc_rows:
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (row["ts"], row["btc_price"]))
    conn.commit()
    return conn


def run_pipeline(d1_query_fn, d1_execute_fn, now_ts):
    """The single entry point. Read-only against `history`/`btc_data`/
    `research_sentiment_archive`/`research_hypotheses` via d1_query_fn;
    all real writes go through d1_execute_fn, built by
    build_insert_archive_sql / build_insert_decision_sql /
    build_update_decision_outcome_sql -- this function never hand-builds
    SQL inline, mirroring live_evidence_pipeline.run_pipeline's own
    discipline."""
    history_rows = d1_query_fn(
        f"SELECT ts, score, sources_json, technical_score, gold_regime FROM history "
        f"WHERE ts >= {now_ts - ARCHIVE_WINDOW_MS} ORDER BY ts ASC"
    )
    btc_rows = d1_query_fn(
        f"SELECT ts, btc_price FROM btc_data WHERE ts >= {now_ts - BTC_DATA_WINDOW_MS} ORDER BY ts ASC"
    )
    archived_ts_rows = d1_query_fn(
        f"SELECT observation_ts FROM research_sentiment_archive WHERE observation_ts >= {now_ts - ARCHIVE_WINDOW_MS}"
    )
    archived_observation_ts = {r["observation_ts"] for r in archived_ts_rows}
    # Bounded to the agent's own observe window (14 days), narrower than
    # ARCHIVE_WINDOW_MS above (30 days) -- this is the fix for the
    # steady-state gap documented in the module docstring: without this,
    # run_agent_cycle's mirror only ever contains this cycle's fresh
    # delta, never the real history already sitting in production.
    historical_archive_rows = d1_query_fn(
        f"SELECT {', '.join(ARCHIVE_COLUMNS)} FROM research_sentiment_archive "
        f"WHERE observation_ts >= {now_ts - agent.DEFAULT_OBSERVE_WINDOW_MS} AND observation_ts <= {now_ts} "
        f"ORDER BY observation_ts ASC"
    )
    pending_decision_rows = d1_query_fn(
        "SELECT hypothesis_id, evidence_summary_json FROM research_hypotheses "
        "WHERE subject LIKE 'experiment5:%' AND out_of_sample_status IS NULL"
    )

    # Identity of every decision created inside the observe window (resolved ones included): a new decision whose
    # (subject, anchor_ts) is already here is a repeat of the same observation and is NOT persisted again. Bounded by
    # created_ts -- a decision can only share an anchor with a new one if it was created at or after that anchor.
    existing_decision_rows = d1_query_fn(
        "SELECT subject, evidence_summary_json FROM research_hypotheses "
        f"WHERE subject LIKE 'experiment5:%' AND created_ts >= {now_ts - agent.DEFAULT_OBSERVE_WINDOW_MS}"
    )
    existing_decision_keys = {decision_key(r["evidence_summary_json"], r["subject"]) for r in existing_decision_rows}
    existing_decision_keys.discard(None)

    diagnostics = {"rejected_ts": set()}
    local_conn = _build_local_mirror(
        history_rows, btc_rows, archived_observation_ts, now_ts,
        historical_archive_rows=historical_archive_rows, diagnostics=diagnostics,
    )
    # Every diagnostic about "what the agent saw" describes the AGENT'S OBSERVE WINDOW (the same bounds
    # experiment5_agent.observe() uses). The mirror also holds older rows (the 30-day archiving read window), which
    # the agent never observes; counting them here made `observed + rejected` describe no real set.
    observe_lo = now_ts - agent.DEFAULT_OBSERVE_WINDOW_MS
    observed_rows = local_conn.execute(
        "SELECT sources_json FROM research_sentiment_archive WHERE observation_ts >= ? AND observation_ts <= ?",
        (observe_lo, now_ts),
    ).fetchall()
    observations_without_sources = sum(1 for (raw,) in observed_rows if classify_sources_json(raw) == "EMPTY")
    rejected_in_window = sorted(t for t in diagnostics["rejected_ts"] if observe_lo <= t <= now_ts)
    rejected_outside_window = len(diagnostics["rejected_ts"]) - len(rejected_in_window)

    # Iterates every mirror archive row -- both the historical replay
    # above and whatever this cycle newly archived. Only a row whose ts
    # is NOT already in archived_observation_ts is genuinely new and
    # gets written back to real D1; every historical row is, by
    # construction, already in that set, so it is read here (confirming
    # it round-tripped correctly) but never re-written.
    newly_archived = 0
    for row in local_conn.execute(
        f"SELECT {', '.join(ARCHIVE_COLUMNS)} FROM research_sentiment_archive"
    ).fetchall():
        archive_row = dict(zip(ARCHIVE_COLUMNS, row))
        if archive_row["observation_ts"] not in archived_observation_ts:
            d1_execute_fn(build_insert_archive_sql(archive_row))
            newly_archived += 1

    # Replay any decisions from PRIOR runs (fetched from real D1 above)
    # into the local mirror BEFORE run_agent_cycle() creates any new
    # decisions this cycle -- and BEFORE using their real hypothesis_id
    # explicitly. SQLite's AUTOINCREMENT bookkeeping (sqlite_sequence)
    # tracks the highest ROWID ever inserted into this table, whether
    # inserted explicitly (as here) or via a bare autoincrement insert
    # (as run_agent_cycle's own persist_experiment5_decision does below)
    # -- so registering these real IDs first guarantees every
    # subsequently auto-assigned local ID is strictly higher than any of
    # them, making a collision with a real hypothesis_id structurally
    # impossible, regardless of what those real ID values are.
    #
    # Previously this replay ran AFTER run_agent_cycle. Because the local
    # mirror is fresh (empty) on every single pipeline run, its
    # AUTOINCREMENT sequence always starts back at 1 -- so a newly
    # created decision could be locally assigned an ID (e.g. 1) that
    # collided with an already-used REAL hypothesis_id (e.g. also 1,
    # likely early in this table's life) being replayed afterwards,
    # raising sqlite3.IntegrityError and -- masked by the GitHub Actions
    # step's continue-on-error -- silently halting the pipeline.
    for row in pending_decision_rows:
        local_conn.execute(
            "INSERT INTO research_hypotheses (hypothesis_id, created_ts, last_updated_ts, subject, "
            "statement, source_analysis_ids, status, evidence_summary_json, out_of_sample_status) "
            "VALUES (?, ?, ?, 'experiment5:replayed', 'x', '[]', 'OBSERVATION', ?, NULL)",
            (row["hypothesis_id"], now_ts, now_ts, row["evidence_summary_json"]),
        )
    local_conn.commit()

    cycle_result = agent.run_agent_cycle(local_conn, as_of_ts=now_ts, created_ts=now_ts)
    new_local_decision_ids = list(cycle_result.get("decision_ids", []))
    decisions_skipped_duplicate = 0
    for hypothesis_id in new_local_decision_ids:
        row = local_conn.execute(
            "SELECT subject, statement, source_analysis_ids, status, evidence_summary_json "
            "FROM research_hypotheses WHERE hypothesis_id = ?",
            (hypothesis_id,),
        ).fetchone()
        subject, statement, source_analysis_ids_json, status, evidence_summary_json = row
        key = decision_key(evidence_summary_json, subject)
        if key is not None and key in existing_decision_keys:
            decisions_skipped_duplicate += 1  # same observation, same subject: already persisted by an earlier run
            continue
        if key is not None:
            existing_decision_keys.add(key)
        d1_execute_fn(build_insert_decision_sql(
            subject, statement, source_analysis_ids_json, status, evidence_summary_json, now_ts,
        ))

    # A decision created in THIS run exists locally under a throwaway AUTOINCREMENT id, while real
    # D1 assigns its own id on INSERT (build_insert_decision_sql carries none). If such a decision
    # were also evaluated below -- possible in a delayed/catch-up run whose newest observation is
    # already older than the horizon -- build_update_decision_outcome_sql would target the LOCAL id
    # and could overwrite a DIFFERENT real row (reproduced in
    # test_experiment5_pipeline.TestSameRunDecisionIdAliasing). So new decisions are marked
    # locally as deferred: evaluate_pending_decisions only considers out_of_sample_status IS NULL,
    # and the next run replays them from D1 under their REAL ids and evaluates them there.
    if new_local_decision_ids:
        placeholders = ",".join("?" for _ in new_local_decision_ids)
        local_conn.execute(
            f"UPDATE research_hypotheses SET out_of_sample_status = ? WHERE hypothesis_id IN ({placeholders})",
            [NEW_DECISION_DEFERRED_MARKER, *new_local_decision_ids],
        )
        local_conn.commit()

    # evaluate_pending_decisions reads every research_hypotheses row
    # matching subject LIKE 'experiment5:%' AND out_of_sample_status IS
    # NULL. That is now ONLY the decisions replayed from prior runs under
    # their real ids: this cycle's own new decisions were marked
    # NEW_DECISION_DEFERRED_MARKER above (their local ids are not their
    # real D1 ids, so they must never be the target of an outcome UPDATE)
    # and are evaluated by the next run. Only genuinely due, replayed
    # decisions get evaluated here.
    evaluation = agent.evaluate_pending_decisions(
        local_conn, as_of_ts=now_ts, horizon_hours=agent.EXPERIMENT5_TARGET_HORIZON_HOURS,
    )
    for result in evaluation["results"]:
        row = local_conn.execute(
            "SELECT evidence_summary_json, out_of_sample_status, last_updated_ts FROM research_hypotheses WHERE hypothesis_id = ?",
            (result["hypothesis_id"],),
        ).fetchone()
        evidence_summary_json, out_of_sample_status, last_updated_ts = row
        d1_execute_fn(build_update_decision_outcome_sql(
            result["hypothesis_id"], evidence_summary_json, out_of_sample_status, last_updated_ts,
        ))

    local_conn.close()

    def _outcome_status(result):
        if result["agent_correct"] is True:
            return "PASSED_HOLDOUT"
        if result["agent_correct"] is False:
            return "FAILED_HOLDOUT"
        return "INSUFFICIENT_DATA_FOR_HOLDOUT"

    outcome_counts = {"PASSED_HOLDOUT": 0, "FAILED_HOLDOUT": 0, "INSUFFICIENT_DATA_FOR_HOLDOUT": 0}
    for result in evaluation["results"]:
        outcome_counts[_outcome_status(result)] += 1

    return {
        "status": "OK",
        "pipeline_version": PIPELINE_VERSION,
        "history_rows_read": len(history_rows),
        "btc_rows_read": len(btc_rows),
        "newly_archived": newly_archived,
        "archive_rows_observed": len(observed_rows),
        "observations_without_sources": observations_without_sources,
        "observations_rejected_malformed": len(rejected_in_window),
        "observations_rejected_malformed_outside_observe_window": rejected_outside_window,
        "rejected_observation_ts_sample": rejected_in_window[:MAX_REJECTED_SAMPLE],
        "agent_cycle": {k: v for k, v in cycle_result.items() if k != "decision_ids"},
        "decisions_created": len(cycle_result.get("decision_ids", [])) - decisions_skipped_duplicate,
        "decisions_skipped_duplicate": decisions_skipped_duplicate,
        "decisions_replayed": len(pending_decision_rows),
        "decisions_evaluated": evaluation["n_evaluated"],
        "evaluation_outcomes": outcome_counts,
    }


def _constants_snapshot():
    return {
        "archive_window_ms": ARCHIVE_WINDOW_MS,
        "observe_window_ms": agent.DEFAULT_OBSERVE_WINDOW_MS,
        "btc_data_window_ms": BTC_DATA_WINDOW_MS,
        "target_horizon_hours": agent.EXPERIMENT5_TARGET_HORIZON_HOURS,
        "horizon_tolerance_ms": agent.EXPERIMENT5_HORIZON_TOLERANCE_MS,
        "max_decisions_per_cycle": agent.MAX_DECISIONS_PER_CYCLE,
    }


def build_insert_run_sql(run_ts, status, summary, error_text):
    summary = summary or {}
    cycle = summary.get("agent_cycle") or {}
    outcomes = summary.get("evaluation_outcomes") or {}
    columns = [
        "run_ts", "status", "error_text", "pipeline_version", "constants_json", "history_rows_read",
        "btc_rows_read", "newly_archived", "archive_rows_observed", "observations_without_sources",
        "observations_rejected_malformed", "observations_rejected_outside_window", "rejected_observation_ts_json", "sources_observed",
        "candidate_new_sources_json", "agent_status", "decisions_replayed", "decisions_created",
        "decisions_skipped_duplicate", "decisions_evaluated", "evaluated_passed", "evaluated_failed", "evaluated_inconclusive",
    ]
    values = [
        run_ts, status, None if error_text is None else str(error_text)[:MAX_ERROR_CHARS], PIPELINE_VERSION,
        json.dumps(_constants_snapshot(), sort_keys=True), summary.get("history_rows_read"),
        summary.get("btc_rows_read"), summary.get("newly_archived"), summary.get("archive_rows_observed"),
        summary.get("observations_without_sources"), summary.get("observations_rejected_malformed"),
        summary.get("observations_rejected_malformed_outside_observe_window"),
        json.dumps(summary.get("rejected_observation_ts_sample") or []), cycle.get("n_sources_observed"),
        json.dumps(cycle.get("candidate_new_sources") or []), cycle.get("status"),
        summary.get("decisions_replayed"), summary.get("decisions_created"), summary.get("decisions_skipped_duplicate"),
        summary.get("decisions_evaluated"),
        outcomes.get("PASSED_HOLDOUT"), outcomes.get("FAILED_HOLDOUT"), outcomes.get("INSUFFICIENT_DATA_FOR_HOLDOUT"),
    ]
    return (f"INSERT INTO {RUN_TABLE} ({', '.join(columns)}) VALUES "
            f"({', '.join(_sql_literal(v) for v in values)})")


def run_table_exists(d1_query_fn):
    return bool(d1_query_fn(f"SELECT name FROM sqlite_master WHERE type = 'table' AND name = '{RUN_TABLE}'"))


def record_run(d1_query_fn, d1_execute_fn, run_ts, status, summary, error_text):
    """Writes this execution's operational record. Returns 'WRITTEN', or 'SKIPPED_TABLE_MISSING' when
    migration 0019 is not applied (an explicit capability check, not a swallowed error: the run is
    reported as unrecorded, never as recorded)."""
    if not run_table_exists(d1_query_fn):
        return "SKIPPED_TABLE_MISSING"
    d1_execute_fn(build_insert_run_sql(run_ts, status, summary, error_text))
    return "WRITTEN"


def run_pipeline_recorded(d1_query_fn, d1_execute_fn, now_ts):
    """run_pipeline() plus a persisted operational record.

    * Pipeline FAILS: the failure is recorded best-effort and then RE-RAISED -- the job must still go red; the
      record exists so the app can show the failure and the consecutive-failure count, not to hide it. A failure
      to write that record is reported on stderr and NEVER masks the original error.
    * Pipeline SUCCEEDS but the record cannot be written: the experiment's own data was written correctly (and is
      idempotent), so the run is not turned into a failure. The summary says so explicitly --
      run_record = "WRITE_FAILED" plus run_record_error -- and the caller must surface it loudly. It is never
      reported as WRITTEN. The Worker's overdue flag covers a prolonged loss of telemetry.
    * Telemetry table missing (migration 0019 not applied): run_record = "SKIPPED_TABLE_MISSING" -- the run is
      reported as unrecorded, never as recorded."""
    try:
        summary = run_pipeline(d1_query_fn, d1_execute_fn, now_ts)
    except Exception as exc:
        try:
            record_run(d1_query_fn, d1_execute_fn, now_ts, "FAILED", None, f"{type(exc).__name__}: {exc}")
        except Exception as record_exc:  # already propagating the real error; never mask it
            print(f"ALSO FAILED to record the failed run: {type(record_exc).__name__}: {record_exc}", file=sys.stderr)
        raise
    try:
        summary["run_record"] = record_run(d1_query_fn, d1_execute_fn, now_ts, "OK", summary, None)
    except Exception as record_exc:
        summary["run_record"] = "WRITE_FAILED"
        summary["run_record_error"] = f"{type(record_exc).__name__}: {record_exc}"[:MAX_ERROR_CHARS]
        print(f"TELEMETRY WRITE FAILED (the pipeline itself succeeded): {summary['run_record_error']}", file=sys.stderr)
    return summary
