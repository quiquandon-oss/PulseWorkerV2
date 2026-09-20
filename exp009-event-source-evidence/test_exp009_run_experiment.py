"""
Tests for exp009-event-source-evidence/run_experiment.py.

Run with: pytest exp009-event-source-evidence/

These tests never touch production D1 -- run_d1()'s subprocess and
d1_api_query()'s urllib.request.urlopen are always mocked here.
research/event_source_evidence_join.py (and everything it reuses) is
imported UNCHANGED and exercised against small synthetic in-memory
data in its own dedicated test file
(research/test_event_source_evidence_join.py, already 21 tests
strong) -- these tests cover only this script's own orchestration:
mirror construction, D1 fetch shape, the D1 API transport, and the one
summary label it adds.
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

import event_detector as ed  # noqa: E402
import event_source_evidence_join as join_module  # noqa: E402


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
    assert run_experiment.SUBJECT == "EXP-009:event_source_evidence"


def test_subject_is_distinct_from_exp005():
    assert run_experiment.SUBJECT != "EXP-005:source_effectiveness"


def test_window_ms_matches_event_detector_own_bound_unchanged():
    assert run_experiment.WINDOW_MS == ed.MAX_WINDOW_MS


def test_lookback_buffer_matches_event_detector_own_bound_unchanged():
    assert run_experiment.LOOKBACK_BUFFER_MS == ed.LOOKBACK_BUFFER_MS


def test_exp005_run_experiment_is_never_imported_by_this_script():
    """EXP-005 is mentioned in prose (this module's own docstring
    explains its relationship to EXP-005 and reuses the same
    d1_api_query design EXP-005 arrived at) -- that disclosure is
    exactly what makes this code auditable, and must not be banned.
    What actually matters is that no code here imports or executes
    EXP-005's own script."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    assert "import exp005" not in src
    assert "from exp005" not in src


def test_main_builds_its_only_production_write_via_d1_api_query():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    assert "INSERT INTO" not in main_src
    assert "UPDATE " not in main_src
    assert "DELETE FROM" not in main_src
    assert main_src.count("d1_api_query(") == 1
    assert main_src.count("run_d1(") == 5  # history, btc_data, predictions, research_events, research_event_evidence


def test_build_local_mirror_writes_are_scoped_to_its_own_function_not_run_d1():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    mirror_start = src.index("def build_local_mirror(")
    mirror_end = src.index("\n\n\n", mirror_start)
    mirror_src = src[mirror_start:mirror_end]
    assert "run_d1(" not in mirror_src
    assert "conn.execute(" in mirror_src


