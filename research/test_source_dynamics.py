"""
Tests for research/source_dynamics.py (Experiment 5, Part 3).

Pure-function module -- no database, no fixture needed.

Run with: python3 -m pytest research/test_source_dynamics.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import source_dynamics as sd  # noqa: E402

HOUR = 3600000


class TestNoise:
    def test_small_oscillation_is_noise(self):
        values = [(0, 50), (HOUR, 51), (2 * HOUR, 49), (3 * HOUR, 52)]
        result = sd.classify_sequence(values)
        assert result["classification"] == "NOISE"

    def test_insufficient_data_below_two_points(self):
        assert sd.classify_sequence([(0, 50)])["classification"] == "INSUFFICIENT_DATA"
        assert sd.classify_sequence([])["classification"] == "INSUFFICIENT_DATA"


class TestPersistence:
    def test_small_but_consistent_moves_are_persistence_not_noise(self):
        # each step is inside NOISE_BAND (3.0) but consistently upward
        values = [(i * HOUR, 50 + i * 1.0) for i in range(5)]
        result = sd.classify_sequence(values)
        assert result["classification"] == "PERSISTENCE"
        assert result["direction"] == 1


class TestTrend:
    def test_consistent_large_upward_moves_are_bullish_trend(self):
        values = [(0, 50), (HOUR, 55), (2 * HOUR, 60), (3 * HOUR, 65)]
        result = sd.classify_sequence(values)
        assert result["classification"] == "BULLISH_TREND"
        assert result["direction"] == 1
        assert result["run_length"] >= sd.TREND_MIN_RUN

    def test_consistent_large_downward_moves_are_bearish_trend(self):
        values = [(0, 80), (HOUR, 74), (2 * HOUR, 68), (3 * HOUR, 62)]
        result = sd.classify_sequence(values)
        assert result["classification"] == "BEARISH_TREND"
        assert result["direction"] == -1


class TestAccelerationDeceleration:
    def test_acceleration_when_moves_grow_same_direction(self):
        values = [(0, 50), (HOUR, 54), (2 * HOUR, 62)]  # +4 then +8
        result = sd.classify_acceleration(values)
        assert result["classification"] == "ACCELERATION"

    def test_deceleration_when_moves_shrink_same_direction(self):
        values = [(0, 50), (HOUR, 62), (2 * HOUR, 66)]  # +12 then +4
        result = sd.classify_acceleration(values)
        assert result["classification"] == "DECELERATION"

    def test_insufficient_data_below_three_points(self):
        assert sd.classify_acceleration([(0, 50), (HOUR, 51)])["classification"] == "INSUFFICIENT_DATA"


class TestReversal:
    def test_established_uptrend_followed_by_down_move_is_reversal(self):
        values = [(0, 50), (HOUR, 55), (2 * HOUR, 60), (3 * HOUR, 65), (4 * HOUR, 55)]
        result = sd.detect_reversal(values)
        assert result["classification"] == "REVERSAL"
        assert result["from_direction"] == 1
        assert result["to_direction"] == -1

    def test_single_uptick_after_single_downtick_is_not_a_reversal(self):
        # no established trend precedes it -- NOISE, not REVERSAL
        values = [(0, 50), (HOUR, 48), (2 * HOUR, 50)]
        result = sd.detect_reversal(values)
        assert result["classification"] in ("NO_REVERSAL", "INSUFFICIENT_DATA")

    def test_continuing_trend_is_not_a_reversal(self):
        values = [(0, 50), (HOUR, 55), (2 * HOUR, 60), (3 * HOUR, 65), (4 * HOUR, 70)]
        result = sd.detect_reversal(values)
        assert result["classification"] == "NO_REVERSAL"


class TestContradiction:
    def test_opposite_directions_are_contradiction(self):
        assert sd.detect_contradiction(1, -1)["classification"] == "CONTRADICTION"

    def test_same_direction_is_agreement(self):
        assert sd.detect_contradiction(1, 1)["classification"] == "AGREEMENT"

    def test_none_or_zero_direction_is_insufficient_data(self):
        assert sd.detect_contradiction(None, 1)["classification"] == "INSUFFICIENT_DATA"
        assert sd.detect_contradiction(0, 1)["classification"] == "INSUFFICIENT_DATA"


class TestDiminishingRepetition:
    def test_first_mention_full_weight(self):
        assert sd.repetition_decay_weight(0) == 1.0

    def test_each_mention_strictly_less_than_previous(self):
        weights = [sd.repetition_decay_weight(i) for i in range(5)]
        for i in range(1, len(weights)):
            assert weights[i] < weights[i - 1]
            assert weights[i] > 0  # diminishing, never zero

    def test_score_repeated_evidence_is_sublinear_not_a_plain_count(self):
        # 5 repeated mentions of the SAME story must score less than 5x
        # a single mention -- the literal "diminishing, not linear" requirement.
        score_1 = sd.score_repeated_evidence([0])
        score_5 = sd.score_repeated_evidence([0, 1, 2, 3, 4])
        assert score_5 < 5 * score_1
        assert score_5 > score_1  # still more evidence than just one mention

    def test_negative_occurrence_index_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            sd.repetition_decay_weight(-1)


class TestCrossSourceConfirmation:
    def test_two_independent_sources_agreeing_within_window_confirm(self):
        events = [("fng", 0, 1), ("etfflows", HOUR, 1)]
        result = sd.cross_source_confirmation(events)
        assert result["classification"] == "CROSS_SOURCE_CONFIRMATION"
        assert set(result["confirming_sources"]) == {"fng", "etfflows"}

    def test_disagreeing_sources_do_not_confirm(self):
        events = [("fng", 0, 1), ("etfflows", HOUR, -1)]
        result = sd.cross_source_confirmation(events)
        assert result["classification"] == "NO_CONFIRMATION"

    def test_agreeing_but_far_apart_in_time_does_not_confirm(self):
        events = [("fng", 0, 1), ("etfflows", 100 * HOUR, 1)]
        result = sd.cross_source_confirmation(events)
        assert result["classification"] == "NO_CONFIRMATION"

    def test_single_source_cannot_self_confirm(self):
        events = [("fng", 0, 1)]
        result = sd.cross_source_confirmation(events)
        assert result["classification"] == "NO_CONFIRMATION"

    def test_three_agreeing_sources_all_returned(self):
        events = [("fng", 0, 1), ("etfflows", HOUR, 1), ("onchain", 2 * HOUR, 1)]
        result = sd.cross_source_confirmation(events)
        assert result["classification"] == "CROSS_SOURCE_CONFIRMATION"
        assert set(result["confirming_sources"]) == {"fng", "etfflows", "onchain"}
