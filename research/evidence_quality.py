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
RAW INPUT DIAGNOSTICS vs. HISTORICAL ELIGIBLE-POPULATION VALIDITY
(corrective fix, post-audit)
=====================================================================

An independent adversarial audit of this module found a real blocker:
`TIMESTAMP_VALIDITY` was (by design) computed over the FULL raw
supplied list, for honest disclosure of how much of the caller's input
was malformed -- but the OVERALL_STATUS rollup used that SAME raw-list
status in its hard-fail branch. The result: a single malformed
observation that had nothing to do with the requested historical
assessment at all (e.g. a garbage `information_available_at` on an
observation whose own `observation_time` was already in the future)
could flip an otherwise perfectly clean historical `SUFFICIENT`
verdict to `INVALID`, even though every eligible-population-scoped
dimension (`SAMPLE_DEPTH`/`CADENCE`/`DUPLICATE_QUALITY`/`PROVENANCE`)
was completely unaffected. This violated this module's own central
promise: the layer itself must never let future/irrelevant information
influence a historical verdict.

The fix keeps BOTH signals, deliberately separate and both visible in
the returned report:

- `TIMESTAMP_VALIDITY` -- a RAW INPUT DIAGNOSTIC. Unchanged in
  behavior. Inspects every supplied observation regardless of
  relevance. Useful for "how messy was what I was handed," never fed
  into OVERALL_STATUS.
- `HISTORICAL_TIMESTAMP_VALIDITY` -- the HISTORICAL ELIGIBLE-POPULATION
  VALIDITY signal that actually feeds OVERALL_STATUS. Scopes the same
  per-observation validity check to only those observations whose OWN
  `observation_time` plausibly places them inside the requested
  historical scope (`_plausibly_historical()`) -- judged using
  `observation_time` alone, deliberately never `information_available_
  at`, so this placement judgement still works even when `information_
  available_at` itself is the malformed field. A malformed observation
  that genuinely falls inside the historical scope still fails closed,
  exactly as before -- this fix narrows WHICH rows can trigger a hard
  failure, it never weakens what happens once a row is confirmed
  historical.

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
# HISTORICAL_TIMESTAMP_VALIDITY_STATUSES: the eligible-population-scoped
# sibling of TIMESTAMP_VALIDITY_STATUSES -- see _assess_historical_
# timestamp_validity()'s own docstring for why a second, differently-
# scoped validity dimension exists.
HISTORICAL_TIMESTAMP_VALIDITY_STATUSES = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
SAMPLE_DEPTH_STATUSES = ("PASS", "INSUFFICIENT_EVIDENCE")
CADENCE_STATUSES = ("PASS", "WARNING", "FAIL", "INSUFFICIENT_EVIDENCE")
DUPLICATE_STATUSES = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
PROVENANCE_STATUSES = ("VERIFIED", "PARTIAL", "UNKNOWN")
# FAIL is deliberately NOT part of this vocabulary (audit finding,
# corrective fix): _assess_window_conformance() can only ever report
# that at least one observation falls inside the window (PASS) or that
# none do (INSUFFICIENT_EVIDENCE) -- there is no code path that
# produces a hard FAIL for this dimension, so declaring FAIL here would
# be dead, misleading vocabulary.
WINDOW_CONFORMANCE_STATUSES = ("PASS", "INSUFFICIENT_EVIDENCE")
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


def _safe_get(obs, field):
    """obs.get(field), but never raises when `obs` itself is not a
    dict (corrective fix, audit finding: a stray string/None/list/int
    entry in the observations list previously raised an unhandled
    AttributeError). A non-dict entry is treated as an observation with
    every field missing -- it is never silently discarded from the
    population counts (n_total/n_invalid etc. still count it), it is
    just diagnosed as malformed through the SAME missing-field paths
    every dimension already has, rather than crashing the whole
    assessment. This never fabricates a value: a missing field stays
    None, exactly as if the caller had passed `{}`."""
    if not isinstance(obs, dict):
        return None
    return obs.get(field)


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
        if _as_of_eligible(_safe_get(obs, "information_available_at"), information_cutoff)
        and _within_window(_safe_get(obs, "observation_time"), window_start, window_end)
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