def test_never_writes_to_selection_decisions_or_coefficient_tables():
    """This script has zero legitimate reason to write to
    selection_decisions or any coefficient table, even in the local
    mirror -- unlike history/btc_data/predictions/research_events(_
    evidence), which legitimately appear in build_local_mirror's own
    (local-only, never production) INSERT statements."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    for forbidden_table in ("selection_decisions", "coefficients"):
        assert not re.search(
            rf"\b(INSERT INTO|UPDATE|DELETE FROM)\s+{forbidden_table}\b", src, re.IGNORECASE
        ), f"found a write statement targeting {forbidden_table}"


def test_production_write_tables_are_only_ever_read_via_run_d1_not_written():
    """history/btc_data/predictions/research_events/research_event_
    evidence DO appear in INSERT statements in this file (build_local_
    mirror's own local-only mirror) -- confirm those INSERTs are scoped
    to build_local_mirror and never reachable via run_d1/d1_api_query,
    which is the only path that could reach production. This
    complements test_build_local_mirror_writes_are_scoped_to_its_own_
    function_not_run_d1 by checking it from the main()/run_d1 side too."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    for table in ("history", "btc_data", "predictions", "research_events", "research_event_evidence"):
        assert not re.search(rf"\b(INSERT INTO|UPDATE|DELETE FROM)\s+{table}\b", main_src, re.IGNORECASE)


# ---- build_local_mirror ----

def test_build_local_mirror_creates_all_five_expected_tables_and_rows():
    history_rows = [{"ts": 1000, "score": 55, "sources_json": json.dumps({"fng": 40}), "gold_regime": "chop"}]
    btc_rows = [{"ts": 1000, "btc_price": 50000.0}]
    predictions_rows = [{"ts": 1000, "horizon_hours": 24, "p_up": 0.6, "realized_up": 1}]
    research_events_rows = [{"event_id": 7, "event_ts": 1000}]
    research_event_evidence_rows = [{
        "evidence_id": 1, "event_id": 7, "feed_url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "article_url": "https://example.test/a", "publisher": "BBC World News",
        "publication_ts": 900, "collection_ts": 1000, "headline": "Test headline",
        "keyword_score": 1.0, "evidence_relation": "PRE_EVENT", "content_hash": "abc",
    }]
    conn = run_experiment.build_local_mirror(
        history_rows, btc_rows, predictions_rows, research_events_rows, research_event_evidence_rows
    )
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM btc_data").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    assert conn.execute("SELECT event_id, event_ts FROM research_events").fetchone() == (7, 1000)
    row = conn.execute("SELECT evidence_id, event_id FROM research_event_evidence").fetchone()
    assert row == (1, 7)
    conn.close()


def test_local_mirror_is_actually_usable_by_the_real_join_module():
    """End-to-end sanity: feed a small synthetic dataset through the
    REAL, unchanged join_module.build_event_source_evidence_dataset()
    and confirm it runs to completion -- proves the mirror's schema
    genuinely matches what the reused modules' own SQL expects."""
    from datetime import datetime, timezone
    base_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    day = 24 * 3600000
    btc_rows = [{"ts": base_ts + i * day, "btc_price": 50000.0} for i in range(9)]
    btc_rows.append({"ts": base_ts + 9 * day, "btc_price": 50000.0 * 1.05})
    history_rows = [
        {"ts": base_ts + i * 6 * 3600000, "score": 50, "sources_json": json.dumps({"geopolitics": 20}), "gold_regime": "chop"}
        for i in range(36)
    ]
    conn = run_experiment.build_local_mirror(history_rows, btc_rows, [], [], [])
    dataset = join_module.build_event_source_evidence_dataset(conn, base_ts, base_ts + 9 * day + 3600000)
    conn.close()
    for key in ("window", "events", "source_keys", "results"):
        assert key in dataset
    assert len(dataset["events"]) >= 1


# ---- D1 fetch shape ----

def test_main_fetches_predictions_scoped_to_horizons_12_and_24_only():
    """event_detector.detect_v2_failure_clusters is only ever called
    (by the reused modules) for horizon_hours in (12, 24) -- the
    predictions fetch must be scoped to match, never wider."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    assert "horizon_hours IN (12, 24)" in src


def test_main_scopes_research_event_evidence_fetch_to_the_same_window_as_research_events():
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    assert "research_event_evidence WHERE event_id IN" in src
    assert "SELECT event_id FROM research_events WHERE event_ts >=" in src


def test_main_exits_nonzero_on_insufficient_market_data():
    with patch.object(run_experiment, "run_d1", return_value=[]):
        with pytest.raises(SystemExit) as exc_info:
            run_experiment.main()
    assert exc_info.value.code == 1


# ---- persisted-report scoping: internal-model events excluded ----

def test_main_filters_internal_model_events_out_of_persisted_results_but_discloses_the_count():
    """V2_FAILURE_CLUSTER events are unconditionally INSUFFICIENT_EVIDENCE
    (never a real-world-evidence candidate) -- persisting their full
    per-source detail only adds bulk, which matters because this
    report's size grows with accumulated events over the whole 90-day
    window (unlike EXP-005's fixed 21-source ceiling). main() must
    filter them from the persisted events/results while still counting
    them explicitly, not silently dropping the fact that they existed."""
    with open(os.path.join(_HERE, "run_experiment.py")) as f:
        src = f.read()
    main_start = src.index("def main():")
    main_src = src[main_start:src.index("\nif __name__")]
    assert "is_internal_model_event" in main_src
    assert "n_internal_model_events_excluded_from_results" in main_src


# ---- summarize_validation_status ----

def test_summarize_validation_status_evidence_observed():
    coverage = {"n_real_world_events_with_any_evidence": 2}
    assert run_experiment.summarize_validation_status(coverage) == "evidence_observed"


def test_summarize_validation_status_insufficient_evidence():
    coverage = {"n_real_world_events_with_any_evidence": 0}
    assert run_experiment.summarize_validation_status(coverage) == "insufficient_evidence"


# ---- d1_api_query: transport, auth, and failure semantics (identical
# contract to exp005-source-effectiveness's own already-audited
# implementation, re-verified here since this is a separate, duplicated
# function in a separate script) ----

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


def test_d1_api_query_raises_when_envelope_reports_failure():
    def fake_urlopen(request, timeout=None):
        body = json.dumps({"success": False, "errors": [{"message": "nope"}], "result": []}).encode()
        return FakeHTTPResponse(200, body)

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
