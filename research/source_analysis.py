"""
PR5c: V1 source effectiveness, redundancy, and incremental-information
analysis.

Objective (per PR5c build authorization): given the 21 raw integer V1
sentiment sources actually observed in `history.sources_json` (see
research/README.md's empirical verification, run directly against
production D1 `sentiment-history` before this module was written --
NOT assumed), determine for each source:

  LEVEL 1 -- does it contain usable signal/variation at all?
  LEVEL 2 -- is it associated with subsequent BTC outcome (correlation,
             NOT causation)?
  LEVEL 3 -- does it add INCREMENTAL information BEYOND THE V1 COMPOSITE
             specifically (out-of-sample)?

SCOPE CORRECTION (post-review): Level 3, as implemented in this PR, only
ever conditions on the V1 composite (history.score) -- via partial
correlation controlling for the composite, and via a nested nested-OLS
comparison of "composite alone" vs. "composite + source". It does NOT
condition on any other empirically correlated/redundant source (Section
9's pairwise_source_redundancy() runs entirely separately and its
output is never fed into Level 3's regression). Therefore PR5c's Level
3 result must be read strictly as "incremental beyond the V1 composite"
-- it does NOT establish, and must never be described as establishing,
"incremental beyond correlated/redundant sources". That broader
question (e.g. controlling for a whole cluster of mutually correlated
sources at once, or a multi-source ablation) is a separate, larger
research question, explicitly NOT addressed by this PR -- narrowing the
claim here rather than building that larger model now.

Read-only with respect to production tables (`history`, `btc_data`). The
only function in this module that writes anything is persist_analysis(),
which INSERTs into the already-deployed `research_analyses` table (per
PR5c Section 15) -- it is never invoked against a production connection
in this PR; see test_source_analysis.py and the PR description.

No coefficient optimization, no V1/V2 modification, no causal claim
anywhere in this module's output.

=====================================================================
Section 5 (source enumeration) -- data-derived, not hard-coded
=====================================================================

discover_sources() enumerates the actual top-level keys of
history.sources_json within the requested window via SQLite's json_each
table-valued function. It does NOT hard-code the 21 keys this project's
own empirical verification found in the current 500-row table --
running this module against a wider or narrower window, or after new
sources are added/removed upstream, discovers whatever keys are
actually present in THAT window. extract_source_matrix() then builds a
row per history.ts with an explicit {key: value-or-None} dict for every
discovered key -- a key absent from a given row's JSON is None, never 0
and never silently omitted (Section 5/6: "missing != zero").

=====================================================================
Section 6 (scale/normalization) -- empirically inspected, documented
=====================================================================

Empirical verification (production D1, see PR description) confirmed
every source value is a direct JSON integer scalar (no nested
{score,confidence} object), but did NOT confirm they share one common
scale (e.g. fng-style sources are commonly 0-100 while others may be
signed deltas). This module does not assume a shared scale and does not
normalize raw values before Pearson correlation -- correlation is scale-
invariant under any positive linear rescaling, so normalizing would not
change any reported r/p/CI (see research/stats_utils.py's own docstring
for the full argument). Raw values are always preserved for
auditability (source_coverage_report() reports each source's own
min/max/mean/stddev on its native scale) and any regression coefficient
this module reports (Level 3's nested-regression slopes) is reported
alongside that same native-scale range rather than presented as a
cross-source-comparable normalized number.

=====================================================================
Section 4 (no-lookahead) -- enforced by construction, not by convention
=====================================================================

Every source value used in Level 2/3 analysis is the value observed AT
history.ts (the anchor). The paired BTC outcome is always looked up via
outcome_engine.compute_forward_returns_from_history(), which resolves
strictly future btc_data observations (future_ts > anchor_ts) using the
same no-lookahead rule as PR5b's predictions-anchored engine. There is
no code path in this module that looks up a source value from a ts
later than the outcome's anchor. See test_source_analysis.py's explicit
no-lookahead test for a constructive proof, not just an assertion.

=====================================================================
Section 12/13 (evidence taxonomy / regime stability)
=====================================================================

classify_evidence() implements exactly the four required labels
(STATISTICALLY_SIGNIFICANT, STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE,
INCONCLUSIVE, CONTRADICTED) using BH-corrected p-values plus a
chronological (never shuffled) discovery/validation split for stability.
CONTRADICTED is reserved for a sign reversal where BOTH halves have an
adequate, independently-computed sample (>= MIN_SAMPLE_FOR_CONTRADICTED)
and a non-trivial effect size in both halves -- insufficient replication
alone always falls to INCONCLUSIVE (Section 12's explicit requirement).
regime_stability_for_source() reports regime-specific estimates without
ever gating or averaging away a sign reversal (Section 13).
"""

