#!/usr/bin/env python3
"""
Experiment 10 -- Source Dialogue Validation.

RESEARCH ONLY. Research question: "When two existing V1 sources observe
information around the same market event, do they support, contradict,
or remain insufficiently comparable to each other after applying strict
temporal as-of rules?"

Validates the merged, independently-audited Source Dialogue Engine
(research/source_dialogue.py, PR #68) against real V1/V2 historical
data BEFORE any new external source (EIA, GDELT -- both still PRIMARY
VERIFICATION BLOCKED) is ever wired into it. Uses ONLY existing V1/V2
research data -- no synthetic provider data, no EIA, no GDELT.

This script:
  1. Fetches real, bounded (<=90 days -- research/event_detector.py's own
     MAX_WINDOW_MS, unchanged) history + btc_data + predictions from
     production D1 (read-only).
  2. Runs research/exp010_source_dialogue_validation.py's own
     build_relationship_dataset() and build_redundancy_dataset() --
     UNCHANGED, imported not reimplemented -- against a throwaway local
     sqlite mirror of that data. Those functions themselves reuse
     event_source_relevance.py (PR59, event detection + V1 snapshots),
     event_source_reaction.py (PR57, source-median direction reference),
     source_analysis.py (PR5, source enumeration + redundancy), and
     research/source_dialogue.py (the LOCKED engine, PR #68) -- all
     completely unchanged.
  3. Persists a size-safe SUMMARY report (descriptive counts only, per
     the same payload-size discipline EXP-009 was forced to adopt
     reactively at ~1.3MB -- applied proactively here) into the
     EXISTING research_analyses table, tagged
     subject='EXP-010:source_dialogue_validation'.

It NEVER writes to history, btc_data, predictions, selection_decisions,
or any V1/V2 production table. Its only write target is
research_analyses, and only ever an INSERT.

Zero new statistical logic and zero new classification vocabulary --
every label comes from the locked Source Dialogue Engine's own
RELATIONSHIP_LABELS/REDUNDANCY_LABELS, never re-typed here except as
literal string comparisons already used by the reused modules
themselves.

Governance: produces research descriptors only (SUPPORTING/
CONTRADICTING/DIFFERENT_TIMING/INSUFFICIENT_EVIDENCE and
REDUNDANCY_UNRESOLVED/NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED/
INSUFFICIENT_EVIDENCE). Never claims causation, never ranks sources,
never recommends a coefficient/weight/production change.

Failure handling: if anything fails, this script exits non-zero and
writes NOTHING.
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
import exp010_source_dialogue_validation as join_module  # noqa: E402 -- UNCHANGED, reused as-is
import evidence_quality as eq  # noqa: E402 -- UNCHANGED, reused as-is (research-only integration point)

DATABASE_NAME = "sentiment-history"
CLOUDFLARE_ACCOUNT_ID = "f58e761fbc8e62dc404d8684290af264"
D1_DATABASE_ID = "f91ca980-b886-423a-bd6f-f3baea46d181"
SUBJECT = "EXP-010:source_dialogue_validation"
WINDOW_MS = ed.MAX_WINDOW_MS  # 90 days
LOOKBACK_BUFFER_MS = ed.LOOKBACK_BUFFER_MS  # 8 days -- event_detector's own


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
    Cloudflare's D1 REST API directly. Identical implementation to
    exp005/exp009's own d1_api_query() (duplicated deliberately, not
    imported -- each experiment script is self-contained). A bound
    parameter's value is transmitted as data, never parsed as SQL
    grammar, so it is not subject to D1's ~100,000-byte maximum SQL
    STATEMENT length -- proven at 600,000-byte scale via EXP-009's own
    real GitHub Actions probe (exact_match=True).

    Uses the existing CLOUDFLARE_API_TOKEN secret. The token is read
    once from the environment and used only in the Authorization
    header; it is never included in any log line, print statement, or
    exception message this function raises.

    Fails loudly on anything but an unambiguous success."""
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


def build_local_mirror(history_rows, btc_rows, predictions_rows):
    """In-memory sqlite mirror containing ONLY the columns the reused
    modules actually query -- same minimal-mirror convention as every
    other experiment's run_experiment.py. Only 3 tables: EXP-010 does
    not use research_events/research_event_evidence at all (it never
    calls PR59's evidence-fetch path, only event detection + V1
    snapshots + redundancy)."""
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
    conn.commit()
    return conn


