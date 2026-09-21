"""
Tests for exp005-source-effectiveness/run_experiment.py.

Run with: pip install pytest --break-system-packages && pytest exp005-source-effectiveness/

These tests never touch production D1 -- run_d1() and d1_api_query()'s
underlying transports (subprocess, urllib.request.urlopen) are always
mocked here. research/source_analysis.py itself is imported UNCHANGED
and exercised against small synthetic in-memory data; these tests do
not re-test its own statistics (that is research/test_source_analysis.py's
job, already 530 tests strong) -- they test only this script's own
orchestration: mirror construction, parameterized SQL/params building,
the D1 API transport, and the one summary label it adds.
"""
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "research"))

spec = importlib.util.spec_from_file_location("run_experiment", os.path.join(_HERE, "run_experiment.py"))
run_experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_experiment)

import source_analysis as sa  # noqa: E402
import evidence_quality  # noqa: E402


class FakeHTTPResponse:
    """Minimal stand-in for the object urllib.request.urlopen() returns
    -- supports the context-manager protocol, .status, and .read(),
    which is all d1_api_query() uses."""
    def __init__(self, status, body_bytes):
        self.status = status
        self._body = body_bytes

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _success_envelope(results=None):
    return json.dumps({
        "success": True, "errors": [], "messages": [],
        "result": [{"success": True, "results": results if results is not None else [], "meta": {}}],
    }).encode("utf-8")


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


def test_main_builds_its_only_production_write_via_d1_api_query():
    """main() must never construct a raw INSERT/UPDATE/DELETE string
    itself -- its only production write is d1_api_query(INSERT_ANALYSIS_SQL,
    params), where params comes from build_insert_analysis_params()
    (separately confirmed, by test_insert_sql_targets_research_analyses_
    with_correct_columns, to target research_analyses only). This proves
    main() has no OTHER, independent write path to production that
    could target a different table, and that the write no longer goes
    through run_d1_file / wrangler --command / --file at all -- those no
    longer exist in this module (removed along with the SQL-text-size
    problem they could never actually solve; see d1_api_query's
    docstring)."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    assert "run_d1_file" not in src
    assert "build_insert_analysis_sql(" not in src  # replaced by build_insert_analysis_params
    assert "tempfile" not in src
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    assert "INSERT INTO" not in main_src
    assert "UPDATE " not in main_src
    assert "DELETE FROM" not in main_src
    assert "build_insert_analysis_params(" in main_src
    assert main_src.count("run_d1(") == 2  # exactly the 2 SELECT fetches, unchanged
    assert main_src.count("d1_api_query(") == 1  # exactly the 1 final write


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


def test_real_multi_source_report_produces_valid_params_via_build_insert_analysis_params():
    """Regression test for a real bug found against production data:
    report["level2"]["tests"] and report["redundancy"]["pairwise"] are
    natively keyed by tuples (e.g. ('fng', 1), ('fng', 'yield10y')),
    which raises TypeError from a bare json.dumps(report) -- main()'s
    only write path (build_insert_analysis_params) must never hit this,
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

    params = run_experiment.build_insert_analysis_params(
        analysis_ts=1, window_start_ts=1, window_end_ts=1, sample_size=report["n_history_rows"],
        metric_json_obj=report, multiple_testing_correction=json.dumps(report["level2"]["multiple_testing_correction"]),
        validation_status="observation",
    )
    assert len(params) == 8
    json.loads(params[5])  # metric_json param must itself be valid, parseable JSON


# ---- Research Evidence Quality Layer integration (additive) ----

def test_build_history_observations_uses_row_ts_for_both_timestamp_axes():
    """V1 history rows have no distinct publication-lag concept -- both
    information_available_at and observation_time must be the row's own
    real ts, same documented design decision as exp010's own
    build_v1_observation()."""
    rows = [{"ts": 1000, "score": 50, "sources_json": "{}", "gold_regime": "chop"}]
    obs = run_experiment.build_history_observations(rows)
    assert obs == [{"information_available_at": 1000, "observation_time": 1000, "provider": "V1", "dataset": "history"}]


def test_build_history_observations_one_per_row_provider_agnostic_of_sources_json_content():
    rows = [{"ts": i * 1000, "score": 1, "sources_json": None, "gold_regime": None} for i in range(5)]
    obs = run_experiment.build_history_observations(rows)
    assert len(obs) == 5
    assert [o["observation_time"] for o in obs] == [0, 1000, 2000, 3000, 4000]