import json

from stats_utils import (
    pearson_correlation,
    fisher_z_ci_and_p,
    benjamini_hochberg,
    partial_correlation,
    ols_1var,
    ols_2var,
    rmse,
)

MAX_WINDOW_MS = 90 * 24 * 3600 * 1000  # same bound/rationale as every other
# research module in this project (resolver.py, outcome_engine.py, ...).

ALPHA = 0.05

# Below this per-test sample size, an association is still computed but
# flagged SMALL_SAMPLE_CAUTION rather than OK (Section 3/14: always
# report actual n, never hide a small sample behind a bare r/p).
MIN_SAMPLE_FOR_LEVEL2 = 30

# CONTRADICTED (Section 12) requires an "adequate independent sample" in
# BOTH the discovery and validation halves -- this is that threshold.
MIN_SAMPLE_FOR_CONTRADICTED = 30

# Minimum combined (discovery+validation) sample before Level 3's
# chronological OOS split is attempted at all; below this, both halves
# would be too small to mean anything (Section 3: "report INSUFFICIENT_
# DATA rather than fabricating").
MIN_SAMPLE_FOR_LEVEL3 = 40

# Chronological (NEVER shuffled -- Section 10) discovery/validation split.
OOS_SPLIT_FRACTION = 0.7

# |r| below this is treated as "no material effect" when checking
# whether a non-significant or reversed-sign result is "stable" /
# "contradicted" vs. merely noise (Section 12).
STABILITY_EPSILON = 0.05

# Section 9: pairwise/composite redundancy flag threshold. Flagging
# only -- Section 9 explicitly forbids auto-removing or combining
# sources on this basis alone.
STRONG_REDUNDANCY_THRESHOLD = 0.7

# Minimum n per regime group before a regime-specific r is reported
# (Section 13: report actual sample sizes, never suppress a small one,
# but also never present two points as if they defined a stable trend).
MIN_SAMPLE_PER_REGIME = 10


def _validate_bounds(start_ts, end_ts):
    if start_ts is None or end_ts is None:
        raise ValueError("start_ts and end_ts are required -- there is no unbounded mode")
    if start_ts >= end_ts:
        raise ValueError(f"start_ts ({start_ts}) must be strictly before end_ts ({end_ts})")
    window = end_ts - start_ts
    if window > MAX_WINDOW_MS:
        raise ValueError(f"requested window ({window}ms) exceeds MAX_WINDOW_MS ({MAX_WINDOW_MS}ms)")


# =====================================================================
# Section 5: data-derived source enumeration
# =====================================================================

DISCOVER_SOURCES_SQL = """
SELECT DISTINCT je.key AS source_key
FROM history h, json_each(h.sources_json) je
WHERE h.ts >= ? AND h.ts <= ?
  AND json_valid(h.sources_json) = 1
  AND json_type(h.sources_json) = 'object'
ORDER BY je.key
"""

HEADER_SQL = """
SELECT ts, score AS v1_composite, gold_regime
FROM history
WHERE ts >= ? AND ts <= ?
ORDER BY ts ASC
"""

SOURCE_VALUES_SQL = """
SELECT h.ts AS ts, je.key AS source_key, je.value AS source_value, json_type(je.value) AS value_type
FROM history h, json_each(h.sources_json) je
WHERE h.ts >= ? AND h.ts <= ?
  AND json_valid(h.sources_json) = 1
  AND json_type(h.sources_json) = 'object'
"""


def discover_sources(conn, start_ts, end_ts):
    """The actual set of history.sources_json top-level keys observed in
    [start_ts, end_ts], sorted. Never a hard-coded list (Section 5)."""
    _validate_bounds(start_ts, end_ts)
    cursor = conn.execute(DISCOVER_SOURCES_SQL, (start_ts, end_ts))
    return [row[0] for row in cursor.fetchall()]