def _plausibly_historical(observation_time, information_cutoff, window_start, window_end):
    """Corrective-fix primitive (audit blocker): decides whether an
    observation's own DESCRIBED time places it inside the scope of the
    requested historical assessment, using ONLY observation_time --
    deliberately never information_available_at, since the whole point
    is to make this determination even when information_available_at
    itself is malformed.

    This is NOT the same test as _within_window(): _within_window()
    returns True when no window is supplied at all (nothing to
    enforce), which is the right behavior for WINDOW_CONFORMANCE
    itself, but wrong here -- absent an explicit window, "historical"
    still means at-or-before the cutoff, so this falls back to
    observation_time <= information_cutoff (the same as-of notion
    AS_OF_SAFETY already applies to information_available_at, applied
    here to observation_time instead) rather than treating every
    observation as automatically in-scope.

    A malformed/missing observation_time cannot be placed in time at
    all and is therefore conservatively treated as NOT plausibly
    historical -- an observation this function cannot show belongs to
    the historical population must never be assumed to belong to it
    (see _assess_historical_timestamp_validity()'s own docstring for
    why this direction of fail-closed, not the other, is correct
    here)."""
    if not _is_valid_timestamp(observation_time):
        return False
    if window_start is not None or window_end is not None:
        return _within_window(observation_time, window_start, window_end)
    if not _is_valid_timestamp(information_cutoff):
        return False
    return observation_time <= information_cutoff


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
        if _as_of_eligible(_safe_get(obs, "information_available_at"), information_cutoff):
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
# B. TIMESTAMP_VALIDITY -- RAW INPUT DIAGNOSTIC, not the OVERALL_STATUS
# signal (see _assess_historical_timestamp_validity() immediately below
# for the eligible-population-scoped sibling that actually feeds the
# rollup -- corrective fix, audit blocker)
# =====================================================================

def _assess_timestamp_validity(observations):
    """Diagnoses the FULL raw supplied list, exactly as before this
    corrective fix -- this is intentionally NOT scoped to the historical
    population, because its whole purpose is to disclose how much of
    whatever the caller passed in was malformed, including rows that
    have nothing to do with the requested historical assessment at all.
    Precisely BECAUSE it is raw-list-scoped, this dimension's status
    must never by itself be allowed to flip OVERALL_STATUS -- see
    _assess_historical_timestamp_validity()."""
    if not observations:
        return {"status": "INSUFFICIENT_EVIDENCE", "reason": "no observations supplied",
                "n_total": 0, "n_valid": 0, "n_invalid": 0}

    n_valid = 0
    n_invalid = 0
    for obs in observations:
        if _is_valid_timestamp(_safe_get(obs, "information_available_at")) and _is_valid_timestamp(_safe_get(obs, "observation_time")):
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
# B2. HISTORICAL_TIMESTAMP_VALIDITY -- corrective fix (audit blocker):
# the eligible-population-scoped validity signal that actually feeds
# OVERALL_STATUS. See module docstring's "RAW INPUT DIAGNOSTICS vs.
# HISTORICAL ELIGIBLE-POPULATION VALIDITY" section.
# =====================================================================

