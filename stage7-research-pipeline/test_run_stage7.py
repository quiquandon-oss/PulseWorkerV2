"""
Tests for stage7-research-pipeline/run_stage7.py's own pure functions
(group_results_by_event, select_eligible_events, resolve_real_event_ids,
classify_stage7_schema_state). No DB, no network, no wrangler/D1 API
calls -- run_d1/d1_api_query are exercised only against real
infrastructure ordinarily (same convention as exp009-event-source-
evidence, which only unit-tests its own pure helpers). The migration-
gating tests below are the one exception: they call the real main(),
but with run_d1 monkeypatched to a fake that returns a specific,
already-fixed response shape for the ONE schema-check query main() issues
before touching anything else -- this proves main()'s own early-exit
wiring (not just the pure classifier) without ever needing a live D1.

Run with: python3 -m pytest stage7-research-pipeline/ -v
"""
import json
import sys
import os
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import run_stage7 as rs  # noqa: E402


def _dataset(events, results):
    return {"events": events, "results": results}


def _set_valid_staging_env(monkeypatch):
    """Every test that calls the real rs.main() must first configure the
    staging target env vars main() now validates before its first D1
    call -- otherwise validate_staging_target() aborts before run_d1 (real
    or monkeypatched) is ever reached. Values match wrangler.staging.toml
    / EXPECTED_STAGING_* exactly."""
    monkeypatch.setenv("STAGE7_TARGET_ACCOUNT_ID", rs.EXPECTED_STAGING_ACCOUNT_ID)
    monkeypatch.setenv("STAGE7_TARGET_DATABASE_NAME", rs.EXPECTED_STAGING_DATABASE_NAME)
    monkeypatch.setenv("STAGE7_TARGET_DATABASE_ID", rs.EXPECTED_STAGING_DATABASE_ID)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-staging-token-for-tests")


def test_group_results_by_event_keys_by_event_ts_not_local_event_id():
    # Two events sharing the SAME session-local event_id (as can happen
    # since collect_events() reassigns 1..N on every run) must never be
    # merged -- they are correctly kept apart because grouping is keyed
    # by event_ts, the only stable join key available.
    events = [
        {"event_id": 1, "event_ts": 1000, "category": "LARGE_MOVE", "is_internal_model_event": False},
        {"event_id": 1, "event_ts": 2000, "category": "REGIME_REVERSAL", "is_internal_model_event": False},
    ]
    results = [
        {"event_ts": 1000, "source_key": "fng", "relevance_result": "RELEVANT",
         "event_source_interpretation": "EVENT_SOURCE_ALIGNED"},
        {"event_ts": 2000, "source_key": "fng", "relevance_result": "NOT_RELEVANT",
         "event_source_interpretation": "INSUFFICIENT_EVIDENCE"},
    ]
    events_by_ts, relevance_by_ts, interpretation_by_ts = rs.group_results_by_event(_dataset(events, results))
    assert set(events_by_ts) == {1000, 2000}
    assert relevance_by_ts[1000]["fng"]["result"] == "RELEVANT"
    assert relevance_by_ts[2000]["fng"]["result"] == "NOT_RELEVANT"
    assert interpretation_by_ts[1000]["fng"] == "EVENT_SOURCE_ALIGNED"


def test_group_results_by_event_excludes_internal_model_events():
    events = [
        {"event_id": 1, "event_ts": 1000, "category": "LARGE_MOVE", "is_internal_model_event": False},
        {"event_id": 2, "event_ts": 2000, "category": "V2_FAILURE_CLUSTER", "is_internal_model_event": True},
    ]
    results = [
        {"event_ts": 1000, "source_key": "fng", "relevance_result": "RELEVANT",
         "event_source_interpretation": "EVENT_SOURCE_ALIGNED"},
        {"event_ts": 2000, "source_key": "fng", "relevance_result": "RELEVANT",
         "event_source_interpretation": "EVENT_SOURCE_ALIGNED"},
    ]
    events_by_ts, relevance_by_ts, _ = rs.group_results_by_event(_dataset(events, results))
    assert set(events_by_ts) == {1000}
    assert 2000 not in relevance_by_ts


def test_select_eligible_events_keeps_only_events_within_max_age():
    now_ts = 10_000_000
    events_by_ts = {
        1_000_000: {"event_ts": 1_000_000},  # within 6 days
        now_ts - 7 * 24 * 3600000: {"event_ts": now_ts - 7 * 24 * 3600000},  # older than 6 days
    }
    eligible = rs.select_eligible_events(events_by_ts, now_ts)
    assert set(eligible) == {1_000_000}


def test_select_eligible_events_boundary_is_inclusive():
    now_ts = rs.MAX_EVENT_AGE_FOR_STAGE7_MS
    events_by_ts = {0: {"event_ts": 0}}
    eligible = rs.select_eligible_events(events_by_ts, now_ts)
    assert 0 in eligible


def test_resolve_real_event_ids_maps_via_event_ts_and_overwrites_event_id():
    events_by_ts = {1000: {"event_id": 7, "event_ts": 1000, "category": "LARGE_MOVE"}}
    research_events_rows = [{"event_id": 42, "event_ts": 1000}]
    resolved = rs.resolve_real_event_ids(events_by_ts, research_events_rows)
    assert set(resolved) == {42}
    assert resolved[42]["event_id"] == 42  # session-local id 7 must be overwritten
    assert resolved[42]["category"] == "LARGE_MOVE"  # other fields preserved


def test_resolve_real_event_ids_drops_events_with_no_persisted_row():
    # A freshly-detected event this run's own local mirror sees but that
    # live_evidence_pipeline.py has not yet persisted to research_events
    # must never be written under an invented event_id.
    events_by_ts = {1000: {"event_id": 1, "event_ts": 1000}, 2000: {"event_id": 2, "event_ts": 2000}}
    research_events_rows = [{"event_id": 42, "event_ts": 1000}]  # 2000 not persisted
    resolved = rs.resolve_real_event_ids(events_by_ts, research_events_rows)
    assert set(resolved) == {42}


def test_resolve_real_event_ids_two_local_events_map_to_distinct_real_ids():
    events_by_ts = {1000: {"event_id": 1, "event_ts": 1000}, 2000: {"event_id": 1, "event_ts": 2000}}
    research_events_rows = [{"event_id": 10, "event_ts": 1000}, {"event_id": 11, "event_ts": 2000}]
    resolved = rs.resolve_real_event_ids(events_by_ts, research_events_rows)
    assert set(resolved) == {10, 11}
    assert resolved[10]["event_id"] == 10
    assert resolved[11]["event_id"] == 11


