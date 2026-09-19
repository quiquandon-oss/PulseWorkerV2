"""
PR5b: empirical BTC movement-distribution descriptive statistics.

Pure functions -- no database access, no network, no LLM. Operates only
on already-resolved outcome rows produced by outcome_engine.compute_forward_returns()
(or any list of dicts shaped like its RESOLVED rows). This separation
(DB-bound query module vs. pure-function statistics module) mirrors the
project's existing pattern of keeping the bounded/indexed data-access
concern and the analytical concern in different, independently testable
places.

This stage is DESCRIPTIVE ONLY, per PR5b Section 4/5/6:
  - No statistical-significance claim is computed or implied anywhere
    in this module.
  - No threshold is optimized.
  - The bucket taxonomy this module can propose (`propose_movement_buckets`)
    is always returned labeled status="PROPOSED" and is never written
    anywhere as a frozen analysis definition. Freezing (if it ever
    happens) is an explicit, separate, human-reviewed step outside this
    module's scope entirely.

Percentile methodology (documented so results are reproducible and
auditable, not because it is the only valid choice): nearest-rank on
the sorted series, rank = round(p * (n - 1)) + 1 (1-indexed), i.e. the
same nearest-rank convention used in the PR5b real-data SQL run against
production D1 (see PR description). This is a simple, deterministic,
descriptive convention -- not a claim of a "correct" percentile
estimator.
"""

import math

# Mirrors research/event_detector.py's own frozen constant. Duplicated
# (not imported) deliberately: this module must not create a runtime
# dependency from the purely descriptive PR5b layer onto PR3's event
# detector. test_movement_distribution.py asserts this value stays
# equal to event_detector.LARGE_MOVE_THRESHOLD_PCT so the two can never
# silently drift apart.
PR3_LARGE_MOVE_THRESHOLD_PCT = 4.0

# Below this sample size, percentile/bucket output is still computed
# but flagged -- per PR5b Section 3 ("do not assume every horizon has
# sufficient observations; report actual sample size for every
# horizon"). This is a reporting flag, not a statistical claim.
MIN_SAMPLE_FOR_RELIABLE_PERCENTILES = 30

BUCKET_BOUNDS_PCT = (1.0, 2.0, 3.0, 4.0)  # produces 5 buckets: <1,1-2,2-3,3-4,>=4
BUCKET_LABELS = ("<1%", "1-2%", "2-3%", "3-4%", ">=4%")


def _percentile(sorted_values, p):
    n = len(sorted_values)
    if n == 0:
        return None
    rank = round(p * (n - 1))
    rank = max(0, min(n - 1, rank))
    return sorted_values[rank]


