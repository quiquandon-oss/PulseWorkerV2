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
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

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
    itself -- its only production write is run_d1_file(sql) where sql
    comes from build_insert_analysis_sql() (separately confirmed, by
    test_insert_sql_targets_research_analyses_with_correct_columns, to
    target research_analyses only). This proves main() has no OTHER,
    independent write path to production that could target a different
    table. The write goes through run_d1_file (file-based transport),
    not run_d1 (argv-based, --command) -- see run_d1_file's own tests
    for why: a full production report is too large for --command."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    assert "INSERT INTO" not in main_src
    assert "UPDATE " not in main_src
    assert "DELETE FROM" not in main_src
    assert "build_insert_analysis_sql(" in main_src
    assert main_src.count("run_d1(") == 2  # exactly the 2 SELECT fetches
    assert main_src.count("run_d1_file(") == 1  # exactly the 1 final write


# ---- run_d1_file (E2BIG fix: production run 35510891642 failed with
# `[Errno 7] Argument list too long: 'wrangler'` because the real
# report's INSERT (~220KB, 21 sources x 5 horizons) was passed as a
# single --command argv element) ----

def _realistic_large_sql_payload():
    """Approximates the real production INSERT's size and shape (a
    single-row INSERT with an embedded, already-escaped JSON blob
    containing many source/horizon test entries, each with the full
    field set level2.tests actually carries) -- not a toy 100-character
    string; the real production run 35510891642 that exposed E2BIG
    produced a ~220KB INSERT from 21 sources x 5 horizons, each entry
    carrying ~15 fields (source_key, horizon_hours, n, n_candidate_rows,
    dropped_*, status, effect_size_r, ci_low, ci_high, p_raw,
    sample_size_status, oos_split{...}, p_corrected, significant).
    Also embeds an already-doubled apostrophe to prove run_d1_file
    transports the SQL byte-for-byte, never re-escaping or otherwise
    mutating it."""
    test_entry = {
        "source_key": "placeholder", "horizon_hours": 1, "n": 220, "n_candidate_rows": 500,
        "dropped_missing_source": 0, "dropped_missing_outcome_row": 0, "dropped_unresolved_outcome": 280,
        "status": "OK", "effect_size_r": 0.01, "ci_low": -0.1, "ci_high": 0.1, "p_raw": 0.5,
        "sample_size_status": "OK",
        "oos_split": {"n_discovery": 154, "n_validation": 66, "r_discovery": 0.01, "r_validation": 0.02, "sign_stable": True},
        "p_corrected": 0.6, "significant": False, "note": "it''s fine",
    }
    # Mirrors the real report's actual sections at real production scale
    # (21 sources x 5 horizons for level2.tests/level3; C(21,2)=210 pairs
    # for redundancy.pairwise) so this fixture's total size is
    # genuinely representative, not just a single inflated section.
    fake_metric_json = json.dumps({
        "level2_tests": {f"source_{i}|{h}h": {**test_entry, "source_key": f"source_{i}", "horizon_hours": h}
                          for i in range(21) for h in (1, 3, 6, 12, 24)},
        "level3": {f"source_{i}|{h}h": {**test_entry, "source_key": f"source_{i}", "horizon_hours": h}
                   for i in range(21) for h in (1, 3, 6, 12, 24)},
        "redundancy_pairwise": {f"source_{i}|source_{j}": {"correlation": 0.1, "note": "it''s fine"}
                                 for i in range(21) for j in range(i + 1, 21)},
    })
    sql = (
        "INSERT INTO research_analyses (analysis_ts, window_start_ts, window_end_ts, sample_size, "
        "subject, metric_json, multiple_testing_correction, validation_status) VALUES "
        f"(1, 1, 1, 500, 'EXP-005:source_effectiveness', '{fake_metric_json}', '{{}}', 'observation')"
    )
    return sql


def test_realistic_payload_is_actually_large_enough_to_represent_production_scale():
    # Sanity check on the test fixture itself: the real production
    # report (21 sources x 5 horizons, plus redundancy pairwise) that
    # exposed E2BIG produced a ~220KB INSERT. This fixture must be the
    # same order of magnitude, not a toy string, or the regression
    # tests below would not actually exercise the failure class that
    # broke production run 35510891642.
    assert len(_realistic_large_sql_payload()) > 100_000


def test_run_d1_file_writes_sql_to_a_temp_file_byte_identical_to_input():
    sql = _realistic_large_sql_payload()
    written_path = {}

    def fake_run(cmd, capture_output, text, check):
        file_idx = cmd.index("--file")
        path = cmd[file_idx + 1]
        written_path["path"] = path
        with open(path) as f:
            assert f.read() == sql  # byte-identical -- never re-escaped or truncated
        result = MagicMock()
        result.stdout = json.dumps([{"results": []}])
        return result

    with patch.object(run_experiment.subprocess, "run", side_effect=fake_run):
        run_experiment.run_d1_file(sql)

    # Cleanup: the temp file must not survive a successful call.
    assert not os.path.exists(written_path["path"])


