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
import sqlite3

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
    columns = ["observation_ts", "sources_json", "score", "technical_score", "btc_price",
               "gold_regime", "source_weights_version", "schema_version", "written_by",
               "content_hash", "archived_ts"]
    values = [archive_row[c] for c in columns]
    return (f"INSERT INTO research_sentiment_archive ({', '.join(columns)}) VALUES "
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
        f"WHERE hypothesis_id = {_sql_literal(hypothesis_id)}"
    )


def _build_local_mirror(history_rows, btc_rows, archived_observation_ts, now_ts):
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
    record TODAY as archived_ts, not the old observation's own moment."""
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

    for row in history_rows:
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?, ?, ?, ?, ?)",
                     (row["ts"], row["score"], row.get("sources_json"), row.get("technical_score"), row.get("gold_regime")))
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
    pending_decision_rows = d1_query_fn(
        "SELECT hypothesis_id, evidence_summary_json FROM research_hypotheses "
        "WHERE subject LIKE 'experiment5:%' AND out_of_sample_status IS NULL"
    )

    local_conn = _build_local_mirror(history_rows, btc_rows, archived_observation_ts, now_ts)

    newly_archived = 0
    for row in local_conn.execute(
        "SELECT observation_ts, sources_json, score, technical_score, btc_price, gold_regime, "
        "source_weights_version, schema_version, written_by, content_hash, archived_ts "
        "FROM research_sentiment_archive"
    ).fetchall():
        columns = ["observation_ts", "sources_json", "score", "technical_score", "btc_price",
                   "gold_regime", "source_weights_version", "schema_version", "written_by",
                   "content_hash", "archived_ts"]
        archive_row = dict(zip(columns, row))
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
    for hypothesis_id in cycle_result.get("decision_ids", []):
        row = local_conn.execute(
            "SELECT subject, statement, source_analysis_ids, status, evidence_summary_json "
            "FROM research_hypotheses WHERE hypothesis_id = ?",
            (hypothesis_id,),
        ).fetchone()
        subject, statement, source_analysis_ids_json, status, evidence_summary_json = row
        d1_execute_fn(build_insert_decision_sql(
            subject, statement, source_analysis_ids_json, status, evidence_summary_json, now_ts,
        ))

    # evaluate_pending_decisions reads every research_hypotheses row
    # matching subject LIKE 'experiment5:%' AND out_of_sample_status IS
    # NULL -- this now sees both the just-replayed prior-run decisions
    # above and any decision run_agent_cycle just created this cycle.
    # The latter always have an anchor_ts at or near now_ts, so their
    # own horizon can never have resolved yet; only genuinely due,
    # replayed decisions actually get evaluated here. Order relative to
    # the replay above does not affect this step's own correctness --
    # only the ID-collision fix above depends on ordering.
    evaluation = agent.evaluate_pending_decisions(local_conn, as_of_ts=now_ts, horizon_hours=24)
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

    return {
        "status": "OK",
        "history_rows_read": len(history_rows),
        "btc_rows_read": len(btc_rows),
        "newly_archived": newly_archived,
        "agent_cycle": {k: v for k, v in cycle_result.items() if k != "decision_ids"},
        "decisions_created": len(cycle_result.get("decision_ids", [])),
        "decisions_evaluated": evaluation["n_evaluated"],
    }