def summarize_absolute_returns(outcome_rows):
    """Descriptive statistics over a list of outcome_engine-shaped rows.

    Only rows with outcome_status == "RESOLVED" are included in the
    statistics; unresolved rows are counted and reported separately
    (never silently dropped, never treated as zero).

    Returns a dict with:
      n, n_unresolved_excluded, mean, median, stddev, min, max
        (all of the signed forward_return_pct),
      abs_p50, abs_p75, abs_p90, abs_p95, abs_p99
        (percentiles of |forward_return_pct|),
      bucket_counts (dict keyed by BUCKET_LABELS, counts of |return|),
      bucket_pct_of_n (same keys, share of n, or None if n == 0),
      count_positive, count_negative, count_flat,
      proportion_below_pr3_threshold (or None if n == 0),
      sample_size_status: "INSUFFICIENT_DATA" if n == 0,
        "SMALL_SAMPLE_CAUTION" if 0 < n < MIN_SAMPLE_FOR_RELIABLE_PERCENTILES,
        else "OK".

    Never raises on empty input -- returns an explicit INSUFFICIENT_DATA
    result instead (PR5b Section 7: "report INSUFFICIENT_DATA rather
    than fabricating or extrapolating").
    """
    resolved = [r for r in outcome_rows if r.get("outcome_status") == "RESOLVED"]
    n_unresolved = sum(1 for r in outcome_rows if r.get("outcome_status") != "RESOLVED")
    n = len(resolved)

    if n == 0:
        return {
            "n": 0,
            "n_unresolved_excluded": n_unresolved,
            "mean": None, "median": None, "stddev": None, "min": None, "max": None,
            "abs_p50": None, "abs_p75": None, "abs_p90": None, "abs_p95": None, "abs_p99": None,
            "bucket_counts": {label: 0 for label in BUCKET_LABELS},
            "bucket_pct_of_n": {label: None for label in BUCKET_LABELS},
            "count_positive": 0, "count_negative": 0, "count_flat": 0,
            "proportion_below_pr3_threshold": None,
            "sample_size_status": "INSUFFICIENT_DATA",
        }

    signed = sorted(r["forward_return_pct"] for r in resolved)
    absolute = sorted(r["absolute_forward_return_pct"] for r in resolved)

    mean = sum(signed) / n
    variance = sum((x - mean) ** 2 for x in signed) / n
    stddev = math.sqrt(variance)
    median = _percentile(signed, 0.5)

    bucket_counts = {label: 0 for label in BUCKET_LABELS}
    for ar in absolute:
        if ar < BUCKET_BOUNDS_PCT[0]:
            bucket_counts[BUCKET_LABELS[0]] += 1
        elif ar < BUCKET_BOUNDS_PCT[1]:
            bucket_counts[BUCKET_LABELS[1]] += 1
        elif ar < BUCKET_BOUNDS_PCT[2]:
            bucket_counts[BUCKET_LABELS[2]] += 1
        elif ar < BUCKET_BOUNDS_PCT[3]:
            bucket_counts[BUCKET_LABELS[3]] += 1
        else:
            bucket_counts[BUCKET_LABELS[4]] += 1
    bucket_pct_of_n = {label: (count / n) * 100.0 for label, count in bucket_counts.items()}

    count_positive = sum(1 for x in signed if x > 0)
    count_negative = sum(1 for x in signed if x < 0)
    count_flat = sum(1 for x in signed if x == 0)

    count_below_threshold = sum(1 for ar in absolute if ar < PR3_LARGE_MOVE_THRESHOLD_PCT)

    if n < MIN_SAMPLE_FOR_RELIABLE_PERCENTILES:
        sample_size_status = "SMALL_SAMPLE_CAUTION"
    else:
        sample_size_status = "OK"

    return {
        "n": n,
        "n_unresolved_excluded": n_unresolved,
        "mean": mean,
        "median": median,
        "stddev": stddev,
        "min": signed[0],
        "max": signed[-1],
        "abs_p50": _percentile(absolute, 0.50),
        "abs_p75": _percentile(absolute, 0.75),
        "abs_p90": _percentile(absolute, 0.90),
        "abs_p95": _percentile(absolute, 0.95),
        "abs_p99": _percentile(absolute, 0.99),
        "bucket_counts": bucket_counts,
        "bucket_pct_of_n": bucket_pct_of_n,
        "count_positive": count_positive,
        "count_negative": count_negative,
        "count_flat": count_flat,
        "proportion_below_pr3_threshold": count_below_threshold / n,
        "sample_size_status": sample_size_status,
    }


def propose_movement_buckets(summary_by_horizon):
    """Builds a candidate (NEVER frozen) movement-bucket taxonomy from
    already-computed per-horizon summaries (the dict returned by
    summarize_absolute_returns(), keyed by horizon_hours).

    Per PR5b Section 5, the result always carries status="PROPOSED" and
    must not be interpreted or persisted as a frozen research
    definition. Freezing requires a separate, explicit, human-reviewed
    step outside this module.

    The proposal reuses BUCKET_BOUNDS_PCT/BUCKET_LABELS (the same
    <1/1-2/2-3/3-4/>=4 boundaries already used for the descriptive pass)
    because the real production distribution (see PR description) does
    not show a natural alternative break: at every horizon the vast
    majority of mass sits under 1%, and the PR3 4% boundary already
    sits at a meaningful, explicitly-requested reference point. This
    function does not invent new boundaries out of the observed
    percentiles; it reports, per horizon, whether each of the EXISTING
    illustrative boundaries is adequately populated and interpretable,
    which is what PR5b Section 5 actually asks for ("propose a
    candidate taxonomy... explain sample count per bucket... whether
    each bucket has enough observations").
    """
    per_horizon = {}
    for horizon_hours, summary in summary_by_horizon.items():
        if summary["n"] == 0:
            per_horizon[horizon_hours] = {"status": "INSUFFICIENT_DATA"}
            continue
        bucket_report = {}
        for label in BUCKET_LABELS:
            count = summary["bucket_counts"][label]
            bucket_report[label] = {
                "count": count,
                "pct_of_n": summary["bucket_pct_of_n"][label],
                "adequately_populated": count >= MIN_SAMPLE_FOR_RELIABLE_PERCENTILES,
            }
        per_horizon[horizon_hours] = {
            "n": summary["n"],
            "buckets": bucket_report,
            "pr3_boundary_represented": True,  # the >=4% bucket IS the PR3 boundary
        }

    return {
        "status": "PROPOSED",
        "boundaries_pct": BUCKET_BOUNDS_PCT,
        "labels": BUCKET_LABELS,
        "per_horizon": per_horizon,
        "note": (
            "Candidate taxonomy only. Not frozen by this implementation. "
            "Freezing requires a separate, explicit, human-reviewed step."
        ),
    }
