"""
Tests for research/source_dialogue.py -- the provider-agnostic,
deterministic Source Dialogue Engine. Pure functions only, no network,
no database -- every test runs fully offline against synthetic
fixtures.
"""
import re

import pytest

import source_analysis as sa
import source_dialogue as sd


def _code_only():
    """The module's source with its leading docstring and full-line
    comments stripped -- for scanning what the code actually DOES,
    never what its own documentation legitimately DISCLOSES it never
    does (the exact test-design mistake this session already caught and
    fixed once for exp009-event-source-evidence and once for
    eia_source.py: a docstring/comment saying "never writes to
    history" or "no KEEP/INCREASE/DECREASE" is the disclosure this
    project's whole documentation culture requires, not something to
    ban)."""
    with open(sd.__file__) as f:
        src = f.read()
    # Strip the leading module docstring (the first \"\"\" ... \"\"\" block).
    match = re.match(r'\s*""".*?"""', src, re.DOTALL)
    if match:
        src = src[match.end():]
    # Strip full-line comments (this module never uses inline trailing
    # comments after real code on the same line for anything these
    # tests check).
    lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    return "\n".join(lines)


def obs(information_available_at=1000, observation_time=500, direction="UP", raw_value=1.0, source_key="a"):
    return {
        "source_key": source_key,
        "source_category": "TEST",
        "information_available_at": information_available_at,
        "observation_time": observation_time,
        "direction": direction,
        "raw_value": raw_value,
        "reference_value": "{}",
        "evidence_reference": "http://example.test",
    }


# ---- Fixture 1: both sources missing ----

