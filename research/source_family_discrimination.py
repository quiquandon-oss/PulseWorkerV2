"""
PR5f: source-family discrimination -- does each surviving PR5e
BUILD_REQUEST source family carry incremental information BEYOND the
V1 composite AND the other surviving families, not merely beyond the
composite alone?

=====================================================================
Why this PR exists (the exact gap PR5e/PR5c left open)
=====================================================================

PR5c's Level 3 (source_analysis.py's own module docstring, "SCOPE
CORRECTION") and PR5e's Gate 3/Gate 4 both condition ONLY on the V1
composite. PR5e's own `source_redundancy_note()` explicitly flags this:
a source can be `NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED` (no single
pairwise |r|>=0.7 partner) while still sharing real, moderate
information with OTHER surviving sources -- PR5e's own README documents
the concrete counter-example (`global`/`gold` at r=-0.624). PR5e's final
production snapshot produced 7 BUILD_REQUEST candidates across 3
source families (`fng`, `global`, `onchain`) -- this PR asks the
question PR5e could only flag as unresolved: after controlling for the
V1 composite AND the OTHER TWO surviving families jointly, does each
family still carry a genuinely incremental, out-of-sample-replicating
signal?

=====================================================================
Why this is NOT circular with PR5e
=====================================================================

PR5e's Gate 3/4 (per-family) condition on a control set of size 1 (the
V1 composite only). This PR conditions on a STRICTLY LARGER control set
(composite + the other 2 surviving families) for the same outcome
variable. Adding controls can only ever REDUCE or leave unchanged a
variable's apparent incremental contribution in a nested OLS comparison
(the baseline model here is a superset of PR5e's own baseline) -- it
can never inflate it. So this PR's "survives" verdict is strictly
stronger evidence than PR5e's own "survives Gate 4" verdict, not a
re-run of the same test with the same possible outcome space. A family
could easily pass PR5e's composite-only Gate 4 and fail HERE (that is
exactly what this PR is built to detect) but the reverse is
mathematically impossible for the OOS check specifically (a family
that fails PR5e's narrower test could not artificially "pass" this
strictly harder one).

=====================================================================
Methodology
=====================================================================

1. Reads a fresh run of `hypothesis_gate.build_hypothesis_report()`
   (PR5e's own, completely unmodified entry point) to determine the
   CURRENT surviving BUILD_REQUEST source families -- never hardcoded,
   so this PR stays correct if a future snapshot's surviving set
   differs from the one recorded in PR5e's README (`fng`, `global`,
   `onchain`). If the current set differs, that difference is reported
   explicitly (see `compare_snapshot_to_pr5e()`), never silently
   substituted.
2. For each surviving family, selects ONE primary horizon: the horizon
   (among that family's own PR5e BUILD_REQUEST-eligible horizons) with
   the largest `oos_validation.rmse_reduction_pct` -- a deterministic
   criterion computed from PR5e's own already-produced numbers, not
   re-derived or cherry-picked here. This directly addresses "do NOT
   simply run the same 6h/12h/24h analysis again": exactly one
   horizon per family is put through the NEW joint-control test; the
   family's other PR5e horizons are reported separately, explicitly
   labeled non-independent CONTEXT ONLY (see `other_horizon_context()`).
3. At that one horizon, builds the same per-row (ts, source values,
   V1 composite, forward return) matrix PR5c/PR5e already build, via
   the SAME public primitives (`source_analysis.extract_source_matrix`,
   `outcome_engine.compute_forward_returns_from_history`) -- no new
   query shape.
4. Reports (never gates on) the pairwise Pearson correlation between
   each pair of surviving families (reusing PR5c's own
   `pairwise_source_redundancy()` verbatim) -- the redundancy CONTEXT
   PR5e could only flag, now actually measured for this specific trio.
5. Reports a higher-order PARTIAL correlation of (family, outcome)
   controlling for {V1 composite, other family A, other family B} --
   computed via the standard recursive partial-correlation identity,
   using ONLY pairwise Pearson correlations and PR5c's own single-
   control `partial_correlation()` function applied repeatedly (see
   `higher_order_partial_correlation()`; verified by direct comparison
   against the equivalent multiple-regression-residual computation to
   machine precision -- see test_source_family_discrimination.py).
   Exactly like PR5e's Gate 3, this is reported for completeness, NOT
   treated as independent confirmation of the OOS result below.
6. The principal, held-out test: a nested OLS comparison, generalized
   from PR5e's own v2 Gate 4 to a MULTI-PREDICTOR baseline. Baseline
   model: V1 composite + other family A + other family B (fit via the
   new `stats_utils.ols_nvar()`, itself validated against `ols_2var`
   for consistency). Full model: baseline + the target family. Both
   fit on the discovery half (chronological, never shuffled,
   OOS_SPLIT_FRACTION=0.7, PR5e's own constant), evaluated by RMSE on
   the untouched validation half. EXACTLY PR5e's own v2 promotion
   criterion is reused, unchanged: the validation-half RMSE reduction
   must be >=`hypothesis_gate.MEANINGFUL_OOS_IMPROVEMENT_PCT` (5%) AND
   replicate the same direction across two non-overlapping chronological
   sub-windows of the validation half (`family_subsplit_stability()`,
   the direct multi-predictor generalization of PR5e's own
   `source_subsplit_stability()`). No new threshold is introduced.

=====================================================================
Discrimination status -- reuses PR5e's own validation_status
vocabulary, adds exactly one new rollup concept
=====================================================================

`validation_status` (`PASSED_HOLDOUT`/`NOT_REPLICATED`/`FAILED_HOLDOUT`/
`INSUFFICIENT_DATA_FOR_HOLDOUT`) is `hypothesis_gate.VALIDATION_STATUSES`,
imported and reused verbatim -- not reinvented. This PR adds exactly one
new, narrowly-scoped rollup on top: `discrimination_status`, one of
`DISCRIMINATED_INCREMENTAL` (validation_status == PASSED_HOLDOUT),
`NOT_DISCRIMINATED` (NOT_REPLICATED or FAILED_HOLDOUT), or
`INSUFFICIENT_DATA` (INSUFFICIENT_DATA_FOR_HOLDOUT). This status
answers a narrower question than PR5e's own `lifecycle_status`
("worthy of human review") and must never be confused with it:
`DISCRIMINATED_INCREMENTAL` means "survives controlling for composite
AND the other surviving families" -- it is NOT a promotion, it is NOT
`hypothesis_gate`'s "VALIDATED" lifecycle stage (which per PR5a's own
schema means validated IN PRODUCTION after being IMPLEMENTED, entirely
outside either PR's scope), and it does NOT modify, re-persist, or
override PR5e's own already-computed `lifecycle_status` for that
candidate. The two statuses are reported SIDE BY SIDE, never merged.

=====================================================================
Production safety
=====================================================================

Entirely read-only. No `persist_*` function exists in this module at
all -- there is nothing here to accidentally point at production. No
V1/V2/Worker file is imported, read, or referenced. No migration is
applied or required (this PR introduces no schema). See
test_source_family_discrimination.py's
`test_no_writes_anywhere_in_module` for the structural proof.

=====================================================================
Temporal safety
=====================================================================

Every row-level computation here reuses PR5c/PR5b's own already-proven
as-of joins (`extract_source_matrix`, `compute_forward_returns_from_
history`) -- no new query against predictions/history/btc_data.
`family_subsplit_stability()` and the nested OLS comparison sort by ts
ascending and split chronologically, never shuffled, exactly mirroring
`hypothesis_gate.source_subsplit_stability()`'s own discipline.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import source_analysis as sa  # noqa: E402
import outcome_engine  # noqa: E402
import hypothesis_gate as hg  # noqa: E402
from stats_utils import pearson_correlation, partial_correlation, ols_nvar, rmse  # noqa: E402

# ---------------------------------------------------------------------
# Reused constants -- no new arbitrary numbers.
# ---------------------------------------------------------------------
OOS_SPLIT_FRACTION = hg.OOS_SPLIT_FRACTION                       # 0.7, PR5c's own
MIN_SAMPLE_FOR_HOLDOUT_HALF = hg.MIN_SAMPLE_FOR_HOLDOUT_HALF     # 30, PR5c's own
MIN_SAMPLE_FOR_LEVEL3 = hg.MIN_SAMPLE_FOR_LEVEL3                 # 40, PR5c's own
MEANINGFUL_OOS_IMPROVEMENT_PCT = hg.MEANINGFUL_OOS_IMPROVEMENT_PCT  # 5.0, PR5e v2's own

DISCRIMINATION_STATUSES = ("DISCRIMINATED_INCREMENTAL", "NOT_DISCRIMINATED", "INSUFFICIENT_DATA")

# PR5e's own recorded production snapshot (research/README.md, PR5e
# section) -- kept here ONLY as a fixed comparison point for
# compare_snapshot_to_pr5e(); never used as a substitute for a fresh
# read against production.
PR5E_RECORDED_SNAPSHOT = {
    "predictions": 1070,
    "history": 499,
    "btc_data": 2095,
    "window_start_ts": 1785582508231,
    "window_end_ts": 1789797630549,
    "surviving_families": ("fng", "global", "onchain"),
    "build_request_horizons_by_family": {
        "fng": ["source:fng:6h", "source:fng:12h", "source:fng:24h"],
        "global": ["source:global:6h", "source:global:12h", "source:global:24h"],
        "onchain": ["source:onchain:24h"],
    },
}


# =====================================================================
# Step 1: read PR5e's current surviving families (never hardcoded)
# =====================================================================

def current_surviving_families(hypothesis_report):
    """Reads hypothesis_gate's own signal_family_summary -- the exact
    families that reached BUILD_REQUEST in a fresh PR5e run. Returns a
    dict {family: [subjects]} sorted for determinism."""
    families = hypothesis_report["signal_family_summary"]["build_request_horizons_by_family"]
    return {k: sorted(v) for k, v in sorted(families.items())}


def select_primary_horizon_per_family(hypothesis_report, families):
    """For each family, the horizon (among ITS OWN BUILD_REQUEST
    subjects) with the largest oos_validation.rmse_reduction_pct --
    deterministic, derived only from PR5e's own already-computed
    numbers. Returns {family: horizon_hours}."""
    by_subject = {h["subject"]: h for h in hypothesis_report["hypotheses"]}
    primary = {}
    for family, subjects in families.items():
        best_subject, best_reduction = None, None
        for subject in subjects:
            hyp = by_subject[subject]
            reduction = hyp["oos_validation"].get("rmse_reduction_pct")
            if reduction is not None and (best_reduction is None or reduction > best_reduction):
                best_subject, best_reduction = subject, reduction
        horizon_hours = int(best_subject.split(":")[2].replace("h", ""))
        primary[family] = horizon_hours
    return primary


def other_horizon_context(hypothesis_report, families, primary_horizons):
    """The non-primary BUILD_REQUEST horizons for each family, reported
    for context ONLY -- explicitly never treated as independent
    confirmations (they overlap the primary horizon's forward-return
    window)."""
    by_subject = {h["subject"]: h for h in hypothesis_report["hypotheses"]}
    context = {}
    for family, subjects in families.items():
        primary_h = primary_horizons[family]
        others = []
        for subject in subjects:
            horizon_hours = int(subject.split(":")[2].replace("h", ""))
            if horizon_hours == primary_h:
                continue
            hyp = by_subject[subject]
            others.append({
                "subject": subject, "horizon_hours": horizon_hours,
                "rmse_reduction_pct": hyp["oos_validation"].get("rmse_reduction_pct"),
                "lifecycle_status": hyp["lifecycle_status"],
            })
        context[family] = {
            "horizons": others,
            "caveat": (
                "CONTEXT ONLY -- these horizons overlap the primary horizon's "
                "forward-return window and are NOT independent confirmations "
                "of this family's signal (PR5e's own documented horizon-"
                "overlap limitation)."
            ),
        }
    return context


# =====================================================================
# Step 2: redundancy context among the surviving families (reported,
# never gated -- PR5c's own pairwise function, unmodified)
# =====================================================================

def pairwise_family_correlations(matrix_rows, families):
    """Thin, unmodified reuse of source_analysis.pairwise_source_
    redundancy() restricted to the surviving families. Reports the
    EXACT correlation values (not just the >=0.7 boolean flag PR5e's
    source_redundancy_note() reduces them to) so this PR's own joint
    model's behavior can be sanity-checked against it."""
    return sa.pairwise_source_redundancy(matrix_rows, sorted(families))