# =====================================================================
# Adversarial-review remediation, finding #3: migration-missing gating.
# classify_stage7_schema_state() is the pure decision function main()
# calls right after a real `SELECT name FROM sqlite_master ...` query --
# that query itself is not mocked here (no live D1/wrangler in tests,
# same constraint every other test file in this repo already follows),
# but its three possible real-world outcomes (all three tables present,
# none present, some present) are exactly what this function classifies,
# so pinning its behavior directly covers both the "normal execution"
# and "missing schema" paths main() actually takes.
# =====================================================================

def test_schema_state_ready_when_all_three_tables_present():
    assert rs.classify_stage7_schema_state(
        {"stage7_research_requests", "stage7_research_responses", "stage7_event_sentiment"}
    ) == "READY"


def test_schema_state_not_applied_when_none_present():
    assert rs.classify_stage7_schema_state(set()) == "NOT_APPLIED"
    assert rs.classify_stage7_schema_state({"history", "btc_data", "predictions"}) == "NOT_APPLIED"


def test_schema_state_partial_when_some_but_not_all_present():
    assert rs.classify_stage7_schema_state({"stage7_research_requests"}) == "PARTIAL"
    assert rs.classify_stage7_schema_state({"stage7_research_requests", "stage7_research_responses"}) == "PARTIAL"


def test_schema_state_ready_ignores_unrelated_extra_tables():
    assert rs.classify_stage7_schema_state(
        {"stage7_research_requests", "stage7_research_responses", "stage7_event_sentiment", "history", "predictions"}
    ) == "READY"


def test_main_returns_clean_skip_result_when_schema_not_applied(monkeypatch):
    # Simulates the exact "normal execution path" main() takes once the
    # schema query itself SUCCEEDS but finds zero of the three tables --
    # run_d1 is monkeypatched only for this one call shape, never for
    # anything that would mask a real, unrelated D1 error.
    _set_valid_staging_env(monkeypatch)

    def fake_run_d1(sql):
        assert "sqlite_master" in sql
        return []  # none of the three Stage7 tables exist
    monkeypatch.setattr(rs, "run_d1", fake_run_d1)
    result = rs.main()
    assert result["ok"] is True
    assert result["status"] == "SKIPPED -- MIGRATION NOT APPLIED"
    assert result["events_considered"] == 0
    assert result["candidates_proposed"] == 0
    assert result["sentiment_rows_written"] == 0


def test_main_raises_loudly_on_partial_schema_never_silently_skips(monkeypatch):
    _set_valid_staging_env(monkeypatch)

    def fake_run_d1(sql):
        assert "sqlite_master" in sql
        return [{"name": "stage7_research_requests"}]  # only one of three
    orig = rs.run_d1
    rs.run_d1 = fake_run_d1
    try:
        with pytest.raises(RuntimeError, match="PARTIALLY applied"):
            rs.main()
    finally:
        rs.run_d1 = orig


def test_main_does_not_swallow_an_unrelated_run_d1_error(monkeypatch):
    # A real connectivity/permission/malformed-SQL failure on the schema
    # check itself must propagate exactly as it did before this fix --
    # never reinterpreted as "migration not applied".
    _set_valid_staging_env(monkeypatch)

    def failing_run_d1(sql):
        raise RuntimeError("wrangler: authentication error")
    orig = rs.run_d1
    rs.run_d1 = failing_run_d1
    try:
        with pytest.raises(RuntimeError, match="authentication error"):
            rs.main()
    finally:
        rs.run_d1 = orig


# =====================================================================
# Regression: a real local `wrangler dev` end-to-end run (against a
# fully-migrated local D1, disposable synthetic events, no production
# access) found that main() was passing a 98-day window (WINDOW_MS +
# LOOKBACK_BUFFER_MS) to join_module.build_event_source_evidence_dataset,
# whose own _validate_window() hard-rejects anything wider than
# event_detector.MAX_WINDOW_MS (90 days) -- every single real run of this
# script would have raised ValueError and crashed, unconditionally, the
# moment migration 0016 was applied. No existing fixture-based test
# caught this, because none of them call the real detector with a real
# window end to end. Fixed by separating detection_start_ts (exactly
# WINDOW_MS wide, what the detector receives) from fetch_start_ts (wider,
# lookback-inclusive, used only for the raw D1 SELECTs) -- the same split
# exp009-event-source-evidence/run_experiment.py's own start_ts/
# fetch_start_ts already uses.
# =====================================================================

def test_main_passes_exactly_window_ms_to_the_detector_never_window_plus_lookback(monkeypatch):
    _set_valid_staging_env(monkeypatch)
    calls = {}

    def fake_run_d1(sql):
        if "sqlite_master" in sql:
            return [{"name": t} for t in rs.REQUIRED_STAGE7_TABLES]
        if "pragma_table_info" in sql:
            table = sql.split("pragma_table_info('")[1].split("')")[0]
            return [{"name": c} for c in rs.REQUIRED_STAGE7_COLUMNS.get(table, {})]
        return []  # history/btc_data/predictions/research_events all empty

    def fake_build_dataset(conn, start_ts, end_ts):
        calls["start_ts"] = start_ts
        calls["end_ts"] = end_ts
        return {"events": [], "results": []}

    orig_run_d1 = rs.run_d1
    orig_build = rs.join_module.build_event_source_evidence_dataset
    rs.run_d1 = fake_run_d1
    rs.join_module.build_event_source_evidence_dataset = fake_build_dataset
    try:
        result = rs.main()
    finally:
        rs.run_d1 = orig_run_d1
        rs.join_module.build_event_source_evidence_dataset = orig_build

    assert result["ok"] is True
    window_passed_to_detector = calls["end_ts"] - calls["start_ts"]
    # Exactly WINDOW_MS (== event_detector.MAX_WINDOW_MS, imported as an
    # alias) -- never wider. The old, broken value was WINDOW_MS +
    # LOOKBACK_BUFFER_MS, which this equality would catch immediately.
    assert window_passed_to_detector == rs.WINDOW_MS


