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


def _fetch_bounded_prices(conn, start_ts, end_ts):
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


MIN_DAILY_SPAN_DAYS = 5  # minimum number of DISTINCT CALENDAR DAYS
# required within a 7-day window before a regime/volatility calculation
# is attempted -- not a raw row count. Confirmed necessary: btc_data is
# genuinely intraday in production (verified directly: 299 rows over 21
# distinct days, ~14.2 readings/day, matching the 3h production cron),
# so a row-count check (e.g. "at least 4 rows") could be satisfied by as
# little as half a day of real elapsed time. Requiring 5 of the 7
# calendar days present tolerates real-world gaps (this project's price
# logging has had documented reliability issues) while still ensuring
# the calculation reflects something close to a genuine 7-day span, not
# an intraday cluster mistaken for one.


def _resample_daily(prices):
    """Reduces an already tick-deduplicated price series to at most one
    observation per UTC calendar day (the first reading of each day) --
    exactly the same methodology used to characterize
    LARGE_MOVE_THRESHOLD_PCT and FROZEN_VOLATILITY_BASELINE_PCT in the
    first place (both were computed from one-price-per-day series, not
    raw intraday data). Every detector below operates on this
    daily-resampled series, not the raw intraday one, so that a 24h/7d
    calculation here is mathematically the same kind of quantity as the
    one the frozen thresholds were derived from -- not a differently-
    sampled approximation of it.

    This also eliminates a real, empirically-confirmed bug: iterating
    over raw intraday rows as independent "candidates" caused the same
    underlying move to be reported as multiple duplicate events (one per
    intraday reading landing near it) rather than once per real event."""
    from datetime import datetime, timezone
    daily = []
    seen_days = set()
    for ts, price in prices:
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        if day not in seen_days:
            seen_days.add(day)
            daily.append((ts, price))
    return daily


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


def _distinct_days_in_range(prices, start_ts, end_ts):
    from datetime import datetime, timezone
    days = set()
    for ts, _ in prices:
        if start_ts <= ts <= end_ts:
            days.add(datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date())
    return len(days)


def _fingerprint(*parts):
    """Deterministic, stable event identity -- same input always produces
    the same string, different logical events produce different strings.
    Pipe-joined rather than hashed, deliberately: these values are all
    small and non-sensitive, and a readable fingerprint is easier to
    inspect/debug than an opaque hash while providing the exact same
    determinism/uniqueness properties PR1's UNIQUE index needs. No
    database access of any kind -- pure string formatting over values
    already computed by the caller."""
    return "|".join(str(p) for p in parts)


def detect_large_moves(conn, start_ts, end_ts):
    _validate_window(start_ts, end_ts)
    raw_prices = _fetch_bounded_prices(conn, start_ts, end_ts)
    prices = _resample_daily(raw_prices)
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
            direction = "UP" if pct_return > 0 else "DOWN"
            events.append({
                "category": "LARGE_MOVE",
                "event_ts": ts,  # the completed move's own end timestamp, not its start
                "direction": direction,
                "intensity": abs(pct_return),
                "trigger_metric": "24h_btc_return_pct",
                "trigger_threshold": LARGE_MOVE_THRESHOLD_PCT,
                "trigger_version": TRIGGER_VERSION,
                "is_post_event_analysis": 0,
                "event_fingerprint": _fingerprint("LARGE_MOVE", ts, direction, TRIGGER_VERSION),
            })
    return events


