"""Risk Regime Shock: convergence of the GDELT geopolitical component and the Hyperliquid cross-asset market
component around BTC stress. RESEARCH ONLY.

Pure functions (no network). Nothing here is a V1 source and nothing here is a Risk Regime Shock score: the
two components stay separately observable (GEO COMPONENT, MARKET COMPONENT) and are only *classified*
together (CONVERGENCE STATE). No weights, no fitting, no thresholds chosen from Event #15. Candidate #1 stays
NEW_SIGNAL / Risk Regime Shock / REGIME_MODIFIER / DATA_COLLECTION_REQUIRED.

Every rule below is declared here, before any result is computed:

- Market abnormality: an instrument's h-hour return is abnormal when it lies outside its own trailing 7-day
  5th-95th percentile of h-hour returns (the convention already used by both Hyperliquid studies).
- MARKET ELEVATED: at least MARKET_ELEVATED_MIN (3) of the 7 basket instruments abnormal on the 6h horizon.
  Rationale: with ~10 % two-tailed abnormality per instrument, >= 3 of 7 would happen ~2.6 % of the time if the
  instruments were independent, the same order as GDELT's elevated rate (~2.9 %). 2 and 4 are reported too.
- GEO ELEVATED: GDELT geo_shock_score >= 90, unchanged from the frozen GDELT component.
- Temporal proximity: both symmetric windows (descriptive, they look forward) and trailing windows (point-in-time,
  usable) are reported for every requested width; none is selected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import hyperliquid_asset_universe as u

UTC = timezone.utc
HOUR = u.HOUR
HORIZONS = (1, 3, 6)
WINDOWS_H = (0, 1, 3, 6, 12, 24)
MARKET_ELEVATED_MIN = 3
MARKET_ELEVATED_SENSITIVITY = (2, 3, 4)
GEO_ELEVATED_PCT = 90.0
ACTIVE_LOOKBACK_H = 6         # an instrument is "trading" at t if any of its last 6 closed 1h candles moved or traded

# The basket and the transparent groups. `classic_risk_off` is the textbook flight-to-quality direction, declared
# a priori; None = no a-priori direction (oil, silver can move either way in a risk-off).
BASKET = {
    "xyz:SP500":    {"group": "equity",    "classic_risk_off": "down"},
    "xyz:XYZ100":   {"group": "equity",    "classic_risk_off": "down"},
    "xyz:GOLD":     {"group": "commodity", "classic_risk_off": "up"},
    "xyz:SILVER":   {"group": "commodity", "classic_risk_off": None},
    "xyz:COPPER":   {"group": "commodity", "classic_risk_off": "down"},
    "xyz:BRENTOIL": {"group": "commodity", "classic_risk_off": None},
    "xyz:EUR":      {"group": "fx",        "classic_risk_off": "down"},     # EUR/USD down = USD up
    "para:10Y":     {"group": "rates",     "classic_risk_off": "down"},     # yields down = flight to quality
}
BREADTH_BASKET = ("xyz:SP500", "xyz:GOLD", "xyz:SILVER", "xyz:COPPER", "xyz:BRENTOIL", "xyz:EUR", "para:10Y")
# Directions that coincided with BTC selling in the asset-universe audit. Derived from the SAME 575-observation
# window, so any result using it is in-sample and is labelled that way.
BTC_ALIGNED_IN_SAMPLE = {"xyz:SP500": "down", "xyz:XYZ100": "down", "xyz:GOLD": "down", "xyz:SILVER": "down",
                         "xyz:COPPER": "down", "xyz:BRENTOIL": "up", "xyz:EUR": "down", "para:10Y": "up"}


# ---------------- underlying market hours ----------------
def underlying_open(t_ms: int) -> bool:
    """Approximate CME/US session for these underlyings in this (summer-time) window: shut from Friday 21:00 UTC
    to Sunday 22:00 UTC. Holidays and the daily one-hour break are not modelled (disclosed limitation). When
    shut, Hyperliquid prices are Hyperliquid traders' own price discovery, not the underlying market."""
    d = datetime.fromtimestamp(t_ms / 1000, UTC)
    wd, h = d.weekday(), d.hour + d.minute / 60
    if wd == 5:
        return False
    if wd == 4 and h >= 21:
        return False
    if wd == 6 and h < 22:
        return False
    return True