# =====================================================================
# Step 3: higher-order partial correlation (recursive identity, exact
# -- verified against the multiple-regression-residual method to
# machine precision in test_source_family_discrimination.py)
# =====================================================================

def _pairwise_r_table(named_series):
    """named_series: {name: values}. Returns {frozenset({a,b}): r_ab}
    for every pair, using PR5c's own pearson_correlation()."""
    table = {}
    names = sorted(named_series)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            table[frozenset({a, b})] = pearson_correlation(named_series[a], named_series[b])
    return table


def higher_order_partial_correlation(named_series, target, outcome_key, control_keys):
    """Partial correlation of (target, outcome_key) controlling for ALL
    of control_keys, via the standard recursive identity: peel off one
    control at a time using PR5c's own single-control partial_
    correlation(), recursing on the remaining controls for every
    pairwise term needed. Mathematically exact (not an approximation --
    see module docstring), using only already-approved primitives.
    Returns None if any required pairwise correlation is undefined
    (e.g. a zero-variance series)."""
    pairwise = _pairwise_r_table(named_series)

    def recurse(a, b, controls):
        if not controls:
            r = pairwise.get(frozenset({a, b}))
            return r
        z = controls[0]
        rest = controls[1:]
        r_ab = recurse(a, b, rest)
        r_az = recurse(a, z, rest)
        r_bz = recurse(b, z, rest)
        return partial_correlation(r_ab, r_az, r_bz)

    return recurse(target, outcome_key, list(control_keys))