def test_main_fetch_queries_use_a_wider_lookback_inclusive_bound_than_the_detector_window(monkeypatch):
    _set_valid_staging_env(monkeypatch)
    fetch_sqls = []

    def fake_run_d1(sql):
        if "sqlite_master" in sql:
            return [{"name": t} for t in rs.REQUIRED_STAGE7_TABLES]
        if "pragma_table_info" in sql:
            table = sql.split("pragma_table_info('")[1].split("')")[0]
            return [{"name": c} for c in rs.REQUIRED_STAGE7_COLUMNS.get(table, {})]
        if "BETWEEN" in sql:
            fetch_sqls.append(sql)
        return []

    def fake_build_dataset(conn, start_ts, end_ts):
        return {"events": [], "results": []}

    now_before = int(time.time() * 1000)
    orig_run_d1 = rs.run_d1
    orig_build = rs.join_module.build_event_source_evidence_dataset
    rs.run_d1 = fake_run_d1
    rs.join_module.build_event_source_evidence_dataset = fake_build_dataset
    try:
        rs.main()
    finally:
        rs.run_d1 = orig_run_d1
        rs.join_module.build_event_source_evidence_dataset = orig_build
    now_after = int(time.time() * 1000)

    assert len(fetch_sqls) >= 4  # history, btc_data, predictions, research_events
    expected_detection_start = now_before - rs.WINDOW_MS  # give or take main()'s own now_ts call
    for sql in fetch_sqls:
        fetch_start = int(sql.split("BETWEEN")[1].split("AND")[0].strip())
        # The raw fetch bound must be strictly EARLIER than the narrower
        # detection window's own start -- i.e. it really does include the
        # extra LOOKBACK_BUFFER_MS (exp009's own pattern), rather than
        # accidentally being narrowed to match the detector's window.
        assert fetch_start <= expected_detection_start - rs.LOOKBACK_BUFFER_MS + (now_after - now_before)
        assert fetch_start >= expected_detection_start - rs.LOOKBACK_BUFFER_MS - (now_after - now_before) - 1000


# =====================================================================
# Copilot-audit remediation: explicit, fail-closed staging-only D1
# target. validate_staging_target() must run -- and succeed -- before
# ANY remote D1 access; run_d1()/d1_api_query() must both read the exact
# same validated config, never independent constants, and must never
# default or fall back to production's own sentiment-history database.
# =====================================================================

def _valid_target_env(**overrides):
    env = {
        "STAGE7_TARGET_ACCOUNT_ID": rs.EXPECTED_STAGING_ACCOUNT_ID,
        "STAGE7_TARGET_DATABASE_NAME": rs.EXPECTED_STAGING_DATABASE_NAME,
        "STAGE7_TARGET_DATABASE_ID": rs.EXPECTED_STAGING_DATABASE_ID,
        "CLOUDFLARE_API_TOKEN": "fake-staging-token",
    }
    env.update(overrides)
    return env


def test_validate_staging_target_accepts_the_correct_staging_configuration():
    config = rs.validate_staging_target(env=_valid_target_env())
    assert config.account_id == rs.EXPECTED_STAGING_ACCOUNT_ID
    assert config.database_name == rs.EXPECTED_STAGING_DATABASE_NAME
    assert config.database_id == rs.EXPECTED_STAGING_DATABASE_ID
    assert config.api_token == "fake-staging-token"


def test_validate_staging_target_raises_before_any_remote_access_when_env_completely_missing():
    with pytest.raises(RuntimeError, match="missing/empty required staging target environment"):
        rs.validate_staging_target(env={})


def test_validate_staging_target_raises_when_any_single_var_is_missing():
    for missing_key in ("STAGE7_TARGET_ACCOUNT_ID", "STAGE7_TARGET_DATABASE_NAME",
                        "STAGE7_TARGET_DATABASE_ID", "CLOUDFLARE_API_TOKEN"):
        env = _valid_target_env()
        del env[missing_key]
        with pytest.raises(RuntimeError, match=missing_key):
            rs.validate_staging_target(env=env)


def test_validate_staging_target_raises_when_a_var_is_present_but_empty():
    env = _valid_target_env(STAGE7_TARGET_DATABASE_NAME="   ")
    with pytest.raises(RuntimeError, match="STAGE7_TARGET_DATABASE_NAME"):
        rs.validate_staging_target(env=env)


def test_validate_staging_target_rejects_production_database_name():
    env = _valid_target_env(STAGE7_TARGET_DATABASE_NAME=rs.PRODUCTION_DATABASE_NAME)
    with pytest.raises(RuntimeError, match="PRODUCTION"):
        rs.validate_staging_target(env=env)


def test_validate_staging_target_rejects_production_database_id():
    env = _valid_target_env(STAGE7_TARGET_DATABASE_ID=rs.PRODUCTION_DATABASE_ID)
    with pytest.raises(RuntimeError, match="PRODUCTION"):
        rs.validate_staging_target(env=env)


def test_validate_staging_target_rejects_production_name_even_when_id_field_still_says_staging():
    # Belt-and-braces: a mismatched name alone is rejected even when the
    # id field still holds the real staging id -- in case the two
    # identifiers are ever set independently/inconsistently by mistake.
    env = _valid_target_env(STAGE7_TARGET_DATABASE_NAME=rs.PRODUCTION_DATABASE_NAME)
    with pytest.raises(RuntimeError, match="PRODUCTION"):
        rs.validate_staging_target(env=env)


def test_validate_staging_target_rejects_a_wrong_staging_database_id_that_is_not_production_either():
    # Distinct from "matches production": a typo'd or unrelated database
    # id that is neither the real staging id nor production's must still
    # be rejected.
    env = _valid_target_env(STAGE7_TARGET_DATABASE_ID="00000000-0000-0000-0000-000000000000")
    with pytest.raises(RuntimeError, match="does not match the expected staging target"):
        rs.validate_staging_target(env=env)


def test_validate_staging_target_rejects_wrong_account_id():
    env = _valid_target_env(STAGE7_TARGET_ACCOUNT_ID="wrong-account-id")
    with pytest.raises(RuntimeError, match="does not match the expected staging target"):
        rs.validate_staging_target(env=env)


def test_missing_staging_token_fails_closed_even_when_database_target_is_correct():
    env = _valid_target_env(CLOUDFLARE_API_TOKEN="")
    with pytest.raises(RuntimeError, match="CLOUDFLARE_API_TOKEN"):
        rs.validate_staging_target(env=env)