def test_run_d1_file_never_passes_the_sql_as_an_argv_element():
    """The whole point of the fix: OLD behavior (run_d1 with --command)
    put the full SQL text directly into the subprocess argv list, which
    the OS rejects past a size limit (E2BIG). NEW behavior must never
    put the SQL text itself into any argv element -- only a short file
    path, via --file."""
    sql = _realistic_large_sql_payload()

    def fake_run(cmd, capture_output, text, check):
        assert "--file" in cmd
        assert "--command" not in cmd
        for arg in cmd:
            assert sql not in arg
            assert len(arg) < 4096  # every argv element stays small regardless of report size
        result = MagicMock()
        result.stdout = json.dumps([{"results": []}])
        return result

    with patch.object(run_experiment.subprocess, "run", side_effect=fake_run):
        run_experiment.run_d1_file(sql)


def test_run_d1_file_cleans_up_temp_file_even_when_wrangler_fails():
    sql = _realistic_large_sql_payload()
    captured = {}

    def fake_run(cmd, capture_output, text, check):
        captured["path"] = cmd[cmd.index("--file") + 1]
        raise subprocess.CalledProcessError(1, cmd)

    with patch.object(run_experiment.subprocess, "run", side_effect=fake_run):
        with pytest.raises(subprocess.CalledProcessError):
            run_experiment.run_d1_file(sql)

    # Failure must not leave a temp file behind, and must not swallow
    # the error into a false success.
    assert not os.path.exists(captured["path"])


def test_run_d1_file_returns_parsed_results_like_run_d1():
    sql = _realistic_large_sql_payload()

    def fake_run(cmd, capture_output, text, check):
        result = MagicMock()
        result.stdout = json.dumps([{"results": [{"ok": 1}]}])
        return result

    with patch.object(run_experiment.subprocess, "run", side_effect=fake_run):
        assert run_experiment.run_d1_file(sql) == [{"ok": 1}]


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


def test_real_multi_source_report_is_actually_json_serializable_via_build_insert_analysis_sql():
    """Regression test for a real bug found against production data:
    report["level2"]["tests"] and report["redundancy"]["pairwise"] are
    natively keyed by tuples (e.g. ('fng', 1), ('fng', 'yield10y')),
    which raises TypeError from a bare json.dumps(report) -- main()'s
    only write path (build_insert_analysis_sql) must never hit this,
    or every real run silently fails and writes nothing. Uses >=2
    sources so both level2.tests and redundancy.pairwise are populated
    with real tuple keys, matching what the actual production report
    looks like."""
    history_rows = [
        {
            "ts": 1000 + i * 3600000,
            "score": 50 + (i % 5),
            "sources_json": json.dumps({"fng": 40 + (i % 7), "yield10y": 30 + (i % 9)}),
            "gold_regime": "chop",
        }
        for i in range(60)
    ]
    btc_rows = [{"ts": 1000 + i * 3600000, "btc_price": 50000.0 + i * 10} for i in range(60)]
    conn = run_experiment.build_local_mirror(history_rows, btc_rows)
    report = sa.build_source_effectiveness_report(conn, 1000, 1000 + 59 * 3600000, horizons=(1, 3))
    conn.close()

    assert any(isinstance(k, tuple) for k in report["level2"]["tests"])
    assert any(isinstance(k, tuple) for k in report["redundancy"]["pairwise"])

    sql = run_experiment.build_insert_analysis_sql(
        analysis_ts=1, window_start_ts=1, window_end_ts=1, sample_size=report["n_history_rows"],
        metric_json_obj=report, multiple_testing_correction=json.dumps(report["level2"]["multiple_testing_correction"]),
        validation_status="observation",
    )
    assert sql.startswith("INSERT INTO research_analyses (")


# ---- make_json_safe ----

def test_make_json_safe_rewrites_2tuple_keys_to_pipe_joined_strings():
    original = {("fng", 1): {"effect_size_r": 0.1}, ("fng", 3): {"effect_size_r": 0.2}}
    safe = run_experiment.make_json_safe(original)
    assert safe == {"fng|1": {"effect_size_r": 0.1}, "fng|3": {"effect_size_r": 0.2}}
    json.dumps(safe)  # must not raise


def test_make_json_safe_rewrites_string_pair_tuple_keys():
    original = {("fng", "yield10y"): 0.42}
    assert run_experiment.make_json_safe(original) == {"fng|yield10y": 0.42}


def test_make_json_safe_leaves_string_keyed_dicts_lists_and_scalars_unchanged():
    original = {"level3": {"fng|1h": {"oos": {"status": "IMPROVED"}}}, "n": 5, "tags": ["a", "b"], "flag": None}
    assert run_experiment.make_json_safe(original) == original


def test_make_json_safe_recurses_into_nested_tuple_keys_inside_lists_and_dicts():
    original = {"outer": [{("a", 1): "x"}, {"plain": {("b", 2): "y"}}]}
    safe = run_experiment.make_json_safe(original)
    assert safe == {"outer": [{"a|1": "x"}, {"plain": {"b|2": "y"}}]}
    json.dumps(safe)  # must not raise


def test_make_json_safe_is_the_one_used_before_json_dumps_in_build_insert_analysis_sql():
    """Static check that build_insert_analysis_sql actually calls
    make_json_safe before json.dumps -- the whole point of this fix is
    that main()'s only write path goes through it, not just that the
    helper function exists and works in isolation."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    fn_start = src.index("def build_insert_analysis_sql(")
    fn_src = src[fn_start:src.index("\n\n\n", fn_start)]
    assert "json.dumps(make_json_safe(" in fn_src


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
