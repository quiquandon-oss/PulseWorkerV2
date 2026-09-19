"""
PR5d-followup: error taxonomy overlap / suppression analysis.

Objective (per follow-up build authorization): measure, WITHOUT
changing it, the analytical effect of PR5d's fixed classification
precedence. PR5d's classify_prediction() reports exactly one
error_type ("winner_label") per prediction; real production data shows
a prediction can genuinely satisfy multiple candidate causes at once.
This module answers: "which taxonomy categories actually matched
simultaneously, and how often does the winner-label precedence
suppress the others?" -- a measurement, not an intervention.

STRICT SCOPE: read-only, local, deterministic aggregation over
already-fetched PR5d data. No V1/V2/Worker change. No priority-chain
change. No new thresholds. No new production query pattern beyond what
PR5d's own classify_all() already runs. No writes of any kind -- this
module has no persistence function at all (unlike PR5c/PR5d, nothing
here was asked to write to research_analyses, so nothing does).

=====================================================================
Reuse, not duplication (Section: "before creating new ones")
=====================================================================

- classify_prediction()/classify_all() (research/error_classification.py,
  UNCHANGED) are called exactly as PR5d itself calls them. winner_label
  below is copied verbatim from their own error_type output -- this
  module never recomputes, second-guesses, or overrides a winner label.
- contributing_signals (also PR5d's own, unchanged output) already
  carries the six independent match-condition booleans/counts this
  analysis needs (large_move_events_in_window,
  regime_reversal_events_in_window, volatility_expansion_events_in_window,
  v1_btc_divergence_events_in_window, technical_sentiment_conflict,
  v1_staleness_gap_exceeds_proposed_threshold) for every row
  classify_prediction() itself evaluates them for. This module reads
  those fields directly -- it does not re-run event-window overlap
  checks, re-derive the empirical thresholds, or re-implement any part
  of the classification rule.
- breakdown_by_error_type_only() (PR5d's own, unchanged) is reused
  verbatim for the winner-label distribution (Section 6A).

=====================================================================
Scope: exactly the six categories in PR5d's reviewed precedence chain
=====================================================================

UNEXPECTED_SHOCK, REGIME_CHANGE, MISSING_EVENT, MISLEADING_SENTIMENT,
TECHNICAL_SENTIMENT_CONFLICT, STALE_SENTIMENT each have their own,
independently-computed match condition inside classify_prediction()
(one of contributing_signals's six fields). WRONG_DIRECTION,
CORRECT_DIRECTION_WRONG_MAGNITUDE, and INSUFFICIENT_INFORMATION are
PR5d's residual/fallback OUTCOMES -- WRONG_DIRECTION's own "condition"
is simply "none of the six more specific categories matched AND
direction was wrong," not an independently-checkable signal with its
own contributing_signals entry. They are therefore excluded from the
co-occurrence/suppression measurement (matching the follow-up build
authorization's own worked example, which never lists WRONG_DIRECTION
alongside a winner even when the winner is itself a wrong-direction
category) -- though the winner-label distribution (Section 6A) still
reports them for completeness, since that reuses PR5d's own unchanged
breakdown_by_error_type_only().

Underlying-event-category naming note: PR3's own event categories
(LARGE_MOVE, REGIME_REVERSAL, VOLATILITY_EXPANSION, V1_BTC_DIVERGENCE)
map ONE-TO-ONE, by construction of PR5d's own classify_prediction(), to
four of these six taxonomy categories (UNEXPECTED_SHOCK, REGIME_CHANGE,
MISSING_EVENT, MISLEADING_SENTIMENT respectively). "LARGE_MOVE +
REGIME_CHANGE" and "UNEXPECTED_SHOCK + REGIME_CHANGE" are therefore the
literal same measurement in this implementation, not two independent
overlaps -- reported once, with this mapping stated explicitly, rather
than computed twice.

=====================================================================
Rows without V1 context: NOT_EVALUABLE, never assumed "no match"
=====================================================================

classify_prediction() returns BEFORE computing any of the six
contributing_signals fields when a prediction has no V1 context at all
(the INSUFFICIENT_INFORMATION population) or is UNRESOLVED. This
module treats those rows as NOT_EVALUABLE for every one of the six
categories -- never as a false/no-match value -- because inventing a
match/no-match verdict for something PR5d's own classifier never
examined would misrepresent, not measure, its actual behavior. The
evaluable population size (n_evaluable) is reported alongside every
statistic so this exclusion is never silent.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import error_classification as ec  # noqa: E402

OVERLAP_CATEGORIES = (
    "UNEXPECTED_SHOCK",
    "REGIME_CHANGE",
    "MISSING_EVENT",
    "MISLEADING_SENTIMENT",
    "TECHNICAL_SENTIMENT_CONFLICT",
    "STALE_SENTIMENT",
)

# Mirrors classify_prediction()'s own if/elif order exactly. Proven
# (not just asserted) equal to the actual source order by
# test_error_overlap_analysis.py::test_precedence_order_matches_classify_prediction_source.
PRECEDENCE_ORDER = OVERLAP_CATEGORIES

# The underlying PR3 event category each taxonomy category's condition
# is a direct count of, per classify_prediction()'s own
# contributing_signals -- see module docstring's naming note.
_COUNT_SIGNAL_KEY_BY_CATEGORY = {
    "UNEXPECTED_SHOCK": "large_move_events_in_window",
    "REGIME_CHANGE": "regime_reversal_events_in_window",
    "MISSING_EVENT": "volatility_expansion_events_in_window",
    "MISLEADING_SENTIMENT": "v1_btc_divergence_events_in_window",
}
_BOOL_SIGNAL_KEY_BY_CATEGORY = {
    "TECHNICAL_SENTIMENT_CONFLICT": "technical_sentiment_conflict",
    "STALE_SENTIMENT": "v1_staleness_gap_exceeds_proposed_threshold",
}

# The underlying PR3/PR5d event-category name each taxonomy category's
# match condition is literally counting (see module docstring).
UNDERLYING_EVENT_CATEGORY = {
    "UNEXPECTED_SHOCK": "LARGE_MOVE",
    "REGIME_CHANGE": "REGIME_REVERSAL",
    "MISSING_EVENT": "VOLATILITY_EXPANSION",
    "MISLEADING_SENTIMENT": "V1_BTC_DIVERGENCE",
}

# Exactly the pairs the follow-up build authorization asks to be
# reported explicitly (Section 5). UNEXPECTED_SHOCK stands in for
# "LARGE_MOVE" per the 1:1 mapping documented above -- reported once.
SPECIFIC_REVIEW_PAIRS = (
    ("UNEXPECTED_SHOCK", "REGIME_CHANGE"),          # == LARGE_MOVE + REGIME_CHANGE
    ("UNEXPECTED_SHOCK", "MISLEADING_SENTIMENT"),   # == LARGE_MOVE + MISLEADING_SENTIMENT
    ("REGIME_CHANGE", "MISSING_EVENT"),
    ("MISLEADING_SENTIMENT", "TECHNICAL_SENTIMENT_CONFLICT"),
    ("STALE_SENTIMENT", "TECHNICAL_SENTIMENT_CONFLICT"),
)


def matched_categories_for_classification(classification):
    """Given ONE classify_prediction() result (unchanged, reused as-is),
    returns (matched, evaluable):
      - evaluable=False (matched=[]) when classify_prediction() never
        computed the six signals at all for this row (UNRESOLVED, or
        RESOLVED with no V1 context -- contributing_signals is then
        either {} or {"reason": "no_v1_context_at_prediction_time"}).
      - evaluable=True otherwise, with matched containing every
        OVERLAP_CATEGORIES entry whose own contributing_signals
        condition was true for this row, independent of winner_label.
    """
    if classification.get("status") != "RESOLVED":
        return [], False
    signals = classification.get("contributing_signals") or {}
    if "reason" in signals:
        return [], False

    matched = []
    for category, key in _COUNT_SIGNAL_KEY_BY_CATEGORY.items():
        if signals.get(key, 0) > 0:
            matched.append(category)
    for category, key in _BOOL_SIGNAL_KEY_BY_CATEGORY.items():
        if signals.get(key) is True:
            matched.append(category)
    return matched, True


def build_overlap_records(classifications):
    """One record per classify_prediction() result, same order, 1:1.
    winner_label is copied verbatim from error_type -- never re-derived.
    """
    records = []
    for c in classifications:
        matched, evaluable = matched_categories_for_classification(c)
        records.append({
            "winner_label": c.get("error_type"),
            "status": c.get("status"),
            "all_matched_categories": matched,
            "evaluable": evaluable,
        })
    return records


def co_occurrence_matrix(records):
    """Deterministic category x category matrix over EVALUABLE records
    only. Symmetric for raw counts by construction: count_both for
    (A, B) is computed from the same per-row matched-set membership
    test as (B, A), so matrix[(A,B)]["count_both"] ==
    matrix[(B,A)]["count_both"] always (see
    test_co_occurrence_matrix_is_symmetric).
    """
    evaluable = [r for r in records if r["evaluable"]]
    n_evaluable = len(evaluable)
    single_counts = {c: 0 for c in OVERLAP_CATEGORIES}
    pair_counts = {}
    for r in evaluable:
        matched_set = set(r["all_matched_categories"])
        for c in matched_set:
            single_counts[c] += 1
        for a in matched_set:
            for b in matched_set:
                key = (a, b)
                pair_counts[key] = pair_counts.get(key, 0) + 1

    matrix = {}
    for a in OVERLAP_CATEGORIES:
        for b in OVERLAP_CATEGORIES:
            count_a = single_counts[a]
            count_b = single_counts[b]
            count_both = pair_counts.get((a, b), 0)
            matrix[(a, b)] = {
                "count_a": count_a,
                "count_b": count_b,
                "count_both": count_both,
                "pct_of_a_containing_b": (count_both / count_a) if count_a > 0 else None,
                "pct_of_b_containing_a": (count_both / count_b) if count_b > 0 else None,
            }
    return {"n_evaluable": n_evaluable, "matrix": matrix, "single_counts": single_counts}


def suppression_analysis(records):
    """For each OVERLAP_CATEGORIES entry: matched (evaluable rows whose
    condition was true), winner (of those, how many also won), and
    suppressed (matched but a DIFFERENT category won), with a
    suppressed_by breakdown of exactly which winner suppressed it.

    By construction, matched == winner + suppressed for every category
    (every matched, evaluable row is partitioned into exactly one of
    "this category won" or "something else won") -- see
    test_suppression_counts_reconcile_with_matched_counts.
    """
    result = {}
    for category in OVERLAP_CATEGORIES:
        matched_count = 0
        winner_count = 0
        suppressed_count = 0
        suppressed_by = {}
        for r in records:
            if not r["evaluable"] or category not in r["all_matched_categories"]:
                continue
            matched_count += 1
            if r["winner_label"] == category:
                winner_count += 1
            else:
                suppressed_count += 1
                winner = r["winner_label"]
                suppressed_by[winner] = suppressed_by.get(winner, 0) + 1
        result[category] = {
            "matched": matched_count,
            "winner": winner_count,
            "suppressed": suppressed_count,
            "suppression_rate": (suppressed_count / matched_count) if matched_count > 0 else None,
            "suppressed_by_winner": suppressed_by,
        }
    return result


def matched_category_distribution(records):
    """Section 6B: category -> how many evaluable rows independently
    matched it, regardless of who won. A DIFFERENT question from the
    winner distribution (Section 6A, PR5d's own breakdown_by_error_type_only)
    -- the two must never be combined into one statistic."""
    counts = {c: 0 for c in OVERLAP_CATEGORIES}
    n_evaluable = 0
    for r in records:
        if not r["evaluable"]:
            continue
        n_evaluable += 1
        for c in r["all_matched_categories"]:
            counts[c] += 1
    return {"matched_counts": counts, "n_evaluable": n_evaluable}


def specific_overlap_report(matrix):
    """Section 5: exactly the pairs the build authorization names."""
    return {
        f"{a}+{b}": matrix[(a, b)]
        for a, b in SPECIFIC_REVIEW_PAIRS
    }


def explain_absent_winners(suppression, categories=("REGIME_CHANGE", "MISLEADING_SENTIMENT")):
    """Directly answers: is a category's absence (or rarity) as a
    winner explained by higher-priority overlap? fully_suppressed=True
    means matched at least once but never won -- every one of its
    matches was outranked by something else, traceable via
    suppressed_by_winner."""
    explanation = {}
    for category in categories:
        s = suppression[category]
        explanation[category] = {
            "matched": s["matched"],
            "winner": s["winner"],
            "suppressed": s["suppressed"],
            "fully_suppressed": s["matched"] > 0 and s["winner"] == 0,
            "suppressed_by_winner": s["suppressed_by_winner"],
        }
    return explanation


def build_overlap_report(conn, start_ts, end_ts, horizons=ec.SUPPORTED_HORIZONS, coin=ec.SUPPORTED_COIN):
    """The single entry point. Read-only. Never writes -- this module
    has no persistence function at all. Reuses ec.classify_all() for
    every horizon separately (no cross-horizon pooling, matching PR5d's
    own multi-horizon isolation).
    """
    if coin != ec.SUPPORTED_COIN:
        raise ValueError(f"only {ec.SUPPORTED_COIN} is supported; got {coin!r}")

    per_horizon = {}
    for horizon_hours in horizons:
        result = ec.classify_all(conn, horizon_hours, start_ts, end_ts)
        rows, classifications = result["rows"], result["classifications"]
        records = build_overlap_records(classifications)
        matrix_result = co_occurrence_matrix(records)
        suppression = suppression_analysis(records)

        per_horizon[horizon_hours] = {
            "n_predictions": len(rows),
            "n_evaluable_for_overlap": matrix_result["n_evaluable"],
            "winner_distribution": ec.breakdown_by_error_type_only(rows, classifications),
            "matched_category_distribution": matched_category_distribution(records),
            "co_occurrence_matrix": {f"{a}|{b}": v for (a, b), v in matrix_result["matrix"].items()},
            "suppression_analysis": suppression,
            "specific_overlap_findings": specific_overlap_report(matrix_result["matrix"]),
            "absent_winner_explanation": explain_absent_winners(suppression),
        }

    return {
        "coin": coin,
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "overlap_categories": list(OVERLAP_CATEGORIES),
        "underlying_event_category_mapping": dict(UNDERLYING_EVENT_CATEGORY),
        "per_horizon": per_horizon,
        "winner_vs_matched_note": (
            "winner_distribution (6A) and matched_category_distribution (6B) "
            "answer DIFFERENT questions and must never be combined into one "
            "statistic: 6A is 'what does the current precedence assign', 6B "
            "is 'what candidate causes were actually present'."
        ),
        "causality_note": (
            "Co-occurrence and suppression counts describe simultaneity under "
            "PR5d's existing deterministic rule, not causation. No priority-"
            "chain change is proposed or made by this module."
        ),
    }
