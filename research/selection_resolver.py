"""
PR5a: selection-decision resolver.

information available at T -> which variant selection was "live" at T

Read-only query capability, mirroring research/resolver.py's own safety
contract exactly (same bounded-window discipline, same as-of join shape).
No internet, no LLM, no V1/V2 modification, no coefficient changes, no
autonomous decisions, no new scheduled workflow, no write path.

WHY THIS EXISTS (empirical finding, not a design assumption -- see the
PR5a PR description for the exact real production rows this is built
from):

selection_decisions.prediction_ts is NOT a reliable join key for "what
did V2 predict for this specific prediction". Real, unmodified queries
against production D1 (`sentiment-history`) show:

  - selectBestVariant() in worker.js scores each variant using only rows
    where `realized_up IS NOT NULL` -- which by construction EXCLUDES the
    very prediction it is nominally attached to via prediction_ts (that
    prediction has not resolved yet at write time). chosen_p_up therefore
    reflects whichever OTHER, already-resolved prediction happened to be
    most recent at that moment -- not the p_up of the prediction named by
    prediction_ts.
  - A real duplicate chain for one BTC/24h prediction
    (prediction_ts=1789614039967) has 7 selection_decisions rows, all
    with ts strictly AFTER prediction_ts (the earliest is ~1.4h later,
    the latest ~29h later). chosen_p_up drifts from 0.667 (earliest row)
    to 0.333 (later rows) for that same prediction_ts -- proof that
    "join on prediction_ts, pick a row" was never a well-defined
    operation in the first place, independent of which row you'd pick.
  - worker.js documents the real semantics directly (search for
    "the actual live decision that was served"): production reads
    selection_decisions via `ORDER BY ts DESC LIMIT 1` relative to *now*
    -- a rolling as-of-now state, not a per-prediction attribute.

Given that, "the decision in force when prediction X was generated" is
defined here as an AS-OF lookup on wall-clock time -- MAX(sd.ts) WHERE
sd.ts <= predictions.ts -- the same shape PR2's resolve_observations()
already uses for V1<->V2 alignment, not a lookup via prediction_ts.

Applied to the real chain above: every one of those 7 selection_decisions
rows has ts > prediction_ts, so the as-of join correctly returns NO
selection decision for that prediction (selection_ts=None) rather than
attaching any of the seven, hindsight-drifted, wrong candidates a
prediction_ts-keyed join would have been forced to choose between.

THE CENTRAL SAFETY PROPERTY, proven in test_selection_resolver.py: the
cost of a call is bounded by the requested (start_ts, end_ts) window, and
every step of the underlying query is index-driven against EXISTING
indexes -- confirmed via a real EXPLAIN QUERY PLAN against production D1
(see PR description): idx_predictions_horizon_ts for the outer scan,
idx_selection_decisions_coin_time for both the correlated subquery and
the join-back. No new index, no schema change, is required for this join.

Scope: BTC only (predictions / selection_decisions), matching PR2's own
precedent of not generalizing across coins until each coin's table shape
is independently verified. LINK/ETH (link_predictions/eth_predictions,
which have no selection_decisions equivalent verified yet) are explicitly
deferred, not silently assumed to share BTC's shape.
"""

MAX_WINDOW_MS = 90 * 24 * 3600 * 1000  # same bound and rationale as
# resolver.py: caps a single call's outer-query row count regardless of
# how large `predictions`/`selection_decisions` eventually grow.

SUPPORTED_COINS = ("BTC",)

SELECTION_RESOLVER_SQL = """
SELECT p.ts AS prediction_ts, p.horizon_hours, p.p_up, p.realized_up, p.target_ts,
       sd.ts AS selection_ts, sd.chosen_variant, sd.chosen_p_up, sd.cleared_gate,
       sd.lca_score, sd.comparison_count
FROM predictions p
LEFT JOIN selection_decisions sd
  ON sd.ts = (
       SELECT MAX(sd2.ts) FROM selection_decisions sd2
       WHERE sd2.coin = ? AND sd2.horizon_hours = p.horizon_hours AND sd2.ts <= p.ts
     )
 AND sd.coin = ? AND sd.horizon_hours = p.horizon_hours
WHERE p.horizon_hours = ? AND p.ts >= ? AND p.ts < ?
ORDER BY p.ts ASC
"""


def resolve_selection_decision(conn, coin, horizon_hours, start_ts, end_ts):
    """The only entry point this module exposes. Both bounds are required
    -- there is no default form, so a caller cannot accidentally invoke an
    unbounded query by omission.

    coin must be 'BTC' for PR5a (see module docstring); any other value
    raises ValueError rather than silently running a query against a
    table shape that has not been verified.

    Raises ValueError for: an unsupported coin, missing bounds, a reversed
    or zero-width window, or a window wider than MAX_WINDOW_MS.

    Returns a list of dict rows in ascending prediction_ts order. Every
    row always carries the prediction's own fields (prediction_ts,
    horizon_hours, p_up, realized_up, target_ts). selection_ts /
    chosen_variant / chosen_p_up / cleared_gate / lca_score /
    comparison_count are None when no selection_decisions row exists at
    or before that prediction's own ts -- reported as missing data, never
    defaulted to a value implying a decision was made.
    """
    if coin not in SUPPORTED_COINS:
        raise ValueError(
            f"resolve_selection_decision only supports {SUPPORTED_COINS} in "
            f"PR5a; got {coin!r}. Other coins are explicitly deferred, not "
            f"silently assumed to share BTC's table shape."
        )
    if start_ts is None or end_ts is None:
        raise ValueError("start_ts and end_ts are both required")
    if end_ts <= start_ts:
        raise ValueError("end_ts must be strictly greater than start_ts")
    if end_ts - start_ts > MAX_WINDOW_MS:
        raise ValueError(
            f"window of {end_ts - start_ts}ms exceeds MAX_WINDOW_MS={MAX_WINDOW_MS}ms"
        )

    cursor = conn.execute(
        SELECTION_RESOLVER_SQL,
        (coin, coin, horizon_hours, start_ts, end_ts),
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]
