"""
PR3: deterministic event detection.

market/V1/V2 existing data -> bounded deterministic queries -> five
frozen event rules -> research_events rows (returned to caller; this
module does not write to D1 itself, matching PR2's "resolver returns
rows, does not autonomously populate" precedent).

No internet. No LLM. No causal interpretation. No coefficient
optimization. No V1/V2 changes.

Architecturally separate from research/resolver.py (PR2): the resolver
is prediction-centric (V1 observation -> V2 prediction -> outcome, for
predictions that exist); this detector is market-centric (a market
event can exist with no relevant V2 prediction at all -- e.g. a large
BTC move needs only price data). This module queries btc_data/history
directly rather than calling the PR2 resolver for price history.

FROZEN CONSTANTS -- the central discipline of this module. Every
threshold below was characterized from historical data (see each
constant's own comment for the exact reference period), then frozen as
a literal value. None of them is recomputed live from data that could
include information from after a given detection run -- "descriptive
statistics may use the complete historical dataset; production event
rules may not."
"""

TRIGGER_VERSION = "pr3-v1"

MAX_WINDOW_MS = 90 * 24 * 3600 * 1000  # same 90-day safety cap as PR2's
# resolver. Typical usage is expected to pass a much smaller window (a
# daily run only needs to look back far enough to compute its rolling
# statistics, roughly 7-8 days) -- this is a ceiling, not a target.

LOOKBACK_BUFFER_MS = 8 * 24 * 3600 * 1000  # every detector needs some
# price history strictly BEFORE start_ts to compute its own trailing
# window at the very first point in the requested range (e.g. a 7-day
# regime classification at start_ts needs 7 days of data before it).
# This buffer is fetched but never itself treated as part of the
# requested detection range -- events are only emitted for observations
# at or after start_ts.

# ---- LARGE_MOVE ----
# 4% threshold, characterized from the complete 136-day btc_data series
# available 2026-05-05 to 2026-09-17 (daily return std ~1.96%; 4% rounds
# the empirical 2-sigma level of ~3.92%, which caught 8 of 135 observed
# days, ~5.9% -- close to the ~5% a roughly-normal distribution predicts
# at 2 sigma, evidence this isn't an arbitrarily tight or loose cutoff).
# Frozen: this module never recomputes the threshold from live data.
LARGE_MOVE_THRESHOLD_PCT = 4.0

# ---- REGIME_REVERSAL ----
# Reuses, verbatim, the regime convention already frozen and used across
# three prior audits this project (trailing 7-day BTC return: rally if
# >+5%, correction if <-5%, chop otherwise) -- not a new threshold.
REGIME_RALLY_THRESHOLD_PCT = 5.0
REGIME_CORRECTION_THRESHOLD_PCT = -5.0
# Consecutive-observation gap tolerance: two observations more than this
# far apart are NOT treated as consecutive for reversal purposes, so a
# data outage (e.g. Monday=rally, next real observation Thursday=
# correction) cannot masquerade as an immediate reversal.
MAX_OBSERVATION_GAP_MS = 48 * 3600 * 1000

# ---- VOLATILITY_EXPANSION ----
# Frozen baseline, NOT recomputed live: whole-sample daily-return std
# from the same 136-day characterization above (2026-05-05 to
# 2026-09-17). Any future PR3 run's detection window is necessarily
# later than this reference period, so using it introduces no leakage.
FROZEN_VOLATILITY_BASELINE_PCT = 1.959
VOLATILITY_EXPANSION_RATIO_THRESHOLD = 1.5

# ---- V2_FAILURE_CLUSTER ----
V2_FAILURE_CLUSTER_LENGTH = 5
# A rarity-oriented heuristic motivated by the ~3.125% (0.5^5) probability
# of five consecutive failures under a 50/50 independent null -- it is
# NOT a statistical significance test against this system's own actual
# ~45-47% measured accuracy, and consecutive prediction errors are not
# necessarily independent. Documented as a deterministic trigger, not a
# hypothesis test.

# ---- V1_BTC_DIVERGENCE ----
# Frozen quartile bounds from the completed V1 sentiment audit
# (2026-09-17, N=33 daily composite observations) -- NOT recomputed
# live. This category is explicitly a POST-OUTCOME research event: V1's
# reading at T only becomes "divergent" once BTC's subsequent move is
# known, so it must never be presented as information available at T.
FROZEN_V1_BEARISH_EXTREME = 45
FROZEN_V1_BULLISH_EXTREME = 58


