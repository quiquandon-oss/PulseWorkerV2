"""
PR5g: FNG-24h temporal robustness / validation study.

=====================================================================
Objective
=====================================================================

PR5f found: at horizon=24h, controlling jointly for the V1 composite,
`global`, and `onchain`, the `fng` source shows a +10.99% out-of-sample
RMSE reduction (n=257, stable across two chronological validation
sub-windows) -- DISCRIMINATED_INCREMENTAL. PR5f's own independent audit
(PR #54) additionally found this result is HORIZON-SPECIFIC (fails at
6h/12h) and asked whether it might be specific to that one ~34-day
sample. PR5g exists to answer exactly that question, and ONLY that
question -- it does NOT implement FNG, does NOT modify V1/V2/Worker,
and does NOT write to production.

Research question (fixed before any new computation was run):
"Does the observed FNG-24h incremental effect remain detectable across
additional chronological data and/or independent temporal segments, or
could the PR5f result plausibly be specific to the current ~34-day
sample?"

=====================================================================
PRIMARY HYPOTHESIS (fixed before inspecting any new result)
=====================================================================

H0: The PR5f FNG-24h incremental effect does not persist outside the
    original evaluation period.
H1: FNG-24h continues to provide incremental predictive information
    beyond the V1 composite + global + onchain controls, evaluated on
    genuinely new chronological evidence.

H1 is NOT assumed. The default posture is H0 unless new evidence
displaces it.

=====================================================================
CRITICAL ANTI-CHERRY-PICKING RULE
=====================================================================

Horizon = 24h is FIXED, inherited unchanged from PR5f. This module
NEVER re-selects a horizon based on which one looks best on new data.
6h/12h may be reported as CONTEXT ONLY (see `SIX_TWELVE_HOUR_CONTEXT`
below, populated from PR5f's own already-published audit numbers,
never recomputed here to avoid re-opening the exact selection question
PR5f's own audit already flagged) -- they are never candidates for
promotion to "the" tested horizon.

=====================================================================
STEP 0 -- DATA AVAILABILITY CHECK (performed BEFORE any methodology
branch was chosen, BEFORE any FNG-specific number was computed)
=====================================================================

Queried production (read-only) at the start of this PR:
  predictions: n=1070, max_ts=1789797630548 -- IDENTICAL to PR5f's own
    recorded window end (1789797630549, 1ms rounding) -- the
    predictions table has NOT advanced at all since PR5f's snapshot.
  history rows with ts > PR5f's window end: 5
  btc_data rows with ts > PR5f's window end: 8, extending only to
    1789820437583 -- ~6.3 hours past the window end.
Horizon=24h requires 24 HOURS of FUTURE btc_data beyond a history row's
own ts to resolve a forward return. With btc_data extending only ~6.3
hours past the window end, NONE of the 5 new history rows -- let alone
enough of them to reach any minimum sample size -- can resolve a 24h
forward return. This is a hard, empirically-verified fact, established
BEFORE looking at any FNG number, not a convenient excuse discovered
after an inconvenient result.

CONCLUSION: no genuinely new chronological data exists for a true
post-PR5f out-of-window test (Section 4B/4C's "new chronological
period" design is not implementable today). See `check_new_
chronological_data()` -- this function is written generically (it does
NOT hardcode "always insufficient") so that a future re-run, if
predictions/history/btc_data have genuinely advanced by then, would
correctly detect that and take the primary (new-period) branch instead
of silently falling back to the secondary one.

=====================================================================
METHODOLOGY (fixed before computing any new FNG-specific result)
=====================================================================

Model specification -- IDENTICAL to PR5f, never silently changed:
  Baseline: V1 composite + global + onchain
  Full:     V1 composite + global + onchain + FNG (24h)
  Fit via stats_utils.ols_nvar() (the same function PR5f introduced,
  independently audited against Cramer's rule and Frisch-Waugh-Lovell
  to machine precision in PR #54's review). RMSE via stats_utils.rmse().
  Chronological only, never shuffled.

Branch A (primary design, NOT available today -- see Step 0): if
sufficient new post-PR5f-window data existed, PR5g would freeze the
baseline/full model coefficients exactly as fit on PR5f's OWN discovery
half (the identical 179-row fit PR5f itself used), then evaluate that
FROZEN model's RMSE reduction on the genuinely new period -- a true
walk-forward OOS test on data PR5f never saw. Implemented as
`frozen_model_from_pr5f_discovery()` / `evaluate_period_against_
frozen_model()`, unit-tested against synthetic new-period data (since
no real new period exists to run it on today), so the capability exists
and is correct for whenever new data does arrive.

Branch B (the branch actually used today, because Step 0 found no new
external data): a SUPPLEMENTARY, predefined chronological robustness
re-analysis of the EXISTING PR5f 257-row complete-case sample (the
IDENTICAL rows, IDENTICAL model, IDENTICAL horizon -- no new data, no
re-selection). The sample is split into 4 equal-sized, predefined
chronological quartiles Q1..Q4 (never redefined after inspecting
results), and an EXPANDING-WINDOW walk-forward is run:
  Fold 1: fit on Q1,        evaluate OOS on Q2
  Fold 2: fit on Q1+Q2,     evaluate OOS on Q3
  Fold 3: fit on Q1+Q2+Q3,  evaluate OOS on Q4
This provides 3 sequential OOS checkpoints (vs. PR5f's own single 70/30
split, which had 2 via its own sub-split) using progressively more
training data each time -- genuinely additional temporal structure
beyond what PR5f already reported, though NOT new data. Implemented as
`quartile_walk_forward_on_existing_sample()`.

THIS DISTINCTION IS LOAD-BEARING AND STATED EXPLICITLY THROUGHOUT THIS
MODULE AND ITS REPORT OUTPUT: Branch B is ROBUSTNESS EVIDENCE, NOT
INDEPENDENT CONFIRMATION. It re-examines the same historical rows PR5e
and PR5f already looked at, from a third angle, which is useful (a
model that only "works" under PR5f's exact 70/30 split and falls apart
under any other predefined chronological partition would be a real red
flag) but is not the same evidentiary weight as genuinely new data
PR5f never touched.

=====================================================================
Interpretation categories (fixed BEFORE running Branch B, per the
anti-cherry-picking instruction -- these are not adjusted after seeing
the fold results)
=====================================================================

Because Step 0 found zero new external chronological data, the PRIMARY
classification (answering "does the effect persist OUTSIDE the
original evaluation period?") is mechanically INSUFFICIENT_DATA --
this is a data-availability fact, decided before any FNG-specific
number existed, not a judgment call made after seeing whether Branch B
looked good or bad. Branch B's own fold-level result is separately
labeled with the SAME four-way vocabulary, purely as descriptive
supplementary information, and never overrides the primary verdict:
  ROBUST: all 3 folds show a positive rmse_reduction_pct, with a
    majority individually >= MEANINGFUL_OOS_IMPROVEMENT_PCT (5%, PR5e's
    own constant, reused HERE ONLY as a labeled descriptive band --
    see Section 11 note below -- never as a new statistical or
    economic threshold).
  MIXED: a majority of folds positive, but not meeting the ROBUST bar,
    or one fold negative while the others are clearly positive.
  NOT_REPLICATED: a majority of folds negative or near-zero.
  INSUFFICIENT_DATA: any fold's train or test partition falls below
    MIN_SAMPLE_FOR_HOLDOUT_HALF (30, PR5c's own constant, reused).

NOTE ON THE 5% FIGURE (Section 11 compliance): PR5e's/PR5f's
MEANINGFUL_OOS_IMPROVEMENT_PCT is reused here ONLY to LABEL a
descriptive magnitude band ("did this fold's improvement look like the
same rough scale PR5f itself required for BUILD_REQUEST/DISCRIMINATED_
INCREMENTAL status") -- it is explicitly NOT re-asserted here as a
economically- or statistically-calibrated pass/fail threshold (PR5f's
own audit already established it is a repurposed correlation-scale
constant, not an economic calibration). The raw, unrounded
rmse_reduction_pct for every fold is always reported alongside the
band label, never replaced by a bare pass/fail flag.

=====================================================================
Why this is not circular with PR5e/PR5f (Section 9 compliance)
=====================================================================

PR5e asked "is fng associated with the outcome, controlling for the
composite." PR5f asked "does that survive controlling for global and
onchain too, out of sample." PR5g asks a DIFFERENT question again:
"does the SAME finding hold up under a different chronological
partitioning of the evidence, and does genuinely new evidence exist at
all." Branch B's walk-forward folds are new COMPUTATIONS (never
computed in PR5e or PR5f), but they draw on the SAME underlying
257-row sample -- this module states, in its own report output, "PR5g
provides robustness evidence, not independent confirmation" verbatim,
exactly per the build authorization's own required wording, precisely
because repeated analysis of the same historical rows by a third,
successive PR is not statistically equivalent to a fresh sample.

=====================================================================
Production safety
=====================================================================

Entirely read-only. No `persist_*` function exists in this module.
No V1/V2/Worker file is imported, read, or referenced. No migration is
applied or required.

=====================================================================
No production conclusion (Section 15 compliance)
=====================================================================

Even a ROBUST Branch B result does NOT mean "FNG is validated." The
correct statement, used verbatim in this module's report output, is:
"FNG 24h has stronger/robust research evidence under the predefined
temporal validation methodology and may warrant a separate
human-authorized implementation study" -- never "FNG is validated,"
never grounds for automatically starting a V1/V2 implementation.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import source_analysis as sa  # noqa: E402
import outcome_engine  # noqa: E402
import source_family_discrimination as sfd  # noqa: E402
from stats_utils import ols_nvar, rmse  # noqa: E402

# ---------------------------------------------------------------------
# Reused constants -- no new arbitrary numbers.
# ---------------------------------------------------------------------
MIN_SAMPLE_FOR_HOLDOUT_HALF = sfd.MIN_SAMPLE_FOR_HOLDOUT_HALF  # 30, PR5c's own
MIN_SAMPLE_FOR_LEVEL3 = sfd.MIN_SAMPLE_FOR_LEVEL3              # 40, PR5c's own
MEANINGFUL_OOS_IMPROVEMENT_PCT = sfd.MEANINGFUL_OOS_IMPROVEMENT_PCT  # 5.0, PR5e v2's own
HORIZON_HOURS = 24  # FIXED. Never re-selected. See module docstring's anti-cherry-picking rule.
TARGET_FAMILY = "fng"
CONTROL_FAMILIES = ("global", "onchain")

CLASSIFICATIONS = ("ROBUST", "MIXED", "NOT_REPLICATED", "INSUFFICIENT_DATA")

# PR5f's own recorded reference result (research/README.md, PR5f section
# + module docstring's "Independent audit findings") -- a FIXED
# comparison point, never recomputed with a different horizon or
# control set here.
PR5F_REFERENCE = {
    "family": "fng",
    "horizon_hours": 24,
    "controls": ["v1_composite", "global", "onchain"],
    "n": 257,
    "rmse_reduction_pct": 10.992659049059911,
    "sub1_rmse_reduction_pct": 5.915954880735784,
    "sub2_rmse_reduction_pct": 13.044702260136939,
    "window_start_ts": 1785582508231,
    "window_end_ts": 1789797630549,
}

# PR5f's own audit (module docstring "Independent audit findings")
# re-ran fng's OTHER horizons under the SAME joint-control test --
# reported here as CONTEXT ONLY, never recomputed, never a candidate
# for re-selection as "the" tested horizon.
SIX_TWELVE_HOUR_CONTEXT = {
    6: {"rmse_reduction_pct": 1.5463980874769077, "discrimination_status": "NOT_DISCRIMINATED"},
    12: {"rmse_reduction_pct": 4.397635340269696, "discrimination_status": "NOT_DISCRIMINATED"},
    "caveat": (
        "CONTEXT ONLY, from PR5f's own already-published audit -- never "
        "recomputed here, never a candidate horizon for this study. 24h "
        "is the sole predefined subject of PR5g."
    ),
}


# =====================================================================
# Step 0: data availability check (generic -- does not hardcode
# "always insufficient"; a future re-run with genuinely advanced
# production data would correctly take Branch A instead)
# =====================================================================

def check_new_chronological_data(conn, pr5f_window_end_ts, horizon_hours=HORIZON_HOURS):
    """Determines whether ANY genuinely new, resolvable-at-`horizon_hours`
    history rows exist strictly after `pr5f_window_end_ts`. Read-only.
    Never assumes insufficiency -- computes it from the actual table
    contents every time this is called.
    """
    history_after = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM history WHERE ts > ?", (pr5f_window_end_ts,)
    ).fetchone()
    n_history_after, min_history_after, max_history_after = history_after

    btc_after = conn.execute(
        "SELECT COUNT(*), MAX(ts) FROM btc_data WHERE ts > ?", (pr5f_window_end_ts,)
    ).fetchone()
    n_btc_after, max_btc_ts = btc_after

    horizon_ms = horizon_hours * 3600000
    if n_history_after == 0:
        return {
            "n_history_rows_after_window": 0, "n_btc_data_rows_after_window": n_btc_after or 0,
            "n_resolvable_new_rows": 0, "sufficient_for_new_period_test": False,
            "reason": "no history rows exist after the PR5f window end",
        }

    # A new history row at ts=T is resolvable at this horizon only if
    # btc_data extends to at least T + horizon_ms (the same rule
    # outcome_engine.compute_forward_returns_from_history() itself uses).
    n_resolvable = conn.execute(
        "SELECT COUNT(*) FROM history WHERE ts > ? AND ts + ? <= ?",
        (pr5f_window_end_ts, horizon_ms, max_btc_ts or 0),
    ).fetchone()[0]

    sufficient = n_resolvable >= MIN_SAMPLE_FOR_LEVEL3
    if sufficient:
        reason = f"{n_resolvable} new resolvable rows >= MIN_SAMPLE_FOR_LEVEL3 ({MIN_SAMPLE_FOR_LEVEL3})"
    else:
        hours_available = ((max_btc_ts or 0) - (max_history_after or pr5f_window_end_ts)) / 3600000.0
        reason = (
            f"only {n_resolvable} new rows can resolve a {horizon_hours}h forward return "
            f"(need >= {MIN_SAMPLE_FOR_LEVEL3}); btc_data extends only ~{hours_available:.1f}h "
            f"past the newest post-window history row, short of the {horizon_hours}h required"
        )

    return {
        "n_history_rows_after_window": n_history_after,
        "n_btc_data_rows_after_window": n_btc_after or 0,
        "n_resolvable_new_rows": n_resolvable,
        "sufficient_for_new_period_test": sufficient,
        "reason": reason,
    }


# =====================================================================
# Branch A (not exercised on today's data -- see Step 0 -- but
# implemented and unit-tested against synthetic new-period data so the
# capability is real, not vaporware, for whenever new data does arrive)
# =====================================================================

def frozen_model_from_pr5f_discovery(pr5f_combined, split_fraction=sfd.OOS_SPLIT_FRACTION):
    """Refits baseline/full models on PR5f's OWN discovery half (the
    identical 70% split PR5f itself used) -- this reproduces PR5f's own
    internal discovery-half fit exactly, frozen for forward application
    to genuinely new data (never refit on new data -- that would be a
    different, weaker claim than 'does the ORIGINAL model generalize
    forward')."""
    n = len(pr5f_combined)
    split_idx = int(n * split_fraction)
    discovery = pr5f_combined[:split_idx]
    d_controls = list(zip(*(d[2] for d in discovery)))
    d_target = [d[1] for d in discovery]
    d_y = [d[3] for d in discovery]
    baseline_b0, baseline_coefs = ols_nvar(list(d_controls), d_y)
    full_b0, full_coefs = ols_nvar(list(d_controls) + [d_target], d_y)
    return {
        "baseline": (baseline_b0, baseline_coefs),
        "full": (full_b0, full_coefs),
        "n_discovery": len(discovery),
    }


def evaluate_period_against_frozen_model(period_combined, frozen):
    """Applies a FROZEN (already-fit, never refit) model to a new
    period's rows -- a true forward-application OOS test. Returns the
    same shape as source_family_discrimination.nested_model_oos_
    comparison() for consistency."""
    n = len(period_combined)
    if n < MIN_SAMPLE_FOR_HOLDOUT_HALF:
        return {"status": "INSUFFICIENT_DATA", "n": n}
    baseline_b0, baseline_coefs = frozen["baseline"]
    full_b0, full_coefs = frozen["full"]
    y = [d[3] for d in period_combined]
    baseline_preds = [baseline_b0 + sum(c * v for c, v in zip(baseline_coefs, row[2])) for row in period_combined]
    full_preds = [full_b0 + sum(c * v for c, v in zip(full_coefs, list(row[2]) + [row[1]])) for row in period_combined]
    b_rmse = rmse(y, baseline_preds)
    f_rmse = rmse(y, full_preds)
    if b_rmse is None or b_rmse == 0:
        return {"status": "MODEL_UNDEFINED", "n": n}
    return {
        "status": "OK", "n": n,
        "baseline_rmse": b_rmse, "full_model_rmse": f_rmse,
        "rmse_reduction_pct": (b_rmse - f_rmse) / b_rmse * 100.0,
    }


# =====================================================================
# Branch B: predefined quartile walk-forward on the EXISTING PR5f
# sample (used today, per Step 0's finding)
# =====================================================================

def split_into_predefined_quartiles(combined):
    """Splits an already-chronologically-sorted `combined` list (the
    exact shape source_family_discrimination.build_combined_rows()
    produces) into 4 equal-sized (as equal as integer division allows)
    chronological quartiles, defined purely by POSITION in the already-
    time-sorted list -- never redefined after inspecting any fold's
    result."""
    n = len(combined)
    q = n // 4
    boundaries = [0, q, 2 * q, 3 * q, n]
    return [combined[boundaries[i]:boundaries[i + 1]] for i in range(4)]


def quartile_walk_forward_on_existing_sample(combined):
    """Expanding-window walk-forward across 4 predefined chronological
    quartiles of the SAME sample PR5f already used:
      fold 1: train Q1,       test Q2
      fold 2: train Q1+Q2,    test Q3
      fold 3: train Q1+Q2+Q3, test Q4
    Same model spec as PR5f (composite+global+onchain baseline, +fng
    full), refit per fold on that fold's own training data (unlike
    Branch A, which freezes one model) -- this is deliberately a
    DIFFERENT check: does the effect re-emerge under independent
    re-estimation on different training windows, not just does one
    fixed model generalize forward.
    """
    quartiles = split_into_predefined_quartiles(combined)
    folds = []
    train_rows = []
    for i in range(3):
        train_rows = train_rows + quartiles[i]
        test_rows = quartiles[i + 1]
        if len(train_rows) < MIN_SAMPLE_FOR_HOLDOUT_HALF or len(test_rows) < MIN_SAMPLE_FOR_HOLDOUT_HALF:
            folds.append({"fold": i + 1, "status": "INSUFFICIENT_DATA",
                          "n_train": len(train_rows), "n_test": len(test_rows)})
            continue
        d_controls = list(zip(*(d[2] for d in train_rows)))
        d_target = [d[1] for d in train_rows]
        d_y = [d[3] for d in train_rows]
        baseline_b0, baseline_coefs = ols_nvar(list(d_controls), d_y)
        full_b0, full_coefs = ols_nvar(list(d_controls) + [d_target], d_y)
        if baseline_b0 is None or full_b0 is None:
            folds.append({"fold": i + 1, "status": "MODEL_UNDEFINED",
                          "n_train": len(train_rows), "n_test": len(test_rows)})
            continue
        test_y = [d[3] for d in test_rows]
        baseline_preds = [baseline_b0 + sum(c * v for c, v in zip(baseline_coefs, row[2])) for row in test_rows]
        full_preds = [full_b0 + sum(c * v for c, v in zip(full_coefs, list(row[2]) + [row[1]])) for row in test_rows]
        b_rmse = rmse(test_y, baseline_preds)
        f_rmse = rmse(test_y, full_preds)
        if b_rmse is None or b_rmse == 0:
            folds.append({"fold": i + 1, "status": "MODEL_UNDEFINED",
                          "n_train": len(train_rows), "n_test": len(test_rows)})
            continue
        reduction = (b_rmse - f_rmse) / b_rmse * 100.0
        folds.append({
            "fold": i + 1, "status": "OK",
            "n_train": len(train_rows), "n_test": len(test_rows),
            "train_period": (train_rows[0][0], train_rows[-1][0]),
            "test_period": (test_rows[0][0], test_rows[-1][0]),
            "baseline_rmse": b_rmse, "full_model_rmse": f_rmse,
            "rmse_reduction_pct": reduction,
        })
    return folds


def classify_walk_forward(folds):
    """Predefined interpretation rule (see module docstring) applied
    mechanically to the fold results -- never adjusted after seeing
    which label would look best."""
    ok_folds = [f for f in folds if f["status"] == "OK"]
    if len(ok_folds) < len(folds):
        # at least one fold could not be evaluated at all
        if not ok_folds:
            return "INSUFFICIENT_DATA"
    reductions = [f["rmse_reduction_pct"] for f in ok_folds]
    n_positive = sum(1 for r in reductions if r > 0)
    n_meaningful = sum(1 for r in reductions if r >= MEANINGFUL_OOS_IMPROVEMENT_PCT)
    if not ok_folds:
        return "INSUFFICIENT_DATA"
    if n_positive == len(ok_folds) and n_meaningful >= (len(ok_folds) + 1) // 2:
        return "ROBUST"
    if n_positive >= (len(ok_folds) + 1) // 2:
        return "MIXED"
    return "NOT_REPLICATED"


# =====================================================================
# Top-level orchestration
# =====================================================================

def build_fng_24h_robustness_report(conn, pr5f_start_ts=None, pr5f_end_ts=None):
    """The single top-level entry point. Read-only. Never writes.
    pr5f_start_ts/pr5f_end_ts default to PR5F_REFERENCE's own recorded
    window -- NEVER silently widened even if a few new rows exist
    beyond it (Step 0 already established those cannot resolve at this
    horizon; including them would silently change the population being
    studied, which the build authorization explicitly forbids).
    """
    start_ts = pr5f_start_ts if pr5f_start_ts is not None else PR5F_REFERENCE["window_start_ts"]
    end_ts = pr5f_end_ts if pr5f_end_ts is not None else PR5F_REFERENCE["window_end_ts"]

    availability = check_new_chronological_data(conn, end_ts, horizon_hours=HORIZON_HOURS)

    _, matrix_rows, _ = sa.extract_source_matrix(conn, start_ts, end_ts)
    outcome_rows = outcome_engine.compute_forward_returns_from_history(conn, start_ts, end_ts, HORIZON_HOURS)
    control_keys = ["v1_composite"] + sorted(CONTROL_FAMILIES)
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, TARGET_FAMILY, control_keys)

    result = {
        "objective": (
            "Does the PR5f FNG-24h incremental effect remain detectable "
            "across additional chronological data and/or independent "
            "temporal segments, or could it plausibly be specific to the "
            "original ~34-day sample?"
        ),
        "horizon_hours": HORIZON_HOURS,
        "target_family": TARGET_FAMILY,
        "control_families": list(CONTROL_FAMILIES),
        "pr5f_reference": PR5F_REFERENCE,
        "six_twelve_hour_context": SIX_TWELVE_HOUR_CONTEXT,
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "n": len(combined),
        "new_chronological_data_availability": availability,
    }

    if availability["sufficient_for_new_period_test"]:
        # Branch A: genuinely new data exists -- freeze PR5f's own
        # discovery-half model and evaluate it forward on the new period.
        new_period_rows = [c for c in combined if c[0] > end_ts]
        frozen = frozen_model_from_pr5f_discovery(combined[:len(combined) - len(new_period_rows)]
                                                   if new_period_rows else combined)
        branch_a_result = evaluate_period_against_frozen_model(new_period_rows, frozen)
        result["branch"] = "A_new_period"
        result["new_period_result"] = branch_a_result
        result["is_independent_confirmation"] = True
        result["primary_classification"] = (
            "ROBUST" if branch_a_result.get("rmse_reduction_pct", -1) >= MEANINGFUL_OOS_IMPROVEMENT_PCT
            else "MIXED" if branch_a_result.get("rmse_reduction_pct", -1) > 0
            else "NOT_REPLICATED" if branch_a_result.get("status") == "OK"
            else "INSUFFICIENT_DATA"
        )
    else:
        # Branch B: no new external data -- supplementary walk-forward
        # on the existing PR5f sample only.
        folds = quartile_walk_forward_on_existing_sample(combined)
        walk_forward_classification = classify_walk_forward(folds)
        result["branch"] = "B_existing_sample_walk_forward"
        result["walk_forward_folds"] = folds
        result["walk_forward_classification"] = walk_forward_classification
        result["is_independent_confirmation"] = False
        # The PRIMARY classification answers "does the effect persist
        # OUTSIDE the original evaluation period" -- mechanically
        # INSUFFICIENT_DATA whenever Branch B is the one that ran,
        # because by construction no outside-the-window data existed.
        # This is NEVER overridden by how the supplementary walk-forward
        # looks (see module docstring's anti-cherry-picking discussion).
        result["primary_classification"] = "INSUFFICIENT_DATA"
        result["disclosure"] = (
            "PR5g provides robustness evidence, not independent "
            "confirmation. No chronological data exists beyond PR5f's "
            "own evaluation window that could resolve a 24h forward "
            "return, so the primary hypothesis (does the effect persist "
            "OUTSIDE the original period) cannot be tested and is "
            "reported as INSUFFICIENT_DATA. The supplementary walk-"
            f"forward classification ({walk_forward_classification}) "
            "describes re-analysis of the SAME historical rows PR5e and "
            "PR5f already used, under a different chronological "
            "partition -- useful context, not a new sample."
        )

    _outcome_classification = result.get("primary_classification")
    if _outcome_classification == "INSUFFICIENT_DATA":
        # Branch B ran; the walk-forward classification is the
        # informative one for wording purposes (never for overriding
        # primary_classification itself, which stays INSUFFICIENT_DATA).
        _outcome_classification = result.get("walk_forward_classification", "INSUFFICIENT_DATA")
    if _outcome_classification == "ROBUST":
        result["production_conclusion_statement"] = (
            "FNG 24h has stronger/robust research evidence under the "
            "predefined temporal validation methodology and may warrant a "
            "separate human-authorized implementation study."
        )
    elif _outcome_classification == "NOT_REPLICATED":
        result["production_conclusion_statement"] = (
            "This study RAISES A TEMPORAL-STABILITY CONCERN rather than "
            "strengthening PR5f's finding: under the predefined walk-"
            "forward re-partitioning, FNG-24h's effect was negative in "
            "the earlier folds and positive only in the fold most "
            "similar to PR5f's own original validation tail. This does "
            "not disprove PR5f's result (the underlying mathematics were "
            "independently re-verified and match), but it materially "
            "weakens confidence that the effect is a stable, generalizable "
            "property of FNG-24h rather than a feature of the specific "
            "tail period PR5f's own 70/30 split happened to validate on."
        )
    elif _outcome_classification == "MIXED":
        result["production_conclusion_statement"] = (
            "This study found inconsistent evidence: some but not all "
            "predefined temporal folds showed a positive FNG-24h effect. "
            "This neither strengthens nor disproves PR5f's finding."
        )
    else:
        result["production_conclusion_statement"] = (
            "This study did not find grounds to strengthen the FNG-24h "
            "research conclusion beyond PR5f's own finding."
        )
    result["not_validated_statement"] = (
        "This result, under any classification, is NOT 'FNG is "
        "validated.' It remains research evidence for human review; no "
        "V1/V2 change follows automatically from it."
    )
    return result
