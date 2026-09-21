"""
Adversarial tests for research/evidence_quality.py -- the generic,
provider-agnostic Research Evidence Quality Layer.

Sections A-J below correspond exactly to the build authorization's own
required test list (Section 15). This file never asserts a numeric
"quality score" (none exists) and never asserts a source ranking (none
exists) -- only the deterministic categorical statuses and their plain
diagnostic detail.
"""
import math

import evidence_quality as eq

HOUR = 3_600_000
DAY = 24 * HOUR


def _obs(information_available_at=1000, observation_time=1000, **extra):
    return {"information_available_at": information_available_at, "observation_time": observation_time, **extra}


# =====================================================================
# A. AS-OF
# =====================================================================

def test_A_observation_before_cutoff_is_eligible():
    result = eq.assess_evidence_quality([_obs(500, 500)], information_cutoff=1000)
    assert result["AS_OF_SAFETY"]["status"] == "PASS"
    assert result["AS_OF_SAFETY"]["n_eligible"] == 1
    assert result["AS_OF_SAFETY"]["n_ineligible"] == 0


def test_A_observation_exactly_at_cutoff_is_eligible():
    result = eq.assess_evidence_quality([_obs(1000, 1000)], information_cutoff=1000)
    assert result["AS_OF_SAFETY"]["status"] == "PASS"
    assert result["AS_OF_SAFETY"]["n_eligible"] == 1