def extract_source_matrix(conn, start_ts, end_ts):
    """Builds the per-row source matrix used by every downstream
    analysis in this module.

    Returns (source_keys, rows) where source_keys is discover_sources()'s
    output and rows is a list of dicts, one per `history` row in the
    window (ordered by ts ascending):
      {"ts": ..., "v1_composite": ..., "gold_regime": ...,
       "sources": {key: raw_value_or_None for key in source_keys}}

    A key missing from a given row's JSON, or a row whose sources_json
    is NULL/malformed/non-object, is None for every key -- never 0,
    never silently dropped from the row list (the row itself is always
    present; only its per-source values may be None). Non-numeric
    JSON-object values (should not occur per Section 6's empirical
    finding, but not assumed) are also reported as None with the
    anomaly counted in a separate "non_numeric_value_count" tally per
    source, surfaced by source_coverage_report().
    """
    _validate_bounds(start_ts, end_ts)
    source_keys = discover_sources(conn, start_ts, end_ts)

    header_cursor = conn.execute(HEADER_SQL, (start_ts, end_ts))
    header_columns = [d[0] for d in header_cursor.description]
    rows_by_ts = {}
    order = []
    for raw in header_cursor.fetchall():
        row = dict(zip(header_columns, raw))
        rows_by_ts[row["ts"]] = {
            "ts": row["ts"],
            "v1_composite": row["v1_composite"],
            "gold_regime": row["gold_regime"],
            "sources": {key: None for key in source_keys},
        }
        order.append(row["ts"])

    non_numeric_counts = {key: 0 for key in source_keys}
    value_cursor = conn.execute(SOURCE_VALUES_SQL, (start_ts, end_ts))
    value_columns = [d[0] for d in value_cursor.description]
    for raw in value_cursor.fetchall():
        vrow = dict(zip(value_columns, raw))
        ts = vrow["ts"]
        key = vrow["source_key"]
        if ts not in rows_by_ts:
            continue
        if vrow["value_type"] in ("integer", "real"):
            rows_by_ts[ts]["sources"][key] = vrow["source_value"]
        else:
            non_numeric_counts[key] = non_numeric_counts.get(key, 0) + 1

    rows = [rows_by_ts[ts] for ts in order]
    return source_keys, rows, non_numeric_counts


# =====================================================================
# LEVEL 1: does the source contain usable signal/variation at all?
# =====================================================================

def source_coverage_report(source_keys, rows, non_numeric_counts=None):
    """Pure function (no DB). Per-source Level 1 report: coverage,
    missingness, unique values, distribution/range, temporal coverage,
    variation. Never mean-imputes, never treats missing as zero.
    """
    non_numeric_counts = non_numeric_counts or {}
    n_total = len(rows)
    report = {}
    for key in source_keys:
        present_rows = [(r["ts"], r["sources"][key]) for r in rows if r["sources"][key] is not None]
        present = len(present_rows)
        missing = n_total - present
        values = [v for _, v in present_rows]
        distinct_values = len(set(values))

        if present > 0:
            mean = sum(values) / present
            variance = sum((v - mean) ** 2 for v in values) / present
            stddev = variance ** 0.5
            vmin = min(values)
            vmax = max(values)
            first_ts_present = min(ts for ts, _ in present_rows)
            last_ts_present = max(ts for ts, _ in present_rows)
        else:
            mean = variance = stddev = vmin = vmax = None
            first_ts_present = last_ts_present = None

        report[key] = {
            "n_rows_in_window": n_total,
            "present": present,
            "missing": missing,
            "coverage_pct": (present / n_total * 100.0) if n_total > 0 else None,
            "distinct_values": distinct_values,
            "has_variation": distinct_values > 1,
            "min": vmin,
            "max": vmax,
            "mean": mean,
            "stddev": stddev,
            "first_ts_present": first_ts_present,
            "last_ts_present": last_ts_present,
            "non_numeric_value_count": non_numeric_counts.get(key, 0),
            "level1_status": "OK" if (present > 0 and distinct_values > 1) else (
                "NO_DATA" if present == 0 else "NO_VARIATION"
            ),
        }
    return report


def structural_shape_report(rows):
    """Reports how many distinct source-key SETS appear across the
    window (schema drift, Section 5) -- a row's "shape" is the sorted
    tuple of keys with a non-None value. Purely descriptive; does not
    reject or normalize any shape."""
    shape_counts = {}
    for r in rows:
        shape = tuple(sorted(k for k, v in r["sources"].items() if v is not None))
        shape_counts[shape] = shape_counts.get(shape, 0) + 1
    return {
        "n_distinct_shapes": len(shape_counts),
        "shape_counts": {",".join(shape) if shape else "(empty)": count for shape, count in shape_counts.items()},
    }