def test_fixture_1_both_sources_missing():
    assert sd.classify_relationship(None, None, information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"


# ---- Fixture 2/3: one side missing ----

def test_fixture_2_source_a_missing():
    assert sd.classify_relationship(None, obs(), information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"


def test_fixture_3_source_b_missing():
    assert sd.classify_relationship(obs(), None, information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"


# ---- Fixture 4: missing information_available_at ----

def test_fixture_4_missing_information_available_at():
    a = obs(information_available_at=None)
    b = obs(information_available_at=1000)
    assert sd.classify_relationship(a, b, information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"
    assert sd.classify_relationship(b, a, information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"


def test_is_eligible_never_guesses_from_a_substitute_field():
    """Missing information_available_at is never treated as eligible,
    however plausible observation_time/collection_ts might look."""
    assert sd.is_eligible(None, information_cutoff=999999) is False


# ---- Fixture 5/6: future observations rejected ----

def test_fixture_5_future_source_a_rejected():
    a = obs(information_available_at=20000)
    b = obs(information_available_at=1000)
    assert sd.classify_relationship(a, b, information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"


def test_fixture_6_future_source_b_rejected():
    a = obs(information_available_at=1000)
    b = obs(information_available_at=20000)
    assert sd.classify_relationship(a, b, information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"


def test_is_eligible_boundary_exactly_at_cutoff_is_eligible():
    assert sd.is_eligible(10000, information_cutoff=10000) is True
    assert sd.is_eligible(10001, information_cutoff=10000) is False


# ---- Fixture 7/8: same/opposite direction ----

def test_fixture_7_both_eligible_same_direction_supporting():
    a = obs(information_available_at=1000, direction="UP")
    b = obs(information_available_at=1000, direction="UP")
    assert sd.classify_relationship(a, b, information_cutoff=10000) == "SUPPORTING"

    a2 = obs(information_available_at=1000, direction="DOWN")
    b2 = obs(information_available_at=1000, direction="DOWN")
    assert sd.classify_relationship(a2, b2, information_cutoff=10000) == "SUPPORTING"


def test_fixture_8_both_eligible_opposite_direction_contradicting():
    a = obs(information_available_at=1000, direction="UP")
    b = obs(information_available_at=1000, direction="DOWN")
    assert sd.classify_relationship(a, b, information_cutoff=10000) == "CONTRADICTING"

    a2 = obs(information_available_at=1000, direction="DOWN")
    b2 = obs(information_available_at=1000, direction="UP")
    assert sd.classify_relationship(a2, b2, information_cutoff=10000) == "CONTRADICTING"


# ---- Fixture 9: neutral direction ----

def test_fixture_9_neutral_direction_is_insufficient_evidence_never_support():
    a = obs(information_available_at=1000, direction="FLAT")
    b = obs(information_available_at=1000, direction="FLAT")
    assert sd.classify_relationship(a, b, information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"

    a2 = obs(information_available_at=1000, direction=None)
    b2 = obs(information_available_at=1000, direction=None)
    assert sd.classify_relationship(a2, b2, information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"


# ---- Fixture 10: malformed direction ----

def test_fixture_10_malformed_direction_is_insufficient_evidence():
    a = obs(information_available_at=1000, direction="sideways")
    b = obs(information_available_at=1000, direction="UP")
    assert sd.classify_relationship(a, b, information_cutoff=10000) == "INSUFFICIENT_EVIDENCE"


# ---- Fixture 11/12: timing ----

def test_fixture_11_different_timing_beyond_tolerance():
    a = obs(information_available_at=0, direction="UP")
    b = obs(information_available_at=sd.SAME_WINDOW_TOLERANCE_MS + 1, direction="UP")
    assert sd.classify_relationship(a, b, information_cutoff=10 ** 9) == "DIFFERENT_TIMING"


def test_fixture_12_exact_timing_boundary_is_not_different_timing():
    """Exactly at the tolerance is still within it -- matches
    evidence_collector.classify_relation's own inclusive boundary
    convention (strict > only, not >=)."""
    a = obs(information_available_at=0, direction="UP")
    b = obs(information_available_at=sd.SAME_WINDOW_TOLERANCE_MS, direction="UP")
    assert sd.classify_relationship(a, b, information_cutoff=10 ** 9) == "SUPPORTING"


def test_timing_checked_before_direction_per_the_specified_priority_order():
    """Even with contradicting directions, a timing gap beyond
    tolerance must report DIFFERENT_TIMING, not CONTRADICTING."""
    a = obs(information_available_at=0, direction="UP")
    b = obs(information_available_at=sd.SAME_WINDOW_TOLERANCE_MS + 1, direction="DOWN")
    assert sd.classify_relationship(a, b, information_cutoff=10 ** 9) == "DIFFERENT_TIMING"


def test_reuses_the_existing_same_window_tolerance_constant_not_a_new_one():
    from evidence_collector import SAME_WINDOW_TOLERANCE_MS
    assert sd.SAME_WINDOW_TOLERANCE_MS == SAME_WINDOW_TOLERANCE_MS


# ---- Fixture 13/14: mirrored / repeated pairs ----

def test_fixture_13_mirrored_ab_ba_pair_produces_identical_canonical_key():
    assert sd.canonical_pair("beta", "alpha") == sd.canonical_pair("alpha", "beta")


def test_fixture_14_repeated_identical_pair_is_idempotent():
    r1 = sd.canonical_pair("alpha", "beta")
    r2 = sd.canonical_pair("alpha", "beta")
    assert r1 == r2


def test_canonical_pair_rejects_a_source_paired_with_itself():
    with pytest.raises(ValueError):
        sd.canonical_pair("alpha", "alpha")


# ---- Fixture 15/16: redundancy threshold ----

def _series(source_key, values, start_time=0, step=3600000):
    return [obs(observation_time=start_time + i * step, raw_value=v, source_key=source_key) for i, v in enumerate(values)]


def test_fixture_15_strong_redundancy_at_or_above_threshold():
    # Perfectly correlated series -> r = 1.0
    series_a = _series("alpha", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    series_b = _series("beta", [2, 4, 6, 8, 10, 12, 14, 16, 18, 20])
    label, detail = sd.classify_pairwise_redundancy(series_a, series_b, "alpha", "beta")
    assert label == "REDUNDANCY_UNRESOLVED"
    assert abs(detail["r"] - 1.0) < 1e-9
    assert detail["strong_redundancy"] is True


def test_fixture_16_below_threshold_correlation():
    series_a = _series("alpha", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    series_b = _series("beta", [5, 3, 8, 1, 9, 2, 7, 4, 6, 5])  # weak/no clean linear relationship
    label, detail = sd.classify_pairwise_redundancy(series_a, series_b, "alpha", "beta")
    assert label == "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED"
    assert detail["r"] is not None
    assert abs(detail["r"]) < sa.STRONG_REDUNDANCY_THRESHOLD


# ---- Fixture 17: no false complementarity ----

def test_fixture_17_below_threshold_never_labeled_complementary():
    """The label vocabulary itself makes this structurally true (no
    'COMPLEMENTARY' string exists anywhere in this module), but assert
    it explicitly against the actual returned value too."""
    series_a = _series("alpha", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    series_b = _series("beta", [5, 3, 8, 1, 9, 2, 7, 4, 6, 5])
    label, _ = sd.classify_pairwise_redundancy(series_a, series_b, "alpha", "beta")
    assert "COMPLEMENTARY" not in label
    assert label in sd.REDUNDANCY_LABELS


def test_no_complementary_label_in_code_or_returned_values():
    """"Complementary" is discussed at length in the module's own
    docstring (explaining exactly why it is deliberately not used) --
    that disclosure is required, not forbidden. What must never exist
    is the LABEL itself in code (the enum tuples, a return statement,
    or an actual returned value)."""
    assert "COMPLEMENTARY" not in sd.RELATIONSHIP_LABELS
    assert "COMPLEMENTARY" not in sd.REDUNDANCY_LABELS
    assert "complementary" not in _code_only().lower()


# ---- Fixture 18/19: event-anchored and rolling-window modes ----

def test_fixture_18_event_anchored_comparison_carries_event_id():
    a = obs(information_available_at=1000, direction="UP")
    b = obs(information_available_at=1000, direction="UP")
    interaction = sd.build_interaction("alpha", "beta", a, b, information_cutoff=10000,
                                        window_start=0, window_end=2000, event_id=42)
    assert interaction["event_id"] == 42
    assert interaction["relationship"] == "SUPPORTING"


def test_fixture_19_rolling_window_comparison_has_no_event_id():
    a = obs(information_available_at=1000, direction="UP")
    b = obs(information_available_at=1000, direction="UP")
    interaction = sd.build_interaction("alpha", "beta", a, b, information_cutoff=10000,
                                        window_start=0, window_end=2000)
    assert interaction["event_id"] is None
    assert interaction["relationship"] == "SUPPORTING"


def test_build_interaction_never_queries_or_recomputes_research_events():
    src = _code_only()
    assert "research_events" not in src
    assert "detect_large_moves" not in src and "detect_regime_reversals" not in src


def test_build_interaction_canonicalizes_output_order_regardless_of_call_order():
    a = obs(information_available_at=1000, direction="UP", source_key="zzz")
    b = obs(information_available_at=1000, direction="DOWN", source_key="aaa")
    forward = sd.build_interaction("zzz", "aaa", a, b, information_cutoff=10000, window_start=0, window_end=2000)
    backward = sd.build_interaction("aaa", "zzz", b, a, information_cutoff=10000, window_start=0, window_end=2000)
    assert forward["source_key_a"] == "aaa" and forward["source_key_b"] == "zzz"
    assert forward == backward


def test_build_interaction_includes_redundancy_only_when_series_supplied():
    a = obs(information_available_at=1000, direction="UP")
    b = obs(information_available_at=1000, direction="UP")
    without_series = sd.build_interaction("alpha", "beta", a, b, information_cutoff=10000, window_start=0, window_end=2000)
    assert without_series["redundancy"] is None
    assert without_series["redundancy_detail"] is None

    series_a = _series("alpha", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    series_b = _series("beta", [2, 4, 6, 8, 10, 12, 14, 16, 18, 20])
    with_series = sd.build_interaction(
        "alpha", "beta", a, b, information_cutoff=10000, window_start=0, window_end=2000,
        series_a=series_a, series_b=series_b,
    )
    assert with_series["redundancy"] == "REDUNDANCY_UNRESOLVED"


# ---- Fixture 20: no production-table references ----

def test_fixture_20_no_production_table_references_or_sql():
    with open(sd.__file__) as f:
        src = f.read()
    assert not re.search(r"\b(INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM)\b", src, re.IGNORECASE)
    for table in ("history", "btc_data", "predictions", "selection_decisions"):
        assert not re.search(rf"\b(INSERT INTO|UPDATE|DELETE FROM)\s+{table}\b", src, re.IGNORECASE)


def test_module_has_zero_sql_execute_calls():
    with open(sd.__file__) as f:
        src = f.read()
    assert "conn.execute" not in src
    assert "cursor.execute" not in src


# ---- Fixture 21: no forbidden weighting vocabulary ----

FORBIDDEN_WEIGHTING_TERMS = ("KEEP", "INCREASE", "DECREASE", "REMOVE", "BUILD_REQUEST")
FORBIDDEN_SCORE_TERMS = (
    "source_score", "dialogue_score", "consensus_score", "confidence_score",
    "weight_score", "ranking", "winner", "loser",
)


def test_fixture_21_no_forbidden_weighting_vocabulary():
    src = _code_only()
    for term in FORBIDDEN_WEIGHTING_TERMS:
        assert term not in src, f"forbidden weighting term found in code: {term}"


def test_no_aggregate_score_or_ranking_vocabulary():
    src = _code_only().lower()
    for term in FORBIDDEN_SCORE_TERMS:
        assert term not in src, f"forbidden score/ranking term found in code: {term}"


def test_interaction_record_has_no_numeric_aggregate_score_field():
    a = obs(information_available_at=1000, direction="UP")
    b = obs(information_available_at=1000, direction="UP")
    interaction = sd.build_interaction("alpha", "beta", a, b, information_cutoff=10000, window_start=0, window_end=2000)
    for forbidden_key in ("score", "weight", "rank", "confidence"):
        assert forbidden_key not in interaction


# ---- Fixture 22: deterministic output ----

def test_fixture_22_deterministic_output_for_identical_inputs():
    a = obs(information_available_at=1000, direction="UP")
    b = obs(information_available_at=1000, direction="UP")
    results = [
        sd.build_interaction("alpha", "beta", a, b, information_cutoff=10000, window_start=0, window_end=2000)
        for _ in range(5)
    ]
    assert all(r == results[0] for r in results)


# ---- Correlation edge cases ----

def test_correlation_edge_case_insufficient_observations_is_insufficient_evidence():
    series_a = _series("alpha", [1])
    series_b = _series("beta", [2])
    label, detail = sd.classify_pairwise_redundancy(series_a, series_b, "alpha", "beta")
    assert label == "INSUFFICIENT_EVIDENCE"
    assert detail["r"] is None


def test_correlation_edge_case_constant_series_is_insufficient_evidence_not_no_redundancy():
    """A constant series makes Pearson r mathematically undefined --
    this must NEVER be reported as NO_STRONG_PAIRWISE_REDUNDANCY_
    DETECTED (which would misleadingly imply a real, checked absence of
    redundancy)."""
    series_a = _series("alpha", [5, 5, 5, 5, 5, 5, 5, 5])
    series_b = _series("beta", [1, 2, 3, 4, 5, 6, 7, 8])
    label, detail = sd.classify_pairwise_redundancy(series_a, series_b, "alpha", "beta")
    assert label == "INSUFFICIENT_EVIDENCE"
    assert detail["r"] is None


def test_correlation_edge_case_missing_values_use_pairwise_deletion():
    """Missing values (None raw_value at a given observation_time for
    one side) are handled entirely by pairwise_source_redundancy()'s
    own pairwise-deletion logic -- this is a pass-through test, not a
    reimplementation."""
    series_a = [obs(observation_time=i * 3600000, raw_value=v, source_key="alpha") for i, v in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])]
    series_b = [obs(observation_time=i * 3600000, raw_value=v, source_key="beta") for i, v in enumerate([2, 4, 6, None, 10, 12, None, 16, 18, 20]) if v is not None]
    label, detail = sd.classify_pairwise_redundancy(series_a, series_b, "alpha", "beta")
    assert detail["n"] == 8  # 10 - 2 missing
    assert label == "REDUNDANCY_UNRESOLVED"


# ---- Invariant tests (Section 15) ----

def test_invariant_1_future_observations_can_never_be_used():
    for cutoff in (0, 500, 999):
        a = obs(information_available_at=1000, direction="UP")
        b = obs(information_available_at=500, direction="UP")
        assert sd.classify_relationship(a, b, information_cutoff=cutoff) == "INSUFFICIENT_EVIDENCE"


def test_invariant_2_missing_information_available_at_never_eligible():
    assert sd.is_eligible(None, information_cutoff=10 ** 12) is False


def test_invariant_3_ab_and_ba_cannot_create_separate_interactions():
    a = obs(information_available_at=1000, direction="UP", source_key="zzz")
    b = obs(information_available_at=1000, direction="DOWN", source_key="aaa")
    forward = sd.build_interaction("zzz", "aaa", a, b, information_cutoff=10000, window_start=0, window_end=2000)
    backward = sd.build_interaction("aaa", "zzz", b, a, information_cutoff=10000, window_start=0, window_end=2000)
    assert forward == backward


def test_invariant_4_engine_cannot_generate_a_production_weight():
    src = _code_only()
    for term in FORBIDDEN_WEIGHTING_TERMS:
        assert term not in src, f"forbidden weighting term found in code: {term}"
    for term in FORBIDDEN_SCORE_TERMS:
        assert term.lower() not in src.lower(), f"forbidden score/ranking term found in code: {term}"


def test_invariant_5_correlation_below_threshold_cannot_be_labeled_complementary():
    series_a = _series("alpha", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    series_b = _series("beta", [5, 3, 8, 1, 9, 2, 7, 4, 6, 5])
    label, detail = sd.classify_pairwise_redundancy(series_a, series_b, "alpha", "beta")
    assert abs(detail["r"]) < sa.STRONG_REDUNDANCY_THRESHOLD
    assert label != "COMPLEMENTARY"
    assert "COMPLEMENTARY" not in sd.REDUNDANCY_LABELS


def test_invariant_6_no_production_table_is_written():
    with open(sd.__file__) as f:
        src = f.read()
    assert not re.search(r"\b(INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM)\b", src, re.IGNORECASE)


def test_invariant_7_results_are_deterministic_for_identical_inputs():
    series_a = _series("alpha", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    series_b = _series("beta", [2, 4, 6, 8, 10, 12, 14, 16, 18, 20])
    r1 = sd.classify_pairwise_redundancy(series_a, series_b, "alpha", "beta")
    r2 = sd.classify_pairwise_redundancy(series_a, series_b, "alpha", "beta")
    assert r1 == r2


# ---- Reuse verification: redundancy threshold/function are the SAME object ----

def test_redundancy_reuses_the_real_source_analysis_function_not_a_copy():
    assert sd.sa.pairwise_source_redundancy is sa.pairwise_source_redundancy


def test_redundancy_threshold_is_not_redefined_in_this_module():
    """The threshold value (0.7) is discussed in this module's own
    docstring (explaining why it is reused, not reinvented) -- that
    disclosure is expected. What must never exist is a CODE-level
    assignment of the threshold anywhere in this file."""
    src = _code_only()
    assert not re.search(r"STRONG_REDUNDANCY_THRESHOLD\s*=\s*[\d.]", src)
    assert not re.search(r"=\s*0\.7\b", src)


def test_never_creates_a_production_v1_source_key():
    """This module operates entirely on caller-supplied source_key
    strings -- it defines no source key of its own, production or
    otherwise."""
    with open(sd.__file__) as f:
        src = f.read()
    v1_source_keys = (
        "fng", "funding", "longshort", "global", "cryptonews", "macrogeo",
        "geopolitics", "regulatory", "sosovalue", "onchain", "oil",
        "yield10y", "usd", "nasdaq", "sp500", "ninemag", "foufi",
        "etfflows", "hypefunding", "gold", "strc",
    )
    for key in v1_source_keys:
        assert f'"{key}"' not in src and f"'{key}'" not in src
