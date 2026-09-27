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
import sys
import os
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import run_stage7 as rs  # noqa: E402


def _dataset(events, results):
    return {"events": events, "results": results}


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
    def fake_run_d1(sql):
        assert "sqlite_master" in sql
        return []  # none of the three Stage7 tables exist
    monkeypatch.setattr(rs, "run_d1", fake_run_d1)
    result = rs.main()
    assert result["ok"] is True
    assert result["status"] == "SKIPPED -- MIGRATION NOT APPLIED"
    assert result["events_considered"] == 0
    assert result["requests_created"] == 0
    assert result["sentiment_rows_written"] == 0


def test_main_raises_loudly_on_partial_schema_never_silently_skips():
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


def test_main_does_not_swallow_an_unrelated_run_d1_error():
    # A real connectivity/permission/malformed-SQL failure on the schema
    # check itself must propagate exactly as it did before this fix --
    # never reinterpreted as "migration not applied".
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

def test_main_passes_exactly_window_ms_to_the_detector_never_window_plus_lookback():
    calls = {}

    def fake_run_d1(sql):
        if "sqlite_master" in sql:
            return [{"name": t} for t in rs.REQUIRED_STAGE7_TABLES]
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


def test_main_fetch_queries_use_a_wider_lookback_inclusive_bound_than_the_detector_window():
    fetch_sqls = []

    def fake_run_d1(sql):
        if "sqlite_master" in sql:
            return [{"name": t} for t in rs.REQUIRED_STAGE7_TABLES]
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