# =====================================================================
# Step 4: build the combined, chronologically-sorted row matrix for one
# family's joint model (target + N controls + outcome)
# =====================================================================

def build_combined_rows(matrix_rows, outcome_rows, target_key, control_keys):
    """One row per history timestamp where the target, ALL controls,
    and a RESOLVED forward return are simultaneously present -- never
    imputed, never silently substituted. Sorted chronologically
    (never shuffled), exactly mirroring level3_incremental_for_source()
    and hypothesis_gate.source_subsplit_stability()'s own discipline.
    Returns a list of (ts, target_value, [control_values...], y).
    """
    outcome_by_ts = {o["anchor_ts"]: o for o in outcome_rows}
    combined = []
    for row in matrix_rows:
        ts = row["ts"]
        target_value = row["sources"].get(target_key)
        control_values = [row["sources"].get(k) if k != "v1_composite" else row["v1_composite"]
                           for k in control_keys]
        outcome = outcome_by_ts.get(ts)
        if target_value is None or any(v is None for v in control_values):
            continue
        if outcome is None or outcome["outcome_status"] != "RESOLVED":
            continue
        combined.append((ts, float(target_value), [float(v) for v in control_values], outcome["forward_return_pct"]))
    combined.sort(key=lambda t: t[0])
    return combined