def test_A_observation_after_cutoff_is_ineligible():
    result = eq.assess_evidence_quality([_obs(1001, 1001)], information_cutoff=1000)
    assert result["AS_OF_SAFETY"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["AS_OF_SAFETY"]["n_eligible"] == 0
    assert result["AS_OF_SAFETY"]["n_ineligible"] == 1


def test_A_future_observation_never_changes_the_as_of_eligible_count():
    """Adding a future observation must never change how many
    observations were found as-of ELIGIBLE -- only n_total/n_ineligible
    may grow."""
    baseline = [_obs(500, 500), _obs(700, 700), _obs(900, 900)]
    with_future = baseline + [_obs(5000, 5000)]

    r1 = eq.assess_evidence_quality(baseline, information_cutoff=1000)
    r2 = eq.assess_evidence_quality(with_future, information_cutoff=1000)

    assert r1["AS_OF_SAFETY"]["n_eligible"] == r2["AS_OF_SAFETY"]["n_eligible"] == 3
    assert r2["AS_OF_SAFETY"]["n_ineligible"] == 1
    assert r1["AS_OF_SAFETY"]["status"] == r2["AS_OF_SAFETY"]["status"] == "PASS"


def test_A_missing_information_available_at_fails_closed():
    result = eq.assess_evidence_quality([_obs(None, 1000)], information_cutoff=1000)
    assert result["AS_OF_SAFETY"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["AS_OF_SAFETY"]["n_eligible"] == 0


def test_A_malformed_cutoff_fails_closed_to_invalid():
    result = eq.assess_evidence_quality([_obs(500, 500)], information_cutoff="not-a-timestamp")
    assert result["AS_OF_SAFETY"]["status"] == "FAIL"
    assert result["OVERALL_STATUS"] == "INVALID"


def test_A_boolean_timestamps_fail_closed():
    result = eq.assess_evidence_quality([_obs(True, True)], information_cutoff=1000)
    assert result["AS_OF_SAFETY"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["TIMESTAMP_VALIDITY"]["status"] == "FAIL"


def test_A_nan_timestamps_fail_closed():
    result = eq.assess_evidence_quality([_obs(float("nan"), 1000)], information_cutoff=1000)
    assert result["AS_OF_SAFETY"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["TIMESTAMP_VALIDITY"]["status"] == "FAIL"


# =====================================================================
# B. TIMESTAMP VALIDITY
# =====================================================================

def test_B_valid_numeric_timestamps_pass():
    result = eq.assess_evidence_quality([_obs(100, 100), _obs(200, 200)], information_cutoff=1000)
    assert result["TIMESTAMP_VALIDITY"]["status"] == "PASS"
    assert result["TIMESTAMP_VALIDITY"]["n_valid"] == 2


def test_B_string_timestamp_rejected():
    result = eq.assess_evidence_quality([_obs("100", 100)], information_cutoff=1000)
    assert result["TIMESTAMP_VALIDITY"]["status"] == "FAIL"


def test_B_bool_timestamp_rejected():
    result = eq.assess_evidence_quality([_obs(100, False)], information_cutoff=1000)
    assert result["TIMESTAMP_VALIDITY"]["status"] == "FAIL"


def test_B_nan_timestamp_rejected():
    result = eq.assess_evidence_quality([_obs(100, math.nan)], information_cutoff=1000)
    assert result["TIMESTAMP_VALIDITY"]["status"] == "FAIL"


def test_B_missing_timestamp_rejected():
    result = eq.assess_evidence_quality([{"information_available_at": 100}], information_cutoff=1000)
    assert result["TIMESTAMP_VALIDITY"]["status"] == "FAIL"


def test_B_all_invalid_reports_fail_not_insufficient():
    result = eq.assess_evidence_quality([_obs("bad", "bad")], information_cutoff=1000)
    assert result["TIMESTAMP_VALIDITY"]["status"] == "FAIL"
    assert result["TIMESTAMP_VALIDITY"]["n_valid"] == 0


def test_B_no_observations_is_insufficient_not_fail():
    result = eq.assess_evidence_quality([], information_cutoff=1000)
    assert result["TIMESTAMP_VALIDITY"]["status"] == "INSUFFICIENT_EVIDENCE"


# =====================================================================
# C. WINDOW CONFORMANCE
# =====================================================================

def test_C_before_window_excluded():
    result = eq.assess_evidence_quality([_obs(50, 50)], information_cutoff=1000, window_start=100, window_end=900)
    assert result["WINDOW_CONFORMANCE"]["n_outside"] == 1
    assert result["WINDOW_CONFORMANCE"]["status"] == "INSUFFICIENT_EVIDENCE"


def test_C_inside_window_included():
    result = eq.assess_evidence_quality([_obs(500, 500)], information_cutoff=1000, window_start=100, window_end=900)
    assert result["WINDOW_CONFORMANCE"]["n_within"] == 1
    assert result["WINDOW_CONFORMANCE"]["status"] == "PASS"


def test_C_exact_boundaries_included_inclusive():
    result = eq.assess_evidence_quality(
        [_obs(100, 100), _obs(900, 900)], information_cutoff=1000, window_start=100, window_end=900,
    )
    assert result["WINDOW_CONFORMANCE"]["n_within"] == 2
    assert result["WINDOW_CONFORMANCE"]["n_outside"] == 0


def test_C_after_window_excluded():
    result = eq.assess_evidence_quality([_obs(950, 950)], information_cutoff=1000, window_start=100, window_end=900)
    assert result["WINDOW_CONFORMANCE"]["n_outside"] == 1


def test_C_omitted_window_bounds_are_never_reinterpreted_as_a_restriction():
    result = eq.assess_evidence_quality([_obs(-999999, -999999)], information_cutoff=1000)
    assert result["WINDOW_CONFORMANCE"]["status"] == "PASS"
    assert result["WINDOW_CONFORMANCE"]["n_within"] == 1


# =====================================================================
# D. DUPLICATES
# =====================================================================

def test_D_no_duplicates_passes():
    obs = [_obs(100, 100), _obs(200, 200), _obs(300, 300)]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000)
    assert result["DUPLICATE_QUALITY"]["status"] == "PASS"
    assert result["DUPLICATE_QUALITY"]["n_duplicate_timestamps"] == 0


def test_D_duplicate_timestamp_detected_never_silently_resolved():
    obs = [_obs(100, 100, source_key="alpha"), _obs(50, 100, source_key="alpha")]  # same observation_time
    result = eq.assess_evidence_quality(obs, information_cutoff=1000)
    assert result["DUPLICATE_QUALITY"]["status"] == "FAIL"
    assert result["DUPLICATE_QUALITY"]["n_duplicate_timestamps"] == 1
    assert result["DUPLICATE_QUALITY"]["duplicate_timestamps"] == [100]
    assert result["OVERALL_STATUS"] == "INVALID"


def test_D_duplicate_timestamp_in_both_series_detected_independently():
    """Two independent single-series calls (the shape a two-source
    caller like EXP-010 would actually make) each detect their own
    duplicate -- one series's cleanliness never masks the other's."""
    series_a = [_obs(100, 100), _obs(50, 100)]
    series_b = [_obs(100, 200), _obs(50, 200)]
    result_a = eq.assess_evidence_quality(series_a, information_cutoff=1000)
    result_b = eq.assess_evidence_quality(series_b, information_cutoff=1000)
    assert result_a["DUPLICATE_QUALITY"]["status"] == "FAIL"
    assert result_b["DUPLICATE_QUALITY"]["status"] == "FAIL"


def test_D_duplicate_never_silently_averaged_or_arbitrarily_chosen():
    """Regression guard for the exact Source Dialogue F4 bug class: a
    dict keyed by observation_time silently overwriting. This layer
    must report the duplicate, never pick a winner."""
    obs = [_obs(1, 100, raw_value=10), _obs(2, 100, raw_value=999)]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000)
    assert result["DUPLICATE_QUALITY"]["status"] == "FAIL"
    # The raw observations list itself must remain untouched (2 items,
    # neither dropped) -- this layer never mutates its input.
    assert len(obs) == 2


# =====================================================================
# E. SAMPLE DEPTH
# =====================================================================

def test_E_zero_observations():
    result = eq.assess_evidence_quality([], information_cutoff=1000, min_observations=5)
    assert result["SAMPLE_DEPTH"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["SAMPLE_DEPTH"]["actual_observations"] == 0
    assert result["SAMPLE_DEPTH"]["required_observations"] == 5


def test_E_below_threshold():
    obs = [_obs(i, i) for i in range(3)]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000, min_observations=5)
    assert result["SAMPLE_DEPTH"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["SAMPLE_DEPTH"]["actual_observations"] == 3


def test_E_exactly_at_threshold_passes():
    obs = [_obs(i, i) for i in range(5)]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000, min_observations=5)
    assert result["SAMPLE_DEPTH"]["status"] == "PASS"
    assert result["SAMPLE_DEPTH"]["actual_observations"] == 5


def test_E_above_threshold_passes():
    obs = [_obs(i, i) for i in range(10)]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000, min_observations=5)
    assert result["SAMPLE_DEPTH"]["status"] == "PASS"


def test_E_no_threshold_supplied_reports_actual_without_pretending_sufficiency():
    obs = [_obs(1, 1)]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000)
    assert result["SAMPLE_DEPTH"]["required_observations"] is None
    assert result["SAMPLE_DEPTH"]["actual_observations"] == 1
    assert result["SAMPLE_DEPTH"]["status"] == "PASS"  # never fabricated as INSUFFICIENT with no threshold to fail


