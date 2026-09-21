"""
Research Evidence Quality Layer -- a generic, deterministic, provider-
agnostic RESEARCH QUALITY CONTROL mechanism for the CryptoPulseV2
research system.

=====================================================================
What this module is, and is not
=====================================================================

This module answers exactly one kind of question:

    "Is the evidence population sufficiently valid and auditable for
    this particular research calculation?"

It NEVER answers:

    "Is source X good?"

It contains no source ranking, no source score, no weighted evidence-
quality score, no confidence percentage, no numeric quality metric of
any kind -- only deterministic, categorical statuses (PASS/FAIL/
INSUFFICIENT_EVIDENCE/WARNING/VERIFIED/PARTIAL/UNKNOWN and the overall
SUFFICIENT/LIMITED/INSUFFICIENT/INVALID), each carrying enough plain
diagnostic detail (counts, timestamps, thresholds) that a caller or a
human auditor can see exactly WHY a result was produced.

It does not change V1 production weights, V2 prediction logic,
selection logic, coefficients, or production scoring. It does not
generate source rankings or scores, does not recommend a weight
change, does not automatically block a production prediction, does
not automatically modify production data, and calls no LLM/paid API.
It is pure computation: no database, no network, no environment
dependency -- exactly the same $0/pure-function discipline every other
research/ module in this project already follows.

=====================================================================
Reused, unchanged -- and one deliberate non-reuse, explained
=====================================================================

`event_source_relevance.py` (PR59) reproduces PR3's own event
detection rather than importing `event_source_reaction.py`, explicitly
"so that dependency cannot even accidentally leak in" (see that
module's own docstring). This module follows the same discipline for
the same reason: `source_dialogue.py`'s `is_eligible()` / `_within_
window()` / `_index_by_observation_time()` / `_is_valid_timestamp()`
implement closely related ideas, but they are private implementation
details of the LOCKED, independently-audited Source Dialogue contract
(PR #68) and are scoped to that contract's own two-question shape
(relationship + redundancy). This module must never depend on, or be
capable of accidentally destabilizing, that locked contract (per this
layer's own build authorization, Section 12: "it must not alter the
locked Source Dialogue classifications... If integration would require
modifying the locked contract, STOP"). This module therefore
implements its own small, independently-tested timestamp/window/
duplicate primitives below, matching the SAME semantics already
established and tested elsewhere in this project (inclusive `<=`
eligibility, inclusive `[window_start, window_end]`, fail-closed on
malformed/missing/boolean timestamps, duplicate observation_time is a
reported failure, never a silent overwrite) -- not a new or different
rule, just a decoupled copy of an already-correct one.

`evidence_temporal.is_predictive_eligible()` was inspected and NOT
reused: it encodes a different, narrower question (publication_ts
STRICTLY BEFORE a single prediction_ts) than this layer's required
INCLUSIVE `information_available_at <= information_cutoff` semantics.

No existing minimum-sample constant in this project (`source_analysis.
MIN_SAMPLE_PER_REGIME`/`MIN_SAMPLE_FOR_LEVEL2`/etc.) legitimately
answers "how many observations make an arbitrary evidence population
auditable" -- each existing constant is scoped to its own caller's own
statistical question. This module therefore never invents a universal
threshold; SAMPLE_DEPTH always requires the CALLER to supply
`min_observations` (Section 7 of the build authorization), and reports
the actual population honestly when no threshold is supplied.

=====================================================================
Two timestamp axes, kept separate throughout (per explicit instruction)
=====================================================================

1. `information_available_at` -- when this observation's information
   became knowable. Gates AS_OF_SAFETY against `information_cutoff`.
2. `observation_time` -- the period this observation DESCRIBES. Gates
   WINDOW_CONFORMANCE against `[window_start, window_end]`.

These are never assumed interchangeable. A caller whose dataset
genuinely has only one true timestamp (e.g. V1 sources, which have no
distinct publication-lag concept -- see `exp010_source_dialogue_
validation.py`'s own documented design decision) must set BOTH fields
to that same real timestamp explicitly, exactly as that module already
does; this layer never infers or defaults one from the other.

=====================================================================
Observation contract
=====================================================================

Each observation is a plain dict. Required:
  - `information_available_at`: numeric epoch-ms, or None/malformed
    (handled, never guessed).
  - `observation_time`: numeric epoch-ms, or None/malformed.
Optional (provenance -- Section 9; unknown fields are simply absent,
never invented):
  - `source_key`, `source_category`, `provider`, `dataset`, `field`,
    `evidence_reference`, `retrieved_at`.

=====================================================================
Where this is consumed
=====================================================================

Additively, from experiment ORCHESTRATION scripts only
(`exp005-source-effectiveness/run_experiment.py`,
`exp010-source-dialogue-validation/run_experiment.py`) -- never from
`source_analysis.py`, `source_dialogue.py`, `event_source_reaction.py`,
or `exp010_source_dialogue_validation.py` themselves, all of which
remain completely unchanged. Each integration point builds the plain
observation list it already has the underlying data for, calls
`assess_evidence_quality()`, and adds ONE new `evidence_quality` key
to the report it already persists -- never altering an existing
calculation, gate, or persisted field.
"""