def test_main_report_carries_an_additive_evidence_quality_key_never_replacing_existing_keys():
    """The Evidence Quality Layer integration must be purely additive:
    every key build_source_effectiveness_report() itself produces must
    remain present and untouched, with evidence_quality as one new
    sibling key."""
    history_rows = [
        {"ts": 1000 + i * 3600000, "score": 50 + (i % 5), "sources_json": json.dumps({"fng": 40 + (i % 7)}), "gold_regime": "chop"}
        for i in range(50)
    ]
    btc_rows = [{"ts": 1000 + i * 3600000, "btc_price": 50000.0 + i * 10} for i in range(60)]
    conn = run_experiment.build_local_mirror(history_rows, btc_rows)
    report = sa.build_source_effectiveness_report(conn, 1000, 1000 + 49 * 3600000, horizons=(1, 3))
    conn.close()
    original_keys = set(report.keys())

    now_ms = 1000 + 49 * 3600000
    report["evidence_quality"] = evidence_quality.assess_evidence_quality(
        run_experiment.build_history_observations(history_rows),
        information_cutoff=now_ms, window_start=1000, window_end=now_ms,
    )

    assert original_keys.issubset(report.keys())
    assert report["evidence_quality"]["OVERALL_STATUS"] in evidence_quality.OVERALL_STATUSES
    assert report["evidence_quality"]["n_observations_supplied"] == 50
    # No numeric score anywhere in the new key either.
    assert "score" not in json.dumps(report["evidence_quality"])

    # Must still survive the exact same serialization path main() uses.
    params = run_experiment.build_insert_analysis_params(
        analysis_ts=1, window_start_ts=1, window_end_ts=1, sample_size=report["n_history_rows"],
        metric_json_obj=report, multiple_testing_correction=json.dumps(report["level2"]["multiple_testing_correction"]),
        validation_status="observation",
    )
    parsed = json.loads(params[5])
    assert "evidence_quality" in parsed


def test_main_calls_evidence_quality_exactly_once():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    assert main_src.count("eq.assess_evidence_quality(") == 1


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


