"""Hyperliquid leverage / market-stress component (Risk Regime Shock, input 2). RESEARCH ONLY.

Reads the free, unauthenticated Hyperliquid Info API (POST https://api.hyperliquid.xyz/info) and derives
transparent, point-in-time raw dimensions for BTC. It is not a V1 source, has no weight, creates no
methodology version and does not replace V1's `funding`, `longshort` or `hypefunding`.

Design rules:
* No black-box score: every dimension is reported on its own. Nothing is combined with GDELT.
* No fitted thresholds: "abnormal" is a reporting convention (outside the 5th-95th percentile of the
  dimension's own trailing 7-day history), the same kind of convention as GDELT's top decile.
* No look-ahead: a candle is usable only after its close time; a funding record only after its time.
* Nothing is filled in: a missing value is reported as missing.
* Hyperliquid is one venue. Nothing here describes the whole crypto market.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

UTC = timezone.utc
INFO_URL = "https://api.hyperliquid.xyz/info"
DOCS_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint"
COIN = "BTC"
FUNDING_FLOOR_HOURLY = 0.0000125   # the fixed interest component V1 already subtracts (CryptoPulse/index.html)
FLOOR_TOL = 1e-10

SOURCE = {
    "name": "Hyperliquid Info API",
    "endpoint": INFO_URL,
    "docs": DOCS_URL,
    "method": "POST JSON",
    "authentication": "none (public)",
    "cost_eur": 0,
    "venue_scope": "Hyperliquid perpetuals only; not the whole crypto market",
}

# Request bodies used by this research. Field names marked "repo" are already used by working code in this
# project (CryptoPulse/index.html, PulseWorkerV2/worker.js); "docs" ones must be confirmed against the
# official Info-endpoint page on the first live run (`verify_*` below fail loudly if they differ).
REQUESTS = {
    "metaAndAssetCtxs": {"body": {"type": "metaAndAssetCtxs"}, "field_basis": "repo (funding, markPx, oraclePx, prevDayPx, midPx); docs (openInterest, dayNtlVlm, premium)"},
    "fundingHistory": {"body": {"type": "fundingHistory", "coin": COIN, "startTime": "<ms>", "endTime": "<ms>"}, "field_basis": "docs (coin, fundingRate, premium, time)"},
    "candleSnapshot": {"body": {"type": "candleSnapshot", "req": {"coin": COIN, "interval": "1h", "startTime": "<ms>", "endTime": "<ms>"}}, "field_basis": "repo (t, c); docs (T, o, h, l, v, n)"},
}

# The raw dimensions asked for (A-J). `expected` is the classification before live verification; the
# runner replaces it with what the live API actually returned.
DIMENSIONS = {
    "A_oi_level": {"field": "metaAndAssetCtxs.openInterest", "expected": "AVAILABLE_LIVE",
                   "history": "No open-interest history request is known in the Info API: history only by collecting snapshots from now on."},
    "B_oi_change": {"field": "derived from collected OI snapshots", "expected": "DERIVABLE_FORWARD_ONLY", "history": "needs A history"},
    "C_oi_acceleration": {"field": "derived from collected OI snapshots", "expected": "DERIVABLE_FORWARD_ONLY", "history": "needs A history"},
    "D_funding_level": {"field": "fundingHistory.fundingRate / metaAndAssetCtxs.funding", "expected": "AVAILABLE_HISTORICALLY", "history": "hourly records"},
    "D2_premium_level": {"field": "fundingHistory.premium / metaAndAssetCtxs.premium", "expected": "AVAILABLE_HISTORICALLY", "history": "hourly records"},
    "E_funding_change": {"field": "derived from D", "expected": "DERIVABLE", "history": "from D"},
    "F_funding_extreme_vs_baseline": {"field": "derived from D / D2", "expected": "DERIVABLE", "history": "from D"},
    "G_price_change": {"field": "candleSnapshot.c", "expected": "AVAILABLE_HISTORICALLY", "history": "most recent candles only (limit to verify)"},
    "H_volume_acceleration": {"field": "candleSnapshot.v", "expected": "DERIVABLE", "history": "from G candles"},
    "I_oi_price_divergence": {"field": "A with G", "expected": "DERIVABLE_FORWARD_ONLY", "history": "needs A history"},
    "J_liquidations": {"field": "none aggregate", "expected": "NOT_AVAILABLE",
                       "history": "No aggregate liquidation request is known in the Info API; per-user fill data is not a market measure."},
}

ABNORMAL_LO, ABNORMAL_HI = 5.0, 95.0      # reporting convention, not a fitted threshold
BASELINE = timedelta(days=7)
MIN_BASELINE_POINTS = 120                 # of 168 hourly points


# ---------------- parsing (strict: a missing or non-numeric field is an error, never a default) ----------------
class ParseError(ValueError):
    pass


def _num(row: Dict, key: str) -> float:
    if key not in row:
        raise ParseError(f"missing field {key!r}")
    try:
        v = float(row[key])
    except (TypeError, ValueError):
        raise ParseError(f"non-numeric {key!r}: {row[key]!r}")
    if not math.isfinite(v):
        raise ParseError(f"non-finite {key!r}")
    return v


def parse_asset_ctx(payload, coin: str = COIN) -> Dict[str, Optional[float]]:
    """metaAndAssetCtxs -> the coin's live context. Fields absent from the payload are reported as None."""
    if not (isinstance(payload, list) and len(payload) == 2):
        raise ParseError("metaAndAssetCtxs: expected [meta, assetCtxs]")
    meta, ctxs = payload
    names = [u.get("name") for u in meta.get("universe", [])]
    if coin not in names:
        raise ParseError(f"{coin} not in universe")
    c = ctxs[names.index(coin)]
    out = {}
    for key, name in (("openInterest", "open_interest"), ("funding", "funding"), ("premium", "premium"), ("markPx", "mark_px"),
                      ("oraclePx", "oracle_px"), ("midPx", "mid_px"), ("prevDayPx", "prev_day_px"), ("dayNtlVlm", "day_ntl_volume")):
        out[name] = _num(c, key) if key in c and c[key] is not None else None
    out["fields_present"] = sorted(k for k in c)
    return out