def build_history_observations(history_rows):
    """Research Evidence Quality Layer integration point (research-only
    -- see research/evidence_quality.py's own module docstring).
    Duplicated deliberately from exp005-source-effectiveness/run_
    experiment.py's own identical helper rather than imported -- same
    "each experiment script is self-contained" convention this file's
    own d1_api_query() docstring already states. Built from the SAME
    history_rows this script already fetched -- no new D1 read. This is
    purely additive: it does not feed into, and is never read by,
    join_module.build_relationship_dataset()/build_redundancy_dataset()
    or the locked Source Dialogue Engine, all of which remain
    completely unchanged."""
    return [
        {
            "information_available_at": r["ts"],
            "observation_time": r["ts"],
            "provider": "V1",
            "dataset": "history",
        }
        for r in history_rows
    ]


INSERT_ANALYSIS_SQL = (
    "INSERT INTO research_analyses "
    "(analysis_ts, window_start_ts, window_end_ts, sample_size, subject, metric_json, "
    "multiple_testing_correction, validation_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

NOT_A_HYPOTHESIS_TEST_NOTE = json.dumps({
    "applicable": False,
    "reason": "This experiment validates the Source Dialogue Engine's own already-deterministic, "
              "already-tested classification (relationship + redundancy) against real V1/V2 data -- "
              "it is a diagnostic/validation run, not a statistical hypothesis test, so no "
              "multiple-testing correction applies. Descriptive counts only.",
})


def summarize_validation_status(relationship_summary):
    """A single coarse validation_status label -- descriptive
    bookkeeping only, mirroring EXP-005/009's own convention. Never a
    coefficient or production recommendation."""
    supporting_or_contradicting = (
        relationship_summary["by_relationship"].get("SUPPORTING", 0)
        + relationship_summary["by_relationship"].get("CONTRADICTING", 0)
    )
    if supporting_or_contradicting > 0:
        return "comparable_pairs_observed"
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
    print(f"[exp010] fetched {len(history_rows)} history rows, {len(btc_rows)} btc_data rows, "
          f"{len(predictions_rows)} predictions rows for window [{start_ts}, {now_ms}]")

    if len(history_rows) < 3 or len(btc_rows) < 3:
        print(f"[exp010] insufficient market data (history={len(history_rows)}, btc_data={len(btc_rows)}) "
              f"-- writing nothing", file=sys.stderr)
        sys.exit(1)

    mirror = build_local_mirror(history_rows, btc_rows, predictions_rows)
    relationship_dataset = join_module.build_relationship_dataset(mirror, start_ts, now_ms)
    redundancy_dataset = join_module.build_redundancy_dataset(mirror, start_ts, now_ms)
    mirror.close()

    relationship_summary = join_module.summarize_relationship_counts(relationship_dataset)
    redundancy_summary = join_module.summarize_redundancy_counts(redundancy_dataset)

    # Payload-size discipline (proactive, see module docstring): the
    # persisted report is descriptive counts only, never the full
    # per-(event, pair) join -- that full detail was computed above
    # (and is fully testable/auditable in build_relationship_dataset's
    # own return value) but is never itself persisted.
    # Research Evidence Quality Layer (research-only integration point,
    # additive): assesses the SAME history_rows already fetched above,
    # never feeds into or alters the locked Source Dialogue Engine's
    # own classifications, or exp010_source_dialogue_validation.py's
    # own as-of-safe direction logic. information_cutoff=now_ms is this
    # live run's real as-of boundary.
    evidence_quality_report = eq.assess_evidence_quality(
        build_history_observations(history_rows),
        information_cutoff=now_ms, window_start=start_ts, window_end=now_ms,
    )

    report = {
        "window": relationship_dataset["window"],
        "source_keys": relationship_dataset["source_keys"],
        "relationship_summary": relationship_summary,
        "redundancy_summary": redundancy_summary,
        "evidence_quality": evidence_quality_report,
    }
    validation_status = summarize_validation_status(relationship_summary)

    sample_size = relationship_summary["n_source_pair_observations"]
    params = [
        now_ms, start_ts, now_ms, sample_size,
        SUBJECT, json.dumps(report), NOT_A_HYPOTHESIS_TEST_NOTE, validation_status,
    ]
    d1_api_query(INSERT_ANALYSIS_SQL, params)
    print(f"[exp010] persisted analysis: n_events_used={relationship_summary['n_events_used']} "
          f"n_internal_model_events_excluded={relationship_summary['n_internal_model_events_excluded']} "
          f"n_source_pair_observations={sample_size} validation_status={validation_status}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[exp010] FAILED (writing nothing): {e}", file=sys.stderr)
        sys.exit(1)