def test_main_aborts_before_any_remote_d1_call_when_staging_target_is_not_configured(monkeypatch):
    # Missing database configuration fails before remote access: run_d1
    # is monkeypatched to a recorder that fails the test if it is ever
    # actually invoked, proving main() never reaches its first remote
    # call once validate_staging_target() raises.
    monkeypatch.delenv("STAGE7_TARGET_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("STAGE7_TARGET_DATABASE_NAME", raising=False)
    monkeypatch.delenv("STAGE7_TARGET_DATABASE_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    def run_d1_must_not_be_called(sql):
        raise AssertionError("run_d1() must never be called when the staging target is unconfigured")
    monkeypatch.setattr(rs, "run_d1", run_d1_must_not_be_called)

    with pytest.raises(RuntimeError, match="missing/empty required staging target environment"):
        rs.main()


def test_main_aborts_before_any_remote_d1_call_when_staging_token_is_missing(monkeypatch):
    monkeypatch.setenv("STAGE7_TARGET_ACCOUNT_ID", rs.EXPECTED_STAGING_ACCOUNT_ID)
    monkeypatch.setenv("STAGE7_TARGET_DATABASE_NAME", rs.EXPECTED_STAGING_DATABASE_NAME)
    monkeypatch.setenv("STAGE7_TARGET_DATABASE_ID", rs.EXPECTED_STAGING_DATABASE_ID)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    def run_d1_must_not_be_called(sql):
        raise AssertionError("run_d1() must never be called when the staging token is missing")
    monkeypatch.setattr(rs, "run_d1", run_d1_must_not_be_called)

    with pytest.raises(RuntimeError, match="CLOUDFLARE_API_TOKEN"):
        rs.main()


def test_run_d1_refuses_to_run_when_target_config_not_yet_validated(monkeypatch):
    monkeypatch.setattr(rs, "_TARGET_CONFIG", None)
    with pytest.raises(RuntimeError, match="before validate_staging_target"):
        rs.run_d1("SELECT 1")


def test_d1_api_query_refuses_to_run_when_target_config_not_yet_validated(monkeypatch):
    monkeypatch.setattr(rs, "_TARGET_CONFIG", None)
    with pytest.raises(RuntimeError, match="before validate_staging_target"):
        rs.d1_api_query("SELECT 1", [])


def test_run_d1_and_d1_api_query_both_read_the_same_validated_target(monkeypatch):
    # Both D1 access paths use the same validated staging target: after
    # validate_staging_target() runs, run_d1() (wrangler CLI) and
    # d1_api_query() (Cloudflare REST API) are proven -- by actually
    # inspecting the subprocess/urllib calls each one makes -- to use the
    # identical account_id/database_name/database_id, never independent
    # values.
    config = rs.validate_staging_target(env=_valid_target_env())
    monkeypatch.setattr(rs, "_TARGET_CONFIG", config)

    captured = {}

    def fake_subprocess_run(args, **kwargs):
        captured["run_d1_database_name"] = args[3]  # ["wrangler","d1","execute",<name>,...]

        class FakeCompletedProcess:
            stdout = '[{"results": []}]'
        return FakeCompletedProcess()
    monkeypatch.setattr(rs.subprocess, "run", fake_subprocess_run)
    rs.run_d1("SELECT 1")

    class FakeHTTPResponse:
        status = 200

        def read(self):
            return b'{"success": true, "result": [{"success": true, "results": []}]}'

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(request, timeout=60):
        captured["d1_api_url"] = request.full_url
        return FakeHTTPResponse()
    monkeypatch.setattr(rs.urllib.request, "urlopen", fake_urlopen)
    rs.d1_api_query("SELECT 1", [])

    assert captured["run_d1_database_name"] == config.database_name
    assert config.account_id in captured["d1_api_url"]
    assert config.database_id in captured["d1_api_url"]


# =====================================================================
# M1 fix: FAILED_RETRYABLE requests are now actually retried on a
# subsequent scheduled run, instead of being published once and then
# stuck forever. build_retry_request() (pure) and
# retry_stage7_request_publish() (the one D1/publish side effect) are
# tested directly here; the fuller main()-level routing decision (retry
# vs. create vs. leave alone, keyed off each existing request's own
# status) is tested further below.
# =====================================================================

def _existing_request_row(**overrides):
    row = {
        "event_id": 42,
        "request_id": "stage7-req-42-1",
        "status": "FAILED_RETRYABLE",
        "publish_attempts": 1,
        "created_ts": 1000,
        "historical_cutoff_ts": 950,
        "sufficiency_status": "INSUFFICIENT_EVIDENCE",
        "reasons_json": json.dumps(["no evidence rows exist"]),
        "questions_json": json.dumps(["what happened?"]),
        "missing_categories_json": json.dumps(["primary_reporting"]),
        "evidence_snapshot_json": json.dumps([{"evidence_id": 1}]),
    }
    row.update(overrides)
    return row


def test_build_retry_request_reconstructs_original_fields_from_the_stored_row():
    # Deliberately uses the row's OWN created_ts/reasons/questions/
    # missing_categories/evidence_snapshot -- never today's now_ts or
    # freshly re-queried evidence -- so a retry publishes the identical
    # file a successful first attempt would have.
    existing = _existing_request_row()
    event = {"event_id": 42, "event_ts": 950, "category": "LARGE_MOVE", "coin": "BTC"}
    request = rs.build_retry_request(existing, event)
    assert request["request_id"] == "stage7-req-42-1"
    assert request["event_id"] == 42
    assert request["created_ts"] == 1000
    assert request["historical_cutoff_ts"] == 950
    assert request["sufficiency_status"] == "INSUFFICIENT_EVIDENCE"
    assert request["reasons"] == ["no evidence rows exist"]
    assert request["questions"] == ["what happened?"]
    assert request["missing_categories"] == ["primary_reporting"]
    assert request["event"] is event
    assert request["evidence_snapshot"] == [{"evidence_id": 1}]


def test_build_retry_request_passes_through_stored_prompt_text():
    # Every request created via the human-controlled candidate flow
    # (worker.js's createStage7ResearchRequests()) has an exact prompt_text
    # already stored -- this must be reused verbatim on retry/first-publish,
    # never regenerated by Python's own build_research_prompt().
    existing = _existing_request_row(prompt_text="EXACT PROMPT THE HUMAN SAW")
    event = {"event_id": 42, "event_ts": 950}
    request = rs.build_retry_request(existing, event)
    assert request["prompt_text"] == "EXACT PROMPT THE HUMAN SAW"


def test_build_retry_request_prompt_text_is_none_when_row_has_none():
    # A legacy row created before this column existed (or created outside
    # the candidate flow) has no prompt_text -- build_retry_request must
    # never crash on the missing key, just pass through None so
    # stage7_github_publisher.build_request_file_content() falls back to
    # its own build_research_prompt().
    existing = _existing_request_row()
    existing.pop("prompt_text", None)
    event = {"event_id": 42, "event_ts": 950}
    request = rs.build_retry_request(existing, event)
    assert request["prompt_text"] is None


def test_build_candidate_id_is_deterministic():
    assert rs.build_candidate_id(42) == rs.build_candidate_id(42)
    assert rs.build_candidate_id(42) != rs.build_candidate_id(43)
    assert rs.build_candidate_id(42) == "stage7-cand-42"


def test_retry_stage7_request_publish_success_updates_row_to_published(monkeypatch):
    existing = _existing_request_row(publish_attempts=1)
    event = {"event_id": 42, "event_ts": 950, "category": "LARGE_MOVE"}
    monkeypatch.setattr(
        rs.pub, "publish_request_file",
        lambda request, repo_dir: {"published": True, "path": "research/stage7_requests/stage7-req-42-1.json",
                                    "error": None, "skipped_unchanged": False},
    )
    calls = []
    monkeypatch.setattr(rs, "d1_api_query", lambda sql, params: calls.append((sql, params)) or [])

    succeeded = rs.retry_stage7_request_publish(existing, event, now_ts=5000)

    assert succeeded is True
    assert len(calls) == 1
    sql, params = calls[0]
    assert "UPDATE stage7_research_requests" in sql
    assert "status IN ('PENDING_RESEARCH', 'FAILED_RETRYABLE')" in sql  # defensive WHERE guard, never an unconditional UPDATE
    assert params == [
        "RESEARCH_REQUEST_PUBLISHED", 5000, 2,
        "research/stage7_requests/stage7-req-42-1.json", 5000, None,
        "stage7-req-42-1",
    ]


def test_retry_stage7_request_publish_failure_below_max_stays_retryable(monkeypatch):
    existing = _existing_request_row(publish_attempts=1)
    event = {"event_id": 42, "event_ts": 950, "category": "LARGE_MOVE"}
    monkeypatch.setattr(
        rs.pub, "publish_request_file",
        lambda request, repo_dir: {"published": False, "path": "research/stage7_requests/stage7-req-42-1.json",
                                    "error": "`git push` failed: network error", "skipped_unchanged": False},
    )
    calls = []
    monkeypatch.setattr(rs, "d1_api_query", lambda sql, params: calls.append((sql, params)) or [])

    succeeded = rs.retry_stage7_request_publish(existing, event, now_ts=5000)

    assert succeeded is False
    _sql, params = calls[0]
    assert params == [
        "FAILED_RETRYABLE", 5000, 2,
        None, None, "`git push` failed: network error",
        "stage7-req-42-1",
    ]


def test_retry_stage7_request_publish_failure_reaching_max_attempts_gives_up_permanently(monkeypatch):
    # publish_attempts already at MAX_PUBLISH_ATTEMPTS - 1 -- this failing
    # attempt is the last one that will ever be tried automatically.
    existing = _existing_request_row(publish_attempts=rs.pub.MAX_PUBLISH_ATTEMPTS - 1)
    event = {"event_id": 42, "event_ts": 950, "category": "LARGE_MOVE"}
    monkeypatch.setattr(
        rs.pub, "publish_request_file",
        lambda request, repo_dir: {"published": False, "path": "research/stage7_requests/stage7-req-42-1.json",
                                    "error": "`git push` failed: permission denied", "skipped_unchanged": False},
    )
    calls = []
    monkeypatch.setattr(rs, "d1_api_query", lambda sql, params: calls.append((sql, params)) or [])

    succeeded = rs.retry_stage7_request_publish(existing, event, now_ts=5000)

    assert succeeded is False
    _sql, params = calls[0]
    assert params[0] == "FAILED_PERMANENT"
    assert params[2] == rs.pub.MAX_PUBLISH_ATTEMPTS


def test_retry_stage7_request_publish_never_creates_a_new_row(monkeypatch):
    # Only ever an UPDATE against the SAME request_id -- never an INSERT,
    # which would either violate the partial unique index (if the old row
    # is still non-terminal) or duplicate the request outright.
    existing = _existing_request_row()
    event = {"event_id": 42, "event_ts": 950, "category": "LARGE_MOVE"}
    monkeypatch.setattr(
        rs.pub, "publish_request_file",
        lambda request, repo_dir: {"published": True, "path": "x", "error": None, "skipped_unchanged": False},
    )
    calls = []
    monkeypatch.setattr(rs, "d1_api_query", lambda sql, params: calls.append(sql) or [])
    rs.retry_stage7_request_publish(existing, event, now_ts=5000)
    assert len(calls) == 1
    assert "INSERT" not in calls[0]
    assert "UPDATE" in calls[0]


# =====================================================================
# main()-level routing (confirmed human-controlled operating model): for
# each eligible event, main() either (a) proposes a CANDIDATE (never a
# request) when none exists yet, (b) attempts to publish/retry an
# existing request's file when its status is PENDING_RESEARCH or
# FAILED_RETRYABLE, or (c) leaves it alone for any other open status.
# INTEGRATED/REJECTED rows never even reach existing_requests (the SQL
# WHERE clause itself excludes them).
# =====================================================================

def _recent_event_ts():
    # ~2 minutes old: comfortably inside MAX_EVENT_AGE_FOR_STAGE7_MS (6
    # days) regardless of any timing skew between this helper and main()'s
    # own now_ts, and comfortably outside any "just detected" edge case.
    return int(time.time() * 1000) - 120_000


def _existing_candidate_row(**overrides):
    row = {"event_id": 42, "candidate_id": "stage7-cand-42", "status": "PROPOSED"}
    row.update(overrides)
    return row


def _run_main_with_fakes(monkeypatch, existing_requests_rows, existing_candidates_rows=(), publish_request_file=None,
                         validated_rows=(), previous_sentiment_rows=(), inserted_sentiment_rows=(),
                         present_columns=None, compute_result=None, idempotent_repeat=True):
    """Drives the real rs.main() through exactly one eligible event
    (event_id=42), with every D1 read/write and the real git-publish call
    faked -- isolating the one thing under test here: main()'s own
    routing decision for that event, keyed off `existing_requests_rows`/
    `existing_candidates_rows`. Returns (result, d1_api_calls,
    publish_calls)."""
    _set_valid_staging_env(monkeypatch)
    event_ts = _recent_event_ts()

    def fake_run_d1(sql):
        if "sqlite_master" in sql:
            return [{"name": t} for t in rs.REQUIRED_STAGE7_TABLES]
        if "pragma_table_info" in sql:
            table = sql.split("pragma_table_info('")[1].split("')")[0]
            columns = (present_columns if present_columns is not None
                       else {t: set(c) for t, c in rs.REQUIRED_STAGE7_COLUMNS.items()})
            return [{"name": c} for c in sorted(columns.get(table, set()))]
        if "FROM research_events WHERE" in sql:
            return [{"event_id": 42, "event_ts": event_ts}]
        if "FROM history WHERE" in sql or "FROM btc_data WHERE" in sql or "FROM predictions WHERE" in sql:
            return []
        if "FROM research_event_evidence WHERE" in sql:
            return []
        if "publish_attempts" in sql and "FROM stage7_research_requests" in sql:
            return existing_requests_rows
        if "FROM stage7_research_candidates" in sql:
            return list(existing_candidates_rows)
        if "FROM stage7_research_responses" in sql:
            return list(validated_rows)  # default: no validated human response for this event
        if "SELECT id FROM stage7_event_sentiment" in sql:
            return list(inserted_sentiment_rows)
        if "FROM stage7_event_sentiment" in sql:
            return list(previous_sentiment_rows)  # default: no previous sentiment row
        raise AssertionError(f"unexpected run_d1 call: {sql}")

    def fake_build_dataset(conn, start_ts, end_ts):
        return {
            "events": [{"event_id": 1, "event_ts": event_ts, "category": "LARGE_MOVE",
                        "is_internal_model_event": False, "coin": "BTC"}],
            "results": [],
        }

    d1_api_calls = []

    def fake_d1_api_query(sql, params):
        d1_api_calls.append((sql, params))
        return []

    publish_calls = []

    def fake_publish(request, repo_dir):
        publish_calls.append(request)
        if publish_request_file is not None:
            return publish_request_file
        return {"published": True, "path": rs.pub.request_file_path(request["request_id"]),
                "error": None, "skipped_unchanged": False}

    monkeypatch.setattr(rs, "run_d1", fake_run_d1)
    monkeypatch.setattr(rs.join_module, "build_event_source_evidence_dataset", fake_build_dataset)
    monkeypatch.setattr(rs, "d1_api_query", fake_d1_api_query)
    monkeypatch.setattr(rs.pub, "publish_request_file", fake_publish)
    monkeypatch.setattr(
        rs.suff, "assess_evidence_sufficiency",
        lambda event, evidence_rows, relevance_results, interpretation_results: {
            "status": "INSUFFICIENT_EVIDENCE", "reasons": ["no evidence rows exist"],
            "questions": ["what happened?"], "missing_categories": ["primary_reporting"],
        },
    )
    default_result = {
        "formula_version": "v1", "evidence_sufficiency": "INSUFFICIENT_EVIDENCE",
        "sentiment_label": None, "sentiment_score": None, "v1_macro_context": {},
        "evidence_interpretation": {}, "contributing_evidence_ids": [], "excluded_evidence": [],
        "duplicate_handling": {}, "ai_research_response_id": None, "previous_sentiment_id": None,
        "input_fingerprint": "fp-fixed",
    }
    if callable(compute_result):
        compute_fn = compute_result
    else:
        compute_fn = lambda *a, **kw: dict(default_result, **(compute_result or {}))
    monkeypatch.setattr(rs.sr, "compute_event_sentiment", compute_fn)
    # Forced True regardless of `previous` -- this suite is about request/
    # candidate routing, not sentiment-row idempotency (already covered
    # elsewhere), so the sentiment INSERT is deliberately suppressed here
    # to keep each test's d1_api_query calls attributable to the request/
    # candidate write path alone.
    monkeypatch.setattr(rs.sr, "is_idempotent_repeat", lambda *a, **kw: idempotent_repeat)

    result = rs.main()
    return result, d1_api_calls, publish_calls


def test_main_proposes_a_candidate_when_none_exists_yet(monkeypatch):
    # Confirmed human-controlled operating model: an insufficient event
    # with no existing request/candidate becomes a PROPOSED candidate --
    # never an auto-created, auto-published request.
    result, d1_api_calls, publish_calls = _run_main_with_fakes(monkeypatch, existing_requests_rows=[])
    assert result["candidates_proposed"] == 1
    assert result["retries_attempted"] == 0
    assert publish_calls == []  # never publishes anything for a mere candidate
    insert_calls = [c for c in d1_api_calls if "INSERT INTO stage7_research_candidates" in c[0]]
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]
    assert "INSERT INTO stage7_research_requests" not in sql
    assert params[0] == "stage7-cand-42"
    assert params[1] == 42
    assert "PROPOSED" in params


def test_main_never_proposes_a_duplicate_candidate_for_an_event_already_proposed(monkeypatch):
    existing_candidate = _existing_candidate_row()
    result, d1_api_calls, publish_calls = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[], existing_candidates_rows=[existing_candidate],
    )
    assert result["candidates_proposed"] == 0
    assert publish_calls == []
    candidate_writes = [c for c in d1_api_calls if "stage7_research_candidates" in c[0] and "INSERT" in c[0]]
    assert candidate_writes == []