def _assess_historical_timestamp_validity(observations, information_cutoff, window_start, window_end):
    """Scopes the SAME per-observation validity check TIMESTAMP_
    VALIDITY performs to only those observations _plausibly_historical()
    finds plausibly part of the requested historical population (judged
    from observation_time alone, which -- unlike information_available_
    at -- this function does not require to already be malformed-free
    just to make that placement judgement).

    This is the fix for the audited blocker: a malformed observation
    whose OWN observation_time places it outside the historical scope
    (a future/out-of-window row, however malformed its other fields)
    must never be able to invalidate an otherwise-clean historical
    assessment -- it was never going to be used by any real historical
    calculation regardless of the layer's own diagnostics. A malformed
    observation that DOES fall inside the historical scope still fails
    closed exactly as TIMESTAMP_VALIDITY always has -- this fix
    narrows WHICH rows can trigger a hard failure, it does not weaken
    what happens once a row is confirmed to be historical.

    An observation whose own observation_time cannot be placed in time
    at all (missing/malformed) is conservatively excluded from
    "historical" here (see _plausibly_historical()'s own docstring) --
    it already cannot enter `eligible` either (WINDOW_CONFORMANCE's
    _within_window() fails closed on it the same way), so excluding it
    here keeps this dimension consistent with what the rest of the
    layer already does with such a row."""
    historical = [
        obs for obs in observations
        if _plausibly_historical(_safe_get(obs, "observation_time"), information_cutoff, window_start, window_end)
    ]
    if not historical:
        return {"status": "INSUFFICIENT_EVIDENCE",
                "reason": "no observation is plausibly part of the requested historical population",
                "n_historical": 0, "n_valid": 0, "n_invalid": 0}

    n_valid = 0
    n_invalid = 0
    for obs in historical:
        if _is_valid_timestamp(_safe_get(obs, "information_available_at")) and _is_valid_timestamp(_safe_get(obs, "observation_time")):
            n_valid += 1
        else:
            n_invalid += 1

    if n_invalid > 0:
        return {"status": "FAIL",
                "reason": f"{n_invalid} of {len(historical)} observations plausibly within the historical "
                          "population have a missing/malformed timestamp",
                "n_historical": len(historical), "n_valid": n_valid, "n_invalid": n_invalid}
    return {"status": "PASS", "reason": "all observations within the historical population have valid timestamps",
            "n_historical": len(historical), "n_valid": n_valid, "n_invalid": 0}


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
    """Corrective fix (audit finding, MEDIUM): gaps are computed from
    UNIQUE observation_time values only. A duplicate observation_time
    sorts adjacent to its twin and previously produced an artificial
    0ms gap, which could materially skew median_gap_ms (confirmed in
    audit: an 8h historical gap was reported as 4h once a single
    duplicate was added) even though DUPLICATE_QUALITY already
    correctly FAILs for the same population. Deduplicating here does
    NOT make duplicate quality pass -- DUPLICATE_QUALITY is computed
    completely independently (see _assess_duplicates()) and this
    function's own `duplicate_timestamps_excluded` field discloses
    exactly how many raw valid timestamps were collapsed into unique
    ones, so a reader of CADENCE alone still sees that something was
    excluded rather than a silently-clean-looking series. CADENCE's
    own gap numbers remain diagnostic only when DUPLICATE_QUALITY has
    FAILed for the same population -- read them together, never
    CADENCE alone, whenever duplicate_timestamps_excluded > 0."""
    valid_times = [
        _safe_get(obs, "observation_time") for obs in observations
        if _is_valid_timestamp(_safe_get(obs, "observation_time"))
    ]
    unique_times = sorted(set(valid_times))
    n_unique = len(unique_times)
    diagnostic = {
        "observation_count": len(valid_times),
        "n_unique_observation_times": n_unique,
        "duplicate_timestamps_excluded": len(valid_times) - n_unique,
        "first_observation": unique_times[0] if n_unique else None,
        "last_observation": unique_times[-1] if n_unique else None,
        "expected_cadence_ms": expected_cadence_ms,
        "gap_threshold_ms": gap_threshold_ms,
        "largest_gap_ms": None,
        "median_gap_ms": None,
        "n_gaps_exceeding_threshold": None,
    }
    if n_unique < 2:
        diagnostic["status"] = "INSUFFICIENT_EVIDENCE"
        diagnostic["reason"] = "fewer than 2 unique valid observation_time values -- no gap can be computed"
        return diagnostic

    gaps = [b - a for a, b in zip(unique_times, unique_times[1:])]
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
        t = _safe_get(obs, "observation_time")
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
            if _safe_get(obs, field) is not None:
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
        if _within_window(_safe_get(obs, "observation_time"), window_start, window_end):
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

