"""
PR5b: continuous BTC forward-return outcome engine.

Objective (per PR5b build authorization): for every V2 prediction in a
bounded window, compute the actual subsequent BTC price movement at
several fixed forward horizons (1h/3h/6h/12h/24h), independent of what
horizon that prediction itself targets (`predictions.horizon_hours`,
which is 12 or 24 in production and answers a different question --
"what did V2 predict for", not "what actually happened next at every
horizon"). This module answers the second question only.

Read-only with respect to production V1/V2 tables (`predictions`,
`btc_data`). No V1/V2 modification, no coefficient change, no writes of
any kind -- this module never executes INSERT/UPDATE/DELETE/DDL
(enforced by test_outcome_engine.py::test_module_never_writes_to_the_database,
mirroring resolver.py's own equivalent test).

Temporal correctness (the CryptoPulse research invariant, preserved
here exactly as in resolver.py/selection_resolver.py):

    prediction_ts
        -> BTC price observation at/before prediction_ts (price_now)
        -> BTC price observation at/before prediction_ts + horizon_ms (price_future)

Both price lookups are as-of (MAX ts <= target), the same proven-safe
correlated-subquery shape as PR2's resolver.py and PR5a's
selection_resolver.py. No future information is used to construct
`price_now`, and `price_future` is only ever looked up relative to a
FIXED, horizon-defined future timestamp -- never relative to "now" at
query time -- so re-running this module later against the same
(start_ts, end_ts, horizon_hours) with the same underlying data always
returns the same rows (determinism, required by PR5b Section 9).

No silent interpolation of missing outcomes (PR5b Section 3): if
`btc_data`'s cadence leaves no observation at or before the target
future timestamp, or if the nearest available "future" observation is
not actually after the prediction timestamp (the cadence gap is wider
than the requested horizon -- no genuinely new price point exists yet),
the row is returned with outcome_status="UNRESOLVED_NO_FUTURE_PRICE_POINT"
and forward_return_pct=None. It is never dropped silently and never
defaulted to zero or interpolated.

Bounded/indexed (PR5b Section 7, mirroring resolver.py): both start_ts
and end_ts are mandatory with no default, and the window is capped by
the same MAX_WINDOW_MS precedent used throughout this project.

Unlike resolver.py/selection_resolver.py, this module's outer query
does NOT filter on predictions.horizon_hours (it must resolve every
prediction in the window regardless of that prediction's own target
horizon), so the existing composite idx_predictions_horizon_ts(horizon_hours, ts)
cannot serve as a range-seek index for a ts-only filter -- confirmed via
a real EXPLAIN QUERY PLAN against production (see PR description),
which showed a full covering-index SCAN rather than a bounded SEARCH.
This PR therefore adds a small additive index,
.ai/migrations/0008_outcome_engine_predictions_ts_index.sql
(`CREATE INDEX idx_predictions_ts ON predictions(ts)`), confirmed via a
second EXPLAIN QUERY PLAN (local SQLite; see PR description) to turn
the outer scan into a genuine bounded `SEARCH ... USING COVERING INDEX
idx_predictions_ts (ts>? AND ts<?)`. The migration is proposed, not
applied to production, by this PR -- pending review, per this
project's established process. The btc_data(ts) as-of lookups reuse
the already-existing idx_btc_data_ts, unchanged.
"""

MAX_WINDOW_MS = 90 * 24 * 3600 * 1000  # 90 days -- same precedent as resolver.py/selection_resolver.py

# Forward-return horizons this module investigates, per PR5b Section 3
# ("at minimum investigate: 1h, 3h, 6h, 12h, 24h"). Keys are hours (for
# caller-facing readability), values are milliseconds (for the SQL bind
# parameter). This is NOT the same axis as predictions.horizon_hours.
OUTCOME_HORIZON_MS = {
    1: 3600000,
    3: 10800000,
    6: 21600000,
    12: 43200000,
    24: 86400000,
}

OUTCOME_SQL = """
SELECT
  p.ts AS prediction_ts,
  p.horizon_hours AS prediction_horizon_hours,
  (SELECT b.btc_price FROM btc_data b WHERE b.ts <= p.ts ORDER BY b.ts DESC LIMIT 1) AS btc_price_at_prediction,
  (SELECT b2.ts FROM btc_data b2 WHERE b2.ts <= p.ts + ? ORDER BY b2.ts DESC LIMIT 1) AS realized_future_ts,
  (SELECT b2.btc_price FROM btc_data b2 WHERE b2.ts <= p.ts + ? ORDER BY b2.ts DESC LIMIT 1) AS realized_btc_price
FROM predictions p
WHERE p.ts >= ? AND p.ts <= ?
ORDER BY p.ts ASC
"""


