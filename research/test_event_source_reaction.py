"""
Tests for research/event_source_reaction.py (PR-1).

All tests execute against real (non-mocked) synthetic fixtures or a
real in-memory sqlite3 database mirroring production. No network, no
D1 connection.

Run with: python3 -m pytest research/test_event_source_reaction.py -v
"""
import inspect
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import event_source_reaction as esr  # noqa: E402

HOUR = 3600000
DAY = 24 * HOUR


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, target_ts INTEGER,
        horizon_hours INTEGER NOT NULL, p_up REAL, realized_up INTEGER, realized_return REAL,
        model_version TEXT, git_commit_sha TEXT
    )""")
    conn.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL,
        sources_json TEXT, technical_score INTEGER, gold_regime TEXT
    )""")
    conn.execute("""CREATE TABLE btc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
    )""")
    return conn


def _insert_history(conn, ts, sources, score=50):
    conn.execute(
        "INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
        (ts, score, json.dumps(sources), 50, None),
    )


def _insert_btc(conn, ts, price):
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))


# =====================================================================
# 1. No-lookahead source snapshot
# =====================================================================

def test_source_snapshot_never_uses_a_row_after_event_ts():
    conn = _fresh_db()
    _insert_history(conn, 100 * HOUR, {"fng": 10})
    _insert_history(conn, 200 * HOUR, {"fng": 90})  # strictly AFTER the event -- must never be used
    conn.commit()
    snap = esr.snapshot_v1_source(conn, 150 * HOUR, "fng")
    assert snap["source_value"] == 10.0
    assert snap["source_observation_ts"] == 100 * HOUR
    conn.close()


def test_source_snapshot_uses_row_exactly_at_event_ts():
    conn = _fresh_db()
    _insert_history(conn, 100 * HOUR, {"fng": 55})
    conn.commit()
    snap = esr.snapshot_v1_source(conn, 100 * HOUR, "fng")
    assert snap["source_value"] == 55.0
    assert snap["status"] == "OK"
    conn.close()


def test_source_snapshot_reports_no_history_before_event_honestly():
    conn = _fresh_db()
    _insert_history(conn, 200 * HOUR, {"fng": 55})  # only AFTER the event
    conn.commit()
    snap = esr.snapshot_v1_source(conn, 100 * HOUR, "fng")
    assert snap["source_value"] is None
    assert snap["status"] == "NO_HISTORY_BEFORE_EVENT"
    conn.close()


def test_source_snapshot_missing_key_is_none_not_zero():
    conn = _fresh_db()
    _insert_history(conn, 100 * HOUR, {"global": 30})  # no "fng" key at all
    conn.commit()
    snap = esr.snapshot_v1_source(conn, 100 * HOUR, "fng")
    assert snap["source_value"] is None
    assert snap["status"] == "SOURCE_MISSING_AT_OBSERVATION"
    conn.close()


# =====================================================================
# 2. No-lookahead pre-event BTC baseline (Section 14's control)
# =====================================================================

def test_pre_event_return_never_uses_a_price_after_event_ts():
    conn = _fresh_db()
    _insert_btc(conn, 0, 100.0)
    _insert_btc(conn, 24 * HOUR, 110.0)   # baseline candidate, at event_ts - 24h
    _insert_btc(conn, 48 * HOUR, 121.0)   # anchor, at event_ts
    _insert_btc(conn, 72 * HOUR, 999.0)   # strictly AFTER event_ts -- must never be used
    conn.commit()
    result = esr.compute_pre_event_return(conn, 48 * HOUR)
    assert result["status"] == "OK"
    # (121-110)/110*100
    assert abs(result["return_pct"] - 10.0) < 1e-9
    conn.close()


def test_pre_event_return_insufficient_when_no_baseline_price_exists():
    conn = _fresh_db()
    _insert_btc(conn, 48 * HOUR, 100.0)  # only the anchor exists, nothing 24h earlier
    conn.commit()
    result = esr.compute_pre_event_return(conn, 48 * HOUR)
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["return_pct"] is None
    conn.close()