def test_E_sample_depth_excludes_future_observations():
    """A future observation must not count toward SAMPLE_DEPTH -- it
    was never actually usable at the stated cutoff."""
    obs = [_obs(1, 1), _obs(2, 2), _obs(9999, 9999)]  # last is post-cutoff
    result = eq.assess_evidence_quality(obs, information_cutoff=1000, min_observations=3)
    assert result["SAMPLE_DEPTH"]["actual_observations"] == 2
    assert result["SAMPLE_DEPTH"]["status"] == "INSUFFICIENT_EVIDENCE"


# =====================================================================
# F. CADENCE
# =====================================================================

def test_F_regular_series_no_threshold_is_pass_diagnostic_only():
    obs = [_obs(i * HOUR, i * HOUR) for i in range(5)]
    result = eq.assess_evidence_quality(obs, information_cutoff=10 * HOUR)
    c = result["CADENCE"]
    assert c["status"] == "PASS"
    assert c["observation_count"] == 5
    assert c["largest_gap_ms"] == HOUR
    assert c["median_gap_ms"] == HOUR
    assert c["n_gaps_exceeding_threshold"] is None  # no threshold supplied -> never classified


def test_F_known_gap_flagged_against_explicit_threshold():
    obs = [_obs(0, 0), _obs(HOUR, HOUR), _obs(HOUR + 10 * HOUR, HOUR + 10 * HOUR)]  # one big gap
    result = eq.assess_evidence_quality(obs, information_cutoff=20 * HOUR, gap_threshold_ms=2 * HOUR)
    c = result["CADENCE"]
    assert c["largest_gap_ms"] == 10 * HOUR
    assert c["n_gaps_exceeding_threshold"] == 1
    assert c["status"] in ("WARNING", "FAIL")


def test_F_multiple_gaps_exceeding_threshold_escalates_to_fail():
    times = [0, HOUR, 2 * HOUR, 20 * HOUR, 40 * HOUR, 60 * HOUR]
    obs = [_obs(t, t) for t in times]
    result = eq.assess_evidence_quality(obs, information_cutoff=100 * HOUR, gap_threshold_ms=2 * HOUR)
    c = result["CADENCE"]
    assert c["n_gaps_exceeding_threshold"] == 3
    assert c["status"] == "FAIL"
    assert result["OVERALL_STATUS"] == "INVALID"


def test_F_unspecified_expected_cadence_never_inferred():
    obs = [_obs(0, 0), _obs(HOUR, HOUR)]
    result = eq.assess_evidence_quality(obs, information_cutoff=10 * HOUR)
    assert result["CADENCE"]["expected_cadence_ms"] is None


