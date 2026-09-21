"""
Tests for research/exp010_source_dialogue_validation.py.

These tests verify EXP-010's own WIRING to the locked Source Dialogue
Engine (research/source_dialogue.py, PR #68) and to the reused
event/relevance/redundancy modules -- they do NOT re-test the engine's
own internal correctness (already 76 tests strong in
test_source_dialogue.py) or PR3/PR57/PR59's own detection/snapshot
logic (already tested in their own suites). "Duplicate its
classification logic" is exactly what this file must not do.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import event_detector as ed
import event_source_relevance as esrel
import source_analysis as sa
import source_dialogue as sd
import exp010_source_dialogue_validation as ev


# ---- Pure unit tests: classify_direction_vs_median / build_v1_observation ----

def test_classify_direction_vs_median_matches_prs57_own_rule():
    assert ev.classify_direction_vs_median(60, 50) == "UP"
    assert ev.classify_direction_vs_median(40, 50) == "DOWN"
    assert ev.classify_direction_vs_median(50, 50) is None
    assert ev.classify_direction_vs_median(None, 50) is None
    assert ev.classify_direction_vs_median(50, None) is None


def test_build_v1_observation_information_available_at_equals_observation_time():
    obs = ev.build_v1_observation("alpha", 70, 12345, 50)
    assert obs["information_available_at"] == 12345
    assert obs["observation_time"] == 12345
    assert obs["direction"] == "UP"
    assert obs["raw_value"] == 70


def test_build_v1_observation_returns_none_for_missing_value():
    assert ev.build_v1_observation("alpha", None, 12345, 50) is None
    assert ev.build_v1_observation("alpha", 70, None, 50) is None


# ---- Fixture ----

def _build_fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "score INTEGER, sources_json TEXT, gold_regime TEXT)"
    )
    conn.execute("CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL)")
    conn.execute(
        "CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "horizon_hours INTEGER, p_up REAL, realized_up INTEGER)"
    )
    conn.commit()
    return conn


HOUR = 3_600_000
DAY = 24 * HOUR
BASE_TS = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _seed_one_large_move_event(conn):
    """One clean daily btc_data reading per day (event_detector's own
    _resample_daily keeps only the first reading per UTC day -- the
    exact same technique test_event_source_evidence_join.py's own
    fixture already established), then a >4% jump to trigger exactly
    one real LARGE_MOVE event. Returns (start_ts, end_ts, event_ts)."""
    price = 50000.0
    for i in range(9):
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (BASE_TS + i * DAY, price))
    event_ts = BASE_TS + 9 * DAY
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (event_ts, price * 1.05))
    conn.commit()
    start_ts, end_ts = BASE_TS, event_ts + HOUR
    events = ed.detect_large_moves(conn, start_ts, end_ts)
    assert len(events) == 1, "fixture must deterministically trigger exactly one LARGE_MOVE event"
    assert events[0]["event_ts"] == event_ts
    return start_ts, end_ts, event_ts


def _insert_history(conn, ts, sources):
    conn.execute(
        "INSERT INTO history (ts, score, sources_json, gold_regime) VALUES (?, ?, ?, ?)",
        (ts, 50, json.dumps(sources), "chop"),
    )


# ---- 3/4. SUPPORTING / CONTRADICTING ----

def test_supporting_when_two_sources_both_above_their_own_median():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    # Baseline low readings to establish a low median for both sources,
    # then one clearly-elevated reading at event_ts for both -- both
    # end up ABOVE their own median (self-normalizing convention).
    for h_ts in range(start_ts, event_ts, 6 * HOUR):
        _insert_history(conn, h_ts, {"alpha": 20, "beta": 25})
    _insert_history(conn, event_ts, {"alpha": 90, "beta": 85})
    conn.commit()

    dataset = ev.build_relationship_dataset(conn, start_ts, end_ts)
    conn.close()
    row = next(r for r in dataset["results"] if {r["source_key_a"], r["source_key_b"]} == {"alpha", "beta"})
    assert row["relationship"] == "SUPPORTING"


def test_contradicting_when_sources_disagree_in_direction():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    for h_ts in range(start_ts, event_ts, 6 * HOUR):
        _insert_history(conn, h_ts, {"alpha": 20, "beta": 80})
    _insert_history(conn, event_ts, {"alpha": 90, "beta": 10})  # alpha UP, beta DOWN vs. their own medians
    conn.commit()

    dataset = ev.build_relationship_dataset(conn, start_ts, end_ts)
    conn.close()
    row = next(r for r in dataset["results"] if {r["source_key_a"], r["source_key_b"]} == {"alpha", "beta"})
    assert row["relationship"] == "CONTRADICTING"


# ---- 5. DIFFERENT_TIMING ----

def test_different_timing_at_the_observation_and_wiring_level():
    """Methodological finding (documented in the module's own
    docstring): event_source_relevance.snapshot_v1_source() always
    reads the SINGLE most recent history row -- two PRESENT V1 sources
    at the same event therefore always share the identical
    observation_time, so DIFFERENT_TIMING cannot arise through
    build_relationship_dataset()'s real V1-vs-V1 pipeline today (a
    stale/absent source there produces INSUFFICIENT_EVIDENCE instead,
    covered separately by the window-exclusion test). This test proves
    the WIRING through build_v1_observation() -> sd.classify_
    relationship() correctly reports DIFFERENT_TIMING once two
    observations genuinely do carry different information_available_at
    values -- the scenario a future independent-timestamp source (EIA/
    GDELT) will actually produce."""
    obs_alpha = ev.build_v1_observation("alpha", 90, 1_700_000_000_000, 20)
    obs_gamma = ev.build_v1_observation("gamma", 90, 1_700_000_000_000 - 6 * HOUR, 20)
    result = sd.classify_relationship(obs_alpha, obs_gamma, information_cutoff=1_700_000_000_000)
    assert result == "DIFFERENT_TIMING"


# ---- 1. as-of exclusion (future data must never leak backward) ----

def test_asof_exclusion_a_future_history_row_never_affects_the_result():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    for h_ts in range(start_ts, event_ts, 6 * HOUR):
        _insert_history(conn, h_ts, {"alpha": 20, "beta": 20})
    _insert_history(conn, event_ts, {"alpha": 20, "beta": 20})  # both AT their own median -> no signal
    # A row strictly AFTER the event with an extreme, easily-detectable value.
    _insert_history(conn, event_ts + HOUR, {"alpha": 999, "beta": 999})
    conn.commit()

    dataset = ev.build_relationship_dataset(conn, start_ts, end_ts)
    conn.close()
    row = next(r for r in dataset["results"] if {r["source_key_a"], r["source_key_b"]} == {"alpha", "beta"})
    # If the future row had leaked in, direction would resolve to UP/UP
    # (999 > median) -> SUPPORTING. It must not.
    assert row["relationship"] != "SUPPORTING"


# ---- 2. observation window exclusion ----

def test_window_exclusion_a_snapshot_older_than_24h_is_insufficient_evidence():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    # alpha has a snapshot right at the event; beta's only data is
    # >24h before the event (the window's own lookback bound).
    _insert_history(conn, event_ts - 30 * HOUR, {"beta": 20})
    for h_ts in range(start_ts, event_ts, 6 * HOUR):
        _insert_history(conn, h_ts, {"alpha": 20})
    _insert_history(conn, event_ts, {"alpha": 90})
    conn.commit()

    dataset = ev.build_relationship_dataset(conn, start_ts, end_ts)
    conn.close()
    row = next(r for r in dataset["results"] if {r["source_key_a"], r["source_key_b"]} == {"alpha", "beta"})
    assert row["relationship"] == "INSUFFICIENT_EVIDENCE"


# ---- 6. insufficient evidence (no history at all before the event) ----

def test_insufficient_evidence_when_no_history_exists_before_the_event():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    # No history rows inserted at all.
    dataset = ev.build_relationship_dataset(conn, start_ts, end_ts)
    conn.close()
    assert len(dataset["results"]) == 0 or all(r["relationship"] == "INSUFFICIENT_EVIDENCE" for r in dataset["results"])


# ---- 7. malformed timestamps degrade, never crash ----

def test_malformed_snapshot_timestamp_degrades_to_insufficient_evidence():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    for h_ts in range(start_ts, event_ts, 6 * HOUR):
        _insert_history(conn, h_ts, {"alpha": 20, "beta": 20})
    _insert_history(conn, event_ts, {"alpha": 90, "beta": 85})
    conn.commit()

    real_snapshot = esrel.snapshot_v1_source

    def malformed_snapshot(c, e_ts, key):
        result = real_snapshot(c, e_ts, key)
        if key == "beta" and result["source_value"] is not None:
            result = dict(result, source_observation_ts="not-a-timestamp")
        return result

    with patch.object(ev.esrel, "snapshot_v1_source", side_effect=malformed_snapshot):
        dataset = ev.build_relationship_dataset(conn, start_ts, end_ts)
    conn.close()
    row = next(r for r in dataset["results"] if {r["source_key_a"], r["source_key_b"]} == {"alpha", "beta"})
    assert row["relationship"] == "INSUFFICIENT_EVIDENCE"


# ---- 8. duplicate observation timestamps (redundancy leg) ----

def test_duplicate_history_ts_raises_via_the_locked_engines_own_guard():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    _insert_history(conn, start_ts, {"alpha": 10, "beta": 10})
    _insert_history(conn, start_ts, {"alpha": 20, "beta": 999})  # SAME ts, different value -- malformed
    for i in range(2, 10):
        _insert_history(conn, start_ts + i * HOUR, {"alpha": 10 + i, "beta": 10 + i})
    conn.commit()

    with pytest.raises(ValueError, match="duplicate observation_time"):
        ev.build_redundancy_dataset(conn, start_ts, end_ts)
    conn.close()


# ---- 9. canonical A/B ordering ----

def test_redundancy_dataset_uses_canonical_pair_ordering():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    for i in range(10):
        _insert_history(conn, start_ts + i * HOUR, {"zzz": i, "aaa": i * 2})
    conn.commit()

    dataset = ev.build_redundancy_dataset(conn, start_ts, end_ts)
    conn.close()
    row = next(r for r in dataset["results"] if {r["source_key_a"], r["source_key_b"]} == {"zzz", "aaa"})
    assert row["source_key_a"] == "aaa" and row["source_key_b"] == "zzz"


# ---- 10. self-pair rejection ----

def test_self_pair_is_never_produced_and_the_engines_own_guard_still_fires():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    for i in range(10):
        _insert_history(conn, start_ts + i * HOUR, {"alpha": i})
    conn.commit()

    dataset = ev.build_relationship_dataset(conn, start_ts, end_ts)
    for r in dataset["results"]:
        assert r["source_key_a"] != r["source_key_b"]
    conn.close()

    a = {"information_available_at": 1000, "observation_time": 1000, "direction": "UP"}
    with pytest.raises(ValueError, match="must differ"):
        sd.build_interaction("alpha", "alpha", a, a, information_cutoff=10000, window_start=0, window_end=2000)


# ---- 11. exact +-0.7 threshold (wiring test -- boundary itself already
# proven at the engine level in test_source_dialogue.py) ----

def test_exact_threshold_boundary_is_passed_through_unmodified():
    conn = _build_fixture_conn()
    for i in range(2):
        _insert_history(conn, i * HOUR, {"alpha": i, "beta": i})
    conn.commit()

    fake_result = ("REDUNDANCY_UNRESOLVED", {"n": 10, "r": sa.STRONG_REDUNDANCY_THRESHOLD, "strong_redundancy": True})
    with patch.object(ev.sd, "classify_pairwise_redundancy", return_value=fake_result):
        dataset = ev.build_redundancy_dataset(conn, 0, 2 * HOUR)
    conn.close()
    assert dataset["results"][0]["redundancy"] == "REDUNDANCY_UNRESOLVED"
    assert dataset["results"][0]["r"] == sa.STRONG_REDUNDANCY_THRESHOLD


# ---- 12. negative strong correlation (real, not mocked) ----

def test_negative_strong_correlation_reports_redundancy_unresolved():
    conn = _build_fixture_conn()
    for i in range(10):
        _insert_history(conn, i * HOUR, {"alpha": i, "beta": -i})
    conn.commit()

    dataset = ev.build_redundancy_dataset(conn, 0, 10 * HOUR)
    conn.close()
    row = next(r for r in dataset["results"] if {r["source_key_a"], r["source_key_b"]} == {"alpha", "beta"})
    assert row["redundancy"] == "REDUNDANCY_UNRESOLVED"
    assert row["r"] < 0


# ---- 13. internal-model event handling ----

def test_internal_model_events_excluded_from_results_but_counted():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    for h_ts in range(start_ts, event_ts, 6 * HOUR):
        _insert_history(conn, h_ts, {"alpha": 20, "beta": 20})
    _insert_history(conn, event_ts, {"alpha": 90, "beta": 85})
    # 5 consecutive INCORRECT horizon=12 predictions -> a real
    # V2_FAILURE_CLUSTER (internal-model) event.
    for i in range(5):
        conn.execute(
            "INSERT INTO predictions (ts, horizon_hours, p_up, realized_up) VALUES (?, ?, ?, ?)",
            (event_ts + (i + 2) * HOUR, 12, 0.9, 0),
        )
    conn.commit()

    dataset = ev.build_relationship_dataset(conn, start_ts, event_ts + 10 * HOUR)
    conn.close()
    assert dataset["n_internal_model_events_excluded"] >= 1
    assert all(not e["is_internal_model_event"] for e in dataset["events"])
    assert all(r["is_internal_model_event"] is False for r in dataset["results"])


def test_summarize_relationship_counts_discloses_internal_model_exclusion():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    for h_ts in range(start_ts, event_ts, 6 * HOUR):
        _insert_history(conn, h_ts, {"alpha": 20, "beta": 20})
    _insert_history(conn, event_ts, {"alpha": 90, "beta": 85})
    conn.commit()
    dataset = ev.build_relationship_dataset(conn, start_ts, end_ts)
    conn.close()
    summary = ev.summarize_relationship_counts(dataset)
    assert "n_internal_model_events_excluded" in summary
    assert sum(summary["by_relationship"].values()) == summary["n_source_pair_observations"]


def test_summarize_redundancy_counts_shape():
    conn = _build_fixture_conn()
    for i in range(10):
        _insert_history(conn, i * HOUR, {"alpha": i, "beta": i * 2})
    conn.commit()
    dataset = ev.build_redundancy_dataset(conn, 0, 10 * HOUR)
    conn.close()
    summary = ev.summarize_redundancy_counts(dataset)
    assert set(summary["by_redundancy"].keys()) == set(sd.REDUNDANCY_LABELS)
    assert sum(summary["by_redundancy"].values()) == summary["n_source_pairs"]


# ---- 14. no production-table mutation ----

def test_no_sql_write_statements_in_this_module():
    with open(ev.__file__) as f:
        src = f.read()
    assert not re.search(r"\b(INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM)\b", src, re.IGNORECASE)
    for table in ("history", "btc_data", "predictions", "selection_decisions", "research_analyses"):
        assert not re.search(rf"\b(INSERT INTO|UPDATE|DELETE FROM)\s+{table}\b", src, re.IGNORECASE)


def test_module_has_zero_execute_calls():
    with open(ev.__file__) as f:
        src = f.read()
    assert "conn.execute" not in src
    assert "cursor.execute" not in src


# ---- 15. deterministic repeatability ----

def test_relationship_dataset_is_deterministic_across_repeated_calls():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_one_large_move_event(conn)
    for h_ts in range(start_ts, event_ts, 6 * HOUR):
        _insert_history(conn, h_ts, {"alpha": 20, "beta": 80, "gamma": 50})
    _insert_history(conn, event_ts, {"alpha": 90, "beta": 10, "gamma": 50})
    conn.commit()

    d1 = ev.build_relationship_dataset(conn, start_ts, end_ts)
    d2 = ev.build_relationship_dataset(conn, start_ts, end_ts)
    conn.close()
    assert d1["results"] == d2["results"]


def test_redundancy_dataset_is_deterministic_across_repeated_calls():
    conn = _build_fixture_conn()
    for i in range(10):
        _insert_history(conn, i * HOUR, {"alpha": i, "beta": i * 2})
    conn.commit()
    d1 = ev.build_redundancy_dataset(conn, 0, 10 * HOUR)
    d2 = ev.build_redundancy_dataset(conn, 0, 10 * HOUR)
    conn.close()
    assert d1["results"] == d2["results"]


# ---- Wiring: never duplicates the engine's own classification logic ----

def test_uses_the_real_locked_engine_functions_not_copies():
    assert ev.sd.classify_relationship is sd.classify_relationship
    assert ev.sd.classify_pairwise_redundancy is sd.classify_pairwise_redundancy
    assert ev.sd.build_interaction is sd.build_interaction
    assert ev.sa.pairwise_source_redundancy is sa.pairwise_source_redundancy
    assert ev.esrel.collect_events is esrel.collect_events


def test_never_redefines_relationship_or_redundancy_label_vocabulary():
    with open(ev.__file__) as f:
        src = f.read()
    for label in sd.RELATIONSHIP_LABELS + sd.REDUNDANCY_LABELS:
        assert f'"{label}"' not in src and f"'{label}'" not in src, (
            f"label {label} must never be re-typed as a literal in this module -- "
            f"only sd.RELATIONSHIP_LABELS/REDUNDANCY_LABELS or the engine's own return values"
        )
