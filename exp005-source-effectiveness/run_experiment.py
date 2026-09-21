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
     table may ever hold. The INSERT is executed via Cloudflare's D1 REST
     API directly (d1_api_query), with metric_json passed as a BOUND
     PARAMETER rather than embedded in the SQL text -- see d1_api_query's
     own docstring for why (D1 enforces a ~100,000-byte maximum SQL
     STATEMENT length, which the real ~220KB report exceeds regardless of
     how the statement text reaches D1; a bound parameter's value is
     transmitted as data, never parsed as SQL grammar, so it is not
     subject to that limit).

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
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "research")
import source_analysis as sa  # noqa: E402 -- UNCHANGED, reused as-is
import evidence_quality as eq  # noqa: E402 -- UNCHANGED, reused as-is (research-only integration point)

DATABASE_NAME = "sentiment-history"
# Non-secret identifiers, already committed/used elsewhere in this project
# (wrangler.toml's own account_id; the same database_id this project's D1
# tooling already references) -- an account/database ID is not a
# credential, only CLOUDFLARE_API_TOKEN is.
CLOUDFLARE_ACCOUNT_ID = "f58e761fbc8e62dc404d8684290af264"
D1_DATABASE_ID = "f91ca980-b886-423a-bd6f-f3baea46d181"
SUBJECT = "EXP-005:source_effectiveness"
HORIZONS = (1, 3, 6, 12, 24)  # source_analysis.py's own default -- reused, not narrowed
WINDOW_MS = sa.MAX_WINDOW_MS  # 90 days -- the existing bound, never widened for this experiment


def run_d1(sql: str):
    """Executes a SQL statement against production D1 via wrangler, exactly
    the same mechanism exp004-timesfm/run_experiment.py and
    export-learning-data.yml already use. Only used for this script's two
    small, fixed-size SELECT fetches, whose SQL text is always small and
    fixed-shape (window bounds only) -- never for the large generated
    INSERT (see d1_api_query)."""
    result = subprocess.run(
        ["wrangler", "d1", "execute", DATABASE_NAME, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    return parsed[0]["results"] if parsed and parsed[0].get("results") is not None else []


def d1_api_query(sql: str, params: list):
    """Executes a PARAMETERIZED SQL statement against production D1 via
    Cloudflare's D1 REST API directly (POST .../d1/database/{id}/query),
    instead of wrangler's --command/--file.

    Both --command and --file embed a value directly as SQL TEXT, which
    D1 must parse as part of the SQL statement itself -- and D1 enforces
    a ~100,000-byte maximum SQL STATEMENT length (confirmed empirically:
    a 99,141-byte statement succeeds, a 100,141-byte statement fails
    with SQLITE_TOOBIG, using the real wrangler binary against D1). The
    real EXP-005 report is a ~220,329-byte INSERT once metric_json is
    embedded as a literal -- more than double that ceiling. This was
    confirmed to fail in production twice: run 35510891642 (E2BIG from
    the OS, before wrangler could even start, using --command) and a
    pre-merge dry run against real production data (SQLITE_TOOBIG from
    D1 itself, using --file). Neither transport mechanism changes how
    much SQL TEXT D1 has to parse.

    A bound PARAMETER's value, by contrast, is transmitted as data
    alongside the statement, never parsed as SQL grammar -- it is not
    subject to the statement-length limit at all. This function keeps
    the SQL text itself small and FIXED-SIZE regardless of how large
    metric_json is; only the params list grows.

    Uses the existing CLOUDFLARE_API_TOKEN secret -- the same token
    wrangler's own --remote execute already authenticates every D1
    write in this project with -- and the project's existing, non-secret
    CLOUDFLARE_ACCOUNT_ID/D1_DATABASE_ID. No new secret, no new
    credential. The token is read once from the environment and used
    only in the Authorization header; it is never included in any log
    line, print statement, or exception message this function raises.

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


INSERT_ANALYSIS_SQL = (
    "INSERT INTO research_analyses "
    "(analysis_ts, window_start_ts, window_end_ts, sample_size, subject, metric_json, "
    "multiple_testing_correction, validation_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def build_insert_analysis_params(analysis_ts, window_start_ts, window_end_ts, sample_size,
                                  metric_json_obj, multiple_testing_correction, validation_status):
    """Mirrors research/source_analysis.py's own persist_analysis() column
    list and order EXACTLY (verified against that function's docstring
    and INSERT statement) -- built independently here because
    persist_analysis() itself operates on a live sqlite3 connection's
    .execute(), not D1's REST API.

    Returns the params list for INSERT_ANALYSIS_SQL's ? placeholders, in
    the same order. INSERT_ANALYSIS_SQL's own text is small and fixed
    regardless of metric_json's size -- metric_json (however large) is
    only ever a PARAM value here, never embedded in SQL text, so no
    string-literal escaping is needed or performed at all (D1 handles
    parameter binding itself, the same SQL-injection-safe mechanism
    every other D1 caller in this project already relies on)."""
    return [
        analysis_ts, window_start_ts, window_end_ts, sample_size,
        SUBJECT, json.dumps(make_json_safe(metric_json_obj)), multiple_testing_correction, validation_status,
    ]


def build_history_observations(history_rows):
    """Research Evidence Quality Layer integration point (research-only
    -- see research/evidence_quality.py's own module docstring). Builds
    the plain observation list the layer's contract requires from the
    SAME history_rows this script already fetched for
    sa.build_source_effectiveness_report() -- no new data fetch, no new
    D1 read. V1 history rows have no distinct publication-lag concept
    (the same documented design decision exp010_source_dialogue_
    validation.py already made for the same reason): information_
    available_at and observation_time are both the row's own real
    history.ts. This is purely additive -- it does not feed into, and
    is never read by, sa.build_source_effectiveness_report() itself."""
    return [
        {
            "information_available_at": r["ts"],
            "observation_time": r["ts"],
            "provider": "V1",
            "dataset": "history",
        }
        for r in history_rows
    ]


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

    # Research Evidence Quality Layer (research-only integration point,
    # additive): assesses the SAME history_rows already fetched above,
    # never feeds into or alters any statistical calculation this
    # experiment's own gates already perform. information_cutoff=now_ms
    # is the real as-of boundary for this run (this is a live scheduled
    # run, not a historical replay) -- no future row can exist in
    # history_rows by construction (the SELECT above already bounds
    # ts <= now_ms), so this also serves as an independent, run-time
    # confirmation that the fetch itself introduced no lookahead.
    report["evidence_quality"] = eq.assess_evidence_quality(
        build_history_observations(history_rows),
        information_cutoff=now_ms, window_start=start_ts, window_end=now_ms,
    )

    # report["level2"]["multiple_testing_correction"] is itself a dict
    # ({"method": "benjamini_hochberg", "alpha": ..., "n_tests": ...})
    # -- stored as its own JSON string in the (TEXT) multiple_testing_
    # correction column, exact shape unchanged from run_level2_battery().
    multiple_testing_correction = json.dumps(report["level2"]["multiple_testing_correction"])
    validation_status = summarize_validation_status(report)

    params = build_insert_analysis_params(
        analysis_ts=now_ms, window_start_ts=start_ts, window_end_ts=now_ms,
        sample_size=report["n_history_rows"], metric_json_obj=report,
        multiple_testing_correction=multiple_testing_correction, validation_status=validation_status,
    )
    d1_api_query(INSERT_ANALYSIS_SQL, params)
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
