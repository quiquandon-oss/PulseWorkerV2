"""
Tests for research/source_intelligence.py (Experiment 5, Part 2).

Run with: python3 -m pytest research/test_source_intelligence.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import source_intelligence as si  # noqa: E402

HOUR = 3600000


def _rows(*source_dicts):
    return [{"observation_ts": i * HOUR, "sources": d} for i, d in enumerate(source_dicts)]


class TestCandidateSources:
    def test_known_v1_sources_are_never_candidates(self):
        rows = _rows({"fng": 50, "onchain": 90})
        assert si.detect_candidate_sources(rows) == []

    def test_unknown_key_is_flagged_as_candidate(self):
        rows = _rows({"fng": 50, "brand_new_source": 42})
        assert si.detect_candidate_sources(rows) == ["brand_new_source"]

    def test_deterministic_same_input_same_output(self):
        rows = _rows({"fng": 50, "x": 1}, {"fng": 51, "x": 2})
        assert si.detect_candidate_sources(rows) == si.detect_candidate_sources(rows)

    def test_never_auto_added_to_known_set(self):
        rows = _rows({"new_one": 1})
        si.detect_candidate_sources(rows)
        assert "new_one" not in si.KNOWN_V1_SOURCE_IDS  # calling the function must not mutate the frozenset


class TestStaleSources:
    def test_flat_value_across_min_observations_is_stale(self):
        rows = _rows(*[{"fng": 50} for _ in range(si.STALE_AFTER_N_OBSERVATIONS)])
        stale = si.detect_stale_sources(rows)
        assert stale["fng"] is True

    def test_varying_value_is_not_stale(self):
        rows = _rows(*[{"fng": 50 + i} for i in range(si.STALE_AFTER_N_OBSERVATIONS)])
        stale = si.detect_stale_sources(rows)
        assert stale["fng"] is False

    def test_insufficient_observations_never_guessed_as_stale(self):
        rows = _rows({"fng": 50}, {"fng": 50})  # only 2, far below the min
        stale = si.detect_stale_sources(rows)
        assert stale["fng"] is False

    def test_absent_rows_do_not_count_toward_staleness_window(self):
        # fng present, flat, for min_observations rows; onchain only present once
        flat_rows = [{"fng": 50} for _ in range(si.STALE_AFTER_N_OBSERVATIONS)]
        flat_rows[0]["onchain"] = 99
        rows = _rows(*flat_rows)
        stale = si.detect_stale_sources(rows)
        assert stale["fng"] is True
        assert stale["onchain"] is False  # only 1 observation ever


class TestRecurrence:
    def test_coverage_percent_computed_correctly(self):
        rows = _rows({"fng": 50}, {"fng": 51}, {})  # present 2 of 3
        recurrence = si.source_recurrence(rows)
        assert recurrence["fng"]["n_present"] == 2
        assert recurrence["fng"]["n_rows"] == 3
        assert recurrence["fng"]["coverage_pct"] == round(200 / 3, 1)

    def test_source_never_present_does_not_appear_in_output(self):
        rows = _rows({"fng": 50})
        recurrence = si.source_recurrence(rows)
        assert "onchain" not in recurrence


class TestRedundancy:
    def test_perfectly_correlated_sources_flagged_redundant(self):
        rows = _rows(
            {"a": 10, "b": 20}, {"a": 20, "b": 40}, {"a": 30, "b": 60}, {"a": 40, "b": 80},
        )
        redundant = si.redundant_source_pairs(rows, ["a", "b"])
        assert "a|b" in redundant
        assert redundant["a|b"]["r"] > 0.99

    def test_uncorrelated_sources_not_flagged(self):
        rows = _rows(
            {"a": 10, "b": 5}, {"a": 20, "b": 40}, {"a": 5, "b": 30}, {"a": 40, "b": 1},
        )
        redundant = si.redundant_source_pairs(rows, ["a", "b"])
        assert "a|b" not in redundant