def test_main_never_proposes_a_candidate_for_an_event_that_already_has_a_request(monkeypatch):
    existing_request = _existing_request_row(status="RESEARCH_REQUEST_PUBLISHED")
    result, d1_api_calls, publish_calls = _run_main_with_fakes(monkeypatch, existing_requests_rows=[existing_request])
    assert result["candidates_proposed"] == 0
    assert publish_calls == []
    candidate_writes = [c for c in d1_api_calls if "stage7_research_candidates" in c[0]]
    assert candidate_writes == []


def test_main_publishes_a_pending_research_request_on_first_attempt(monkeypatch):
    # A human created this request via the Worker's candidate-selection
    # flow (status PENDING_RESEARCH, publish_attempts=0, github_path never
    # set) -- main() must attempt its FIRST publish, reusing the exact
    # same mechanism a FAILED_RETRYABLE row's retry uses.
    existing = _existing_request_row(status="PENDING_RESEARCH", publish_attempts=0)
    result, d1_api_calls, publish_calls = _run_main_with_fakes(monkeypatch, existing_requests_rows=[existing])

    assert result["candidates_proposed"] == 0  # never a second, duplicate candidate/request
    assert result["retries_attempted"] == 1
    assert result["retries_succeeded"] == 1
    assert len(publish_calls) == 1
    assert publish_calls[0]["request_id"] == "stage7-req-42-1"

    write_calls = [c for c in d1_api_calls if "stage7_research_requests" in c[0]]
    assert len(write_calls) == 1
    sql, params = write_calls[0]
    assert sql.startswith("UPDATE stage7_research_requests")
    assert params[0] == "RESEARCH_REQUEST_PUBLISHED"
    assert params[2] == 1  # publish_attempts incremented from 0