def _validate_window(start_ts, end_ts):
    if start_ts is None or end_ts is None:
        raise ValueError("start_ts and end_ts are required -- there is no unbounded mode")
    if start_ts >= end_ts:
        raise ValueError(f"start_ts ({start_ts}) must be strictly before end_ts ({end_ts})")
    if end_ts - start_ts > MAX_WINDOW_MS:
        raise ValueError(f"requested window ({end_ts - start_ts}ms) exceeds MAX_WINDOW_MS ({MAX_WINDOW_MS}ms)")


def _fetch_bounded_daily_prices(conn, start_ts, end_ts):
    """The one bounded, indexed SQL query every detector below builds on.
    Fetches btc_data rows from (start_ts - LOOKBACK_BUFFER_MS) to end_ts
    -- still a fixed-width bound, never unbounded -- then deduplicates
    near-simultaneous readings (the same duplicate-price-log-write
    pattern already found and handled in Experiment 4) down to one
    observation per real tick, keeping the first reading in each
    cluster."""
    rows = conn.execute(
        "SELECT ts, btc_price FROM btc_data WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
        (start_ts - LOOKBACK_BUFFER_MS, end_ts),
    ).fetchall()
    deduped = []
    for ts, price in rows:
        if not deduped or ts - deduped[-1][0] > 60000:
            deduped.append((ts, price))
    return deduped


def _price_at_or_before(prices, ts):
    """Nearest prior price at or before ts, from an already-fetched,
    already-sorted, already-bounded list -- no further SQL query. Linear
    scan is fine here: prices is already small (a handful to a few
    hundred rows at most, per the LOOKBACK_BUFFER_MS/MAX_WINDOW_MS caps)."""
    result = None
    for p_ts, p_price in prices:
        if p_ts <= ts:
            result = (p_ts, p_price)
        else:
            break
    return result


def detect_large_moves(conn, start_ts, end_ts):
    _validate_window(start_ts, end_ts)
    prices = _fetch_bounded_daily_prices(conn, start_ts, end_ts)
    events = []
    for ts, price in prices:
        if ts < start_ts:
            continue  # part of the lookback buffer only, not itself a candidate
        prior = _price_at_or_before(prices, ts - 24 * 3600000)
        if prior is None:
            continue
        prior_ts, prior_price = prior
        pct_return = (price - prior_price) / prior_price * 100
        if abs(pct_return) > LARGE_MOVE_THRESHOLD_PCT:
            events.append({
                "category": "LARGE_MOVE",
                "event_ts": ts,  # the completed move's own end timestamp, not its start
                "direction": "UP" if pct_return > 0 else "DOWN",
                "intensity": abs(pct_return),
                "trigger_metric": "24h_btc_return_pct",
                "trigger_threshold": LARGE_MOVE_THRESHOLD_PCT,
                "trigger_version": TRIGGER_VERSION,
                "is_post_event_analysis": 0,
            })
    return events


def _classify_regime(prices, ts):
    prior = _price_at_or_before(prices, ts - 7 * 24 * 3600000)
    current = _price_at_or_before(prices, ts)
    if prior is None or current is None:
        return None
    trail7_pct = (current[1] - prior[1]) / prior[1] * 100
    if trail7_pct > REGIME_RALLY_THRESHOLD_PCT:
        return "rally"
    if trail7_pct < REGIME_CORRECTION_THRESHOLD_PCT:
        return "correction"
    return "chop"


def detect_regime_reversals(conn, start_ts, end_ts):
    _validate_window(start_ts, end_ts)
    prices = _fetch_bounded_daily_prices(conn, start_ts, end_ts)
    candidates = [(ts, price) for ts, price in prices if ts >= start_ts]
    events = []
    prev_ts, prev_regime = None, None
    for ts, _ in candidates:
        regime = _classify_regime(prices, ts)
        if regime is None:
            continue
        if prev_regime is not None and regime != prev_regime:
            if prev_ts is not None and (ts - prev_ts) <= MAX_OBSERVATION_GAP_MS:
                events.append({
                    "category": "REGIME_REVERSAL",
                    "event_ts": ts,
                    "direction": f"{prev_regime}_to_{regime}",
                    "intensity": None,
                    "trigger_metric": "7d_regime_classification",
                    "trigger_threshold": None,
                    "trigger_version": TRIGGER_VERSION,
                    "is_post_event_analysis": 0,
                })
            # A gap wider than MAX_OBSERVATION_GAP_MS deliberately does NOT
            # emit an event -- the two states aren't treated as consecutive.
        prev_ts, prev_regime = ts, regime
    return events


