#!/usr/bin/env python3
"""
Experiment 5 -- V1 Source / Coefficient Research.

RESEARCH ONLY. This script:
  1. Fetches real, bounded (<=90 days -- research/source_analysis.py's own
     MAX_WINDOW_MS, unchanged) history + btc_data from production D1
     (read-only).
  2. Runs research/source_analysis.py's own build_source_effectiveness_report()
     -- UNCHANGED, imported not reimplemented -- against a throwaway local
     sqlite mirror of that data.
  3. Persists the full report into the EXISTING research_analyses table
     (schema unchanged since PR1 -- .ai/migrations/0005_research_schema.sql),
     tagged with subject='EXP-005:source_effectiveness' so this experiment's
     own rows are unambiguously distinguishable from anything else that
     table may ever hold. The generated INSERT is written to a private
     temp file and applied via `wrangler d1 execute --file` (run_d1_file)
     rather than `--command`, because the full report is too large for a
     single subprocess argv element -- see run_d1_file's own docstring.

It NEVER writes to history, btc_data, predictions, selection_decisions,
research_events, research_event_evidence, or experiment_4_timesfm. Its
only write target is research_analyses, and only ever an INSERT (never
UPDATE/DELETE) -- each run is a new, independent, timestamped
observation, not a mutation of a prior one.

Zero new statistical logic. research/source_analysis.py (and the
outcome_engine.py / movement_distribution.py it itself imports) is
reused completely UNCHANGED -- this script is orchestration only:
fetch -> build local mirror -> call the existing report builder ->
replicate the result to production. It already implements every axis
this experiment's research question decomposes into:
  A. directional alignment       -> level2's effect_size_r sign / oos_split.sign_stable
  B. predictive association      -> level2's p_raw / p_corrected / effect_size_r
  C. incremental beyond composite -> level3's partial_correlation / oos.rmse_reduction_pct
  D. horizon dependence          -> run across horizons=(1,3,6,12,24), unchanged
  E. regime/event dependence     -> regime_stability_for_source()
  F. redundancy with other sources -> pairwise_source_redundancy() / source_vs_composite_redundancy()
  G. evidence strength           -> classify_evidence() + Benjamini-Hochberg
                                     multiple-testing correction (run_level2_battery,
                                     already applied before classify_evidence sees p_corrected)
These are reported separately in the persisted metric_json, never
collapsed into one score -- see the report's own "level3_scope_note"
and "normalization_note" for the explicit scope boundaries this
experiment inherits, unchanged, from PR5.

Failure handling: if anything fails, this script exits non-zero and
writes NOTHING. It never falls back to a fake/partial/synthetic report.
"""
import json
import os
import subprocess
import sys
import sqlite3
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, "research")
import source_analysis as sa  # noqa: E402 -- UNCHANGED, reused as-is

DATABASE_NAME = "sentiment-history"
SUBJECT = "EXP-005:source_effectiveness"
HORIZONS = (1, 3, 6, 12, 24)  # source_analysis.py's own default -- reused, not narrowed
WINDOW_MS = sa.MAX_WINDOW_MS  # 90 days -- the existing bound, never widened for this experiment