def test_main_retries_a_failed_retryable_request_and_updates_it_in_place(monkeypatch):
    existing = _existing_request_row(publish_attempts=2)
    result, d1_api_calls, publish_calls = _run_main_with_fakes(monkeypatch, existing_requests_rows=[existing])

    assert result["candidates_proposed"] == 0  # never a second row
    assert result["retries_attempted"] == 1
    assert result["retries_succeeded"] == 1
    assert len(publish_calls) == 1
    assert publish_calls[0]["request_id"] == "stage7-req-42-1"  # SAME request_id, not a new one

    write_calls = [c for c in d1_api_calls if "stage7_research_requests" in c[0]]
    assert len(write_calls) == 1
    sql, params = write_calls[0]
    assert sql.startswith("UPDATE stage7_research_requests")
    assert "INSERT" not in sql
    assert params[0] == "RESEARCH_REQUEST_PUBLISHED"
    assert params[2] == 3  # publish_attempts incremented from 2


def test_main_repeated_publish_failure_keeps_request_failed_retryable(monkeypatch):
    existing = _existing_request_row(publish_attempts=1)
    failing_publish = {"published": False, "path": "research/stage7_requests/stage7-req-42-1.json",
                        "error": "`git push` failed: network error", "skipped_unchanged": False}
    result, d1_api_calls, publish_calls = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[existing], publish_request_file=failing_publish,
    )

    assert result["retries_attempted"] == 1
    assert result["retries_succeeded"] == 0
    assert result["candidates_proposed"] == 0
    write_calls = [c for c in d1_api_calls if "stage7_research_requests" in c[0]]
    assert len(write_calls) == 1
    _sql, params = write_calls[0]
    assert params[0] == "FAILED_RETRYABLE"
    assert params[2] == 2