def detect_volatility_expansion(conn, start_ts, end_ts):
    _validate_window(start_ts, end_ts)
    prices = _fetch_bounded_daily_prices(conn, start_ts, end_ts)
    candidates = [(ts, price) for ts, price in prices if ts >= start_ts]
    events = []
    for ts, _ in candidates:
        window = [(p_ts, p) for p_ts, p in prices if ts - 7 * 24 * 3600000 <= p_ts <= ts]
        if len(window) < 4:
            continue
        rets = [(window[i + 1][1] - window[i][1]) / window[i][1] * 100 for i in range(len(window) - 1)]
        mean = sum(rets) / len(rets)
        variance = sum((r - mean) ** 2 for r in rets) / len(rets)
        realized_vol = variance ** 0.5
        ratio = realized_vol / FROZEN_VOLATILITY_BASELINE_PCT
        if ratio > VOLATILITY_EXPANSION_RATIO_THRESHOLD:
            events.append({
                "category": "VOLATILITY_EXPANSION",
                "event_ts": ts,
                "direction": None,
                "intensity": ratio,
                "trigger_metric": "7d_realized_vol_over_frozen_baseline",
                "trigger_threshold": VOLATILITY_EXPANSION_RATIO_THRESHOLD,
                "trigger_version": TRIGGER_VERSION,
                "is_post_event_analysis": 0,
            })
    return events


def detect_v2_failure_clusters(conn, coin, horizon_hours, start_ts, end_ts):
    """Scoped to exactly one coin/horizon per call -- BTC/24h failures
    are never combined with ETH/12h or any other pair into one streak."""
    _validate_window(start_ts, end_ts)
    table = {"BTC": "predictions", "LINK": "link_predictions", "ETH": "eth_predictions"}[coin]
    rows = conn.execute(
        f"SELECT ts, p_up, realized_up FROM {table} "
        f"WHERE horizon_hours = ? AND ts >= ? AND ts < ? AND realized_up IS NOT NULL AND p_up IS NOT NULL "
        f"ORDER BY ts ASC",
        (horizon_hours, start_ts, end_ts),
    ).fetchall()
    events = []
    streak = 0
    for ts, p_up, realized_up in rows:
        correct = (p_up > 0.5 and realized_up == 1) or (p_up <= 0.5 and realized_up == 0)
        if correct:
            streak = 0
        else:
            streak += 1
            if streak == V2_FAILURE_CLUSTER_LENGTH:
                events.append({
                    "category": "V2_FAILURE_CLUSTER",
                    "event_ts": ts,
                    "direction": f"{coin}_{horizon_hours}h",
                    "intensity": float(streak),
                    "trigger_metric": "consecutive_incorrect_predictions",
                    "trigger_threshold": float(V2_FAILURE_CLUSTER_LENGTH),
                    "trigger_version": TRIGGER_VERSION,
                    "is_post_event_analysis": 0,
                })
    return events


def detect_v1_btc_divergence(conn, start_ts, end_ts):
    """Explicitly a post-outcome research event -- is_post_event_analysis
    is always 1 here. V1's reading at T only becomes 'divergent' once
    BTC's subsequent move is known; this must never be read as evidence
    V1 predicted the move."""
    _validate_window(start_ts, end_ts)
    prices = _fetch_bounded_daily_prices(conn, start_ts, end_ts)
    history_rows = conn.execute(
        "SELECT ts, score FROM history WHERE ts >= ? AND ts < ?",
        (start_ts, end_ts),
    ).fetchall()
    events = []
    for h_ts, v1_score in history_rows:
        if v1_score is None:
            continue
        if not (v1_score < FROZEN_V1_BEARISH_EXTREME or v1_score > FROZEN_V1_BULLISH_EXTREME):
            continue
        current = _price_at_or_before(prices, h_ts)
        future = _price_at_or_before(prices, h_ts + 24 * 3600000)
        if current is None or future is None or future[0] <= current[0]:
            continue
        pct_return = (future[1] - current[1]) / current[1] * 100
        if abs(pct_return) <= LARGE_MOVE_THRESHOLD_PCT:
            continue
        v1_bullish = v1_score > FROZEN_V1_BULLISH_EXTREME
        btc_up = pct_return > 0
        if v1_bullish == btc_up:
            continue  # same direction -- not a divergence
        events.append({
            "category": "V1_BTC_DIVERGENCE",
            "event_ts": future[0],
            "direction": f"v1_{'bullish' if v1_bullish else 'bearish'}_btc_{'up' if btc_up else 'down'}",
            "intensity": abs(pct_return),
            "trigger_metric": "v1_extreme_vs_24h_btc_return",
            "trigger_threshold": LARGE_MOVE_THRESHOLD_PCT,
            "trigger_version": TRIGGER_VERSION,
            "is_post_event_analysis": 1,
        })
    return events