def parse_funding_history(rows, coin: str = COIN) -> List[Dict]:
    """fundingHistory -> sorted, de-duplicated hourly records {t, funding, premium}."""
    if not isinstance(rows, list):
        raise ParseError("fundingHistory: expected a list")
    seen, out = set(), []
    for r in rows:
        if r.get("coin") not in (None, coin):
            raise ParseError(f"fundingHistory: unexpected coin {r.get('coin')!r}")
        t = int(_num(r, "time"))
        if t in seen:
            continue
        seen.add(t)
        out.append({"t": t, "funding": _num(r, "fundingRate"), "premium": _num(r, "premium")})
    return sorted(out, key=lambda r: r["t"])


def parse_candles(rows) -> List[Dict]:
    """candleSnapshot -> sorted, de-duplicated candles {t (open ms), T (close ms), o, h, l, c, v, n}."""
    if not isinstance(rows, list):
        raise ParseError("candleSnapshot: expected a list")
    seen, out = set(), []
    for r in rows:
        t = int(_num(r, "t"))
        if t in seen:
            continue
        seen.add(t)
        out.append({"t": t, "T": int(_num(r, "T")), "o": _num(r, "o"), "h": _num(r, "h"), "l": _num(r, "l"),
                    "c": _num(r, "c"), "v": _num(r, "v"), "n": int(_num(r, "n")) if "n" in r else None})
    return sorted(out, key=lambda r: r["t"])


# ---------------- point-in-time access ----------------
def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def candles_usable_at(candles: Sequence[Dict], t_ms: int) -> List[Dict]:
    """Only candles whose close time has passed: a candle closing at T is known from T+1 ms."""
    return [c for c in candles if c["T"] < t_ms]


def funding_usable_at(records: Sequence[Dict], t_ms: int) -> List[Dict]:
    return [r for r in records if r["t"] <= t_ms]


def percentile_rank(value: float, history: Sequence[float]) -> Optional[float]:
    if not history:
        return None
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    return round(100.0 * (below + 0.5 * equal) / len(history), 2)


def at_floor(funding: float) -> bool:
    return abs(funding - FUNDING_FLOOR_HOURLY) < FLOOR_TOL