def test_F_fewer_than_two_points_is_insufficient_not_a_fabricated_gap():
    result = eq.assess_evidence_quality([_obs(1, 1)], information_cutoff=1000)
    assert result["CADENCE"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["CADENCE"]["largest_gap_ms"] is None


# =====================================================================
# G. PROVENANCE
# =====================================================================

def test_G_complete_provenance_is_verified():
    obs = [
        _obs(1, 1, source_key="alpha", provider="V1"),
        _obs(2, 2, source_key="alpha", provider="V1"),
    ]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000)
    assert result["PROVENANCE"]["status"] == "VERIFIED"
    assert "source_key" in result["PROVENANCE"]["fields_fully_present"]
    assert "provider" in result["PROVENANCE"]["fields_fully_present"]


def test_G_partial_provenance():
    obs = [_obs(1, 1, source_key="alpha"), _obs(2, 2)]  # only one has source_key
    result = eq.assess_evidence_quality(obs, information_cutoff=1000)
    assert result["PROVENANCE"]["status"] == "PARTIAL"
    assert "source_key" in result["PROVENANCE"]["fields_partially_present"]


def test_G_missing_provenance_is_unknown_never_invented():
    obs = [_obs(1, 1), _obs(2, 2)]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000)
    assert result["PROVENANCE"]["status"] == "UNKNOWN"
    assert set(result["PROVENANCE"]["fields_missing"]) == set(eq.PROVENANCE_FIELDS)


# =====================================================================
# H. BOUNDED RESEARCH WINDOW -- data outside the requested range must
# never influence the result
# =====================================================================

def test_H_data_outside_requested_window_never_influences_sample_depth():
    in_window = [_obs(t, t) for t in (100, 200, 300)]
    outside_window = [_obs(t, t) for t in (5000, 6000)]  # would satisfy as-of but not window

    r_in_only = eq.assess_evidence_quality(in_window, information_cutoff=10000, window_start=0, window_end=1000, min_observations=3)
    r_with_outside = eq.assess_evidence_quality(in_window + outside_window, information_cutoff=10000, window_start=0, window_end=1000, min_observations=3)

    assert r_in_only["SAMPLE_DEPTH"]["actual_observations"] == r_with_outside["SAMPLE_DEPTH"]["actual_observations"] == 3
    assert r_in_only["SAMPLE_DEPTH"]["status"] == r_with_outside["SAMPLE_DEPTH"]["status"] == "PASS"


def test_H_data_outside_window_never_influences_cadence():
    in_window = [_obs(0, 0), _obs(HOUR, HOUR)]
    far_outside = [_obs(1000 * HOUR, 1000 * HOUR)]  # would create a huge gap if it leaked in
    r1 = eq.assess_evidence_quality(in_window, information_cutoff=2000 * HOUR, window_start=0, window_end=2 * HOUR)
    r2 = eq.assess_evidence_quality(in_window + far_outside, information_cutoff=2000 * HOUR, window_start=0, window_end=2 * HOUR)
    assert r1["CADENCE"]["largest_gap_ms"] == r2["CADENCE"]["largest_gap_ms"] == HOUR


# =====================================================================
# I. DETERMINISTIC BEHAVIOR
# =====================================================================

def test_I_same_input_twice_is_byte_identical():
    obs = [_obs(1, 1, source_key="alpha"), _obs(2, 2, source_key="alpha"), _obs(3, 3)]
    r1 = eq.assess_evidence_quality(obs, information_cutoff=1000, window_start=0, window_end=1000,
                                     min_observations=2, gap_threshold_ms=10)
    r2 = eq.assess_evidence_quality(obs, information_cutoff=1000, window_start=0, window_end=1000,
                                     min_observations=2, gap_threshold_ms=10)
    assert r1 == r2


def test_I_input_list_is_never_mutated():
    obs = [_obs(1, 1), _obs(2, 2)]
    original = [dict(o) for o in obs]
    eq.assess_evidence_quality(obs, information_cutoff=1000)
    assert obs == original


# =====================================================================
# J. FUTURE-DATA REGRESSION
# =====================================================================