# =====================================================================
# 3. Correct post-event horizon calculation + resolution quality
# =====================================================================

def test_horizon_reaction_good_quality_when_price_lands_near_target():
    conn = _fresh_db()
    _insert_btc(conn, 0, 100.0)  # anchor at event_ts
    _insert_btc(conn, 6 * HOUR - 1000, 106.0)  # 1s short of the 6h target -- GOOD
    conn.commit()
    anchor = esr._price_at_or_before(conn, 0)
    result = esr.resolve_horizon_reaction(conn, 0, 6 * HOUR, anchor)
    assert result["status"] == "OK"
    assert result["quality"] == "GOOD"
    assert abs(result["return_pct"] - 6.0) < 1e-9
    conn.close()


def test_horizon_reaction_poor_quality_when_match_far_short_of_target():
    conn = _fresh_db()
    _insert_btc(conn, 0, 100.0)
    _insert_btc(conn, 1 * HOUR, 101.0)  # only 1h in, but target is 24h away -- POOR
    conn.commit()
    anchor = esr._price_at_or_before(conn, 0)
    result = esr.resolve_horizon_reaction(conn, 0, 24 * HOUR, anchor)
    assert result["status"] == "OK"
    assert result["quality"] == "POOR"
    assert result["elapsed_ms"] == 1 * HOUR
    conn.close()


def test_horizon_reaction_never_uses_a_point_at_or_before_event_ts():
    conn = _fresh_db()
    _insert_btc(conn, 0, 100.0)   # exactly at event_ts -- must never be treated as the REACTION point
    conn.commit()
    anchor = esr._price_at_or_before(conn, 0)
    result = esr.resolve_horizon_reaction(conn, 0, 6 * HOUR, anchor)
    assert result["status"] == "NO_DATA"
    conn.close()


def test_horizon_reaction_no_data_when_nothing_exists_up_to_target():
    conn = _fresh_db()
    _insert_btc(conn, 0, 100.0)
    _insert_btc(conn, 100 * HOUR, 200.0)  # far beyond the 6h target -- not a candidate at all
    conn.commit()
    anchor = esr._price_at_or_before(conn, 0)
    result = esr.resolve_horizon_reaction(conn, 0, 6 * HOUR, anchor)
    assert result["status"] == "NO_DATA"
    conn.close()


def test_horizon_reaction_reports_real_elapsed_time_not_just_the_label():
    conn = _fresh_db()
    _insert_btc(conn, 0, 100.0)
    _insert_btc(conn, 20 * HOUR, 105.0)
    conn.commit()
    anchor = esr._price_at_or_before(conn, 0)
    result = esr.resolve_horizon_reaction(conn, 0, 24 * HOUR, anchor)
    assert result["elapsed_ms"] == 20 * HOUR  # the REAL time used, not silently "24h"
    assert result["quality"] == "GOOD"  # within 25% of 24h (4h) -- 20h is only 4h short
    conn.close()


# =====================================================================
# 4. Insufficient data handling (alignment layer)
# =====================================================================

def test_alignment_insufficient_evidence_when_source_value_missing():
    result = esr.classify_alignment(None, 50.0, {}, {})
    assert result["result"] == "INSUFFICIENT_EVIDENCE"


def test_alignment_insufficient_evidence_when_no_usable_horizon():
    reactions = {"6h": {"status": "NO_DATA", "quality": None, "return_pct": None}}
    result = esr.classify_alignment(70.0, 50.0, reactions, {})
    assert result["result"] == "INSUFFICIENT_EVIDENCE"


def test_alignment_insufficient_evidence_when_source_at_its_own_median():
    reactions = {"6h": {"status": "OK", "quality": "GOOD", "return_pct": 2.0}}
    result = esr.classify_alignment(50.0, 50.0, reactions, {"6h": 0.0})
    assert result["result"] == "INSUFFICIENT_EVIDENCE"


