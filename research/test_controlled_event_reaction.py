"""
Tests for research/controlled_event_reaction.py (PR-2).

All tests execute against real (non-mocked) synthetic fixtures or a
real in-memory sqlite3 database mirroring production. No network, no
D1 connection.

Run with: python3 -m pytest research/test_controlled_event_reaction.py -v
"""
import inspect
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import controlled_event_reaction as cer  # noqa: E402
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
# 1. No-lookahead source snapshot (inherited from PR-1, re-verified at
# the PR-2 orchestration level)
# =====================================================================

def test_dataset_never_uses_a_source_row_after_event_ts():
    conn = _fresh_db()
    for i in range(60):
        _insert_history(conn, i * HOUR, {"fng": (i % 11) * 5, "global": (i % 7) * 3, "onchain": (i % 5) * 4})
        _insert_btc(conn, i * HOUR, 100.0 + i * 0.2)
    conn.commit()
    dataset = cer.build_controlled_reaction_dataset(conn, 10 * HOUR, 59 * HOUR)
    for r in dataset["results"]:
        if r["source_observation_ts"] is not None:
            assert r["source_observation_ts"] <= r["event_ts"]
    conn.close()


# =====================================================================
# 2. No-lookahead pre-event baseline
# =====================================================================

def test_continuation_baseline_uses_only_pre_event_return():
    # pre-event return is +24% over 24h -> rate = 1%/hour; a 6h baseline
    # must be exactly 6%, using nothing after event_ts.
    pre_event = {"status": "OK", "return_pct": 24.0}
    baseline = cer.compute_continuation_baseline(pre_event, 6 * HOUR)
    assert baseline["status"] == "OK"
    assert abs(baseline["baseline_return_pct"] - 6.0) < 1e-9


def test_continuation_baseline_insufficient_when_pre_event_unavailable():
    pre_event = {"status": "INSUFFICIENT_EVIDENCE", "return_pct": None}
    baseline = cer.compute_continuation_baseline(pre_event, 6 * HOUR)
    assert baseline["status"] == "INSUFFICIENT_EVIDENCE"
    assert baseline["baseline_return_pct"] is None


# =====================================================================
# 3. No-lookahead post-event reaction (inherited from PR-1, re-verified)
# =====================================================================

def test_post_event_reaction_never_uses_a_point_at_or_before_event_ts():
    conn = _fresh_db()
    _insert_btc(conn, 0, 100.0)  # exactly at event_ts
    conn.commit()
    anchor = esr._price_at_or_before(conn, 0)
    reaction = esr.resolve_horizon_reaction(conn, 0, 6 * HOUR, anchor)
    assert reaction["status"] == "NO_DATA"
    conn.close()


# =====================================================================
# 4. Continuation-control calculation
# =====================================================================

def test_reaction_above_control_when_residual_exceeds_floor():
    reaction = {"status": "OK", "quality": "GOOD", "return_pct": 5.0}
    residual = {"status": "OK", "residual_pct": 4.0}
    result = cer.classify_control(reaction, residual, reaction_noise_floor=0.1, residual_noise_floor=1.0)
    assert result["result"] == "REACTION_ABOVE_CONTROL"


def test_reaction_below_control_when_residual_is_negative_and_exceeds_floor():
    reaction = {"status": "OK", "quality": "GOOD", "return_pct": -5.0}
    residual = {"status": "OK", "residual_pct": -4.0}
    result = cer.classify_control(reaction, residual, reaction_noise_floor=0.1, residual_noise_floor=1.0)
    assert result["result"] == "REACTION_BELOW_CONTROL"


def test_continuation_consistent_when_residual_within_floor():
    reaction = {"status": "OK", "quality": "GOOD", "return_pct": 5.0}
    residual = {"status": "OK", "residual_pct": 0.2}
    result = cer.classify_control(reaction, residual, reaction_noise_floor=0.1, residual_noise_floor=1.0)
    assert result["result"] == "CONTINUATION_CONSISTENT"


# =====================================================================
# 5. Residual reaction calculation
# =====================================================================

def test_residual_is_post_event_minus_baseline():
    reaction = {"status": "OK", "return_pct": 7.5}
    baseline = {"status": "OK", "baseline_return_pct": 2.5}
    residual = cer.compute_residual_reaction(reaction, baseline)
    assert residual["status"] == "OK"
    assert abs(residual["residual_pct"] - 5.0) < 1e-9