# ---------------- features at one time (BTC, hourly candles + hourly funding) ----------------
def features_at(candles: Sequence[Dict], funding: Sequence[Dict], t_ms: int) -> Dict:
    """Raw dimensions known at t_ms. Each is reported separately; there is no combined score."""
    cs = candles_usable_at(candles, t_ms)
    fs = funding_usable_at(funding, t_ms)
    res: Dict = {"t": datetime.fromtimestamp(t_ms / 1000, UTC).isoformat()}
    hour = 3_600_000
    # freshness: the newest usable candle must have closed within 2 hours, the newest funding within 2 hours
    if not cs or t_ms - cs[-1]["T"] > 2 * hour or not fs or t_ms - fs[-1]["t"] > 2 * hour:
        res["status"] = "STALE_OR_MISSING"
        return res
    closes = [c["c"] for c in cs]

    def ret(n):
        return (closes[-1] / closes[-1 - n] - 1) * 100 if len(closes) > n else None

    res.update(status="OK", last_candle_close=cs[-1]["T"], last_funding_t=fs[-1]["t"], price=closes[-1],
               G_ret_1h=ret(1), G_ret_6h=ret(6), G_ret_24h=ret(24))
    vols = [c["v"] for c in cs]
    if len(vols) >= 6 + 168:
        cur6 = sum(vols[-6:])
        base6 = [sum(vols[i - 6:i]) for i in range(len(vols) - 168, len(vols) - 6 + 1, 6)]
        res["H_volume_6h_vs_7d_median"] = round(cur6 / median(base6), 4) if median(base6) > 0 else None
    f = fs[-1]
    res.update(D_funding=f["funding"], D2_premium=f["premium"], funding_at_floor=at_floor(f["funding"]))
    prev8 = [r for r in fs if r["t"] <= f["t"] - 8 * hour]
    res["E_funding_change_8h"] = f["funding"] - prev8[-1]["funding"] if prev8 else None
    res["E_premium_change_8h"] = f["premium"] - prev8[-1]["premium"] if prev8 else None
    base = [r for r in fs if f["t"] - BASELINE.total_seconds() * 1000 <= r["t"] < f["t"]]
    if len(base) >= MIN_BASELINE_POINTS:
        res["F_premium_pctile_7d"] = percentile_rank(f["premium"], [r["premium"] for r in base])
        res["F_funding_pctile_7d"] = percentile_rank(f["funding"], [r["funding"] for r in base])
        res["funding_at_floor_share_7d"] = round(sum(at_floor(r["funding"]) for r in base) / len(base), 4)
    if len(closes) > 6 + 168:
        rets6 = [(closes[i] / closes[i - 6] - 1) * 100 for i in range(len(closes) - 168, len(closes))]
        res["G_ret_6h_pctile_7d"] = percentile_rank(res["G_ret_6h"], rets6[:-1])
    return res


def abnormal_flags(f: Dict) -> Dict[str, bool]:
    """Which dimensions sit outside their own trailing 5th-95th percentile (reporting convention)."""
    out = {}
    for k in ("F_premium_pctile_7d", "F_funding_pctile_7d", "G_ret_6h_pctile_7d"):
        v = f.get(k)
        if v is not None:
            out[k.replace("_pctile_7d", "_low")] = v <= ABNORMAL_LO
            out[k.replace("_pctile_7d", "_high")] = v >= ABNORMAL_HI
    v = f.get("H_volume_6h_vs_7d_median")
    if v is not None:
        out["H_volume_high"] = v >= 2.0        # disclosed convention: twice the 7-day median 6-hour volume
    return out


# ---------------- timing classification (Event #15) ----------------
def classify_timing(first_seen_ms: Optional[int], pre_boundary_ms: int, event_ms: int) -> str:
    """PRE-EVENT: usable before the first failed V1 prediction of the cluster; CONTEMPORANEOUS: between that
    and the event; POST-EVENT: after the event; UNUSABLE: never observed point-in-time."""
    if first_seen_ms is None:
        return "UNUSABLE"
    if first_seen_ms < pre_boundary_ms:
        return "PRE-EVENT"
    if first_seen_ms <= event_ms:
        return "CONTEMPORANEOUS"
    return "POST-EVENT"


def dumps_deterministic(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n"