# ---------------- per-instrument point-in-time features ----------------
class Instrument:
    """Hourly candles of one instrument with precomputed h-hour returns; every lookup is point-in-time."""

    def __init__(self, symbol: str, candles: Sequence[Dict]):
        self.symbol = symbol
        self.candles = list(candles)
        self.rets = {h: u.hourly_returns(self.candles, h) for h in HORIZONS}
        self._T = [c["T"] for c in self.candles]

    def features(self, t_ms: int) -> Dict:
        last = u.last_closed(self.candles, t_ms)
        if not last:
            return {"status": "NO_DATA"}
        if t_ms - last["T"] > u.MAX_CANDLE_AGE_MS:
            return {"status": "STALE"}
        recent = u.closes_before(self.candles, t_ms, ACTIVE_LOOKBACK_H)
        active = any(c["v"] > 0 or not (c["o"] == c["h"] == c["l"] == c["c"]) for c in recent)
        out = {"status": "OK" if active else "INACTIVE", "price": last["c"], "underlying_open": underlying_open(t_ms)}
        for h in HORIZONS:
            r = self.rets[h].get(last["T"])
            base = [v for T, v in self.rets[h].items() if last["T"] - u.BASELINE_HOURS * HOUR < T < last["T"]]
            pct = u.percentile_rank(r, base) if r is not None and len(base) >= u.MIN_BASELINE_POINTS else None
            out[f"ret_{h}h"] = None if r is None else round(r, 5)
            out[f"pct_{h}h"] = pct
        return out


def abnormal_dir(f: Dict, h: int = 6) -> Optional[str]:
    """'down' / 'up' / None. INACTIVE or unmeasurable instruments never count as abnormal."""
    p = f.get(f"pct_{h}h")
    if f.get("status") != "OK" or p is None:
        return None
    if p <= 5.0:
        return "down"
    if p >= 95.0:
        return "up"
    return None


def market_state(feats: Dict[str, Dict], h: int = 6) -> Dict:
    """Transparent breadth formulations over the basket; individual components stay visible."""
    dirs = {s: abnormal_dir(f, h) for s, f in feats.items()}
    measurable = [s for s in BREADTH_BASKET if feats.get(s, {}).get("status") == "OK" and feats[s].get(f"pct_{h}h") is not None]
    any_count = sum(1 for s in BREADTH_BASKET if dirs.get(s))
    classic = sum(1 for s in BREADTH_BASKET if BASKET[s]["classic_risk_off"] and dirs.get(s) == BASKET[s]["classic_risk_off"])
    aligned = sum(1 for s in BREADTH_BASKET if dirs.get(s) == BTC_ALIGNED_IN_SAMPLE[s])
    groups = {}
    for g in ("equity", "commodity", "rates", "fx"):
        members = [s for s in BASKET if BASKET[s]["group"] == g]
        groups[f"{g}_stress"] = sorted(f"{s}:{dirs[s]}" for s in members if dirs.get(s))
    open_now = [s for s in BREADTH_BASKET if feats.get(s, {}).get("underlying_open")]
    return {"abnormal_any_count": any_count, "classic_risk_off_count": classic, "btc_aligned_count_in_sample": aligned,
            "measurable": len(measurable), "inactive": sorted(s for s in BREADTH_BASKET if feats.get(s, {}).get("status") == "INACTIVE"),
            "underlying_open_count": len(open_now), "abnormal": {s: d for s, d in sorted(dirs.items()) if d}, **groups,
            "elevated": any_count >= MARKET_ELEVATED_MIN,
            "elevated_sensitivity": {str(k): any_count >= k for k in MARKET_ELEVATED_SENSITIVITY}}


def btc_pairs(btc: Dict, feats: Dict[str, Dict], h: int = 6) -> Dict:
    """Dimensions A and C: BTC jointly abnormal with US equity / with the 10Y yield (raw, both directions)."""
    b = abnormal_dir(btc, h)
    eq = [s for s in ("xyz:SP500", "xyz:XYZ100") if abnormal_dir(feats.get(s, {}), h) == "down"]
    y = abnormal_dir(feats.get("para:10Y", {}), h)
    return {"btc_abnormal": b, "btc_down_equity_down": b == "down" and bool(eq), "equity_down_symbols": eq,
            "btc_down_10y_up": b == "down" and y == "up", "btc_down_10y_down": b == "down" and y == "down"}


# ---------------- GDELT component (values come from the frozen GDELT module) ----------------
def geo_at(grid: Sequence[Dict], t_ms: int) -> Dict:
    """Latest GDELT grid reading at or before t (each grid reading is itself point-in-time). `grid` holds
    {"t_ms", "status", "geo_shock_score", "elevated"} sorted by t_ms on a 15-minute grid."""
    lo, hi = 0, len(grid)
    while lo < hi:
        mid = (lo + hi) // 2
        if grid[mid]["t_ms"] <= t_ms:
            lo = mid + 1
        else:
            hi = mid
    if not lo or t_ms - grid[lo - 1]["t_ms"] > 15 * 60_000:
        return {"status": "NO_DATA", "geo_shock_score": None, "elevated": False, "persistence_h": 0.0}
    r = grid[lo - 1]
    run, i = 0, lo - 1
    while i >= 0 and grid[i].get("elevated") and grid[i].get("status") == "OK":
        run += 1
        i -= 1
    return {"status": r["status"], "geo_shock_score": r.get("geo_shock_score"), "elevated": bool(r.get("elevated")),
            "persistence_h": run * 0.25}