def test_make_json_safe_is_the_one_used_before_json_dumps_in_build_insert_analysis_params():
    """Static check that build_insert_analysis_params actually calls
    make_json_safe before json.dumps -- the whole point of this fix is
    that main()'s only write path goes through it, not just that the
    helper function exists and works in isolation."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    fn_start = src.index("def build_insert_analysis_params(")
    fn_src = src[fn_start:src.index("\n\n\n", fn_start)]
    assert "json.dumps(make_json_safe(" in fn_src


# ---- INSERT_ANALYSIS_SQL / build_insert_analysis_params ----

def test_insert_sql_targets_research_analyses_with_correct_columns_and_placeholders():
    sql = run_experiment.INSERT_ANALYSIS_SQL
    assert sql.startswith("INSERT INTO research_analyses (")
    assert "subject" in sql and "metric_json" in sql
    assert re.match(r"^INSERT\s", sql)
    assert not re.search(r"\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b", sql)
    assert sql.count("?") == 8  # one placeholder per column, no literal values


def test_build_insert_analysis_params_preserves_apostrophes_unmodified():
    # Unlike the old literal-embedding design, params need NO manual
    # escaping at all -- D1 binds the value directly. Confirms the
    # apostrophe survives exactly as-is (not doubled, not stripped).
    params = run_experiment.build_insert_analysis_params(
        analysis_ts=1, window_start_ts=1, window_end_ts=1, sample_size=1,
        metric_json_obj={"note": "it's a test"}, multiple_testing_correction="{}",
        validation_status="observation",
    )
    metric_json_param = params[5]
    assert "it's a test" in metric_json_param
    assert "it''s a test" not in metric_json_param  # no SQL-style doubling -- params need none


def test_build_insert_analysis_params_returns_values_in_insert_sql_column_order():
    params = run_experiment.build_insert_analysis_params(
        analysis_ts=111, window_start_ts=222, window_end_ts=333, sample_size=444,
        metric_json_obj={"a": 1}, multiple_testing_correction='{"method": "benjamini_hochberg"}',
        validation_status="observation",
    )
    assert params[0:4] == [111, 222, 333, 444]
    assert params[4] == "EXP-005:source_effectiveness"
    assert json.loads(params[5]) == {"a": 1}
    assert params[6] == '{"method": "benjamini_hochberg"}'
    assert params[7] == "observation"


# ---- large-payload regression: proves the SQL text stays fixed-size
# regardless of metric_json's size -- the actual fix for the real
# production defect (workflow run 35510891642: E2BIG under --command;
# a pre-merge dry run: SQLITE_TOOBIG under --file, both because the
# report was embedded as SQL TEXT rather than a bound parameter) ----

def _large_synthetic_report():
    """Generated PROGRAMMATICALLY (not a hand-typed giant literal in
    source) -- mirrors the real production report's actual scale (21
    sources x 5 horizons for level2.tests/level3, C(21,2)=210 pairs for
    redundancy.pairwise, each entry carrying the same field set the
    real report's level2.tests entries carry) closely enough that its
    serialized size is genuinely representative of the real ~220KB
    report, not a toy fixture."""
    test_entry = {
        "source_key": "placeholder", "horizon_hours": 1, "n": 220, "n_candidate_rows": 500,
        "dropped_missing_source": 0, "dropped_missing_outcome_row": 0, "dropped_unresolved_outcome": 280,
        "status": "OK", "effect_size_r": 0.01, "ci_low": -0.1, "ci_high": 0.1, "p_raw": 0.5,
        "sample_size_status": "OK",
        "oos_split": {"n_discovery": 154, "n_validation": 66, "r_discovery": 0.01, "r_validation": 0.02, "sign_stable": True},
        "p_corrected": 0.6, "significant": False,
    }
    return {
        "level2_tests": {f"source_{i}|{h}h": {**test_entry, "source_key": f"source_{i}", "horizon_hours": h}
                          for i in range(21) for h in (1, 3, 6, 12, 24)},
        "level3": {f"source_{i}|{h}h": {**test_entry, "source_key": f"source_{i}", "horizon_hours": h}
                   for i in range(21) for h in (1, 3, 6, 12, 24)},
        "redundancy_pairwise": {f"source_{i}|source_{j}": {"correlation": 0.1}
                                 for i in range(21) for j in range(i + 1, 21)},
    }


def test_large_synthetic_report_fixture_is_substantially_larger_than_the_d1_statement_limit():
    # Sanity check on the fixture itself: must be the same order of
    # magnitude as the real ~220KB production report (which exceeded
    # D1's empirically-confirmed ~100,000-byte SQL statement-length
    # ceiling), or the regression tests below would not actually
    # exercise the failure class that broke production twice.
    payload = json.dumps(_large_synthetic_report())
    assert len(payload) > 100_000


def test_insert_sql_text_stays_small_and_fixed_regardless_of_metric_json_size():
    """The core proof: INSERT_ANALYSIS_SQL's own text length can never
    again approach D1's ~100,000-byte statement-length ceiling,
    regardless of how large a real report gets, because metric_json
    never appears inside it."""
    large_params = run_experiment.build_insert_analysis_params(
        analysis_ts=1, window_start_ts=1, window_end_ts=1, sample_size=500,
        metric_json_obj=_large_synthetic_report(), multiple_testing_correction="{}", validation_status="observation",
    )
    assert len(run_experiment.INSERT_ANALYSIS_SQL) < 1000
    assert len(large_params[5]) > 100_000  # the large payload lives in params[5] (metric_json) instead
    assert len(large_params) == 8  # shape is identical regardless of payload size


def test_metric_json_appears_only_in_params_never_in_sql_text():
    params = run_experiment.build_insert_analysis_params(
        analysis_ts=1, window_start_ts=1, window_end_ts=1, sample_size=500,
        metric_json_obj=_large_synthetic_report(), multiple_testing_correction="{}", validation_status="observation",
    )
    metric_json_param = params[5]
    assert "source_0|1h" in metric_json_param  # confirms this really is the large payload
    assert "source_0|1h" not in run_experiment.INSERT_ANALYSIS_SQL  # never present in the fixed SQL text


def test_d1_api_query_request_body_keeps_sql_small_while_params_carries_the_large_payload():
    """End-to-end proof at the actual transport layer used against
    production: the 'sql' field inside the real JSON request body sent
    to D1's API stays tiny while 'params' carries the full large
    payload -- this is precisely what the old --command/--file design
    could never do, since it had no separate channel for large values."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return FakeHTTPResponse(200, _success_envelope())

    large_params = run_experiment.build_insert_analysis_params(
        analysis_ts=1, window_start_ts=1, window_end_ts=1, sample_size=500,
        metric_json_obj=_large_synthetic_report(), multiple_testing_correction="{}", validation_status="observation",
    )
    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            run_experiment.d1_api_query(run_experiment.INSERT_ANALYSIS_SQL, large_params)

    assert len(captured["body"]["sql"]) < 1000
    assert len(json.dumps(captured["body"]["params"])) > 100_000


# ---- d1_api_query: transport, auth, and failure semantics ----