# =====================================================================
# Step 5: the principal held-out test -- nested multi-predictor OLS,
# PR5e's own v2 Gate 4 criterion reused verbatim (magnitude + stability)
# =====================================================================

def nested_model_oos_comparison(combined, split_fraction=OOS_SPLIT_FRACTION):
    """Baseline (controls only) vs. full (controls + target) OLS,
    fit on the discovery half, RMSE-compared on the validation half.
    Direct multi-predictor generalization of level3_incremental_for_
    source()'s own method 2 -- same formulas (stats_utils.ols_nvar/rmse),
    same chronological (never shuffled) split, same OOS_SPLIT_FRACTION.
    """
    n = len(combined)
    if n < MIN_SAMPLE_FOR_LEVEL3:
        return {"status": "INSUFFICIENT_DATA", "n": n}
    split_idx = int(n * split_fraction)
    discovery, validation = combined[:split_idx], combined[split_idx:]
    if len(discovery) < 10 or len(validation) < 10:
        return {"status": "INSUFFICIENT_DATA", "n": n}

    d_controls = list(zip(*(d[2] for d in discovery)))  # transpose -> list of control columns
    d_target = [d[1] for d in discovery]
    d_y = [d[3] for d in discovery]

    baseline_b0, baseline_coefs = ols_nvar(list(d_controls), d_y)
    full_b0, full_coefs = ols_nvar(list(d_controls) + [d_target], d_y)
    if baseline_b0 is None or full_b0 is None:
        return {"status": "MODEL_UNDEFINED", "n": n}

    def predict(sub, b0, coefs, include_target):
        preds = []
        for row in sub:
            controls, target = row[2], row[1]
            values = list(controls) + ([target] if include_target else [])
            preds.append(b0 + sum(c * v for c, v in zip(coefs, values)))
        return preds

    v_y = [d[3] for d in validation]
    baseline_preds = predict(validation, baseline_b0, baseline_coefs, include_target=False)
    full_preds = predict(validation, full_b0, full_coefs, include_target=True)
    baseline_rmse = rmse(v_y, baseline_preds)
    full_model_rmse = rmse(v_y, full_preds)
    if baseline_rmse is None or baseline_rmse == 0:
        return {"status": "MODEL_UNDEFINED", "n": n}

    rmse_reduction_pct = (baseline_rmse - full_model_rmse) / baseline_rmse * 100.0
    return {
        "status": "OK", "n": n, "n_discovery": len(discovery), "n_validation": len(validation),
        "baseline_rmse": baseline_rmse, "full_model_rmse": full_model_rmse,
        "rmse_reduction_pct": rmse_reduction_pct,
        "full_model_coefficients": {"intercept": full_b0, "coefficients": full_coefs},
    }