# =====================================================================
# LEVEL 2: association with subsequent BTC outcome
# =====================================================================

def _pair_source_with_outcome(rows, outcome_rows, source_key):
    """Pairs each row's source value with its RESOLVED forward return at
    the same ts, dropping (and counting, never silently) rows where the
    source is missing or the outcome never resolved."""
    outcome_by_ts = {o["anchor_ts"]: o for o in outcome_rows}
    xs, ys, ts_used = [], [], []
    dropped_missing_source = 0
    dropped_missing_outcome_row = 0
    dropped_unresolved_outcome = 0

    for row in rows:
        ts = row["ts"]
        value = row["sources"].get(source_key)
        outcome = outcome_by_ts.get(ts)
        if value is None:
            dropped_missing_source += 1
            continue
        if outcome is None:
            dropped_missing_outcome_row += 1
            continue
        if outcome["outcome_status"] != "RESOLVED":
            dropped_unresolved_outcome += 1
            continue
        xs.append(float(value))
        ys.append(outcome["forward_return_pct"])
        ts_used.append(ts)

    return {
        "xs": xs,
        "ys": ys,
        "ts_used": ts_used,
        "n": len(xs),
        "n_candidate_rows": len(rows),
        "dropped_missing_source": dropped_missing_source,
        "dropped_missing_outcome_row": dropped_missing_outcome_row,
        "dropped_unresolved_outcome": dropped_unresolved_outcome,
    }


def level2_association_for_source(rows, outcome_rows, source_key, horizon_hours,
                                   oos_split_fraction=OOS_SPLIT_FRACTION):
    """Level 2 report for one source at one outcome horizon.

    Reports (Section 3): n, effect size (Pearson r over the FULL paired
    sample), 95% CI, raw two-tailed p-value (correction applied later,
    across the whole test battery, by apply_multiple_testing_correction()),
    and a chronological (never shuffled) discovery/validation split used
    ONLY to report sign stability here -- the split itself, plus nested-
    regression incremental error reduction, is Level 3's job
    (level3_incremental_for_source()), kept separate per Section 3
    ("a source reaching Level 2 must NOT automatically be considered
    Level 3").
    """
    pairing = _pair_source_with_outcome(rows, outcome_rows, source_key)
    n = pairing["n"]
    base = {
        "source_key": source_key,
        "horizon_hours": horizon_hours,
        "n": n,
        "n_candidate_rows": pairing["n_candidate_rows"],
        "dropped_missing_source": pairing["dropped_missing_source"],
        "dropped_missing_outcome_row": pairing["dropped_missing_outcome_row"],
        "dropped_unresolved_outcome": pairing["dropped_unresolved_outcome"],
    }

    if n < 3:
        return {**base, "status": "INSUFFICIENT_DATA", "effect_size_r": None,
                "ci_low": None, "ci_high": None, "p_raw": None,
                "oos_split": None, "sample_size_status": "INSUFFICIENT_DATA"}

    r_full = pearson_correlation(pairing["xs"], pairing["ys"])
    if r_full is None:
        return {**base, "status": "NO_VARIATION", "effect_size_r": None,
                "ci_low": None, "ci_high": None, "p_raw": None,
                "oos_split": None, "sample_size_status": "NO_VARIATION"}

    ci = fisher_z_ci_and_p(r_full, n)

    split_idx = int(n * oos_split_fraction)
    r_discovery = pearson_correlation(pairing["xs"][:split_idx], pairing["ys"][:split_idx])
    r_validation = pearson_correlation(pairing["xs"][split_idx:], pairing["ys"][split_idx:])
    n_discovery = split_idx
    n_validation = n - split_idx
    sign_stable = None
    if r_discovery is not None and r_validation is not None:
        sign_stable = (r_discovery > 0) == (r_validation > 0)

    return {
        **base,
        "status": "OK",
        "effect_size_r": r_full,
        "ci_low": ci["ci_low"],
        "ci_high": ci["ci_high"],
        "p_raw": ci["p_two_tailed"],
        "sample_size_status": "OK" if n >= MIN_SAMPLE_FOR_LEVEL2 else "SMALL_SAMPLE_CAUTION",
        "oos_split": {
            "n_discovery": n_discovery,
            "n_validation": n_validation,
            "r_discovery": r_discovery,
            "r_validation": r_validation,
            "sign_stable": sign_stable,
        },
    }