def test_main_gives_up_permanently_after_max_attempts_of_persistent_failure(monkeypatch):
    existing = _existing_request_row(publish_attempts=rs.pub.MAX_PUBLISH_ATTEMPTS - 1)
    failing_publish = {"published": False, "path": "research/stage7_requests/stage7-req-42-1.json",
                        "error": "`git push` failed: permission denied", "skipped_unchanged": False}
    result, d1_api_calls, _publish_calls = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[existing], publish_request_file=failing_publish,
    )

    assert result["retries_attempted"] == 1
    assert result["retries_succeeded"] == 0
    write_calls = [c for c in d1_api_calls if "stage7_research_requests" in c[0]]
    _sql, params = write_calls[0]
    assert params[0] == "FAILED_PERMANENT"
    assert params[2] == rs.pub.MAX_PUBLISH_ATTEMPTS


@pytest.mark.parametrize("status", ["FAILED_PERMANENT", "RESEARCH_REQUEST_PUBLISHED",
                                     "RESEARCH_RESPONSE_RECEIVED", "INTEGRATION_REVIEW"])
def test_main_never_retries_or_duplicates_a_request_that_is_open_but_not_pending_or_failed(monkeypatch, status):
    existing = _existing_request_row(status=status)
    result, d1_api_calls, publish_calls = _run_main_with_fakes(monkeypatch, existing_requests_rows=[existing])

    assert result["retries_attempted"] == 0
    assert result["candidates_proposed"] == 0
    assert publish_calls == []
    write_calls = [c for c in d1_api_calls if "stage7_research_requests" in c[0]]
    assert write_calls == []  # no UPDATE, no INSERT -- this event is left completely alone


@pytest.mark.parametrize("status", ["INTEGRATED", "REJECTED"])
def test_main_never_retries_a_terminal_request_even_if_one_defensively_appeared(monkeypatch, status):
    # The real SQL WHERE clause already excludes INTEGRATED/REJECTED rows
    # from existing_requests entirely -- this pins the routing logic's
    # OWN defense-in-depth: even if one leaked through (a future SQL
    # change, a test double), it is still never retried and never
    # mistaken for "no request exists" (which would insert a duplicate).
    existing = _existing_request_row(status=status)
    result, d1_api_calls, publish_calls = _run_main_with_fakes(monkeypatch, existing_requests_rows=[existing])

    assert result["retries_attempted"] == 0
    assert result["candidates_proposed"] == 0
    assert publish_calls == []
    write_calls = [c for c in d1_api_calls if "stage7_research_requests" in c[0]]
    assert write_calls == []


def test_main_retry_never_writes_a_new_sentiment_row_beyond_normal_idempotency_gating(monkeypatch):
    # The sentiment-row write path is untouched by the retry/candidate
    # changes -- it is gated by sr.is_idempotent_repeat exactly as before,
    # independent of whether this event's request is being retried,
    # proposed as a candidate, or left alone. Forcing is_idempotent_repeat
    # True (as every test in this section does) must mean ZERO
    # stage7_event_sentiment writes here.
    existing = _existing_request_row(publish_attempts=2)
    result, d1_api_calls, _publish_calls = _run_main_with_fakes(monkeypatch, existing_requests_rows=[existing])
    assert result["sentiment_rows_written"] == 0
    sentiment_writes = [c for c in d1_api_calls if "INSERT INTO stage7_event_sentiment" in c[0]]
    assert sentiment_writes == []


def test_main_marks_a_stale_candidate_when_its_event_ages_out_of_eligibility(monkeypatch):
    # existing_candidates_rows simulates a candidate whose event_id is NOT
    # among the events the fake dataset reports (event_id=42 is not the
    # event the fake dataset detects -- only event_id resolved via
    # research_events, which this harness always resolves to 42 for its
    # OWN single eligible event). A candidate for a DIFFERENT, no-longer-
    # eligible event_id must be marked STALE.
    stale_candidate = _existing_candidate_row(event_id=999, candidate_id="stage7-cand-999")
    result, d1_api_calls, _publish_calls = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[], existing_candidates_rows=[stale_candidate],
    )
    assert result["candidates_marked_stale"] == 1
    stale_updates = [c for c in d1_api_calls if "UPDATE stage7_research_candidates" in c[0]]
    assert len(stale_updates) == 1
    sql, params = stale_updates[0]
    assert "STALE" in sql
    assert params[1] == "stage7-cand-999"


# ---------------------------------------------------------------------------
# Stage 7.2: persisted recalculation lifecycle (migration 0018). A human-
# requested recalculation moves REQUESTED -> RUNNING -> COMPLETED | FAILED,
# each transition written by run_stage7.py, never inferred.
# ---------------------------------------------------------------------------

def _validated_row(status="REQUESTED", request_id="stage7-req-42-1", event_id=42):
    return {
        "event_id": event_id, "response_id": f"stage7-resp-{request_id}", "request_id": request_id,
        "validation_status": "VALIDATED", "recalculation_status": status,
        "findings_json": '{"sentiment_assessment": "POSITIVE", "summary": "s"}', "sources_json": "[]",
    }


def _writes(d1_api_calls, fragment):
    return [(sql, params) for sql, params in d1_api_calls if fragment in sql]