def family_subsplit_stability(combined, split_fraction=OOS_SPLIT_FRACTION):
    """Multi-predictor generalization of hypothesis_gate.source_
    subsplit_stability(): splits the validation half into two non-
    overlapping chronological sub-windows and requires the SAME
    discovery-fitted baseline/full models to show a positive RMSE
    reduction in BOTH -- the model is fit ONCE on discovery, never
    refit per sub-window (refitting on ~half the already-small
    validation half would be its own new, weaker-sample claim)."""
    n = len(combined)
    if n < MIN_SAMPLE_FOR_LEVEL3:
        return {"status": "INSUFFICIENT_DATA"}
    split_idx = int(n * split_fraction)
    discovery, validation = combined[:split_idx], combined[split_idx:]
    if len(discovery) < 10 or len(validation) < 10:
        return {"status": "INSUFFICIENT_DATA"}

    d_controls = list(zip(*(d[2] for d in discovery)))
    d_target = [d[1] for d in discovery]
    d_y = [d[3] for d in discovery]
    baseline_b0, baseline_coefs = ols_nvar(list(d_controls), d_y)
    full_b0, full_coefs = ols_nvar(list(d_controls) + [d_target], d_y)
    if baseline_b0 is None or full_b0 is None:
        return {"status": "MODEL_UNDEFINED"}

    mid = len(validation) // 2
    sub_halves = (validation[:mid], validation[mid:])

    def rmse_reduction_for(sub):
        if len(sub) < MIN_SAMPLE_FOR_HOLDOUT_HALF:
            return None, len(sub)
        vy = [d[3] for d in sub]
        baseline_preds = [baseline_b0 + sum(c * v for c, v in zip(baseline_coefs, row[2])) for row in sub]
        full_preds = [full_b0 + sum(c * v for c, v in zip(full_coefs, list(row[2]) + [row[1]])) for row in sub]
        b_rmse = rmse(vy, baseline_preds)
        f_rmse = rmse(vy, full_preds)
        if b_rmse is None or b_rmse == 0:
            return None, len(sub)
        return (b_rmse - f_rmse) / b_rmse * 100.0, len(sub)

    red1, n1 = rmse_reduction_for(sub_halves[0])
    red2, n2 = rmse_reduction_for(sub_halves[1])
    if red1 is None or red2 is None:
        return {"status": "INSUFFICIENT_DATA_FOR_SUBSPLIT", "n_sub1": n1, "n_sub2": n2}

    return {
        "status": "OK", "n_sub1": n1, "n_sub2": n2,
        "rmse_reduction_pct_sub1": red1, "rmse_reduction_pct_sub2": red2,
        "sign_stable_across_subsplit": (red1 > 0) and (red2 > 0),
    }