def run_level2_battery(rows, outcome_rows_by_horizon, source_keys):
    """Runs level2_association_for_source() for every (source, horizon)
    pair, then applies ONE Benjamini-Hochberg correction across the
    whole battery (Section 11: "there will be many source x horizon
    tests -- use an explicit multiple-testing correction"). Returns a
    dict keyed by (source_key, horizon_hours) with p_corrected and
    significant added to each result, plus metadata about the
    correction itself (method, number of tests, alpha).
    """
    results = {}
    order = []
    for horizon_hours, outcome_rows in outcome_rows_by_horizon.items():
        for source_key in source_keys:
            result = level2_association_for_source(rows, outcome_rows, source_key, horizon_hours)
            key = (source_key, horizon_hours)
            results[key] = result
            order.append(key)

    p_values = [results[k]["p_raw"] for k in order]
    corrected = benjamini_hochberg(p_values, alpha=ALPHA)
    for key, correction in zip(order, corrected):
        results[key]["p_corrected"] = correction["p_corrected"]
        results[key]["significant"] = correction["significant"]

    return {
        "tests": results,
        "multiple_testing_correction": {
            "method": "benjamini_hochberg",
            "alpha": ALPHA,
            "n_tests": len(order),
        },
    }


# =====================================================================
# LEVEL 3: incremental information beyond the V1 composite
# =====================================================================

def level3_incremental_for_source(rows, outcome_rows, source_key, horizon_hours,
                                   split_fraction=OOS_SPLIT_FRACTION):
    """Does `source_key` add information beyond the V1 composite
    (`history.score`), evaluated chronologically out-of-sample (Section
    10 -- NEVER shuffled k-fold)?

    Two complementary, independently-reportable methods (Section 3
    explicitly allows "an empirically appropriate method... do not
    assume in advance"):

    1. Partial correlation of source vs. outcome controlling for the V1
       composite, computed on the discovery half only.
    2. Nested regression: a baseline model (composite only) vs. a full
       model (composite + source), both trained on the discovery half,
       evaluated by RMSE on the untouched validation half. A source
       "adds incremental information" here only if the full model's
       out-of-sample RMSE is measurably lower than the baseline's.

    No coefficient from either model is used anywhere else in this
    codebase -- this function only ever reports whether OOS error goes
    down, never republishes a "better" formula.

    SCOPE (post-review correction): both methods here condition ONLY on
    the V1 composite. Neither controls for any other source, correlated
    or not -- Section 9's redundancy helpers are never called from, or
    fed into, this function. This result answers "incremental beyond the V1
    composite" and nothing broader; "incremental beyond correlated/
    redundant sources" is a separate, larger research question this PR
    does not address (see module docstring's SCOPE CORRECTION section).
    """
    outcome_by_ts = {o["anchor_ts"]: o for o in outcome_rows}
    combined = []
    for row in rows:
        ts = row["ts"]
        source_value = row["sources"].get(source_key)
        composite_value = row["v1_composite"]
        outcome = outcome_by_ts.get(ts)
        if source_value is None or composite_value is None:
            continue
        if outcome is None or outcome["outcome_status"] != "RESOLVED":
            continue
        combined.append((ts, float(source_value), float(composite_value), outcome["forward_return_pct"]))

    n = len(combined)
    base = {"source_key": source_key, "horizon_hours": horizon_hours, "n": n}
    if n < MIN_SAMPLE_FOR_LEVEL3:
        return {**base, "status": "INSUFFICIENT_DATA", "partial_correlation": None,
                "oos": None}

    combined.sort(key=lambda t: t[0])  # chronological, never shuffled
    split_idx = int(n * split_fraction)
    discovery = combined[:split_idx]
    validation = combined[split_idx:]
    if len(discovery) < 10 or len(validation) < 10:
        return {**base, "status": "INSUFFICIENT_DATA", "partial_correlation": None,
                "oos": None}

    d_source = [d[1] for d in discovery]
    d_composite = [d[2] for d in discovery]
    d_y = [d[3] for d in discovery]
    v_source = [d[1] for d in validation]
    v_composite = [d[2] for d in validation]
    v_y = [d[3] for d in validation]

    r_sy = pearson_correlation(d_source, d_y)
    r_sc = pearson_correlation(d_source, d_composite)
    r_cy = pearson_correlation(d_composite, d_y)
    partial_r = partial_correlation(r_sy, r_sc, r_cy)

    baseline_slope, baseline_intercept = ols_1var(d_composite, d_y)
    full_b0, full_b1, full_b2 = ols_2var(d_composite, d_source, d_y)

    baseline_rmse = full_model_rmse = rmse_reduction_pct = None
    oos_status = "MODEL_UNDEFINED"
    if baseline_slope is not None:
        baseline_preds = [baseline_intercept + baseline_slope * c for c in v_composite]
        baseline_rmse = rmse(v_y, baseline_preds)
    if full_b0 is not None:
        full_preds = [full_b0 + full_b1 * c + full_b2 * s for c, s in zip(v_composite, v_source)]
        full_model_rmse = rmse(v_y, full_preds)
    if baseline_rmse is not None and full_model_rmse is not None and baseline_rmse > 0:
        rmse_reduction_pct = (baseline_rmse - full_model_rmse) / baseline_rmse * 100.0
        oos_status = "IMPROVED" if rmse_reduction_pct > 0 else "NOT_IMPROVED"

    return {
        **base,
        "status": "OK",
        "n_discovery": len(discovery),
        "n_validation": len(validation),
        "partial_correlation": partial_r,
        "discovery_pairwise_r": {"source_vs_outcome": r_sy, "source_vs_composite": r_sc,
                                  "composite_vs_outcome": r_cy},
        "oos": {
            "baseline_rmse_composite_only": baseline_rmse,
            "full_model_rmse_composite_plus_source": full_model_rmse,
            "rmse_reduction_pct": rmse_reduction_pct,
            "status": oos_status,
            "regression_coefficients_full_model": {
                "intercept": full_b0, "composite_coef": full_b1, "source_coef": full_b2,
            } if full_b0 is not None else None,
        },
    }


