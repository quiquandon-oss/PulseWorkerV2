"""Hyperliquid asset-universe audit: which zero-cost cross-asset data does Hyperliquid expose? RESEARCH ONLY.

Pure functions (no network) used by `hyperliquid_asset_universe_run.py`. Nothing here is a V1 source: no
weight, no methodology version, no Risk Regime Shock score. Candidate #1 stays NEW_SIGNAL / Risk Regime Shock
/ REGIME_MODIFIER / DATA_COLLECTION_REQUIRED.

What an instrument *is* comes from the API, never from its ticker: native perps price off the validator
oracle (a weighted median of crypto-exchange spot prices); HIP-3 perps price off an oracle the deployer
defines and sets, described only by the deployer's own `perpAnnotation` text. A HIP-3 "GOLD" is therefore a
builder/oracle-based synthetic perpetual referencing gold, not gold and not tokenized gold.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from hyperliquid_leverage_stress import classify_timing, parse_candles, percentile_rank  # noqa: F401  (re-exported)

UTC = timezone.utc
HOUR = 3_600_000
DAY = 24 * HOUR
BASELINE_HOURS = 7 * 24
MIN_BASELINE_POINTS = 120          # of 168 hourly points, same convention as the BTC leverage component
MAX_CANDLE_AGE_MS = 2 * HOUR       # a V1 observation is covered if the last closed 1h candle closed <= 2h earlier
CANDLE_HISTORY_LIMIT = 5000        # docs: "Only the most recent 5000 candles are available"
PAGE_ROWS = 500                    # docs: time-range responses return at most 500 elements

CATEGORIES = ("GOLD", "OIL / ENERGY", "EQUITY INDEX", "FX / USD", "INTEREST RATE / BOND", "COMMODITY",
              "OTHER MACRO", "CRYPTO", "UNKNOWN")

# Instrument types (section 10). Only the API decides which applies.
NATIVE_PERP = "PERPETUAL/DERIVATIVE REPRESENTATION (native perp, validator oracle from crypto-exchange spot)"
HIP3_SYNTHETIC = "BUILDER/ORACLE-BASED SYNTHETIC EXPOSURE (HIP-3 perp, deployer-set oracle)"
SPOT_TOKEN = "TOKENIZED ASSET (spot token; issuer claim not verifiable from the Hyperliquid API)"

# Ticker/annotation keywords -> category. Applied to the deployer's annotation text first, ticker second.
_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("GOLD", ("gold", "xau", "paxg")),
    ("OIL / ENERGY", ("crude", "brent", "wti", "natural gas", "henry hub", "diesel", "ulsd", "gasoline",
                      "energy select", "energy companies")),
    ("INTEREST RATE / BOND", ("treasury", "yield on", "bond")),
    ("FX / USD", ("exchange rate", "eur/usd", "gbp/usd", "usd/jpy", "dxy", "dollar index")),
    ("EQUITY INDEX", ("s&p 500", "500 of the largest", "100 of the largest", "nasdaq", "russell", "2,000 smaller",
                      "japan 225", "korea 200", "msci", "magnificent seven", "semiconductor")),
    ("COMMODITY", ("silver", "copper", "platinum", "palladium", "uranium", "xag", "xpt", "xpd")),
    ("OTHER MACRO", ("volatility index", "implied volatility")),
)


def classify(name: str, annotation: Optional[Dict], concise_category: Optional[str]) -> str:
    """Category from the deployer's own description (preferred) or its concise category; UNKNOWN otherwise.
    Single stocks and memecoins are CRYPTO/UNKNOWN for this purpose; equity ETFs that track a broad index
    count as EQUITY INDEX only when the description says so."""
    text = ((annotation or {}).get("description") or "").lower()
    cat = (annotation or {}).get("category") or concise_category
    if text:
        if cat == "crypto" or "memecoin" in text or "utility token" in text:
            return "CRYPTO"        # incl. crypto aggregates (BTC dominance, TOTAL2, OTHERS)
        if "preferred stock" in text:
            return "UNKNOWN"       # e.g. STRC: a single issuer's preferred share, not a rate or bond
        for label, keys in _RULES:
            if any(k in text for k in keys):
                return label
    if cat == "crypto":
        return "CRYPTO"
    return "UNKNOWN"


def instrument_type(name: str, dex: Optional[str]) -> str:
    return HIP3_SYNTHETIC if dex else NATIVE_PERP


# ---------------- history and coverage ----------------
def last_closed(candles: Sequence[Dict], t_ms: int) -> Optional[Dict]:
    """Latest candle whose close time T is strictly before t_ms (no look-ahead). Candles are sorted by t."""
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["T"] < t_ms:
            lo = mid + 1
        else:
            hi = mid
    return candles[lo - 1] if lo else None


def closes_before(candles: Sequence[Dict], t_ms: int, hours: int) -> List[Dict]:
    """The closed candles in the `hours` before t_ms, oldest first."""
    return [c for c in candles if t_ms - hours * HOUR <= c["T"] < t_ms]


def ret_pct(candles: Sequence[Dict], t_ms: int, hours: int) -> Optional[float]:
    """% change from the close `hours` before the last closed candle to the last closed candle at t_ms."""
    last = last_closed(candles, t_ms)
    if not last or t_ms - last["T"] > MAX_CANDLE_AGE_MS:
        return None
    base = last_closed(candles, last["T"] + 1 - hours * HOUR)
    if not base or base["c"] == 0 or last["T"] - base["T"] > (hours + 2) * HOUR:
        return None
    return (last["c"] / base["c"] - 1) * 100


def hourly_returns(candles: Sequence[Dict], hours: int) -> Dict[int, float]:
    """close-time -> % change over `hours` closed candles, for every candle with an exact predecessor."""
    by_T = {c["T"]: c["c"] for c in candles}
    out = {}
    for c in candles:
        prev = by_T.get(c["T"] - hours * HOUR)
        if prev:
            out[c["T"]] = (c["c"] / prev - 1) * 100
    return out


def return_features(candles: Sequence[Dict], t_ms: int, rets6: Dict[int, float]) -> Dict:
    """Point-in-time price features at t_ms; every input closed before t_ms."""
    last = last_closed(candles, t_ms)
    if not last:
        return {"status": "NO_DATA"}
    if t_ms - last["T"] > MAX_CANDLE_AGE_MS:
        return {"status": "STALE", "last_close_age_h": round((t_ms - last["T"]) / HOUR, 2)}
    r6 = rets6.get(last["T"])
    base = [v for T, v in rets6.items() if last["T"] - BASELINE_HOURS * HOUR < T < last["T"]]
    pct = percentile_rank(r6, base) if r6 is not None and len(base) >= MIN_BASELINE_POINTS else None
    window = closes_before(candles, t_ms, 6)
    return {"status": "OK", "price": last["c"], "ret_6h": r6, "ret_24h": ret_pct(candles, t_ms, 24),
            "ret_6h_pctile_7d": pct, "baseline_points": len(base),
            "moved_last_6h": len({c["c"] for c in window}) > 1 or any(c["v"] > 0 for c in window)}


def flags(f: Dict) -> Dict[str, bool]:
    """Abnormal = outside the asset's own trailing 7-day 5th-95th percentile of 6h returns. A disclosed
    reporting convention reused from the BTC component, not a threshold fitted to Event #15."""
    p = f.get("ret_6h_pctile_7d")
    if p is None:
        return {}
    return {"ret_6h_low": p <= 5.0, "ret_6h_high": p >= 95.0}