def _validate_bounds(start_ts, end_ts):
    if start_ts is None or end_ts is None:
        raise ValueError("start_ts and end_ts are required -- there is no unbounded mode")
    if start_ts >= end_ts:
        raise ValueError(f"start_ts ({start_ts}) must be strictly before end_ts ({end_ts})")
    window = end_ts - start_ts
    if window > MAX_WINDOW_MS:
        raise ValueError(f"requested window ({window}ms) exceeds MAX_WINDOW_MS ({MAX_WINDOW_MS}ms)")


def compute_forward_returns(conn, start_ts, end_ts, horizon_hours):
    """The only entry point this module exposes for computing outcomes.

    Both start_ts and end_ts are required (no default) -- an unbounded
    call cannot be constructed by omission, matching resolver.py's
    contract. horizon_hours must be one of OUTCOME_HORIZON_MS's keys.

    Returns a list of dicts, one per prediction in the window, each with:
      prediction_ts, prediction_horizon_hours, horizon_hours (the
      outcome horizon requested, e.g. 1/3/6/12/24), btc_price_at_prediction,
      realized_future_ts, realized_btc_price, forward_return_pct,
      absolute_forward_return_pct, realized_direction, outcome_status.

    outcome_status is one of:
      "RESOLVED" -- a genuinely new future price point exists and the
        return was computed.
      "UNRESOLVED_NO_FUTURE_PRICE_POINT" -- no btc_data observation
        exists at/after prediction_ts within the requested horizon (or
        btc_price_at_prediction itself could not be resolved). All
        outcome fields are None. Never fabricated, never dropped.

    Every input prediction row in the window is represented exactly
    once in the output (RESOLVED or UNRESOLVED) -- callers that need
    only resolved rows should filter on outcome_status themselves;
    this function never filters silently.
    """
    _validate_bounds(start_ts, end_ts)
    if horizon_hours not in OUTCOME_HORIZON_MS:
        raise ValueError(
            f"unsupported horizon_hours ({horizon_hours}); must be one of {sorted(OUTCOME_HORIZON_MS)}"
        )
    horizon_ms = OUTCOME_HORIZON_MS[horizon_hours]

    cursor = conn.execute(OUTCOME_SQL, (horizon_ms, horizon_ms, start_ts, end_ts))
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    results = []
    for row in rows:
        pts = row["prediction_ts"]
        price_now = row["btc_price_at_prediction"]
        future_ts = row["realized_future_ts"]
        price_future = row["realized_btc_price"]

        # No-lookahead / no-stale-data guard: future_ts must be a
        # genuinely new observation strictly after the prediction ts.
        # If the cadence gap is wider than the horizon, there is no
        # real outcome yet -- report that honestly rather than reusing
        # a stale (or absent) price point.
        resolvable = (
            price_now is not None
            and price_future is not None
            and future_ts is not None
            and future_ts > pts
        )

        if resolvable:
            forward_return_pct = ((price_future - price_now) / price_now) * 100.0
            absolute_forward_return_pct = abs(forward_return_pct)
            if forward_return_pct > 0:
                realized_direction = "UP"
            elif forward_return_pct < 0:
                realized_direction = "DOWN"
            else:
                realized_direction = "FLAT"
            outcome_status = "RESOLVED"
        else:
            forward_return_pct = None
            absolute_forward_return_pct = None
            realized_direction = None
            outcome_status = "UNRESOLVED_NO_FUTURE_PRICE_POINT"

        results.append({
            "prediction_ts": pts,
            "prediction_horizon_hours": row["prediction_horizon_hours"],
            "horizon_hours": horizon_hours,
            "btc_price_at_prediction": price_now,
            "realized_future_ts": future_ts,
            "realized_btc_price": price_future,
            "forward_return_pct": forward_return_pct,
            "absolute_forward_return_pct": absolute_forward_return_pct,
            "realized_direction": realized_direction,
            "outcome_status": outcome_status,
        })

    return results


def explain_outcome_query_plan(conn, start_ts, end_ts, horizon_hours):
    """Returns the real EXPLAIN QUERY PLAN rows for the exact same bound
    query compute_forward_returns() would run -- used by tests to assert
    on the actual execution plan, not a hand-written expectation of it."""
    horizon_ms = OUTCOME_HORIZON_MS[horizon_hours]
    return conn.execute(
        "EXPLAIN QUERY PLAN " + OUTCOME_SQL, (horizon_ms, horizon_ms, start_ts, end_ts)
    ).fetchall()