def classify_state(geo_elevated: bool, market_elevated: bool) -> str:
    if geo_elevated and market_elevated:
        return "A_BOTH"
    if geo_elevated:
        return "B_GEO_ONLY"
    if market_elevated:
        return "C_MARKET_ONLY"
    return "D_NEITHER"


def any_in(series: Sequence[Tuple[int, bool]], lo: int, hi: int) -> bool:
    return any(flag for t, flag in series if lo <= t <= hi)


def proximity(t_ms: int, geo: Sequence[Tuple[int, bool]], mkt: Sequence[Tuple[int, bool]]) -> Dict:
    """For each window w: is each component elevated somewhere within [t-w, t+w] (symmetric, descriptive only)
    and within [t-w, t] (trailing, point-in-time)?"""
    out = {}
    for w in WINDOWS_H:
        lo, hi = t_ms - w * HOUR, t_ms + w * HOUR
        g_s, m_s = any_in(geo, lo, hi), any_in(mkt, lo, hi)
        g_t, m_t = any_in(geo, lo, t_ms), any_in(mkt, lo, t_ms)
        out[f"{w}h"] = {"symmetric": classify_state(g_s, m_s), "trailing": classify_state(g_t, m_t)}
    return out


def cooccurrence(geo: Sequence[Tuple[int, bool]], mkt: Sequence[Tuple[int, bool]], w: int, shift_h: int = 0) -> Dict:
    """Hour-grid co-occurrence: share of geo-elevated hours with a market-elevated hour within +-w (market series
    optionally circularly shifted by `shift_h` hours, for the null distribution)."""
    m = list(mkt)
    if shift_h:
        k = shift_h % len(m)
        flags = [f for _, f in m]
        flags = flags[-k:] + flags[:-k]
        m = [(t, f) for (t, _), f in zip(m, flags)]
    mk = [t for t, f in m if f]
    geo_hours = [t for t, f in geo if f]
    hit = 0
    j = 0
    for t in geo_hours:
        while j < len(mk) and mk[j] < t - w * HOUR:
            j += 1
        if j < len(mk) and mk[j] <= t + w * HOUR:
            hit += 1
    return {"geo_hours": len(geo_hours), "with_market_within": hit,
            "share": round(hit / len(geo_hours), 4) if geo_hours else None}


def shift_null(geo, mkt, w: int, min_shift_h: int = 48, step_h: int = 7) -> Dict:
    """Deterministic circular-shift null: every shift of `step_h` hours that is at least `min_shift_h` from zero."""
    n = len(mkt)
    shares = [cooccurrence(geo, mkt, w, s)["share"] for s in range(min_shift_h, n - min_shift_h, step_h)]
    shares = sorted(x for x in shares if x is not None)
    obs = cooccurrence(geo, mkt, w)["share"]
    if not shares or obs is None:
        return {"observed": obs, "null_n": len(shares)}
    return {"observed": obs, "null_median": shares[len(shares) // 2], "null_p95": shares[int(0.95 * (len(shares) - 1))],
            "null_n": len(shares), "share_of_null_at_or_above_observed": round(sum(1 for x in shares if x >= obs) / len(shares), 4)}


def episodes(series: Sequence[Tuple[int, bool]]) -> List[Dict]:
    out, cur = [], None
    for t, f in series:
        if f:
            cur = cur or {"start": t, "end": t, "hours": 0}
            cur["end"], cur["hours"] = t, cur["hours"] + 1
        elif cur:
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


# ---------------- outcome summaries ----------------
def group_performance(rows: Iterable[Dict], key: str) -> Dict:
    """Prediction quality per convergence class. rows: {key, p_up, realized_up, realized_return}."""
    out: Dict[str, Dict] = {}
    for r in rows:
        if r.get("realized_up") is None:
            continue
        g = out.setdefault(r[key], {"n": 0, "hits": 0, "brier": 0.0, "ret": 0.0, "pred_up": 0, "realized_up": 0})
        g["n"] += 1
        g["hits"] += int((r["p_up"] > 0.5) == bool(r["realized_up"])) if r["p_up"] != 0.5 else 0
        g["brier"] += (r["p_up"] - r["realized_up"]) ** 2
        g["ret"] += r["realized_return"] or 0.0
        g["pred_up"] += int(r["p_up"] > 0.5)
        g["realized_up"] += int(r["realized_up"])
    for g in out.values():
        n = g["n"]
        g.update(hit_rate=round(g["hits"] / n, 4), brier=round(g["brier"] / n, 4), mean_return_pct=round(g["ret"] / n, 4),
                 small_sample=n < 30)
        del g["ret"]
    return dict(sorted(out.items()))


def iso(t_ms: Optional[int]) -> Optional[str]:
    return u.iso(t_ms)
