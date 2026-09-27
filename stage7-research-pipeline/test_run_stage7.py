"""
Tests for stage7-research-pipeline/run_stage7.py's own pure functions
(group_results_by_event, select_eligible_events, resolve_real_event_ids).
No DB, no network, no wrangler/D1 API calls -- run_d1/d1_api_query/main
are exercised only against real infrastructure and are intentionally not
covered here (same convention as exp009-event-source-evidence, which
only unit-tests its own pure helpers).

Run with: python3 -m pytest stage7-research-pipeline/ -v
"""
import sys
import os

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
