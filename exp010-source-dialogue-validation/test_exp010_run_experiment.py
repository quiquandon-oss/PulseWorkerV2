"""
Tests for exp010-source-dialogue-validation/run_experiment.py.

Never touches production D1 -- run_d1()'s subprocess and d1_api_query()'s
urllib.request.urlopen are always mocked here. research/exp010_source_
dialogue_validation.py (and everything it reuses) is exercised against
small synthetic in-memory data in its own dedicated test file
(research/test_exp010_source_dialogue_validation.py, 24 tests strong) --
these tests cover only this script's own orchestration.
"""
import importlib.util
import io
import json
import os
import re
import sys
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "research"))

spec = importlib.util.spec_from_file_location("run_experiment", os.path.join(_HERE, "run_experiment.py"))
run_experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_experiment)

import event_detector as ed  # noqa: E402


class FakeHTTPResponse:
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
    assert run_experiment.SUBJECT == "EXP-010:source_dialogue_validation"


def test_subject_is_distinct_from_exp005_and_exp009():
    assert run_experiment.SUBJECT != "EXP-005:source_effectiveness"
    assert run_experiment.SUBJECT != "EXP-009:event_source_evidence"


def test_window_ms_matches_event_detector_own_bound_unchanged():
    assert run_experiment.WINDOW_MS == ed.MAX_WINDOW_MS


def test_lookback_buffer_matches_event_detector_own_bound_unchanged():
    assert run_experiment.LOOKBACK_BUFFER_MS == ed.LOOKBACK_BUFFER_MS


def test_main_builds_its_only_production_write_via_d1_api_query():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    assert "INSERT INTO" not in main_src
    assert "UPDATE " not in main_src
    assert "DELETE FROM" not in main_src
    assert main_src.count("d1_api_query(") == 1
    assert main_src.count("run_d1(") == 3  # history, btc_data, predictions


def test_build_local_mirror_writes_are_scoped_to_its_own_function_not_run_d1():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    mirror_start = src.index("def build_local_mirror(")
    mirror_end = src.index("\n\n\n", mirror_start)
    mirror_src = src[mirror_start:mirror_end]
    assert "run_d1(" not in mirror_src
    assert "conn.execute(" in mirror_src


def test_never_writes_to_selection_decisions_or_coefficient_tables():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    for forbidden_table in ("selection_decisions", "coefficients"):
        assert not re.search(
            rf"\b(INSERT INTO|UPDATE|DELETE FROM)\s+{forbidden_table}\b", src, re.IGNORECASE
        ), f"found a write statement targeting {forbidden_table}"


def test_production_write_tables_are_only_ever_read_via_run_d1_not_written():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    for table in ("history", "btc_data", "predictions"):
        assert not re.search(rf"\b(INSERT INTO|UPDATE|DELETE FROM)\s+{table}\b", main_src, re.IGNORECASE)


def test_no_eia_or_gdelt_module_is_imported_or_called():
    """This script's own docstring legitimately DISCLOSES that it does
    not integrate EIA/GDELT (exactly the disclosure this project's
    documentation culture requires) -- that is not something to ban.
    What must never exist is an actual import of, or call into, an
    EIA/GDELT adapter module."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    assert "import eia_source" not in src
    assert "from eia_source" not in src
    assert "import gdelt_source" not in src
    assert "from gdelt_source" not in src
    assert "eia_source." not in src
    assert "gdelt_source." not in src


# ---- build_local_mirror ----

def test_build_local_mirror_creates_the_three_expected_tables():
    history_rows = [{"ts": 1000, "score": 55, "sources_json": json.dumps({"alpha": 40}), "gold_regime": "chop"}]
    btc_rows = [{"ts": 1000, "btc_price": 50000.0}]
    predictions_rows = [{"ts": 1000, "horizon_hours": 24, "p_up": 0.6, "realized_up": 1}]
    conn = run_experiment.build_local_mirror(history_rows, btc_rows, predictions_rows)
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM btc_data").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    # Never creates research_events/research_event_evidence -- EXP-010
    # doesn't use PR59's evidence-fetch path at all.
    with pytest.raises(Exception):
        conn.execute("SELECT COUNT(*) FROM research_events")
    conn.close()


def test_local_mirror_is_actually_usable_by_the_real_join_module():
    from datetime import datetime, timezone
    base_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    day = 24 * 3600000
    btc_rows = [{"ts": base_ts + i * day, "btc_price": 50000.0} for i in range(9)]
    btc_rows.append({"ts": base_ts + 9 * day, "btc_price": 50000.0 * 1.05})
    history_rows = [
        {"ts": base_ts + i * 6 * 3600000, "score": 50, "sources_json": json.dumps({"alpha": 20, "beta": 20}), "gold_regime": "chop"}
        for i in range(36)
    ]
    conn = run_experiment.build_local_mirror(history_rows, btc_rows, [])
    import exp010_source_dialogue_validation as join_module
    dataset = join_module.build_relationship_dataset(conn, base_ts, base_ts + 9 * day + 3600000)
    conn.close()
    for key in ("window", "events", "source_keys", "results"):
        assert key in dataset
    assert len(dataset["events"]) >= 1


# ---- D1 fetch shape ----

def test_main_fetches_predictions_scoped_to_horizons_12_and_24_only():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    assert "horizon_hours IN (12, 24)" in src


def test_main_exits_nonzero_on_insufficient_market_data():
    with patch.object(run_experiment, "run_d1", return_value=[]):
        with pytest.raises(SystemExit) as exc_info:
            run_experiment.main()
    assert exc_info.value.code == 1


# ---- persisted report: summaries only, never the full per-row join
# (payload-size discipline, applied proactively) ----

def test_persisted_report_never_contains_the_full_per_pair_results_array():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    assert '"relationship_summary"' in main_src
    assert '"redundancy_summary"' in main_src
    # The report dict must not embed the raw dataset["results"] list.
    assert 'relationship_dataset["results"]' not in main_src.split("report = {")[1].split("}")[0]


def test_sample_size_equals_the_disclosed_n_source_pair_observations():
    """Regression guard modeled directly on the EXP-009 sample_size
    audit finding: sample_size must describe the actual quantity this
    analysis is based on and disclosed in the report, not an
    unrelated/undisclosed count."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    assert 'sample_size = relationship_summary["n_source_pair_observations"]' in main_src