# =====================================================================
# 5. No-measurable-reaction handling
# =====================================================================

def test_no_measurable_reaction_when_return_below_noise_floor():
    reactions = {"6h": {"status": "OK", "quality": "GOOD", "return_pct": 0.01}}
    result = esr.classify_alignment(70.0, 50.0, reactions, {"6h": 0.5})
    assert result["result"] == "NO_MEASURABLE_REACTION"


def test_aligned_when_source_above_median_and_btc_up():
    reactions = {"6h": {"status": "OK", "quality": "GOOD", "return_pct": 2.0}}
    result = esr.classify_alignment(70.0, 50.0, reactions, {"6h": 0.5})
    assert result["result"] == "ALIGNED"


def test_not_aligned_when_source_above_median_and_btc_down():
    reactions = {"6h": {"status": "OK", "quality": "GOOD", "return_pct": -2.0}}
    result = esr.classify_alignment(70.0, 50.0, reactions, {"6h": 0.5})
    assert result["result"] == "NOT_ALIGNED"


def test_mixed_when_horizons_disagree_evenly():
    reactions = {
        "6h": {"status": "OK", "quality": "GOOD", "return_pct": 2.0},
        "12h": {"status": "OK", "quality": "GOOD", "return_pct": -2.0},
    }
    result = esr.classify_alignment(70.0, 50.0, reactions, {"6h": 0.1, "12h": 0.1})
    assert result["result"] == "MIXED"


def test_poor_quality_reactions_are_never_used_for_alignment():
    reactions = {"6h": {"status": "OK", "quality": "POOR", "return_pct": 5.0}}
    result = esr.classify_alignment(70.0, 50.0, reactions, {"6h": 0.1})
    assert result["result"] == "INSUFFICIENT_EVIDENCE"


# =====================================================================
# 6. Deterministic repeated execution
# =====================================================================

def test_deterministic_rerun_identical_dataset():
    conn = _fresh_db()
    for i in range(60):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i % 11) * 5, "global": (i % 7) * 3, "onchain": (i % 5) * 4})
        _insert_btc(conn, ts, 100.0 + i * 0.3 + (5 if i % 13 == 0 else 0))
    conn.commit()
    d1 = esr.build_event_source_reaction_dataset(conn, 0, 59 * HOUR)
    d2 = esr.build_event_source_reaction_dataset(conn, 0, 59 * HOUR)
    assert d1 == d2
    conn.close()


def test_fingerprint_deduplication_produces_stable_event_ids():
    conn = _fresh_db()
    for i in range(60):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i % 11) * 5})
        _insert_btc(conn, ts, 100.0 + i * 0.3)
    conn.commit()
    events1 = esr.collect_pr3_events(conn, 0, 59 * HOUR)
    events2 = esr.collect_pr3_events(conn, 0, 59 * HOUR)
    assert [e["event_id"] for e in events1] == [e["event_id"] for e in events2]
    assert [e["fingerprint"] for e in events1] == [e["fingerprint"] for e in events2]
    conn.close()


# =====================================================================
# 7. No production writes / no network / no scope creep
# =====================================================================

def test_no_writes_anywhere_in_module():
    src = inspect.getsource(esr)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in src
    assert "def persist" not in src


