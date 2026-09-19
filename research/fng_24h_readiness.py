"""
research/fng_24h_readiness.py

Read-only data-readiness check for FNG-24h PR5g Branch A.

=====================================================================
Scope (read this before touching anything below)
=====================================================================

This module answers exactly ONE question, mechanically:

    "Is there now enough genuinely new, resolvable 24h data after the
    frozen PR5f reference window to run PR5g's already-defined Branch A
    (research/fng_24h_robustness.py: frozen_model_from_pr5f_discovery +
    evaluate_period_against_frozen_model)?"

It does NOT:
  - compute or estimate any FNG performance number (RMSE, RMSE
    reduction %, correlation, or otherwise)
  - classify FNG as ROBUST / MIXED / NOT_REPLICATED / anything else
  - rank FNG against global / onchain / the V1 composite
  - select a different horizon
  - fit, refit, freeze, or otherwise touch any model
  - write to any table, apply a migration, or touch V1/V2/Worker

It is a strict subset of PR5g's own already-audited logic, reused
verbatim wherever possible -- no new statistical method is introduced:

  - `fng_24h_robustness.PR5F_REFERENCE["window_end_ts"]` is the single
    frozen reference window end already recorded by the merged PR5g
    module -- never re-hardcoded here, so it can never silently drift
    from PR5g's own recorded value.
  - `fng_24h_robustness.check_new_chronological_data()` is called
    UNMODIFIED for the raw candidate-row count, for the existing
    MIN_SAMPLE_FOR_LEVEL3=40 threshold (PR5c's own constant, threaded
    through unchanged), and for its own aggregate timing-resolvable
    estimate (reported here as a diagnostic field only -- see below).
  - `source_analysis.extract_source_matrix()` is reused UNMODIFIED,
    restricted to the post-window range only, to obtain each candidate
    row's parsed source values (fng/global/onchain/v1_composite) with
    all of that module's existing safety behavior (missing key -> None,
    never 0; malformed JSON handled; nothing imputed).

IMPORTANT -- why this module does NOT use
`outcome_engine.compute_forward_returns_from_history()`'s own
`outcome_status == "RESOLVED"` flag as its resolution criterion, even
though that flag exists and is reused elsewhere in this project:
that flag is an AS-OF proxy -- it marks a row RESOLVED as soon as ANY
later `btc_data` point exists at all (even one only minutes past the
anchor), which is the intentional, documented behavior for its own
callers (PR5b/PR5c's correlation-style analyses, which accept the
nearest available future price as a usable approximation). It does NOT
mean "a genuine price observation exists at the full 24h horizon."
Using it here would make this readiness gate report READY based on
data that has not actually reached 24h out -- exactly the raw-
timestamp-shaped mistake this module exists to prevent. Instead, this
module applies `check_new_chronological_data()`'s own STRICT per-row
rule directly (`ts + horizon_ms <= max(btc_data.ts)`, the same
arithmetic already used by that function's own aggregate count) to
each candidate row individually, alongside that row's parsed source
completeness -- so a row only ever counts as "new_resolvable" here
when both a genuine 24h-out price point AND every required source
value are actually present.

Running this module never mutates and never re-analyzes the existing
257-row PR5f/PR5g sample. It only ever looks at rows strictly after
PR5F_REFERENCE's frozen window end (`ts > reference_window_end`,
matching `check_new_chronological_data`'s own strict-inequality rule).

=====================================================================
Output contract
=====================================================================

`build_readiness_record()` returns a plain dict with a
`status` of exactly `"WAITING_FOR_NEW_24H_DATA"` or
`"READY_FOR_PR5G_BRANCH_A"`. Reaching `READY_FOR_PR5G_BRANCH_A` does
NOT run Branch A and does NOT authorize running it -- it only reports
that the data preconditions Branch A already requires are now met. Any
actual FNG re-analysis remains a separate, explicitly authorized step.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))
import source_analysis as sa  # noqa: E402
import fng_24h_robustness as fr  # noqa: E402

# ---------------------------------------------------------------------
# Reused constants -- no new arbitrary numbers, no new thresholds.
# ---------------------------------------------------------------------
REFERENCE_WINDOW_END_TS = fr.PR5F_REFERENCE["window_end_ts"]  # frozen, from PR5g's own recorded reference
REQUIRED_MINIMUM_ROWS = fr.MIN_SAMPLE_FOR_LEVEL3               # 40, PR5c's own constant, reused unchanged
HORIZON_HOURS = fr.HORIZON_HOURS                                # 24, fixed, inherited unchanged from PR5f/PR5g
TARGET_FAMILY = fr.TARGET_FAMILY                                # "fng"
CONTROL_FAMILIES = fr.CONTROL_FAMILIES                          # ("global", "onchain")

READINESS_STATUSES = ("WAITING_FOR_NEW_24H_DATA", "READY_FOR_PR5G_BRANCH_A")


def build_readiness_record(conn, reference_window_end_ts=None, extraction_timestamp=None):
    """Read-only. Returns a deterministic readiness record (see module
    docstring's Output contract). Never writes, never fits a model,
    never computes an FNG performance number.

    `reference_window_end_ts` / `extraction_timestamp` are injectable
    only for deterministic testing -- production callers always default
    to PR5g's own frozen reference and the real current time.
    """
    window_end = reference_window_end_ts if reference_window_end_ts is not None else REFERENCE_WINDOW_END_TS
    extraction_ts = extraction_timestamp if extraction_timestamp is not None else int(time.time() * 1000)
    horizon_ms = HORIZON_HOURS * 3600000

    pred_max = conn.execute("SELECT MAX(ts) FROM predictions").fetchone()[0]
    hist_max = conn.execute("SELECT MAX(ts) FROM history").fetchone()[0]
    btc_max = conn.execute("SELECT MAX(ts) FROM btc_data").fetchone()[0]

    # Reuse PR5g's own generic availability check UNMODIFIED -- this is
    # the same function PR5g's merged report calls, and the same rule
    # ("ts > window_end") that guarantees the existing 257-row PR5f/PR5g
    # sample itself can never be double-counted as "new" here.
    availability = fr.check_new_chronological_data(conn, window_end, horizon_hours=HORIZON_HOURS)
    n_candidate = availability["n_history_rows_after_window"]
    n_resolvable_by_timestamp_only = availability["n_resolvable_new_rows"]

    data_gap_ms = None
    if hist_max is not None:
        # How much further btc_data still needs to extend before the
        # newest post-window history row can resolve a 24h forward
        # return -- same arithmetic already narrated in
        # check_new_chronological_data's own "reason" string, exposed
        # here as a plain number.
        data_gap_ms = max(0, (hist_max + horizon_ms) - (btc_max or 0))

    required_sources = [TARGET_FAMILY] + list(CONTROL_FAMILIES)
    resolvable_ts_list = []
    if n_candidate > 0:
        # Restricted strictly to the post-window range -- this NEVER
        # re-reads or re-counts the frozen PR5f/PR5g sample itself.
        extraction_end_ts = max(hist_max, window_end + 1)
        _, matrix_rows, _ = sa.extract_source_matrix(conn, window_end + 1, extraction_end_ts)
        for row in matrix_rows:
            has_all_sources = row["v1_composite"] is not None and all(
                row["sources"].get(key) is not None for key in required_sources
            )
            # Same strict rule check_new_chronological_data() uses in
            # aggregate, applied per row -- NOT outcome_engine's looser
            # as-of "RESOLVED" flag (see module docstring).
            is_resolvable_at_horizon = btc_max is not None and (row["ts"] + horizon_ms) <= btc_max
            if has_all_sources and is_resolvable_at_horizon:
                resolvable_ts_list.append(row["ts"])

    n_resolvable = len(resolvable_ts_list)
    n_incomplete_or_unresolved = n_candidate - n_resolvable
    sufficient = n_resolvable >= REQUIRED_MINIMUM_ROWS
    status = "READY_FOR_PR5G_BRANCH_A" if sufficient else "WAITING_FOR_NEW_24H_DATA"

    return {
        "reference_window_end": window_end,
        "current_prediction_max_ts": pred_max,
        "current_history_max_ts": hist_max,
        "current_btc_data_max_ts": btc_max,
        "new_candidate_rows": n_candidate,
        "new_resolvable_rows": n_resolvable,
        "new_resolvable_rows_by_timestamp_only": n_resolvable_by_timestamp_only,
        "required_minimum_rows": REQUIRED_MINIMUM_ROWS,
        "sufficient_for_branch_a": sufficient,
        "data_gap_ms": data_gap_ms,
        "extraction_timestamp": extraction_ts,
        "earliest_new_resolvable_ts": resolvable_ts_list[0] if resolvable_ts_list else None,
        "latest_new_resolvable_ts": resolvable_ts_list[-1] if resolvable_ts_list else None,
        "n_incomplete_or_unresolved_candidate_rows": n_incomplete_or_unresolved,
        "source_families_required_for_branch_a": [TARGET_FAMILY] + list(CONTROL_FAMILIES),
        "horizon_hours": HORIZON_HOURS,
        "status": status,
        "availability_detail": availability,
        "not_a_performance_result_statement": (
            "This record answers ONLY whether enough new data exists to "
            "run PR5g's already-defined Branch A. It contains no FNG "
            "performance number, no classification, and authorizes no "
            "analysis or implementation by itself. Running Branch A "
            "requires a separate, explicit authorization even when "
            "status is READY_FOR_PR5G_BRANCH_A."
        ),
    }