# =====================================================================
# Section 9: redundancy (source-source and source-vs-composite)
# =====================================================================

def pairwise_source_redundancy(rows, source_keys):
    """All pairwise source-source Pearson correlations (pairwise
    deletion: each pair uses only rows where BOTH are present, with n
    reported per pair -- never a single global n assumed for every
    pair). Flags |r| >= STRONG_REDUNDANCY_THRESHOLD; never removes or
    merges anything (Section 9)."""
    results = {}
    for i, key_a in enumerate(source_keys):
        for key_b in source_keys[i + 1:]:
            xs, ys = [], []
            for row in rows:
                a = row["sources"].get(key_a)
                b = row["sources"].get(key_b)
                if a is not None and b is not None:
                    xs.append(float(a))
                    ys.append(float(b))
            n = len(xs)
            r = pearson_correlation(xs, ys) if n >= 2 else None
            results[(key_a, key_b)] = {
                "n": n,
                "r": r,
                "strong_redundancy": (r is not None and abs(r) >= STRONG_REDUNDANCY_THRESHOLD),
            }
    return results


def source_vs_composite_redundancy(rows, source_keys):
    """Per-source Pearson correlation against the V1 composite
    (history.score). Same flag-only semantics as pairwise redundancy."""
    results = {}
    for key in source_keys:
        xs, ys = [], []
        for row in rows:
            value = row["sources"].get(key)
            composite = row["v1_composite"]
            if value is not None and composite is not None:
                xs.append(float(value))
                ys.append(float(composite))
        n = len(xs)
        r = pearson_correlation(xs, ys) if n >= 2 else None
        results[key] = {
            "n": n,
            "r": r,
            "strong_redundancy": (r is not None and abs(r) >= STRONG_REDUNDANCY_THRESHOLD),
        }
    return results


# =====================================================================
# Section 13: regime stability (report, never gate or average away)
# =====================================================================