def run_d1(sql: str):
    """Executes a SQL statement against production D1 via wrangler, exactly
    the same mechanism exp004-timesfm/run_experiment.py and
    export-learning-data.yml already use. Only used for this script's two
    small, fixed-size SELECT fetches -- never for the large generated
    INSERT (see run_d1_file)."""
    result = subprocess.run(
        ["wrangler", "d1", "execute", DATABASE_NAME, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    return parsed[0]["results"] if parsed and parsed[0].get("results") is not None else []


def run_d1_file(sql: str):
    """Executes a SQL statement against production D1 via wrangler's
    --file mechanism instead of --command.

    A full production EXP-005 report (one row per source, plus a
    per-source-per-horizon test entry -- 21 sources x 5 horizons in the
    real run) serializes to a multi-hundred-KB INSERT statement.
    Passing that as a single --command argv element exceeds the OS's
    execve() argument-list size limit -- confirmed in production:
    GitHub Actions workflow run 35510891642 failed with `[Errno 7]
    Argument list too long: 'wrangler'` before wrangler even started,
    fetching real data (500 history rows, 2070 btc_data rows)
    successfully but persisting nothing.

    `wrangler d1 execute --help` documents --file as a first-class,
    equally-supported alternative to --command ("A .sql file to
    ingest"), verified against the exact wrangler version this
    project's workflow installs. Writing sql to a private temp file
    sidesteps the argv limit entirely -- the file has no size
    constraint comparable to argv, and the SQL text itself is
    transported byte-for-byte, unmodified.

    The temp file holds only the generated INSERT text (no
    credentials) and is always removed -- on both success and
    failure -- via the finally block; it is never left behind for
    debugging, and never written into the repository."""
    fd, path = tempfile.mkstemp(suffix=".sql", prefix="exp005_insert_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(sql)
        result = subprocess.run(
            ["wrangler", "d1", "execute", DATABASE_NAME, "--remote", "--json", "--file", path],
            capture_output=True, text=True, check=True,
        )
        parsed = json.loads(result.stdout)
        return parsed[0]["results"] if parsed and parsed[0].get("results") is not None else []
    finally:
        os.remove(path)


def sql_escape(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def build_local_mirror(history_rows, btc_rows):
    """In-memory sqlite mirror containing ONLY the columns
    research/source_analysis.py and research/outcome_engine.py's
    history-anchored path actually query (h.ts, h.score, h.sources_json,
    h.gold_regime; b.ts, b.btc_price) -- same minimal-mirror convention
    already used by research/live_evidence_pipeline.py."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "score INTEGER, sources_json TEXT, gold_regime TEXT)"
    )
    conn.execute("CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL)")
    for r in history_rows:
        conn.execute(
            "INSERT INTO history (ts, score, sources_json, gold_regime) VALUES (?, ?, ?, ?)",
            (r["ts"], r.get("score"), r.get("sources_json"), r.get("gold_regime")),
        )
    for r in btc_rows:
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (r["ts"], r["btc_price"]))
    conn.commit()
    return conn


def make_json_safe(obj):
    """report["level2"]["tests"] and report["redundancy"]["pairwise"] are
    natively keyed by tuples (e.g. ('cryptonews', 1) or
    ('cryptonews', 'etfflows')) -- valid Python dict keys but not valid
    JSON object keys, so json.dumps(report) raises TypeError as-is.
    Recursively rewrites every tuple key to the pipe-joined string
    "|".join(str(part) for part in key) (e.g. "cryptonews|1"), leaving
    every value, and every already-string-keyed dict (report["level3"]'s
    keys are already native strings like "cryptonews|1h"), unchanged.
    This is a serialization fix only -- it does not touch
    research/source_analysis.py or alter any computed value."""
    if isinstance(obj, dict):
        return {
            ("|".join(str(part) for part in k) if isinstance(k, tuple) else k): make_json_safe(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    return obj


def build_insert_analysis_sql(analysis_ts, window_start_ts, window_end_ts, sample_size,
                               metric_json_obj, multiple_testing_correction, validation_status):
    """Mirrors research/source_analysis.py's own persist_analysis() column
    list and order EXACTLY (verified against that function's docstring
    and INSERT statement) -- built independently here, with explicit
    string-literal escaping, because persist_analysis() itself operates
    on a live sqlite3 connection's .execute(), not a D1-over-wrangler
    text command; this is the same "build the INSERT text separately
    from the pure-python function that computed the values" pattern
    research/live_evidence_pipeline.py already uses for
    build_insert_event_sql/build_insert_evidence_sql."""
    columns = ["analysis_ts", "window_start_ts", "window_end_ts", "sample_size",
               "subject", "metric_json", "multiple_testing_correction", "validation_status"]
    values = [analysis_ts, window_start_ts, window_end_ts, sample_size,
              SUBJECT, json.dumps(make_json_safe(metric_json_obj)), multiple_testing_correction, validation_status]
    return (f"INSERT INTO research_analyses ({', '.join(columns)}) VALUES "
            f"({', '.join(sql_escape(v) for v in values)})")


def summarize_validation_status(report):
    """A single coarse validation_status label for the research_analyses
    row itself -- descriptive bookkeeping only (did this run find ANY
    STATISTICALLY_SIGNIFICANT+IMPROVED pair), never a coefficient
    recommendation. The full nuance (per-source, per-horizon, every
    gate) lives in metric_json; nothing here is fed back into a
    production decision."""
    evidence = report["evidence_labels"]
    level3 = report["level3"]
    any_significant_and_improved = any(
        evidence.get(key) == "STATISTICALLY_SIGNIFICANT" and level3.get(key, {}).get("oos", {}).get("status") == "IMPROVED"
        for key in evidence
    )
    return "candidate_signal_observed" if any_significant_and_improved else "observation"


def main():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = now_ms - WINDOW_MS

    history_rows = run_d1(
        f"SELECT ts, score, sources_json, gold_regime FROM history "
        f"WHERE ts >= {start_ts} AND ts <= {now_ms} ORDER BY ts ASC"
    )
    btc_rows = run_d1(
        f"SELECT ts, btc_price FROM btc_data WHERE ts >= {start_ts} AND ts <= {now_ms} ORDER BY ts ASC"
    )
    print(f"[exp005] fetched {len(history_rows)} history rows, {len(btc_rows)} btc_data rows "
          f"for window [{start_ts}, {now_ms}]")

    if len(history_rows) < 3:
        print(f"[exp005] insufficient history rows ({len(history_rows)}) to run any analysis -- writing nothing", file=sys.stderr)
        sys.exit(1)

    mirror = build_local_mirror(history_rows, btc_rows)
    report = sa.build_source_effectiveness_report(mirror, start_ts, now_ms, horizons=HORIZONS)
    mirror.close()

    # report["level2"]["multiple_testing_correction"] is itself a dict
    # ({"method": "benjamini_hochberg", "alpha": ..., "n_tests": ...})
    # -- stored as its own JSON string in the (TEXT) multiple_testing_
    # correction column, exact shape unchanged from run_level2_battery().
    multiple_testing_correction = json.dumps(report["level2"]["multiple_testing_correction"])
    validation_status = summarize_validation_status(report)

    sql = build_insert_analysis_sql(
        analysis_ts=now_ms, window_start_ts=start_ts, window_end_ts=now_ms,
        sample_size=report["n_history_rows"], metric_json_obj=report,
        multiple_testing_correction=multiple_testing_correction, validation_status=validation_status,
    )
    run_d1_file(sql)
    print(f"[exp005] persisted analysis: sample_size={report['n_history_rows']} "
          f"sources_discovered={report['sources_discovered']} "
          f"sources_eligible={report['sources_eligible_for_level2plus']} "
          f"validation_status={validation_status}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[exp005] FAILED (writing nothing): {e}", file=sys.stderr)
        sys.exit(1)