def test_d1_api_query_uses_bearer_auth_from_env_and_never_leaks_the_token_elsewhere():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        return FakeHTTPResponse(200, _success_envelope())

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "super-secret-token"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            run_experiment.d1_api_query("SELECT 1", [])

    request = captured["request"]
    assert request.get_header("Authorization") == "Bearer super-secret-token"
    assert "super-secret-token" not in request.full_url
    assert "super-secret-token" not in request.data.decode("utf-8")


def test_d1_api_query_targets_the_existing_account_and_database_id():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return FakeHTTPResponse(200, _success_envelope())

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            run_experiment.d1_api_query("SELECT 1", [])

    assert captured["url"] == (
        f"https://api.cloudflare.com/client/v4/accounts/{run_experiment.CLOUDFLARE_ACCOUNT_ID}"
        f"/d1/database/{run_experiment.D1_DATABASE_ID}/query"
    )


def test_d1_api_query_sends_sql_and_params_in_the_json_body():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return FakeHTTPResponse(200, _success_envelope())

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            run_experiment.d1_api_query("SELECT ? AS n", ["value"])

    assert captured["body"] == {"sql": "SELECT ? AS n", "params": ["value"]}


def test_d1_api_query_returns_parsed_results_on_success():
    def fake_urlopen(request, timeout=None):
        return FakeHTTPResponse(200, _success_envelope(results=[{"ok": 1}]))

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            assert run_experiment.d1_api_query("SELECT 1", []) == [{"ok": 1}]


def test_d1_api_query_raises_on_http_error_and_never_leaks_the_token():
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "Internal Server Error", None, io.BytesIO(b'{"errors":["boom"]}'))

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "super-secret-token"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            with pytest.raises(RuntimeError) as exc_info:
                run_experiment.d1_api_query("SELECT 1", [])
    assert "super-secret-token" not in str(exc_info.value)


def test_d1_api_query_raises_on_network_error():
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            with pytest.raises(RuntimeError):
                run_experiment.d1_api_query("SELECT 1", [])


def test_d1_api_query_raises_on_malformed_non_json_response():
    def fake_urlopen(request, timeout=None):
        return FakeHTTPResponse(200, b"not json at all {{{")

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            with pytest.raises(RuntimeError):
                run_experiment.d1_api_query("SELECT 1", [])


def test_d1_api_query_raises_when_envelope_reports_failure():
    def fake_urlopen(request, timeout=None):
        body = json.dumps({"success": False, "errors": [{"message": "nope"}], "result": []}).encode()
        return FakeHTTPResponse(200, body)

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            with pytest.raises(RuntimeError):
                run_experiment.d1_api_query("SELECT 1", [])


def test_d1_api_query_raises_when_per_statement_result_reports_failure():
    def fake_urlopen(request, timeout=None):
        body = json.dumps({"success": True, "errors": [], "result": [{"success": False, "results": None}]}).encode()
        return FakeHTTPResponse(200, body)

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            with pytest.raises(RuntimeError):
                run_experiment.d1_api_query("SELECT 1", [])


def test_d1_api_query_raises_on_missing_or_empty_result_list():
    def fake_urlopen(request, timeout=None):
        body = json.dumps({"success": True, "errors": [], "result": []}).encode()
        return FakeHTTPResponse(200, body)

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            with pytest.raises(RuntimeError):
                run_experiment.d1_api_query("SELECT 1", [])


def test_d1_api_query_raises_on_non_200_status_even_if_body_claims_success():
    """Defensive: a 200 status is required in addition to success:true
    -- never trust the body alone if the transport itself reported
    something other than 200."""
    def fake_urlopen(request, timeout=None):
        return FakeHTTPResponse(202, _success_envelope())

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            with pytest.raises(RuntimeError):
                run_experiment.d1_api_query("SELECT 1", [])


# ---- run_d1 (existing small SELECT path, unaffected by this fix) ----

def test_run_d1_still_uses_wrangler_command_for_small_fixed_size_selects():
    """The SELECT reads were never the problem (their SQL text is
    always small and fixed-shape -- window bounds only) and are
    intentionally left on the existing wrangler --command mechanism,
    unaffected by the write-path fix."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    fn_start = src.index("def run_d1(")
    fn_src = src[fn_start:src.index("\n\n\n", fn_start)]
    assert "--command" in fn_src
    assert "wrangler" in fn_src


def test_run_d1_still_parses_wrangler_json_output_correctly():
    from unittest.mock import MagicMock
    result = MagicMock()
    result.stdout = json.dumps([{"results": [{"ts": 1}]}])
    with patch.object(run_experiment.subprocess, "run", return_value=result):
        assert run_experiment.run_d1("SELECT 1") == [{"ts": 1}]


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