def test_main_end_to_end_with_mocked_transport_persists_a_summary_only_report():
    from datetime import datetime, timezone
    base_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    day = 24 * 3600000
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = now_ms - run_experiment.WINDOW_MS
    anchor = start_ts + 20 * day  # comfortably inside the fetch window

    btc_rows = [{"ts": anchor + i * day, "btc_price": 50000.0} for i in range(9)]
    btc_rows.append({"ts": anchor + 9 * day, "btc_price": 50000.0 * 1.05})
    history_rows = [
        {"ts": anchor + i * 6 * 3600000, "score": 50, "sources_json": json.dumps({"alpha": 20, "beta": 80}), "gold_regime": "chop"}
        for i in range(36)  # up to anchor + 210h, strictly before the elevated row below
    ]
    history_rows.append({"ts": anchor + 9 * day, "score": 50, "sources_json": json.dumps({"alpha": 90, "beta": 10}), "gold_regime": "chop"})

    call_order = ["history", "btc", "predictions"]
    fixtures = {"history": history_rows, "btc": btc_rows, "predictions": []}
    call_index = {"i": 0}

    def fake_run_d1(sql):
        key = call_order[call_index["i"]]
        call_index["i"] += 1
        return fixtures[key]

    captured = {}

    def fake_d1_api_query(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return []

    with patch.object(run_experiment, "run_d1", side_effect=fake_run_d1):
        with patch.object(run_experiment, "d1_api_query", side_effect=fake_d1_api_query):
            run_experiment.main()

    report = json.loads(captured["params"][5])
    assert "relationship_summary" in report
    assert "redundancy_summary" in report
    assert "results" not in report
    assert captured["params"][3] == report["relationship_summary"]["n_source_pair_observations"]


# ---- summarize_validation_status ----

def test_summarize_validation_status_comparable_pairs_observed():
    summary = {"by_relationship": {"SUPPORTING": 1, "CONTRADICTING": 0, "DIFFERENT_TIMING": 0, "INSUFFICIENT_EVIDENCE": 0}}
    assert run_experiment.summarize_validation_status(summary) == "comparable_pairs_observed"


def test_summarize_validation_status_insufficient_evidence():
    summary = {"by_relationship": {"SUPPORTING": 0, "CONTRADICTING": 0, "DIFFERENT_TIMING": 0, "INSUFFICIENT_EVIDENCE": 5}}
    assert run_experiment.summarize_validation_status(summary) == "insufficient_evidence"


# ---- d1_api_query: transport, auth, and failure semantics (identical
# contract to exp005/exp009's own already-audited implementation) ----

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


def test_d1_api_query_raises_when_envelope_reports_failure():
    def fake_urlopen(request, timeout=None):
        body = json.dumps({"success": False, "errors": [{"message": "nope"}], "result": []}).encode()
        return FakeHTTPResponse(200, body)

    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "t"}):
        with patch.object(run_experiment.urllib.request, "urlopen", side_effect=fake_urlopen):
            with pytest.raises(RuntimeError):
                run_experiment.d1_api_query("SELECT 1", [])


# ---- INSERT_ANALYSIS_SQL ----

def test_insert_sql_targets_research_analyses_with_correct_columns_and_placeholders():
    sql = run_experiment.INSERT_ANALYSIS_SQL
    assert sql.startswith("INSERT INTO research_analyses (")
    assert "subject" in sql and "metric_json" in sql
    assert re.match(r"^INSERT\s", sql)
    assert not re.search(r"\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b", sql)
    assert sql.count("?") == 8


def test_not_a_hypothesis_test_note_is_valid_json_and_explicit():
    parsed = json.loads(run_experiment.NOT_A_HYPOTHESIS_TEST_NOTE)
    assert parsed["applicable"] is False
    assert "reason" in parsed