import statistics

# =====================================================================
# Categorical vocabularies -- deterministic only, never numeric
# =====================================================================

AS_OF_SAFETY_STATUSES = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
TIMESTAMP_VALIDITY_STATUSES = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
SAMPLE_DEPTH_STATUSES = ("PASS", "INSUFFICIENT_EVIDENCE")
CADENCE_STATUSES = ("PASS", "WARNING", "FAIL", "INSUFFICIENT_EVIDENCE")
DUPLICATE_STATUSES = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
PROVENANCE_STATUSES = ("VERIFIED", "PARTIAL", "UNKNOWN")
WINDOW_CONFORMANCE_STATUSES = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
OVERALL_STATUSES = ("SUFFICIENT", "LIMITED", "INSUFFICIENT", "INVALID")

PROVENANCE_FIELDS = (
    "source_key", "source_category", "provider", "dataset", "field",
    "evidence_reference", "retrieved_at",
)


# =====================================================================
# Timestamp primitives -- see module docstring for why these are a
# deliberate, decoupled copy rather than an import from source_dialogue.py
# =====================================================================

def _is_valid_timestamp(value):
    """True only for a real numeric epoch-ms value. `bool` is
    deliberately excluded despite being an `int` subclass in Python
    (`isinstance(True, int) is True`) -- silently accepting True/False
    as 1/0 would be exactly the kind of silent coercion this layer must
    never perform. NaN is explicitly rejected (a float NaN passes
    `isinstance(x, float)` but must never be treated as a valid
    timestamp -- `x != x` is the standard NaN test, since NaN is the
    only value that never equals itself). Negative values are not
    rejected -- nothing in this contract restricts timestamps to
    non-negative epoch values."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if value != value:  # NaN
        return False
    return True


def _as_of_eligible(information_available_at, information_cutoff):
    """information_available_at <= information_cutoff, inclusive --
    matches this layer's own explicit as-of contract (Section 3: an
    observation exactly AT the cutoff is eligible). Malformed/missing
    values on either side degrade to ineligible, never raise."""
    if not _is_valid_timestamp(information_available_at) or not _is_valid_timestamp(information_cutoff):
        return False
    return information_available_at <= information_cutoff


def _eligible_population(observations, information_cutoff, window_start, window_end):
    """The population a historical research calculation would actually
    be allowed to use: as-of eligible (information_available_at <=
    information_cutoff) AND window-conforming (observation_time inside
    [window_start, window_end], when a window is supplied). SAMPLE_
    DEPTH / CADENCE / DUPLICATE_QUALITY / PROVENANCE are all computed
    against THIS population, never the raw supplied list -- otherwise
    a future or out-of-window observation could silently inflate a
    sample-depth count, mask a cadence gap, or hide a duplicate that
    would never actually have been used (Section 3/10: "future data
    must not enter the calculation"). AS_OF_SAFETY / TIMESTAMP_
    VALIDITY / WINDOW_CONFORMANCE themselves still inspect the FULL raw
    list below, since their entire purpose is to report how much of the
    raw population was excluded and why."""
    return [
        obs for obs in observations
        if _as_of_eligible(obs.get("information_available_at"), information_cutoff)
        and _within_window(obs.get("observation_time"), window_start, window_end)
    ]


def _within_window(observation_time, window_start, window_end):
    """window_start <= observation_time <= window_end, inclusive on
    both ends (Section 4). Returns True (nothing to enforce) when
    EITHER bound is omitted -- an explicitly-omitted window is never
    silently reinterpreted as a de-facto restriction. Malformed values
    for a bound that IS supplied, or for observation_time itself,
    degrade to "not within" (fails closed)."""
    if window_start is None and window_end is None:
        return True
    if not _is_valid_timestamp(observation_time):
        return False
    if window_start is not None:
        if not _is_valid_timestamp(window_start) or observation_time < window_start:
            return False
    if window_end is not None:
        if not _is_valid_timestamp(window_end) or observation_time > window_end:
            return False
    return True


# =====================================================================
# A. AS_OF_SAFETY
# =====================================================================

def _assess_as_of_safety(observations, information_cutoff):
    if not observations:
        return {"status": "INSUFFICIENT_EVIDENCE", "reason": "no observations supplied",
                "n_total": 0, "n_eligible": 0, "n_ineligible": 0}
    if not _is_valid_timestamp(information_cutoff):
        return {"status": "FAIL", "reason": "information_cutoff is missing or malformed",
                "n_total": len(observations), "n_eligible": 0, "n_ineligible": len(observations)}

    n_eligible = 0
    n_ineligible = 0
    for obs in observations:
        if _as_of_eligible(obs.get("information_available_at"), information_cutoff):
            n_eligible += 1
        else:
            n_ineligible += 1

    if n_eligible == 0:
        return {"status": "INSUFFICIENT_EVIDENCE",
                "reason": "no observation has information_available_at <= information_cutoff",
                "n_total": len(observations), "n_eligible": 0, "n_ineligible": n_ineligible}
    return {"status": "PASS", "reason": f"{n_eligible} of {len(observations)} observations are as-of eligible",
            "n_total": len(observations), "n_eligible": n_eligible, "n_ineligible": n_ineligible}


# =====================================================================
# B. TIMESTAMP_VALIDITY
# =====================================================================

def _assess_timestamp_validity(observations):
    if not observations:
        return {"status": "INSUFFICIENT_EVIDENCE", "reason": "no observations supplied",
                "n_total": 0, "n_valid": 0, "n_invalid": 0}

    n_valid = 0
    n_invalid = 0
    for obs in observations:
        if _is_valid_timestamp(obs.get("information_available_at")) and _is_valid_timestamp(obs.get("observation_time")):
            n_valid += 1
        else:
            n_invalid += 1

    if n_valid == 0:
        return {"status": "FAIL", "reason": "no observation has both a valid information_available_at and observation_time",
                "n_total": len(observations), "n_valid": 0, "n_invalid": n_invalid}
    if n_invalid > 0:
        return {"status": "FAIL", "reason": f"{n_invalid} of {len(observations)} observations have a missing/malformed timestamp",
                "n_total": len(observations), "n_valid": n_valid, "n_invalid": n_invalid}
    return {"status": "PASS", "reason": "all observations have valid numeric timestamps",
            "n_total": len(observations), "n_valid": n_valid, "n_invalid": 0}


# =====================================================================
# C. SAMPLE_DEPTH -- never a universal threshold (Section 7)
# =====================================================================

def _assess_sample_depth(observations, min_observations):
    actual = len(observations)
    if min_observations is None:
        return {"status": "PASS", "actual_observations": actual, "required_observations": None,
                "reason": "no min_observations threshold was supplied by the caller -- "
                          "reporting the actual population only, not asserting it is scientifically sufficient"}
    if actual >= min_observations:
        return {"status": "PASS", "actual_observations": actual, "required_observations": min_observations,
                "reason": f"{actual} >= required {min_observations}"}
    return {"status": "INSUFFICIENT_EVIDENCE", "actual_observations": actual, "required_observations": min_observations,
            "reason": f"{actual} < required {min_observations}"}


# =====================================================================
# D. CADENCE / GAP QUALITY -- diagnostic only, never auto-classified
# as bad without an explicit threshold (Section 8)
# =====================================================================

def _assess_cadence(observations, expected_cadence_ms, gap_threshold_ms):
    valid_times = sorted(
        obs["observation_time"] for obs in observations
        if _is_valid_timestamp(obs.get("observation_time"))
    )
    n = len(valid_times)
    diagnostic = {
        "observation_count": n,
        "first_observation": valid_times[0] if n else None,
        "last_observation": valid_times[-1] if n else None,
        "expected_cadence_ms": expected_cadence_ms,
        "gap_threshold_ms": gap_threshold_ms,
        "largest_gap_ms": None,
        "median_gap_ms": None,
        "n_gaps_exceeding_threshold": None,
    }
    if n < 2:
        diagnostic["status"] = "INSUFFICIENT_EVIDENCE"
        diagnostic["reason"] = "fewer than 2 valid observation_time values -- no gap can be computed"
        return diagnostic

    gaps = [b - a for a, b in zip(valid_times, valid_times[1:])]
    diagnostic["largest_gap_ms"] = max(gaps)
    diagnostic["median_gap_ms"] = statistics.median(gaps)

    if gap_threshold_ms is None:
        diagnostic["status"] = "PASS"
        diagnostic["reason"] = "no gap_threshold_ms was supplied -- gap sizes are reported as diagnostics only, not classified"
        return diagnostic

    n_exceeding = sum(1 for g in gaps if g > gap_threshold_ms)
    diagnostic["n_gaps_exceeding_threshold"] = n_exceeding
    if n_exceeding == 0:
        diagnostic["status"] = "PASS"
        diagnostic["reason"] = "no gap exceeds gap_threshold_ms"
    elif n_exceeding <= max(1, len(gaps) // 10):
        diagnostic["status"] = "WARNING"
        diagnostic["reason"] = f"{n_exceeding} of {len(gaps)} gaps exceed gap_threshold_ms -- a diagnostic gap is not automatically a research failure"
    else:
        diagnostic["status"] = "FAIL"
        diagnostic["reason"] = f"{n_exceeding} of {len(gaps)} gaps exceed gap_threshold_ms"
    return diagnostic


# =====================================================================
# E. DUPLICATE QUALITY -- protects against the exact class of bug
# previously found in Source Dialogue (a dict keyed by observation_time
# silently overwriting an observation, audit finding F4)
# =====================================================================

def _assess_duplicates(observations):
    if not observations:
        return {"status": "INSUFFICIENT_EVIDENCE", "reason": "no observations supplied",
                "n_total": 0, "n_duplicate_timestamps": 0, "duplicate_timestamps": []}

    seen = {}
    for obs in observations:
        t = obs.get("observation_time")
        if not _is_valid_timestamp(t):
            continue
        seen[t] = seen.get(t, 0) + 1

    duplicate_timestamps = sorted(t for t, count in seen.items() if count > 1)
    if duplicate_timestamps:
        return {"status": "FAIL",
                "reason": f"{len(duplicate_timestamps)} observation_time value(s) appear more than once -- "
                          "never silently overwritten/averaged/interpolated/arbitrarily chosen",
                "n_total": len(observations), "n_duplicate_timestamps": len(duplicate_timestamps),
                "duplicate_timestamps": duplicate_timestamps}
    return {"status": "PASS", "reason": "no duplicate observation_time values",
            "n_total": len(observations), "n_duplicate_timestamps": 0, "duplicate_timestamps": []}


# =====================================================================
# F. PROVENANCE -- never invented; unknown stays UNKNOWN (Section 9)
# =====================================================================

def _assess_provenance(observations):
    if not observations:
        return {"status": "UNKNOWN", "reason": "no observations supplied", "fields_present": [], "fields_missing": list(PROVENANCE_FIELDS)}

    present_counts = {field: 0 for field in PROVENANCE_FIELDS}
    for obs in observations:
        for field in PROVENANCE_FIELDS:
            if obs.get(field) is not None:
                present_counts[field] += 1

    n = len(observations)
    fully_present = [f for f, c in present_counts.items() if c == n]
    partially_present = [f for f, c in present_counts.items() if 0 < c < n]
    missing = [f for f, c in present_counts.items() if c == 0]

    # VERIFIED means: every field that appears AT ALL appears
    # consistently on every observation -- it does NOT require every
    # possible provenance field in PROVENANCE_FIELDS to be present (a
    # caller who never supplies e.g. `retrieved_at` at all is not
    # thereby downgraded; only a field present on SOME but not ALL
    # observations -- a genuine inconsistency -- downgrades to PARTIAL).
    if partially_present:
        status = "PARTIAL"
        reason = f"{len(fully_present)} field(s) fully present, {len(partially_present)} partially present, {len(missing)} never supplied"
    elif fully_present:
        status = "VERIFIED"
        reason = f"all supplied observations consistently carry the same {len(fully_present)} provenance field(s)"
    else:
        status = "UNKNOWN"
        reason = "no provenance fields were supplied on any observation"

    return {"status": status, "reason": reason,
            "fields_fully_present": fully_present, "fields_partially_present": partially_present,
            "fields_missing": missing}


# =====================================================================
# G. WINDOW_CONFORMANCE
# =====================================================================

def _assess_window_conformance(observations, window_start, window_end):
    if window_start is None and window_end is None:
        return {"status": "PASS", "reason": "no research window was supplied -- nothing to enforce",
                "n_total": len(observations), "n_within": len(observations), "n_outside": 0}
    if not observations:
        return {"status": "INSUFFICIENT_EVIDENCE", "reason": "no observations supplied",
                "n_total": 0, "n_within": 0, "n_outside": 0}

    n_within = 0
    n_outside = 0
    for obs in observations:
        if _within_window(obs.get("observation_time"), window_start, window_end):
            n_within += 1
        else:
            n_outside += 1

    if n_within == 0:
        return {"status": "INSUFFICIENT_EVIDENCE",
                "reason": "no observation's observation_time falls inside [window_start, window_end]",
                "n_total": len(observations), "n_within": 0, "n_outside": n_outside}
    return {"status": "PASS", "reason": f"{n_within} of {len(observations)} observations fall inside the window",
            "n_total": len(observations), "n_within": n_within, "n_outside": n_outside}


# =====================================================================
# H. OVERALL EVIDENCE STATUS -- deterministic categorical rollup only
# =====================================================================

def _overall_status(as_of, timestamp_validity, sample_depth, cadence, duplicates, window_conformance):
    """Deterministic precedence, never a weighted/numeric combination:

    1. Any hard FAIL among AS_OF_SAFETY / TIMESTAMP_VALIDITY /
       DUPLICATE_QUALITY / WINDOW_CONFORMANCE / CADENCE -> INVALID.
       These five all describe the evidence population itself being
       wrong/corrupted/out-of-bounds, not merely thin.
    2. Any INSUFFICIENT_EVIDENCE among AS_OF_SAFETY / TIMESTAMP_
       VALIDITY / SAMPLE_DEPTH / DUPLICATE_QUALITY / WINDOW_CONFORMANCE
       -> INSUFFICIENT. (CADENCE's own INSUFFICIENT_EVIDENCE, e.g. from
       fewer than 2 points, is already implied by an insufficient
       sample elsewhere and does not need its own branch here.)
    3. CADENCE == WARNING (and nothing above fired) -> LIMITED.
    4. Otherwise -> SUFFICIENT.

    PROVENANCE is intentionally excluded from this rollup: an UNKNOWN
    or PARTIAL provenance describes AUDITABILITY of the evidence, not
    whether the evidence itself is temporally/statistically valid --
    folding it in would conflate two different questions (Section 18).
    """
    hard_fail_statuses = (as_of["status"], timestamp_validity["status"], duplicates["status"],
                          window_conformance["status"], cadence["status"])
    if "FAIL" in hard_fail_statuses:
        return "INVALID"

    insufficient_statuses = (as_of["status"], timestamp_validity["status"], sample_depth["status"],
                             duplicates["status"], window_conformance["status"])
    if "INSUFFICIENT_EVIDENCE" in insufficient_statuses:
        return "INSUFFICIENT"

    if cadence["status"] == "WARNING":
        return "LIMITED"

    return "SUFFICIENT"


# =====================================================================
# Top-level entry point
# =====================================================================

def assess_evidence_quality(observations, information_cutoff,
                             window_start=None, window_end=None,
                             min_observations=None,
                             expected_cadence_ms=None, gap_threshold_ms=None):
    """The single entry point. Pure function: no I/O, no randomness,
    same input always produces a byte-identical result.

    `observations`: a plain list of dicts (see module docstring's
    "Observation contract"). Never mutated.
    `information_cutoff`: required -- the as-of boundary. A missing or
    malformed cutoff degrades AS_OF_SAFETY to FAIL (Section 3: "the
    quality layer itself must never introduce future information";
    without a real cutoff, eligibility cannot be determined at all, so
    this fails closed rather than defaulting to "everything eligible").
    `window_start`/`window_end`: optional research-window bounds
    (Section 4). Omitting BOTH means no window is enforced -- an
    omitted bound is never silently reinterpreted (Section 4).
    `min_observations`: optional caller-supplied sample-depth floor
    (Section 7) -- never invented by this module.
    `expected_cadence_ms`/`gap_threshold_ms`: optional cadence
    diagnostics (Section 8) -- gaps are always reported; they are only
    ever CLASSIFIED (WARNING/FAIL) when `gap_threshold_ms` is supplied.

    Returns a plain dict:
      {"AS_OF_SAFETY", "TIMESTAMP_VALIDITY", "SAMPLE_DEPTH", "CADENCE",
       "DUPLICATE_QUALITY", "PROVENANCE", "WINDOW_CONFORMANCE",
       "OVERALL_STATUS", "n_observations_supplied"}
    each dimension (except OVERALL_STATUS/n_observations_supplied) is
    itself a dict with at least {"status", "reason", ...diagnostic
    detail}."""
    observations = list(observations or [])
    eligible = _eligible_population(observations, information_cutoff, window_start, window_end)

    as_of = _assess_as_of_safety(observations, information_cutoff)
    timestamp_validity = _assess_timestamp_validity(observations)
    window_conformance = _assess_window_conformance(observations, window_start, window_end)

    # Sample depth / cadence / duplicates / provenance describe the
    # population a historical calculation would actually be allowed to
    # use -- see _eligible_population()'s own docstring.
    sample_depth = _assess_sample_depth(eligible, min_observations)
    cadence = _assess_cadence(eligible, expected_cadence_ms, gap_threshold_ms)
    duplicates = _assess_duplicates(eligible)
    provenance = _assess_provenance(eligible)

    overall = _overall_status(as_of, timestamp_validity, sample_depth, cadence, duplicates, window_conformance)

    return {
        "n_observations_supplied": len(observations),
        "AS_OF_SAFETY": as_of,
        "TIMESTAMP_VALIDITY": timestamp_validity,
        "SAMPLE_DEPTH": sample_depth,
        "CADENCE": cadence,
        "DUPLICATE_QUALITY": duplicates,
        "PROVENANCE": provenance,
        "WINDOW_CONFORMANCE": window_conformance,
        "OVERALL_STATUS": overall,
    }