def test_residual_insufficient_when_reaction_not_resolved():
    reaction = {"status": "NO_DATA", "return_pct": None}
    baseline = {"status": "OK", "baseline_return_pct": 2.5}
    residual = cer.compute_residual_reaction(reaction, baseline)
    assert residual["status"] == "INSUFFICIENT_EVIDENCE"


# =====================================================================
# 6. Insufficient source history
# =====================================================================

def test_event_level_table_reports_insufficient_evidence_for_missing_source():
    conn = _fresh_db()
    for i in range(60):
        # "fng" is discovered (present in SOME rows) but absent from the
        # row nearest to any given event -- must report None, not 0 or
        # an imputed value (source_analysis's own "missing != zero" rule).
        sources = {"global": (i % 7) * 3}
        if i % 10 == 0:
            sources["fng"] = 50  # present only occasionally
        _insert_history(conn, i * HOUR, sources)
        _insert_btc(conn, i * HOUR, 100.0 + i * 0.2)
    conn.commit()
    dataset = cer.build_controlled_reaction_dataset(conn, 10 * HOUR, 59 * HOUR)
    rows = cer.build_event_level_table(dataset)
    fng_rows = [r for r in rows if r["source_key"] == "fng"]
    assert fng_rows
    assert any(r["source_value"] is None for r in fng_rows)
    conn.close()


# =====================================================================
# 7. Insufficient BTC reaction
# =====================================================================

def test_control_insufficient_when_no_horizon_resolves():
    reaction = {"status": "NO_DATA", "quality": None, "return_pct": None}
    residual = {"status": "INSUFFICIENT_EVIDENCE", "residual_pct": None}
    result = cer.classify_control(reaction, residual, reaction_noise_floor=0.1, residual_noise_floor=1.0)
    assert result["result"] == "INSUFFICIENT_EVIDENCE"


def test_select_primary_horizon_returns_none_when_nothing_resolves():
    reactions = {h: {"status": "NO_DATA", "quality": None} for h in esr.CANDIDATE_HORIZONS_MS}
    assert cer.select_primary_horizon(reactions) is None


# =====================================================================
# 8. Resolution-quality preservation (never silently substituted)
# =====================================================================

def test_event_level_table_preserves_real_elapsed_time_and_quality():
    conn = _fresh_db()
    _insert_history(conn, 0, {"fng": 90, "global": 10, "onchain": 10})
    for i in range(1, 100):
        _insert_history(conn, i * HOUR, {"fng": 50, "global": 50, "onchain": 50})
    _insert_btc(conn, 0, 100.0)
    for i in range(1, 30):
        _insert_btc(conn, i * HOUR, 100.0 + i)
    conn.commit()
    dataset = cer.build_controlled_reaction_dataset(conn, 0, 29 * HOUR)
    rows = cer.build_event_level_table(dataset)
    resolved = [r for r in rows if r["requested_horizon"] is not None]
    assert resolved
    for r in resolved:
        assert r["actual_elapsed_ms"] is not None
        assert r["resolution_quality"] in ("GOOD", "APPROXIMATE")
        assert r["requested_horizon"] in cer.PRIMARY_HORIZON_PRIORITY
    conn.close()


def test_primary_horizon_prefers_longest_available():
    # both 6h and 24h resolve at GOOD quality -> 24h must be preferred.
    reactions = {h: {"status": "NO_DATA", "quality": None} for h in esr.CANDIDATE_HORIZONS_MS}
    reactions["6h"] = {"status": "OK", "quality": "GOOD", "return_pct": 1.0}
    reactions["24h"] = {"status": "OK", "quality": "GOOD", "return_pct": 2.0}
    assert cer.select_primary_horizon(reactions) == "24h"


# =====================================================================
# 9. Deterministic repeated execution
# =====================================================================