def test_find_missing_stage7_columns_names_the_migration_for_each_gap():
    present = {t: set(cols) for t, cols in rs.REQUIRED_STAGE7_COLUMNS.items()}
    assert rs.find_missing_stage7_columns(present) == []
    present["stage7_research_requests"].discard("recalculation_status")
    present["stage7_research_responses"].discard("raw_response_text")
    assert sorted(rs.find_missing_stage7_columns(present)) == [
        ("stage7_research_requests", "recalculation_status", "0018"),
        ("stage7_research_responses", "raw_response_text", "0017"),
    ]


def test_main_refuses_to_run_when_migration_0018_columns_are_missing(monkeypatch):
    columns = {t: set(c) for t, c in rs.REQUIRED_STAGE7_COLUMNS.items()}
    columns["stage7_research_requests"].discard("recalculation_status")
    with pytest.raises(RuntimeError, match=r"recalculation_status \(migration 0018\)"):
        _run_main_with_fakes(monkeypatch, existing_requests_rows=[], present_columns=columns)


def test_requested_recalculation_runs_inserts_one_row_and_is_marked_completed(monkeypatch):
    result, calls, _ = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[], validated_rows=[_validated_row("REQUESTED")],
        idempotent_repeat=False, inserted_sentiment_rows=[{"id": 7}],
    )
    running = _writes(calls, "recalculation_status = 'RUNNING'")
    completed = _writes(calls, "recalculation_status = 'COMPLETED'")
    inserts = _writes(calls, "INSERT INTO stage7_event_sentiment")
    assert len(running) == 1 and len(inserts) == 1 and len(completed) == 1
    assert calls.index(running[0]) < calls.index(inserts[0]) < calls.index(completed[0])
    assert completed[0][1][1] == 7  # recalculation_sentiment_id = the id of the row just written
    assert "INTEGRATION_REVIEW" in completed[0][0]
    assert result["recalculations_attempted"] == 1 and result["recalculations_completed"] == 1
    assert result["ok"] is True and result["recalculation_failures"] == 0


def test_previous_sentiment_id_is_actually_passed_to_the_calculation(monkeypatch):
    # Regression: the previous-sentiment query omitted `id`, so previous_sentiment_id
    # was always None and every recalculation looked like the event's first.
    seen = {}

    def compute(*args, **kwargs):
        seen.update(kwargs)
        return {
            "formula_version": "v1", "evidence_sufficiency": "SUFFICIENT", "sentiment_label": "POSITIVE",
            "sentiment_score": 100.0, "v1_macro_context": {}, "evidence_interpretation": {},
            "contributing_evidence_ids": [], "excluded_evidence": [], "duplicate_handling": {},
            "ai_research_response_id": "r", "previous_sentiment_id": kwargs.get("previous_sentiment_id"),
            "input_fingerprint": "fp-new",
        }

    _, calls, _ = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[], validated_rows=[_validated_row("REQUESTED")],
        previous_sentiment_rows=[{"id": 41, "event_id": 42, "input_fingerprint": "fp-old"}],
        compute_result=compute, idempotent_repeat=False, inserted_sentiment_rows=[{"id": 42}],
    )
    assert seen["previous_sentiment_id"] == 41
    insert = _writes(calls, "INSERT INTO stage7_event_sentiment")[0]
    assert 41 in insert[1]  # persisted as previous_sentiment_id


def test_recalculation_failure_is_isolated_recorded_and_surfaced(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("source cutoff check exploded")

    result, calls, _ = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[], validated_rows=[_validated_row("REQUESTED")],
        compute_result=boom, idempotent_repeat=False,
    )
    failed = _writes(calls, "recalculation_status = 'FAILED'")
    assert len(failed) == 1 and "ValueError: source cutoff check exploded" in failed[0][1][1]
    assert not _writes(calls, "recalculation_status = 'COMPLETED'")
    assert result["ok"] is False and result["status"] == "COMPLETED_WITH_FAILURES"
    assert result["recalculation_failures"] == 1 and rs.exit_code_for(result) == 1


def test_exit_code_is_zero_when_nothing_failed():
    assert rs.exit_code_for({"recalculation_failures": 0}) == 0
    assert rs.exit_code_for({}) == 0


def test_a_failure_for_an_event_nobody_requested_is_never_swallowed(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("unexpected baseline failure")

    with pytest.raises(ValueError, match="unexpected baseline failure"):
        _run_main_with_fakes(monkeypatch, existing_requests_rows=[], validated_rows=[], compute_result=boom)


def test_a_failed_recalculation_is_not_retried_automatically(monkeypatch):
    calls_made = []

    def compute(*args, **kwargs):
        calls_made.append(1)
        raise AssertionError("must not be recomputed")

    result, calls, _ = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[], validated_rows=[_validated_row("FAILED")], compute_result=compute,
    )
    assert calls_made == []
    assert not _writes(calls, "recalculation_status")
    assert result["recalculations_attempted"] == 0


def test_a_completed_recalculation_is_not_touched_again(monkeypatch):
    result, calls, _ = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[], validated_rows=[_validated_row("COMPLETED")],
    )
    assert not _writes(calls, "recalculation_status")
    assert not _writes(calls, "INSERT INTO stage7_event_sentiment")
    assert result["recalculations_attempted"] == 0


def test_an_interrupted_run_converges_without_a_duplicate_sentiment_row(monkeypatch):
    # A previous run wrote the sentiment row but died before bookkeeping: the
    # request is still RUNNING and the recomputed result is an idempotent repeat.
    result, calls, _ = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[], validated_rows=[_validated_row("RUNNING")],
        previous_sentiment_rows=[{"id": 5, "event_id": 42, "input_fingerprint": "fp-fixed"}],
        idempotent_repeat=True,
    )
    assert not _writes(calls, "INSERT INTO stage7_event_sentiment")
    completed = _writes(calls, "recalculation_status = 'COMPLETED'")
    assert len(completed) == 1 and completed[0][1][1] == 5
    assert _writes(calls, "recalculation_attempts = recalculation_attempts + 1")  # the retry is counted


def test_a_request_whose_event_left_the_evidence_window_fails_visibly_instead_of_hanging(monkeypatch):
    result, calls, _ = _run_main_with_fakes(
        monkeypatch, existing_requests_rows=[],
        validated_rows=[_validated_row("REQUESTED", request_id="stage7-req-999-1", event_id=999)],
    )
    failed = _writes(calls, "recalculation_status = 'FAILED'")
    assert len(failed) == 1 and "EVENT_NOT_ELIGIBLE" in failed[0][1][1]
    assert result["recalculation_failures"] == 1 and rs.exit_code_for(result) == 1