def test_J_extreme_post_cutoff_observations_never_change_the_historical_assessment():
    """Construct a fixture where a naive (non-as-of-aware) statistic
    over ALL supplied observations would visibly change once extreme
    future observations are added (e.g. a naive count/median/duplicate
    check run over the raw list). Confirm every dimension of this
    layer's own historical assessment (sample depth, cadence,
    duplicates, provenance, overall status) is IDENTICAL whether or not
    those future observations are present."""
    cutoff = 10 * HOUR
    historical = [_obs(i * HOUR, i * HOUR, source_key="alpha") for i in range(1, 6)]  # 5 pre-cutoff obs
    # Extreme future block: many more observations than the historical
    # set, with a duplicate timestamp among themselves and no
    # provenance -- exactly the kind of data that would swing a naive
    # sample-depth/cadence/duplicate/provenance check if it leaked in.
    extreme_future = (
        [_obs(cutoff + i * HOUR, cutoff + i * HOUR) for i in range(1, 50)]
        + [_obs(cutoff + 5 * HOUR, cutoff + 5 * HOUR), _obs(cutoff + 5 * HOUR, cutoff + 5 * HOUR)]  # future duplicate
    )

    # Sanity check that the naive (non-as-of-aware) count WOULD change --
    # proving this is a real regression fixture, not a vacuous one.
    assert len(historical) != len(historical + extreme_future)

    r_historical_only = eq.assess_evidence_quality(historical, information_cutoff=cutoff, min_observations=5, gap_threshold_ms=90 * 60 * 1000)
    r_with_future = eq.assess_evidence_quality(historical + extreme_future, information_cutoff=cutoff, min_observations=5, gap_threshold_ms=90 * 60 * 1000)

    for dimension in ("SAMPLE_DEPTH", "CADENCE", "DUPLICATE_QUALITY", "PROVENANCE"):
        assert r_historical_only[dimension] == r_with_future[dimension], dimension
    assert r_historical_only["OVERALL_STATUS"] == r_with_future["OVERALL_STATUS"]
    # AS_OF_SAFETY's own eligible count (the historically-usable count)
    # is likewise unaffected -- only its ineligible/total counters grow.
    assert r_historical_only["AS_OF_SAFETY"]["n_eligible"] == r_with_future["AS_OF_SAFETY"]["n_eligible"] == 5


# =====================================================================
# Overall-status precedence, and the vocabulary invariants themselves
# =====================================================================

def test_overall_status_sufficient_when_everything_passes():
    obs = [_obs(i * HOUR, i * HOUR, source_key="alpha") for i in range(5)]
    result = eq.assess_evidence_quality(obs, information_cutoff=10 * HOUR, min_observations=3)
    assert result["OVERALL_STATUS"] == "SUFFICIENT"


def test_overall_status_limited_on_cadence_warning_only():
    times = [0, HOUR, 2 * HOUR, 3 * HOUR, 4 * HOUR, 5 * HOUR, 6 * HOUR, 7 * HOUR, 8 * HOUR, 9 * HOUR, 30 * HOUR]
    obs = [_obs(t, t) for t in times]
    result = eq.assess_evidence_quality(obs, information_cutoff=40 * HOUR, gap_threshold_ms=2 * HOUR)
    assert result["CADENCE"]["status"] == "WARNING"
    assert result["OVERALL_STATUS"] == "LIMITED"


def test_overall_status_insufficient_when_sample_depth_fails_alone():
    obs = [_obs(1, 1)]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000, min_observations=10)
    assert result["SAMPLE_DEPTH"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["OVERALL_STATUS"] == "INSUFFICIENT"


def test_no_numeric_score_anywhere_in_the_contract():
    """This layer must never emit a numeric quality score or a
    weighted/confidence figure -- only the fixed categorical
    vocabularies (Section 2)."""
    obs = [_obs(1, 1, source_key="alpha"), _obs(2, 2, source_key="alpha")]
    result = eq.assess_evidence_quality(obs, information_cutoff=1000, min_observations=1, gap_threshold_ms=10)
    for dimension in ("AS_OF_SAFETY", "TIMESTAMP_VALIDITY", "SAMPLE_DEPTH", "CADENCE", "DUPLICATE_QUALITY", "PROVENANCE", "WINDOW_CONFORMANCE"):
        assert "score" not in result[dimension]
        assert "confidence" not in result[dimension]
    assert isinstance(result["OVERALL_STATUS"], str)
    assert result["OVERALL_STATUS"] in eq.OVERALL_STATUSES


def test_vocabulary_constants_match_the_contract_exactly():
    assert eq.AS_OF_SAFETY_STATUSES == ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
    assert eq.SAMPLE_DEPTH_STATUSES == ("PASS", "INSUFFICIENT_EVIDENCE")
    assert eq.CADENCE_STATUSES == ("PASS", "WARNING", "FAIL", "INSUFFICIENT_EVIDENCE")
    assert eq.PROVENANCE_STATUSES == ("VERIFIED", "PARTIAL", "UNKNOWN")
    assert eq.OVERALL_STATUSES == ("SUFFICIENT", "LIMITED", "INSUFFICIENT", "INVALID")