def test_no_network_or_llm_calls_anywhere_in_module():
    src = inspect.getsource(esr)
    for forbidden in ["import requests", "requests.get(", "requests.post(", "urllib", "fetch(",
                       "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_never_modifies_or_reimports_v1_v2_worker():
    src = inspect.getsource(esr)
    for forbidden in ["worker.js", "import worker", "V1_WEIGHT", "prediction_generation", "0008", "0009"]:
        assert forbidden not in src


def test_never_produces_a_weight_recommendation_or_build_request():
    src = inspect.getsource(esr)
    # Forbidden as an actual emitted value (quoted string literal), not
    # merely as a word inside a docstring disclaiming that this module
    # does NOT produce one -- those disclaimers legitimately name them.
    for forbidden in ['"KEEP"', "'KEEP'", '"INCREASE"', "'INCREASE'", '"DECREASE"', "'DECREASE'",
                       '"BUILD_REQUEST"', "'BUILD_REQUEST'", '"CONDITIONAL"', "'CONDITIONAL'",
                       '"REDUNDANT"', "'REDUNDANT'"]:
        assert forbidden not in src
    for forbidden_def in ["def recommend", "def build_build_request", "def rank_source", "def score_source"]:
        assert forbidden_def not in src


def test_never_computes_a_migration_or_schema():
    src = inspect.getsource(esr)
    for forbidden in ["CREATE TABLE", "research_event_source_evaluations", "ALTER TABLE"]:
        assert forbidden not in src


# =====================================================================
# 8. End-to-end, real detector integration (synthetic but realistic)
# =====================================================================

def test_end_to_end_reuses_real_pr3_detectors_and_produces_full_dataset():
    conn = _fresh_db()
    n = 200
    for i in range(n):
        ts = i * HOUR
        price = 100.0 * (1.05 ** (i // 40))  # occasional large jumps -> LARGE_MOVE events
        _insert_btc(conn, ts, price)
        _insert_history(conn, ts, {"fng": (i * 7) % 100, "global": (i * 3) % 100, "onchain": (i * 5) % 100})
    conn.commit()
    dataset = esr.build_event_source_reaction_dataset(conn, 10 * HOUR, (n - 1) * HOUR)
    assert isinstance(dataset["events"], list)
    assert dataset["source_keys"] == sorted(dataset["source_keys"])
    assert len(dataset["results"]) == len(dataset["events"]) * len(dataset["source_keys"])
    for r in dataset["results"]:
        assert r["alignment"]["result"] in (
            "ALIGNED", "NOT_ALIGNED", "MIXED", "NO_MEASURABLE_REACTION", "INSUFFICIENT_EVIDENCE"
        )
    horizon_summary = esr.summarize_horizon_resolvability(dataset)
    assert set(horizon_summary.keys()) == set(esr.CANDIDATE_HORIZONS_MS.keys())
    source_summary = esr.summarize_by_source(dataset)
    assert set(source_summary.keys()) == set(dataset["source_keys"])
    conn.close()


def test_temporal_sequence_and_alignment_are_reported_as_separate_fields():
    # A row can have a full, valid temporal sequence (source snapshot +
    # resolved horizon) while alignment is still honestly
    # NO_MEASURABLE_REACTION or INSUFFICIENT_EVIDENCE -- the two must
    # never be collapsed into one flag (Section 7 of the build authorization).
    conn = _fresh_db()
    _insert_history(conn, 0, {"fng": 50})  # exactly at its own eventual median
    for i in range(1, 30):
        _insert_history(conn, i * HOUR, {"fng": 50})
    _insert_btc(conn, 0, 100.0)
    _insert_btc(conn, 6 * HOUR, 100.0001)  # temporal sequence exists, reaction is negligible
    conn.commit()
    anchor = esr._price_at_or_before(conn, 0)
    reaction = esr.resolve_horizon_reaction(conn, 0, 6 * HOUR, anchor)
    assert reaction["status"] == "OK"  # temporal sequence: fully established
    snap = esr.snapshot_v1_source(conn, 0, "fng")
    assert snap["status"] == "OK"  # temporal sequence: fully established
    alignment = esr.classify_alignment(snap["source_value"], 50.0, {"6h": reaction}, {"6h": 0.001})
    assert alignment["result"] == "INSUFFICIENT_EVIDENCE"  # source at its own median -- no signal
    conn.close()