def market_hours_profile(candles: Sequence[Dict]) -> Dict:
    """How the instrument behaves when its underlying market is shut: share of hourly candles that are flat
    (o == h == l == c) and that traded nothing, split weekday / weekend (UTC)."""
    def part(rows):
        n = len(rows)
        if not n:
            return {"hours": 0}
        return {"hours": n, "flat_share": round(sum(1 for c in rows if c["o"] == c["h"] == c["l"] == c["c"]) / n, 4),
                "zero_volume_share": round(sum(1 for c in rows if c["v"] == 0) / n, 4),
                "median_abs_1h_move_pct": round(sorted(abs(c["c"] / c["o"] - 1) * 100 for c in rows if c["o"])[n // 2], 4)}
    wk = [c for c in candles if datetime.fromtimestamp(c["t"] / 1000, UTC).weekday() >= 5]
    wd = [c for c in candles if datetime.fromtimestamp(c["t"] / 1000, UTC).weekday() < 5]
    gaps = sum(1 for a, b in zip(candles, candles[1:]) if b["t"] - a["t"] > HOUR)
    return {"weekday": part(wd), "weekend": part(wk), "gaps_over_1h": gaps}


def coverage(candles: Sequence[Dict], v1_ts: Sequence[int], rets6: Dict[int, float]) -> Dict:
    status, moved, weekend_ok, with_base = {}, 0, 0, 0
    for ts in v1_ts:
        f = return_features(candles, ts, rets6)
        status[f["status"]] = status.get(f["status"], 0) + 1
        if f["status"] == "OK":
            moved += f["moved_last_6h"]
            with_base += f["ret_6h_pctile_7d"] is not None
            weekend_ok += datetime.fromtimestamp(ts / 1000, UTC).weekday() >= 5
    ok = status.get("OK", 0)
    return {"v1_observations": len(v1_ts), "usable": ok, "missing": len(v1_ts) - ok, "status": status,
            "coverage_pct": round(100 * ok / len(v1_ts), 2) if v1_ts else None,
            "usable_with_7d_baseline": with_base, "weekend_usable": weekend_ok,
            "moved_in_prior_6h_share": round(moved / ok, 4) if ok else None}


# ---------------- Event #15 ----------------
def event_analysis(candles: Sequence[Dict], rets6: Dict[int, float], event_ms: int, boundary_ms: int) -> Dict:
    """Hourly -48h..+24h around the event. PRE-EVENT only if a flag is ON at the boundary (the issue time of the
    first failed V1 prediction); an episode that cleared before then could not have warned that prediction."""
    grid = [event_ms - 48 * HOUR + i * HOUR for i in range(73)]
    pts = []
    for t in grid:
        f = return_features(candles, t, rets6)
        pts.append((t, f, flags(f) if f["status"] == "OK" else {}))
    b_t = max(t for t in grid if t <= boundary_ms)
    at_b = next(f for t, f, _ in pts if t == b_t)
    at_e = next(f for t, f, _ in pts if t == event_ms)
    pb, pe = (at_b.get("price"), at_e.get("price"))
    timing = {}
    for k in ("ret_6h_low", "ret_6h_high"):
        episodes, cur = [], None
        for t, _, fl in pts:
            if fl.get(k):
                cur = cur or {"start": t, "hours": 0}
                cur["end"], cur["hours"] = t, cur["hours"] + 1
            elif cur:
                episodes.append(cur)
                cur = None
        if cur:
            episodes.append(cur)
        active = any(e["start"] <= b_t <= e["end"] for e in episodes)
        later = [e["start"] for e in episodes if e["start"] > b_t]
        timing[k] = {"active_at_boundary": active,
                     "classification": "PRE-EVENT" if active else classify_timing(later[0] if later else None, boundary_ms, event_ms),
                     "persisted_hours_max": max((e["hours"] for e in episodes), default=0),
                     "episodes": [{"start": iso(e["start"]), "end": iso(e["end"]), "hours": e["hours"],
                                   "class": classify_timing(e["start"], boundary_ms, event_ms)} for e in episodes]}
    usable = at_b.get("status") == "OK" and at_e.get("status") == "OK"
    return {"usable": usable, "status_at_boundary": at_b.get("status"), "status_at_event": at_e.get("status"),
            "price_at_boundary": pb, "ret_24h_before_boundary_pct": rnd(at_b.get("ret_24h")),
            "ret_6h_pctile_at_boundary": at_b.get("ret_6h_pctile_7d"),
            "change_boundary_to_event_pct": rnd((pe / pb - 1) * 100) if usable and pb else None,
            "ret_6h_pctile_at_event": at_e.get("ret_6h_pctile_7d"), "flag_timing": timing,
            "overall": overall_class(timing) if usable else "UNUSABLE"}


def overall_class(timing: Dict) -> str:
    order = ("PRE-EVENT", "CONTEMPORANEOUS", "POST-EVENT")
    found = [v["classification"] for v in timing.values()]
    for c in order:
        if c in found:
            return c
    return "NO ABNORMAL MOVE"


# ---------------- cross-asset feasibility ----------------
def spearman(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    if len(x) < 10:
        return None
    def rank(v):
        o = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(o):
            j = i
            while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
                j += 1
            for k in range(i, j + 1):
                r[o[k]] = (i + j) / 2
            i = j + 1
        return r
    rx, ry = rank(list(x)), rank(list(y))
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(num / den, 3) if den else None


def joint_condition(btc: Sequence[Dict], other: Sequence[Dict], btc_dir: str, other_dir: str,
                    ts: Optional[Sequence[int]] = None) -> Dict:
    """Counts V1 observations where BTC's 6h move is abnormal in `btc_dir` AND the other asset's 6h move is
    abnormal in `other_dir` (both against their own trailing 7-day percentiles). Descriptive only: V1
    observations cluster in time, so `both_distinct_days` is the more honest count of separate episodes."""
    ts = ts or [None] * len(btc)
    trip = [(b, o, t) for b, o, t in zip(btc, other, ts)
            if b.get("ret_6h_pctile_7d") is not None and o.get("ret_6h_pctile_7d") is not None]
    pairs = [(b, o) for b, o, _ in trip]
    hit = lambda f, d: f["ret_6h_pctile_7d"] <= 5.0 if d == "down" else f["ret_6h_pctile_7d"] >= 95.0
    b_n = sum(1 for b, _ in pairs if hit(b, btc_dir))
    both = sum(1 for b, o in pairs if hit(b, btc_dir) and hit(o, other_dir))
    o_n = sum(1 for _, o in pairs if hit(o, other_dir))
    expected = round(b_n * o_n / len(pairs), 2) if pairs else None
    days = {datetime.fromtimestamp(t / 1000, UTC).date() for b, o, t in trip if t and hit(b, btc_dir) and hit(o, other_dir)}
    return {"observations_both_measurable": len(pairs), "btc_abnormal": b_n, "other_abnormal": o_n,
            "both": both, "both_if_independent": expected, "both_distinct_days": len(days)}


def price_consistency(token_px: Optional[float], reference_px: Optional[float], tolerance: float = 0.05) -> str:
    """A spot token whose name implies 1:1 exposure must trade near the matching reference perp."""
    if not token_px or not reference_px:
        return "NOT CHECKABLE"
    return "CONSISTENT" if abs(token_px / reference_px - 1) <= tolerance else "NOT CONSISTENT WITH NAMED UNDERLYING"


# ---------------- helpers ----------------
def iso(t_ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(t_ms / 1000, UTC).isoformat() if t_ms else None


def rnd(x: Optional[float], n: int = 4) -> Optional[float]:
    return None if x is None else round(x, n)