def _overall_status(as_of, historical_timestamp_validity, sample_depth, cadence, duplicates, window_conformance):
    """Deterministic precedence, never a weighted/numeric combination:

    1. Any hard FAIL among AS_OF_SAFETY / HISTORICAL_TIMESTAMP_VALIDITY
       / DUPLICATE_QUALITY / CADENCE -> INVALID. These describe the
       HISTORICAL/ELIGIBLE evidence population itself being wrong/
       corrupted/out-of-bounds, not merely thin.
    2. Any INSUFFICIENT_EVIDENCE among AS_OF_SAFETY / HISTORICAL_
       TIMESTAMP_VALIDITY / SAMPLE_DEPTH / DUPLICATE_QUALITY /
       WINDOW_CONFORMANCE -> INSUFFICIENT. (CADENCE's own INSUFFICIENT_
       EVIDENCE, e.g. from fewer than 2 points, is already implied by
       an insufficient sample elsewhere and does not need its own
       branch here.)
    3. CADENCE == WARNING (and nothing above fired) -> LIMITED.
    4. Otherwise -> SUFFICIENT.

    Corrective fix (audit blocker): this rollup uses HISTORICAL_
    TIMESTAMP_VALIDITY, the eligible-population-scoped sibling of
    TIMESTAMP_VALIDITY -- NOT the raw-list-scoped TIMESTAMP_VALIDITY
    itself. TIMESTAMP_VALIDITY remains a genuinely useful diagnostic
    (how much of what the caller handed in was malformed, regardless
    of relevance), but it must never by itself flip this rollup: a
    malformed observation that plays no part in the requested
    historical population (e.g. a garbage future row) can never make
    an otherwise-clean historical assessment INVALID. WINDOW_
    CONFORMANCE is also deliberately excluded from the hard-FAIL set
    (it has no FAIL state in its own vocabulary -- see
    _assess_window_conformance()) but remains in the INSUFFICIENT set.

    PROVENANCE is intentionally excluded from this rollup entirely: an
    UNKNOWN or PARTIAL provenance describes AUDITABILITY of the
    evidence, not whether the evidence itself is temporally/
    statistically valid -- folding it in would conflate two different
    questions (Section 18).
    """
    hard_fail_statuses = (as_of["status"], historical_timestamp_validity["status"],
                          duplicates["status"], cadence["status"])
    if "FAIL" in hard_fail_statuses:
        return "INVALID"

    insufficient_statuses = (as_of["status"], historical_timestamp_validity["status"], sample_depth["status"],
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
      {"AS_OF_SAFETY", "TIMESTAMP_VALIDITY", "HISTORICAL_TIMESTAMP_
       VALIDITY", "SAMPLE_DEPTH", "CADENCE", "DUPLICATE_QUALITY",
       "PROVENANCE", "WINDOW_CONFORMANCE", "OVERALL_STATUS",
       "n_observations_supplied"}
    each dimension (except OVERALL_STATUS/n_observations_supplied) is
    itself a dict with at least {"status", "reason", ...diagnostic
    detail}.

    TIMESTAMP_VALIDITY is a RAW INPUT DIAGNOSTIC -- it inspects every
    supplied observation, whether or not it has anything to do with the
    requested historical assessment, and exists purely so a caller can
    see how much of what they handed in was malformed. HISTORICAL_
    TIMESTAMP_VALIDITY is the HISTORICAL ELIGIBLE-POPULATION VALIDITY
    signal that actually feeds OVERALL_STATUS -- see _assess_
    historical_timestamp_validity()'s own docstring. Never conflate the
    two: a caller reading only TIMESTAMP_VALIDITY could see FAIL while
    OVERALL_STATUS is still SUFFICIENT, correctly, because the
    malformed rows TIMESTAMP_VALIDITY found were never part of the
    historical population at all."""
    observations = list(observations or [])
    eligible = _eligible_population(observations, information_cutoff, window_start, window_end)

    as_of = _assess_as_of_safety(observations, information_cutoff)
    timestamp_validity = _assess_timestamp_validity(observations)
    historical_timestamp_validity = _assess_historical_timestamp_validity(
        observations, information_cutoff, window_start, window_end)
    window_conformance = _assess_window_conformance(observations, window_start, window_end)

    # Sample depth / cadence / duplicates / provenance describe the
    # population a historical calculation would actually be allowed to
    # use -- see _eligible_population()'s own docstring.
    sample_depth = _assess_sample_depth(eligible, min_observations)
    cadence = _assess_cadence(eligible, expected_cadence_ms, gap_threshold_ms)
    duplicates = _assess_duplicates(eligible)
    provenance = _assess_provenance(eligible)

    overall = _overall_status(as_of, historical_timestamp_validity, sample_depth, cadence, duplicates, window_conformance)

    return {
        "n_observations_supplied": len(observations),
        "AS_OF_SAFETY": as_of,
        "TIMESTAMP_VALIDITY": timestamp_validity,
        "HISTORICAL_TIMESTAMP_VALIDITY": historical_timestamp_validity,
        "SAMPLE_DEPTH": sample_depth,
        "CADENCE": cadence,
        "DUPLICATE_QUALITY": duplicates,
        "PROVENANCE": provenance,
        "WINDOW_CONFORMANCE": window_conformance,
        "OVERALL_STATUS": overall,
    }