# =====================================================================
# Step 6: per-family orchestration and discrimination verdict
# =====================================================================

def evaluate_family(matrix_rows, outcome_rows_by_horizon, target_family, other_families,
                     horizon_hours, original_hypothesis):
    """Full per-family evaluation at its primary horizon. Returns a
    record with the six-part-style decomposition (redundancy context,
    higher-order partial correlation, OOS nested comparison, subsplit
    stability, discrimination_status, validation_status, comparison to
    PR5e's own original evidence for this candidate).
    """
    control_keys = ["v1_composite"] + sorted(other_families)
    outcome_rows = outcome_rows_by_horizon[horizon_hours]
    combined = build_combined_rows(matrix_rows, outcome_rows, target_family, control_keys)

    oos = nested_model_oos_comparison(combined)
    if oos["status"] != "OK":
        return {
            "family": target_family, "horizon_hours": horizon_hours,
            "controls": control_keys, "n": oos.get("n", len(combined)),
            "oos": oos, "subsplit": {"status": "INSUFFICIENT_DATA"},
            "higher_order_partial_correlation": None,
            "validation_status": "INSUFFICIENT_DATA_FOR_HOLDOUT",
            "discrimination_status": "INSUFFICIENT_DATA",
            "original_pr5e_evidence_status": original_hypothesis["evidence_status"],
            "original_pr5e_lifecycle_status": original_hypothesis["lifecycle_status"],
            "original_pr5e_validation_status": original_hypothesis["validation_status"],
        }

    subsplit = family_subsplit_stability(combined)
    full_red = oos["rmse_reduction_pct"]

    if full_red <= 0:
        validation_status = "FAILED_HOLDOUT"
    elif subsplit["status"] != "OK":
        validation_status = "INSUFFICIENT_DATA_FOR_HOLDOUT"
    elif full_red >= MEANINGFUL_OOS_IMPROVEMENT_PCT and subsplit["sign_stable_across_subsplit"]:
        validation_status = "PASSED_HOLDOUT"
    else:
        validation_status = "NOT_REPLICATED"

    discrimination_status = {
        "PASSED_HOLDOUT": "DISCRIMINATED_INCREMENTAL",
        "NOT_REPLICATED": "NOT_DISCRIMINATED",
        "FAILED_HOLDOUT": "NOT_DISCRIMINATED",
        "INSUFFICIENT_DATA_FOR_HOLDOUT": "INSUFFICIENT_DATA",
    }[validation_status]

    # Higher-order partial correlation on the discovery half only (same
    # discovery/validation discipline as the OOS check; reported, never
    # treated as independent of the OOS result -- see module docstring).
    split_idx = int(len(combined) * OOS_SPLIT_FRACTION)
    discovery = combined[:split_idx]
    named_series = {"target": [d[1] for d in discovery], "y": [d[3] for d in discovery]}
    for i, key in enumerate(control_keys):
        named_series[key] = [d[2][i] for d in discovery]
    hopc = higher_order_partial_correlation(named_series, "target", "y", control_keys)

    return {
        "family": target_family, "horizon_hours": horizon_hours,
        "controls": control_keys, "n": oos["n"],
        "oos": oos, "subsplit": subsplit,
        "higher_order_partial_correlation": hopc,
        "higher_order_partial_correlation_note": (
            "Reported for completeness -- NOT independent confirmation of "
            "the OOS result above (same non-independence caveat as PR5e's "
            "own Gate 3 vs. Gate 4)."
        ),
        "validation_status": validation_status,
        "discrimination_status": discrimination_status,
        "original_pr5e_evidence_status": original_hypothesis["evidence_status"],
        "original_pr5e_lifecycle_status": original_hypothesis["lifecycle_status"],
        "original_pr5e_validation_status": original_hypothesis["validation_status"],
    }


