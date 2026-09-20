"""
Tests for exp005-source-effectiveness/run_experiment.py.

Run with: pip install pytest --break-system-packages && pytest exp005-source-effectiveness/

These tests never touch production D1 -- run_d1() is never called here.
research/source_analysis.py itself is imported UNCHANGED and exercised
against small synthetic in-memory data; these tests do not re-test its
own statistics (that is research/test_source_analysis.py's job, already
530 tests strong) -- they test only this script's own orchestration:
mirror construction, SQL building, and the one summary label it adds.
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys

_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "research"))

spec = importlib.util.spec_from_file_location("run_experiment", os.path.join(_HERE, "run_experiment.py"))
run_experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_experiment)

import source_analysis as sa  # noqa: E402


# ---- Scope ----

def test_subject_is_stable_and_namespaced():
    assert run_experiment.SUBJECT == "EXP-005:source_effectiveness"


def test_window_ms_matches_source_analysis_own_bound_unchanged():
    # Must be IMPORTED from source_analysis, never a separately
    # hardcoded 90-day literal that could silently drift from it.
    assert run_experiment.WINDOW_MS == sa.MAX_WINDOW_MS


def test_horizons_match_source_analysis_default_unchanged():
    import inspect
    default_horizons = inspect.signature(sa.build_source_effectiveness_report).parameters["horizons"].default
    assert run_experiment.HORIZONS == default_horizons


def test_exactly_one_exp005_workflow_with_exactly_one_schedule():
    workflows_dir = os.path.join(_HERE, "..", ".github", "workflows")
    matches = [f for f in os.listdir(workflows_dir) if "exp005" in f.lower() or "source-effectiveness" in f.lower()]
    assert len(matches) == 1, f"expected exactly one Experiment 5 workflow file, found {matches}"
    with open(os.path.join(workflows_dir, matches[0])) as f:
        content = f.read()
    assert len(re.findall(r"- cron:", content)) == 1


def test_workflow_does_not_touch_cloudflare_cron_triggers():
    wrangler_toml = os.path.join(_HERE, "..", "wrangler.toml")
    with open(wrangler_toml) as f:
        content = f.read()
    workflows_dir = os.path.join(_HERE, "..", ".github", "workflows")
    matches = [f for f in os.listdir(workflows_dir) if "exp005" in f.lower() or "source-effectiveness" in f.lower()]
    with open(os.path.join(workflows_dir, matches[0])) as f:
        workflow_content = f.read()
    cron_line = re.search(r"- cron: '([^']+)'", workflow_content).group(1)
    assert cron_line not in content  # this exact cron string must not also appear in wrangler.toml's [triggers]


def test_main_builds_its_only_production_write_via_the_one_sql_builder_helper():
    """main() must never construct a raw INSERT/UPDATE/DELETE string
    itself -- its only production write is run_d1(sql) where sql comes
    from build_insert_analysis_sql() (separately confirmed, by
    test_insert_sql_targets_research_analyses_with_correct_columns, to
    target research_analyses only). This proves main() has no OTHER,
    independent write path to production that could target a different
    table."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    assert "INSERT INTO" not in main_src
    assert "UPDATE " not in main_src
    assert "DELETE FROM" not in main_src
    assert "build_insert_analysis_sql(" in main_src
    assert main_src.count("run_d1(") == 3  # 2 SELECT fetches + 1 final write via the builder's output