def _classify_regime(daily_prices, ts):
    prior = _price_at_or_before(daily_prices, ts - 7 * 24 * 3600000)
    current = _price_at_or_before(daily_prices, ts)
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
    raw_prices = _fetch_bounded_prices(conn, start_ts, end_ts)
    prices = _resample_daily(raw_prices)
    candidates = [(ts, price) for ts, price in prices if ts >= start_ts]
    events = []
    prev_ts, prev_regime = None, None
    for ts, _ in candidates:
        # Minimum-span check: require at least MIN_DAILY_SPAN_DAYS distinct
        # calendar days within the trailing 7-day window before trusting a
        # classification -- not a raw row count (see MIN_DAILY_SPAN_DAYS's
        # own comment for why a row-count check would be wrong here).
        if _distinct_days_in_range(prices, ts - 7 * 24 * 3600000, ts) < MIN_DAILY_SPAN_DAYS:
            continue
        regime = _classify_regime(prices, ts)
        if regime is None:
            continue
        if prev_regime is not None and regime != prev_regime:
            if prev_ts is not None and (ts - prev_ts) <= MAX_OBSERVATION_GAP_MS:
                direction = f"{prev_regime}_to_{regime}"
                events.append({
                    "category": "REGIME_REVERSAL",
                    "event_ts": ts,
                    "direction": direction,
                    "intensity": None,
                    "trigger_metric": "7d_regime_classification",
                    "trigger_threshold": None,
                    "trigger_version": TRIGGER_VERSION,
                    "is_post_event_analysis": 0,
                    "event_fingerprint": _fingerprint("REGIME_REVERSAL", ts, direction, TRIGGER_VERSION),
                })
            # A gap wider than MAX_OBSERVATION_GAP_MS deliberately does NOT
            # emit an event -- the two states aren't treated as consecutive.
        prev_ts, prev_regime = ts, regime
    return events


def detect_volatility_expansion(conn, start_ts, end_ts):
    _validate_window(start_ts, end_ts)
    raw_prices = _fetch_bounded_prices(conn, start_ts, end_ts)
    prices = _resample_daily(raw_prices)
    candidates = [(ts, price) for ts, price in prices if ts >= start_ts]
    events = []
    for ts, _ in candidates:
        window = [(p_ts, p) for p_ts, p in prices if ts - 7 * 24 * 3600000 <= p_ts <= ts]
        # Minimum-span check: distinct calendar days actually present in
        # the trailing 7-day window, not a raw row count -- see
        # MIN_DAILY_SPAN_DAYS's own comment for why this distinction is
        # required once btc_data is known to be intraday.
        if _distinct_days_in_range(prices, ts - 7 * 24 * 3600000, ts) < MIN_DAILY_SPAN_DAYS:
            continue
        # Returns computed between consecutive DAILY-resampled points --
        # the same kind of quantity (day-over-day return) that
        # FROZEN_VOLATILITY_BASELINE_PCT was itself characterized from,
        # not returns between whatever intraday rows happen to exist.
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
                "event_fingerprint": _fingerprint("VOLATILITY_EXPANSION", ts, TRIGGER_VERSION),
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
                direction = f"{coin}_{horizon_hours}h"
                events.append({
                    "category": "V2_FAILURE_CLUSTER",
                    "event_ts": ts,
                    "direction": direction,
                    "intensity": float(streak),
                    "trigger_metric": "consecutive_incorrect_predictions",
                    "trigger_threshold": float(V2_FAILURE_CLUSTER_LENGTH),
                    "trigger_version": TRIGGER_VERSION,
                    "is_post_event_analysis": 0,
                    "event_fingerprint": _fingerprint("V2_FAILURE_CLUSTER", ts, direction, TRIGGER_VERSION),
                })
    return events


def detect_v1_btc_divergence(conn, start_ts, end_ts):
    """Explicitly a post-outcome research event -- is_post_event_analysis
    is always 1 here. V1's reading at T only becomes 'divergent' once
    BTC's subsequent move is known; this must never be read as evidence
    V1 predicted the move."""
    _validate_window(start_ts, end_ts)
    raw_prices = _fetch_bounded_prices(conn, start_ts, end_ts)
    prices = _resample_daily(raw_prices)
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
        direction = f"v1_{'bullish' if v1_bullish else 'bearish'}_btc_{'up' if btc_up else 'down'}"
        events.append({
            "category": "V1_BTC_DIVERGENCE",
            "event_ts": future[0],
            "direction": direction,
            "intensity": abs(pct_return),
            "trigger_metric": "v1_extreme_vs_24h_btc_return",
            "trigger_threshold": LARGE_MOVE_THRESHOLD_PCT,
            "trigger_version": TRIGGER_VERSION,
            "is_post_event_analysis": 1,
            "event_fingerprint": _fingerprint("V1_BTC_DIVERGENCE", future[0], direction, TRIGGER_VERSION),
        })
    return events