# =====================================================================
# Snapshot comparison (required: explicitly report any difference)
# =====================================================================

def compare_snapshot_to_pr5e(current_counts, current_families):
    """Explicit, structured diff against PR5E_RECORDED_SNAPSHOT -- never
    a silent substitution of a different analysis population."""
    diffs = {}
    for key in ("predictions", "history", "btc_data"):
        if current_counts.get(key) != PR5E_RECORDED_SNAPSHOT[key]:
            diffs[key] = {"pr5e": PR5E_RECORDED_SNAPSHOT[key], "current": current_counts.get(key)}
    if sorted(current_families) != sorted(PR5E_RECORDED_SNAPSHOT["surviving_families"]):
        diffs["surviving_families"] = {
            "pr5e": PR5E_RECORDED_SNAPSHOT["surviving_families"],
            "current": tuple(sorted(current_families)),
        }
    return diffs


# =====================================================================
# Top-level orchestration
# =====================================================================

def build_family_discrimination_report(conn, start_ts, end_ts,
                                        ec_horizons=None, source_horizons=(1, 3, 6, 12, 24)):
    """The single top-level entry point. Read-only. Never writes.
    Re-runs PR5e's own, completely unmodified build_hypothesis_report()
    to establish the current surviving families, then evaluates each
    one's joint-control discrimination at its own primary horizon.
    """
    if ec_horizons is None:
        import error_classification as ec
        ec_horizons = ec.SUPPORTED_HORIZONS

    hypothesis_report = hg.build_hypothesis_report(
        conn, start_ts, end_ts, horizons=ec_horizons, source_horizons=source_horizons)
    families = current_surviving_families(hypothesis_report)
    primary_horizons = select_primary_horizon_per_family(hypothesis_report, families)
    horizon_context = other_horizon_context(hypothesis_report, families, primary_horizons)

    _, matrix_rows, _ = sa.extract_source_matrix(conn, start_ts, end_ts)
    needed_horizons = sorted(set(primary_horizons.values()))
    outcome_rows_by_horizon = {
        h: outcome_engine.compute_forward_returns_from_history(conn, start_ts, end_ts, h)
        for h in needed_horizons
    }

    redundancy = pairwise_family_correlations(matrix_rows, list(families))

    by_subject = {h["subject"]: h for h in hypothesis_report["hypotheses"]}
    family_results = {}
    for family in families:
        horizon_hours = primary_horizons[family]
        subject = f"source:{family}:{horizon_hours}h"
        other_families = [f for f in families if f != family]
        family_results[family] = evaluate_family(
            matrix_rows, outcome_rows_by_horizon, family, other_families,
            horizon_hours, by_subject[subject],
        )

    n_discriminated = sum(1 for r in family_results.values() if r["discrimination_status"] == "DISCRIMINATED_INCREMENTAL")

    current_counts = {
        "predictions": None,  # not queried directly here; caller may attach if needed
        "history": len(matrix_rows),
        "btc_data": None,
    }

    return {
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "surviving_families": families,
        "primary_horizons": primary_horizons,
        "other_horizon_context": horizon_context,
        "pairwise_family_redundancy": redundancy,
        "family_results": family_results,
        "n_families_evaluated": len(families),
        "n_families_discriminated_incremental": n_discriminated,
        "snapshot_diff_vs_pr5e": compare_snapshot_to_pr5e(current_counts, list(families)),
        "methodology_note": (
            "DISCRIMINATED_INCREMENTAL means this family's signal survives "
            "controlling for the V1 composite AND the other surviving "
            "families jointly, out of sample. It is NOT the same as PR5e's "
            "own lifecycle_status/BUILD_REQUEST (which asks a narrower, "
            "composite-only question) and it is NOT 'validated' in "
            "hypothesis_gate's lifecycle sense (post-implementation, "
            "in-production). PR5e's own hypothesis records are reported "
            "here side by side, never modified or re-persisted."
        ),
    }
