"""
PR2: observation/outcome resolver.

information available at T -> V2 prediction at T -> actual BTC outcome

Read-only query capability. Does NOT populate any persistent research
table on a schedule -- callers get rows back and decide what to do with
them, keeping this deterministic and auditable (per explicit review
requirement). No internet, no LLM, no event detection, no V1/V2
modification, no coefficient changes, no autonomous decisions, no new
scheduled workflow.

THE CENTRAL SAFETY PROPERTY, proven in test_resolver.py, not just
asserted here: the cost of a call is bounded by the requested
(start_ts, end_ts) window, not by the total size of `history` or
`predictions`. There is no code path in this module that executes an
unbounded query -- resolve_observations() cannot be called without both
bounds, and MAX_WINDOW_MS caps how wide a single call is allowed to be.

The as-of lookup (finding the V1 `history` observation available at or
immediately before a given V2 prediction's own timestamp) genuinely
requires a correlated subquery -- that shape is inherent to "find the
nearest prior row," not a design mistake. What makes THIS one safe,
confirmed via a real EXPLAIN QUERY PLAN against both a local SQLite
instance and production D1 (see PR description): every step -- the
outer scan over `predictions`, the correlated subquery over `history`,
and the equality join back to fetch the matched row -- is index-driven.
None of them is a table scan.
"""

MAX_WINDOW_MS = 90 * 24 * 3600 * 1000  # 90 days -- matches this project's
# own established "narrow diagnostic window" discipline (the same order
# of magnitude already used for D1 forensic queries elsewhere in this
# codebase). Not a magic number: it bounds a single call's outer-query
# row count to roughly a few hundred predictions at today's cadence,
# regardless of how large `history`/`predictions` eventually grow.

RESOLVER_SQL = """
SELECT p.ts AS prediction_ts, p.horizon_hours, p.realized_up AS actual_outcome, p.target_ts,
       h.ts AS v1_observation_ts, h.score AS v1_composite_at_prediction_time,
       h.technical_score, h.bottom_score, h.regime_mag
FROM predictions p
LEFT JOIN history h ON h.ts = (SELECT MAX(h2.ts) FROM history h2 WHERE h2.ts <= p.ts)
WHERE p.horizon_hours = ? AND p.ts >= ? AND p.ts < ?
ORDER BY p.ts ASC
"""


def resolve_observations(conn, horizon_hours, start_ts, end_ts):
    """The only entry point this module exposes. There is deliberately no
    optional/default form of start_ts or end_ts -- both are required
    positional-or-keyword arguments with no default value, so a caller
    cannot accidentally invoke an unbounded query by omission.

    Raises ValueError (not a silent fallback) for: missing bounds (via
    Python's own TypeError if omitted entirely, or ValueError if
    explicitly passed as None), a reversed or zero-width window, or a
    window wider than MAX_WINDOW_MS.
    """
    if start_ts is None or end_ts is None:
        raise ValueError("start_ts and end_ts are required -- there is no unbounded mode")
    if start_ts >= end_ts:
        raise ValueError(f"start_ts ({start_ts}) must be strictly before end_ts ({end_ts})")
    window = end_ts - start_ts
    if window > MAX_WINDOW_MS:
        raise ValueError(f"requested window ({window}ms) exceeds MAX_WINDOW_MS ({MAX_WINDOW_MS}ms)")

    cursor = conn.execute(RESOLVER_SQL, (horizon_hours, start_ts, end_ts))
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def explain_resolver_query_plan(conn, horizon_hours, start_ts, end_ts):
    """Returns the real EXPLAIN QUERY PLAN rows for the exact same bound
    query resolve_observations() would run -- used by tests to assert on
    the actual execution plan, not a hand-written expectation of it."""
    return conn.execute("EXPLAIN QUERY PLAN " + RESOLVER_SQL, (horizon_hours, start_ts, end_ts)).fetchall()