def regime_stability_for_source(rows, outcome_rows, source_key, horizon_hours):
    """Per-regime (history.gold_regime) Pearson r for one source/horizon,
    plus an explicit sign-reversal flag across regimes. Never rejects,
    never averages the regimes together -- a regime-dependent effect is
    reported as exactly that (Section 13: "may become a separate
    research hypothesis")."""
    pairing = _pair_source_with_outcome(rows, outcome_rows, source_key)
    ts_used = set(pairing["ts_used"])
    regime_by_ts = {row["ts"]: row["gold_regime"] for row in rows}

    by_regime = {}
    for x, y, ts in zip(pairing["xs"], pairing["ys"], pairing["ts_used"]):
        regime = regime_by_ts.get(ts)
        by_regime.setdefault(regime, {"xs": [], "ys": []})
        by_regime[regime]["xs"].append(x)
        by_regime[regime]["ys"].append(y)

    per_regime = {}
    for regime, data in by_regime.items():
        n = len(data["xs"])
        label = regime if regime is not None else "(null)"
        if n < MIN_SAMPLE_PER_REGIME:
            per_regime[label] = {"n": n, "status": "INSUFFICIENT_DATA", "r": None,
                                  "ci_low": None, "ci_high": None}
            continue
        r = pearson_correlation(data["xs"], data["ys"])
        ci = fisher_z_ci_and_p(r, n) if r is not None else {"ci_low": None, "ci_high": None, "p_two_tailed": None}
        per_regime[label] = {"n": n, "status": "OK", "r": r,
                              "ci_low": ci["ci_low"], "ci_high": ci["ci_high"]}

    defined_rs = [(label, v["r"]) for label, v in per_regime.items() if v["r"] is not None]
    sign_reversal_detected = False
    for i in range(len(defined_rs)):
        for j in range(i + 1, len(defined_rs)):
            r_a, r_b = defined_rs[i][1], defined_rs[j][1]
            if (r_a > 0) != (r_b > 0) and abs(r_a) >= STABILITY_EPSILON and abs(r_b) >= STABILITY_EPSILON:
                sign_reversal_detected = True

    return {
        "source_key": source_key,
        "horizon_hours": horizon_hours,
        "per_regime": per_regime,
        "sign_reversal_detected": sign_reversal_detected,
        "n_total_used": len(ts_used),
    }


# =====================================================================
# Section 12: evidence taxonomy
# =====================================================================

def classify_evidence(level2_result, alpha=ALPHA, stability_epsilon=STABILITY_EPSILON,
                       min_adequate_n=MIN_SAMPLE_FOR_CONTRADICTED):
    """Exactly the four labels required by Section 12, derived only from
    a single level2_association_for_source() result (already carrying
    p_corrected/significant from run_level2_battery(), and its own
    chronological discovery/validation split for stability).

    CONTRADICTED requires an adequate (>= min_adequate_n), independently
    computed sample in BOTH halves with a genuine sign reversal and a
    non-trivial effect in both -- anything short of that is INCONCLUSIVE,
    never CONTRADICTED (Section 12's explicit correction).
    """
    if level2_result.get("status") in ("INSUFFICIENT_DATA", "NO_VARIATION"):
        return "INCONCLUSIVE"

    p_corrected = level2_result.get("p_corrected")
    significant = p_corrected is not None and p_corrected < alpha
    if significant:
        return "STATISTICALLY_SIGNIFICANT"

    split = level2_result.get("oos_split") or {}
    r_discovery = split.get("r_discovery")
    r_validation = split.get("r_validation")
    n_discovery = split.get("n_discovery", 0)
    n_validation = split.get("n_validation", 0)

    if (
        r_discovery is not None and r_validation is not None
        and n_discovery >= min_adequate_n and n_validation >= min_adequate_n
        and (r_discovery > 0) != (r_validation > 0)
        and abs(r_discovery) >= stability_epsilon and abs(r_validation) >= stability_epsilon
    ):
        return "CONTRADICTED"

    r_full = level2_result.get("effect_size_r")
    sign_stable = split.get("sign_stable")
    if sign_stable and r_full is not None and abs(r_full) >= stability_epsilon:
        return "STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE"

    return "INCONCLUSIVE"


# =====================================================================
# Orchestration
# =====================================================================