def test_build_local_mirror_writes_are_scoped_to_its_own_function_not_run_d1():
    """Confirms build_local_mirror's INSERTs go through conn.execute()
    on its OWN local connection parameter, never through run_d1() --
    i.e. they can never reach production regardless of what data is
    passed in."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    mirror_start = src.index("def build_local_mirror(")
    mirror_end = src.index("\n\n\n", mirror_start)
    mirror_src = src[mirror_start:mirror_end]
    assert "run_d1(" not in mirror_src
    assert "conn.execute(" in mirror_src


# ---- build_local_mirror ----

def test_build_local_mirror_creates_expected_schema_and_rows():
    history_rows = [
        {"ts": 1000, "score": 55, "sources_json": json.dumps({"fng": 40}), "gold_regime": "chop"},
        {"ts": 2000, "score": 60, "sources_json": json.dumps({"fng": 45}), "gold_regime": "chop"},
    ]
    btc_rows = [{"ts": 1000, "btc_price": 50000.0}, {"ts": 2000, "btc_price": 50500.0}]
    conn = run_experiment.build_local_mirror(history_rows, btc_rows)
    h = conn.execute("SELECT ts, score, sources_json, gold_regime FROM history ORDER BY ts").fetchall()
    b = conn.execute("SELECT ts, btc_price FROM btc_data ORDER BY ts").fetchall()
    assert h == [(1000, 55, '{"fng": 40}', "chop"), (2000, 60, '{"fng": 45}', "chop")]
    assert b == [(1000, 50000.0), (2000, 50500.0)]
    conn.close()


def test_local_mirror_is_actually_usable_by_the_real_source_analysis_module():
    """End-to-end sanity: feed a small synthetic dataset through the
    REAL, unchanged sa.build_source_effectiveness_report() and confirm
    it runs to completion without exception and returns the documented
    top-level shape -- proves the mirror's schema genuinely matches
    what source_analysis.py's own SQL expects, not just that it looks
    plausible."""
    history_rows = [
        {"ts": 1000 + i * 3600000, "score": 50 + (i % 5), "sources_json": json.dumps({"fng": 40 + (i % 7)}), "gold_regime": "chop"}
        for i in range(50)
    ]
    btc_rows = [{"ts": 1000 + i * 3600000, "btc_price": 50000.0 + i * 10} for i in range(60)]
    conn = run_experiment.build_local_mirror(history_rows, btc_rows)
    report = sa.build_source_effectiveness_report(conn, 1000, 1000 + 49 * 3600000, horizons=(1, 3))
    conn.close()
    for key in ("window", "n_history_rows", "sources_discovered", "level1", "level2", "level3",
                "regime_stability", "evidence_labels", "redundancy"):
        assert key in report
    assert report["n_history_rows"] == 50
    assert report["sources_discovered"] == ["fng"]


# ---- build_insert_analysis_sql ----

def test_insert_sql_targets_research_analyses_with_correct_columns():
    sql = run_experiment.build_insert_analysis_sql(
        analysis_ts=5000, window_start_ts=1000, window_end_ts=5000, sample_size=10,
        metric_json_obj={"a": 1}, multiple_testing_correction=json.dumps({"method": "benjamini_hochberg"}),
        validation_status="observation",
    )
    assert sql.startswith("INSERT INTO research_analyses (")
    assert "subject" in sql
    assert "'EXP-005:source_effectiveness'" in sql
    assert re.match(r"^INSERT\s", sql)
    assert not re.search(r"\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b", sql)


def test_insert_sql_escapes_embedded_apostrophes_in_metric_json():
    # A source key or note containing an apostrophe must not break the
    # generated SQL -- this is exactly the class of bug this session's
    # own audit found (drifted apostrophes from hand-typed SQL); this
    # script builds SQL programmatically specifically to avoid it.
    sql = run_experiment.build_insert_analysis_sql(
        analysis_ts=1, window_start_ts=1, window_end_ts=1, sample_size=1,
        metric_json_obj={"note": "it's a test"}, multiple_testing_correction="{}",
        validation_status="observation",
    )
    assert "it''s a test" in sql  # correctly doubled for SQL, not left as a raw unescaped apostrophe
    assert "it's a test" not in sql


# ---- summarize_validation_status ----

def test_summarize_validation_status_observation_when_nothing_significant():
    report = {
        "evidence_labels": {"fng|12h": "INCONCLUSIVE"},
        "level3": {"fng|12h": {"oos": {"status": "NOT_IMPROVED"}}},
    }
    assert run_experiment.summarize_validation_status(report) == "observation"


def test_summarize_validation_status_candidate_signal_when_significant_and_improved():
    report = {
        "evidence_labels": {"fng|12h": "STATISTICALLY_SIGNIFICANT"},
        "level3": {"fng|12h": {"oos": {"status": "IMPROVED"}}},
    }
    assert run_experiment.summarize_validation_status(report) == "candidate_signal_observed"


def test_summarize_validation_status_requires_BOTH_significant_and_improved_not_either_alone():
    # Significant but NOT incremental beyond the composite -> still just
    # an observation, never a candidate signal on significance alone.
    report = {
        "evidence_labels": {"fng|12h": "STATISTICALLY_SIGNIFICANT"},
        "level3": {"fng|12h": {"oos": {"status": "NOT_IMPROVED"}}},
    }
    assert run_experiment.summarize_validation_status(report) == "observation"