def test_deterministic_rerun_identical_dataset():
    conn = _fresh_db()
    for i in range(80):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i * 7) % 100, "global": (i * 3) % 100, "onchain": (i * 5) % 100})
        _insert_btc(conn, ts, 100.0 * (1.02 ** (i // 20)))
    conn.commit()
    d1 = cer.build_controlled_reaction_dataset(conn, 10 * HOUR, 79 * HOUR)
    d2 = cer.build_controlled_reaction_dataset(conn, 10 * HOUR, 79 * HOUR)
    assert d1 == d2
    conn.close()


def test_deterministic_rerun_of_summaries():
    conn = _fresh_db()
    for i in range(80):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i * 7) % 100, "global": (i * 3) % 100, "onchain": (i * 5) % 100})
        _insert_btc(conn, ts, 100.0 * (1.02 ** (i // 20)))
    conn.commit()
    d = cer.build_controlled_reaction_dataset(conn, 10 * HOUR, 79 * HOUR)
    assert cer.summarize_by_source_controlled(d) == cer.summarize_by_source_controlled(d)
    assert cer.summarize_by_horizon(d) == cer.summarize_by_horizon(d)
    conn.close()


# =====================================================================
# 10-14. Safety / scope tests
# =====================================================================

def test_no_writes_anywhere_in_module():
    src = inspect.getsource(cer)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in src
    assert "def persist" not in src


def test_no_network_or_llm_calls_anywhere_in_module():
    src = inspect.getsource(cer)
    for forbidden in ["import requests", "requests.get(", "requests.post(", "urllib", "fetch(",
                       "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_never_modifies_or_reimports_v1_v2_worker():
    src = inspect.getsource(cer)
    for forbidden in ["worker.js", "import worker", "V1_WEIGHT", "prediction_generation", "0008", "0009", "0010"]:
        assert forbidden not in src


def test_never_produces_a_weight_recommendation_vocabulary():
    src = inspect.getsource(cer)
    for forbidden in ['"KEEP"', "'KEEP'", '"INCREASE"', "'INCREASE'", '"DECREASE"', "'DECREASE'",
                       '"CONDITIONAL"', "'CONDITIONAL'", '"REDUNDANT"', "'REDUNDANT'"]:
        assert forbidden not in src
    for forbidden_def in ["def recommend", "def rank_source", "def score_source", "def best_source"]:
        assert forbidden_def not in src


def test_never_generates_a_build_request():
    src = inspect.getsource(cer)
    for forbidden in ['"BUILD_REQUEST"', "'BUILD_REQUEST'", "def build_build_request", "hypothesis_gate"]:
        assert forbidden not in src


def test_never_creates_schema_or_migration():
    src = inspect.getsource(cer)
    for forbidden in ["CREATE TABLE", "ALTER TABLE", "research_event_source_evaluations", ".ai/migrations"]:
        assert forbidden not in src


# =====================================================================
# Structural / distinction tests (Section 7's five-concepts requirement)
# =====================================================================

def test_control_classification_is_source_independent():
    # The control layer must never depend on which source is being
    # looked at -- every source row for the SAME event must carry the
    # SAME control classification at the SAME horizon.
    conn = _fresh_db()
    for i in range(60):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i * 7) % 100, "global": (i * 11) % 100, "onchain": (i * 13) % 100})
        _insert_btc(conn, ts, 100.0 + (10 if i == 40 else 0) + i * 0.1)
    conn.commit()
    dataset = cer.build_controlled_reaction_dataset(conn, 10 * HOUR, 59 * HOUR)
    by_event = {}
    for r in dataset["results"]:
        key = r["event_id"]
        cls = r["control_by_horizon"]["6h"]["classification"]["result"]
        by_event.setdefault(key, set()).add(cls)
    for event_id, classes in by_event.items():
        assert len(classes) == 1, f"event {event_id} has source-dependent control classification: {classes}"
    conn.close()


def test_pr1_alignment_field_reused_verbatim_not_recomputed():
    conn = _fresh_db()
    for i in range(60):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i * 7) % 100, "global": (i * 11) % 100, "onchain": (i * 13) % 100})
        _insert_btc(conn, ts, 100.0 + i * 0.1)
    conn.commit()
    base = esr.build_event_source_reaction_dataset(conn, 10 * HOUR, 59 * HOUR)
    controlled = cer.build_controlled_reaction_dataset(conn, 10 * HOUR, 59 * HOUR)
    base_alignments = [r["alignment"] for r in base["results"]]
    controlled_alignments = [r["alignment"] for r in controlled["results"]]
    assert base_alignments == controlled_alignments
    conn.close()


def test_source_summary_never_ranks_sources():
    conn = _fresh_db()
    for i in range(60):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i * 7) % 100, "global": (i * 11) % 100, "onchain": (i * 13) % 100})
        _insert_btc(conn, ts, 100.0 + i * 0.1)
    conn.commit()
    dataset = cer.build_controlled_reaction_dataset(conn, 10 * HOUR, 59 * HOUR)
    summary = cer.summarize_by_source_controlled(dataset)
    for source_key, stats in summary.items():
        assert "rank" not in stats
        assert "score" not in stats
        assert "best" not in stats