def build_source_effectiveness_report(conn, start_ts, end_ts, horizons=(1, 3, 6, 12, 24)):
    """The single entry point that assembles Level 1/2/3, redundancy,
    regime stability, sub-4% movement, and evidence classification into
    one deterministic, nested report. Read-only. Never writes.

    Level 3 (the "level3" key below) is scoped strictly to "incremental
    beyond the V1 composite" -- it does NOT condition on the "redundancy"
    key's own findings (pairwise or vs.-composite correlations among
    sources). The two are computed and reported entirely independently;
    see level3_incremental_for_source()'s docstring and the module
    docstring's SCOPE CORRECTION section for why that broader,
    correlated-source-conditioned question is out of scope for this PR.

    Importing here (not at module top) keeps this orchestration function
    the one place that depends on outcome_engine/movement_distribution,
    consistent with this project's existing pattern of a DB-bound
    module composing pure-function modules rather than the reverse.
    """
    import outcome_engine
    import movement_distribution

    _validate_bounds(start_ts, end_ts)
    source_keys, rows, non_numeric_counts = extract_source_matrix(conn, start_ts, end_ts)
    level1 = source_coverage_report(source_keys, rows, non_numeric_counts)
    shapes = structural_shape_report(rows)

    outcome_rows_by_horizon = {}
    movement_by_horizon = {}
    for horizon_hours in horizons:
        outcome_rows = outcome_engine.compute_forward_returns_from_history(
            conn, start_ts, end_ts, horizon_hours
        )
        outcome_rows_by_horizon[horizon_hours] = outcome_rows
        movement_by_horizon[horizon_hours] = movement_distribution.summarize_absolute_returns(outcome_rows)

    # Level 2: only run the full battery for sources that at least pass
    # Level 1 (have data and vary at all) -- a constant/empty source
    # cannot be associated with anything, and running the test anyway
    # would just inflate the multiple-testing denominator with a
    # guaranteed-null result.
    eligible_sources = [k for k in source_keys if level1[k]["level1_status"] == "OK"]
    level2_battery = run_level2_battery(rows, outcome_rows_by_horizon, eligible_sources)

    level3 = {}
    regime_stability = {}
    evidence = {}
    for horizon_hours in horizons:
        outcome_rows = outcome_rows_by_horizon[horizon_hours]
        for source_key in eligible_sources:
            level3[(source_key, horizon_hours)] = level3_incremental_for_source(
                rows, outcome_rows, source_key, horizon_hours
            )
            regime_stability[(source_key, horizon_hours)] = regime_stability_for_source(
                rows, outcome_rows, source_key, horizon_hours
            )
            level2_result = level2_battery["tests"][(source_key, horizon_hours)]
            evidence[(source_key, horizon_hours)] = classify_evidence(level2_result)

    redundancy = {
        "pairwise": pairwise_source_redundancy(rows, eligible_sources),
        "vs_composite": source_vs_composite_redundancy(rows, eligible_sources),
    }

    return {
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "n_history_rows": len(rows),
        "sources_discovered": source_keys,
        "sources_eligible_for_level2plus": eligible_sources,
        "level1": level1,
        "structural_shapes": shapes,
        "movement_distribution_by_horizon": movement_by_horizon,
        "level2": level2_battery,
        "level3": {f"{k[0]}|{k[1]}h": v for k, v in level3.items()},
        "regime_stability": {f"{k[0]}|{k[1]}h": v for k, v in regime_stability.items()},
        "evidence_labels": {f"{k[0]}|{k[1]}h": v for k, v in evidence.items()},
        "redundancy": redundancy,
        "normalization_note": (
            "Raw source values were NOT normalized before correlation -- "
            "Pearson r is scale-invariant. See source_analysis.py module "
            "docstring (Section 6) for the full rationale."
        ),
        "level3_scope_note": (
            "Level 3 ('level3' key) measures incremental information "
            "BEYOND THE V1 COMPOSITE ONLY (partial correlation and "
            "nested-OLS RMSE, both controlling only for history.score). "
            "It does NOT condition on any other source, correlated or "
            "not -- the 'redundancy' key above is computed entirely "
            "separately and is never fed into Level 3. Incremental "
            "information beyond correlated/redundant sources remains a "
            "separate research question, not established by this PR."
        ),
    }


# =====================================================================
# Section 15: persistence into the already-deployed research_analyses
# =====================================================================
# THIS IS THE ONLY FUNCTION IN THIS MODULE THAT WRITES ANYTHING. It is
# tested only against an in-memory sqlite3 fixture in
# test_source_analysis.py and is NEVER invoked against a production
# connection anywhere in this PR (Section 19: production D1 access in
# this project stays read-only). A future, separately authorized PR
# decides when/whether to actually call this against production.

def persist_analysis(conn, analysis_ts, window_start_ts, window_end_ts, sample_size,
                      subject, metric_json_obj, multiple_testing_correction,
                      validation_status="observation"):
    """INSERTs one row into research_analyses (schema unchanged from PR1
    -- see .ai/migrations/0005_research_schema.sql; PR5c requires no new
    migration because metric_json's existing free-form TEXT column
    already accommodates this PR's full nested findings payload).

    Returns the new row's analysis_id.
    """
    cursor = conn.execute(
        "INSERT INTO research_analyses "
        "(analysis_ts, window_start_ts, window_end_ts, sample_size, subject, "
        " metric_json, multiple_testing_correction, validation_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            analysis_ts, window_start_ts, window_end_ts, sample_size, subject,
            json.dumps(metric_json_obj), multiple_testing_correction, validation_status,
        ),
    )
    return cursor.lastrowid
