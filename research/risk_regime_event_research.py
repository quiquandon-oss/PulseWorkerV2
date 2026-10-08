"""Risk Regime HUMAN RESEARCH: point-in-time raw-dimension analysis of Event #15 and the 123 episodes. RESEARCH ONLY.

Reads only the committed artifacts of `risk_regime_reconstruction.py` (raw observations, DOC articles, V1 history,
episodes). Computes no score, no weights, no combined indicator and no threshold of its own.

"Abnormal" is the repository's existing, disclosed reporting convention (`hyperliquid_leverage_stress.py`):
a measure outside the 5th-95th percentile of its own trailing 7-day history, where that history is built only
from values available before the time being assessed. Daily DeFiLlama series have 7 points in 7 days, so their
trailing history is the previous 30 daily values (disclosed adaptation). A measure with too little history is
`None` (inconclusive), never filled. Every measure is reported separately; base rates at all 575 V1
observations are computed with the identical rule so episode frequencies can be compared with chance.

Time labels (relative to a boundary b and the outcome horizon h of the prediction):
  PRE-EVENT        value available at or before b (the only values used as evidence)
  CONTEMPORANEOUS  the same measure evaluated at the outcome time b + h (never used as evidence)
  POST-EVENT       the same measure 24 h after the outcome (excluded)

Usage: python3 research/risk_regime_event_research.py --results research/results --out research/results/risk_regime_event_research.json
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hyperliquid_leverage_stress import percentile_rank  # noqa: E402  (existing convention, reused)
from risk_regime_data import iso  # noqa: E402

MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
LO_PCT, HI_PCT = 5.0, 95.0                       # existing convention
TRAIL_MS = 7 * DAY
DAILY_TRAIL_POINTS = 30
MIN_HISTORY = 24                                 # fewer trailing values than this -> inconclusive
EVENT15 = {"boundary_ms": 1790510487481, "event_ms": 1790564484213, "horizon_h": 24,
           "boundary_label": "2026-09-27 12:01:27 UTC (PRED-1125, locked)",
           "sensitivity_boundary_ms": 1790499673795,
           "sensitivity_label": "2026-09-27 09:01 UTC (PRED-1124, first failing prediction of PREDRUN-24h-1124 / RE-15)"}

HL = "hyperliquid_hip3|{}|close"
PRICE_SERIES = {"BTC": HL.format("BTC"), "SP500": HL.format("xyz:SP500"), "XYZ100": HL.format("xyz:XYZ100"),
                "GOLD": HL.format("xyz:GOLD"), "SILVER": HL.format("xyz:SILVER"), "COPPER": HL.format("xyz:COPPER"),
                "BRENTOIL": HL.format("xyz:BRENTOIL"), "EUR": HL.format("xyz:EUR")}
TEN_Y = HL.format("para:10Y")
DVOL = "deribit_dvol|BTC DVOL|dvol_close"
FUNDING = "hyperliquid_btc_funding|BTC|funding_rate"
PREMIUM = "hyperliquid_btc_funding|BTC|premium"
DAILY = {"stablecoins_total": "defillama_stablecoins|ALL_STABLECOINS|circulating_usd",
         "USDT": "defillama_stablecoins|USDT|circulating_usd", "USDC": "defillama_stablecoins|USDC|circulating_usd",
         "DEX_volume": "defillama_dex|ALL_DEXS|dex_volume_usd", "TVL": "defillama_tvl|ALL_CHAINS|tvl_usd"}
GD = "gdelt_events|GDELT 2.0 events|{}"
GEO_SHOCK = "gdelt_events|geo_shock (frozen GDELT component)|geo_shock_score"
DOC_TOPICS = ("crypto", "monetary_policy", "geopolitical_conflict", "energy")
V1_SOURCES = ("geopolitics", "macrogeo", "oil", "yield10y", "nasdaq", "sp500", "usd", "funding", "longshort", "hypefunding")
MAX_AGE = {"hourly": 2 * HOUR, "gdelt": 45 * MIN, "daily": 2 * DAY + HOUR, "v1": 6 * HOUR}

# The 24 dimensions of the brief -> the measures computed for each (dimension 22-23 are descriptive only).
DIMENSIONS = {
    1: ("BTC price/returns", ["BTC.chg24", "BTC.chg6"]),
    2: ("Deribit DVOL", ["DVOL.level", "DVOL.chg24"]),
    3: ("Hyperliquid funding", ["funding.level", "funding.chg24"]),
    4: ("Hyperliquid premium", ["premium.level", "premium.chg24"]),
    5: ("Hyperliquid SP500 / XYZ100", ["SP500.chg24", "SP500.chg6", "XYZ100.chg24", "XYZ100.chg6"]),
    6: ("GOLD", ["GOLD.chg24", "GOLD.chg6"]),
    7: ("SILVER", ["SILVER.chg24", "SILVER.chg6"]),
    8: ("COPPER", ["COPPER.chg24", "COPPER.chg6"]),
    9: ("BRENTOIL", ["BRENTOIL.chg24", "BRENTOIL.chg6"]),
    10: ("EUR", ["EUR.chg24", "EUR.chg6"]),
    11: ("10Y", ["10Y.chg24", "10Y.chg6"]),
    12: ("Stablecoin total supply", ["stablecoins_total.dchg"]),
    13: ("USDT", ["USDT.dchg"]),
    14: ("USDC", ["USDC.dchg"]),
    15: ("DEX volume", ["DEX_volume.level", "DEX_volume.dchg"]),
    16: ("TVL", ["TVL.dchg"]),
    17: ("GDELT raw event volume", ["gdelt_total.batch", "gdelt_total.sum24"]),
    18: ("GDELT escalation volume", ["gdelt_escalation.batch", "gdelt_escalation.sum24", "gdelt_escalation.share24"]),
    19: ("GDELT corridor escalation", ["gdelt_corridor.batch", "gdelt_corridor.sum24"]),
    20: ("GDELT geo_shock", ["geo_shock.level", "geo_shock.elevated"]),
    21: ("GDELT DOC article volume", []),
    22: ("GDELT DOC topic distribution", []),
    23: ("GDELT article concentration/acceleration", []),
    24: ("existing V1 source readings", [f"v1.{s}" for s in V1_SOURCES] + ["v1.score"]),
}
EVIDENCE_DIMS = [k for k, (_, m) in DIMENSIONS.items() if m]


# ---------------- point-in-time store ----------------
class PIT:
    """Per series, observations sorted by available_at. `asof` never returns anything published after t."""

    def __init__(self, rows: Sequence[Dict]):
        by = defaultdict(list)
        for o in rows:
            by[f"{o['source']}|{o['instrument']}|{o['metric']}"].append(o)
        self.s = {}
        for k, v in by.items():
            v.sort(key=lambda o: (o["available_at"], o["timestamp"]))
            self.s[k] = (v, [o["available_at"] for o in v])

    def asof(self, key: str, t: int, max_age: Optional[int] = None) -> Optional[Dict]:
        if key not in self.s:
            return None
        v, av = self.s[key]
        i = bisect.bisect_right(av, t) - 1
        if i < 0:
            return None
        o = v[i]
        if max_age is not None and t - o["available_at"] > max_age:
            return None
        return o

    def window(self, key: str, lo: int, hi: int) -> List[Dict]:
        """Observations with lo < available_at <= hi."""
        if key not in self.s:
            return []
        v, av = self.s[key]
        return v[bisect.bisect_right(av, lo):bisect.bisect_right(av, hi)]


# ---------------- measures (each returns a raw value known at t, or None) ----------------
def m_change(p: PIT, key: str, t: int, h: int, pct: bool) -> Optional[float]:
    a, b = p.asof(key, t, MAX_AGE["hourly"]), p.asof(key, t - h, MAX_AGE["hourly"])
    if a is None or b is None or a["timestamp"] == b["timestamp"]:
        return None if a is None or b is None else 0.0
    return 100.0 * (a["value"] / b["value"] - 1) if pct else a["value"] - b["value"]


def m_level(p: PIT, key: str, t: int, age: int) -> Optional[float]:
    o = p.asof(key, t, age)
    return None if o is None else o["value"]


def daily_points(p: PIT, key: str, t: int, n: int) -> List[Dict]:
    """Last n daily observations available at t (oldest first)."""
    if key not in p.s:
        return []
    v, av = p.s[key]
    i = bisect.bisect_right(av, t)
    return v[max(0, i - n):i]


def m_dchg(p: PIT, key: str, t: int) -> Optional[float]:
    pts = daily_points(p, key, t, 2)
    if len(pts) < 2 or t - pts[-1]["available_at"] > MAX_AGE["daily"] or pts[-1]["timestamp"] - pts[-2]["timestamp"] != DAY:
        return None
    return 100.0 * (pts[-1]["value"] / pts[-2]["value"] - 1)


def m_gdelt_batch(p: PIT, metric: str, t: int) -> Optional[float]:
    return m_level(p, GD.format(metric), t, MAX_AGE["gdelt"])


def m_gdelt_sum(p: PIT, metric: str, t: int, h: int = DAY) -> Optional[float]:
    w = p.window(GD.format(metric), t - h, t)
    return float(sum(o["value"] for o in w)) if len(w) >= 0.9 * h / (15 * MIN) else None


def measure(p: PIT, name: str, t: int, v1: "V1") -> Optional[float]:
    base, _, kind = name.partition(".")
    if base in PRICE_SERIES:
        return m_change(p, PRICE_SERIES[base], t, DAY if kind == "chg24" else 6 * HOUR, True)
    if base == "10Y":
        return m_change(p, TEN_Y, t, DAY if kind == "chg24" else 6 * HOUR, False)
    if base in ("DVOL", "funding", "premium"):
        key = {"DVOL": DVOL, "funding": FUNDING, "premium": PREMIUM}[base]
        return m_level(p, key, t, MAX_AGE["hourly"]) if kind == "level" else m_change(p, key, t, DAY, False)
    if base in DAILY:
        if kind == "level":
            o = p.asof(DAILY[base], t, MAX_AGE["daily"])
            return None if o is None else o["value"]
        return m_dchg(p, DAILY[base], t)
    if base.startswith("gdelt_"):
        metric = {"gdelt_total": "total_events", "gdelt_escalation": "escalation_events", "gdelt_corridor": "corridor_escalation_events"}[base]
        if kind == "batch":
            return m_gdelt_batch(p, metric, t)
        if kind == "sum24":
            return m_gdelt_sum(p, metric, t)
        tot, esc = m_gdelt_sum(p, "total_events", t), m_gdelt_sum(p, "escalation_events", t)
        return None if not tot or esc is None else esc / tot
    if base == "geo_shock":
        o = p.asof(GEO_SHOCK, t, MAX_AGE["gdelt"])
        if o is None:
            return None
        return o["value"] if kind == "level" else (1.0 if o["raw"].get("elevated") else 0.0)
    if base == "v1":
        return v1.value(kind, t)
    raise KeyError(name)


class V1:
    """V1 observations (ts = the time the reading existed). Read-only extract from risk_regime_history.json."""

    def __init__(self, rows: Sequence[Dict]):
        self.rows = sorted(rows, key=lambda r: r["ts"])
        self.ts = [r["ts"] for r in self.rows]

    def at(self, t: int) -> Optional[Dict]:
        i = bisect.bisect_right(self.ts, t) - 1
        return self.rows[i] if i >= 0 and t - self.rows[i]["ts"] <= MAX_AGE["v1"] else None

    def value(self, src: str, t: int) -> Optional[float]:
        r = self.at(t)
        if r is None:
            return None
        return r["v1_score"] if src == "score" else r["v1_sources"].get(src)

    def history(self, src: str, t: int) -> List[float]:
        lo = bisect.bisect_left(self.ts, t - TRAIL_MS)
        hi = bisect.bisect_right(self.ts, t)
        vals = [(r["v1_score"] if src == "score" else r["v1_sources"].get(src)) for r in self.rows[lo:hi]]
        return [x for x in vals[:-1] if x is not None]          # exclude the current reading itself


def trailing(p: PIT, v1: V1, name: str, t: int) -> List[float]:
    """The measure's own history, built only from values available before t."""
    base, _, kind = name.partition(".")
    if base == "v1":
        return v1.history(kind, t)
    if base in DAILY:
        pts = daily_points(p, DAILY[base], t, DAILY_TRAIL_POINTS + 2)[:-1]
        if kind == "level":
            return [o["value"] for o in pts]
        return [100.0 * (b["value"] / a["value"] - 1) for a, b in zip(pts, pts[1:]) if b["timestamp"] - a["timestamp"] == DAY]
    if base.startswith("gdelt_") and kind == "batch":
        metric = {"gdelt_total": "total_events", "gdelt_escalation": "escalation_events", "gdelt_corridor": "corridor_escalation_events"}[base]
        return [o["value"] for o in p.window(GD.format(metric), t - TRAIL_MS, t - 1)][:-1]
    if base == "geo_shock":
        return [o["value"] for o in p.window(GEO_SHOCK, t - TRAIL_MS, t - 1)]
    out = []
    for k in range(1, 7 * 24 + 1):                                 # hourly grid over the trailing 7 days
        x = measure(p, name, t - k * HOUR, v1)
        if x is not None:
            out.append(x)
    return out


def assess(p: PIT, v1: V1, name: str, t: int) -> Dict:
    x = measure(p, name, t, v1)
    if name == "geo_shock.elevated":
        # The frozen component's own flag (its rule, not a new threshold).
        return {"value": x, "abnormal": None if x is None else bool(x), "pct_rank": None, "n_hist": None, "rule": "component flag"}
    hist = trailing(p, v1, name, t)
    if x is None or len(hist) < MIN_HISTORY and not (name.split(".")[0] in DAILY and len(hist) >= 20):
        return {"value": x, "abnormal": None, "pct_rank": None, "n_hist": len(hist)}
    r = percentile_rank(x, hist)
    med_abs = sorted(abs(h) for h in hist)[len(hist) // 2]
    return {"value": round(x, 6), "pct_rank": r, "abnormal": r < LO_PCT or r > HI_PCT, "n_hist": len(hist),
            "trailing_median_abs": round(med_abs, 6), "x_over_median_abs": round(abs(x) / med_abs, 2) if med_abs else None}


def all_measures() -> List[str]:
    return [m for _, (_, ms) in DIMENSIONS.items() for m in ms]


def assess_all(p: PIT, v1: V1, t: int) -> Dict[str, Dict]:
    return {m: assess(p, v1, m, t) for m in all_measures()}


def dim_flags(ms: Dict[str, Dict]) -> Dict[int, Optional[bool]]:
    """A dimension is abnormal if any of its measures is; None if none of its measures could be assessed."""
    out = {}
    for d in EVIDENCE_DIMS:
        fl = [ms[m]["abnormal"] for m in DIMENSIONS[d][1]]
        known = [f for f in fl if f is not None]
        out[d] = None if not known else any(known)
    return out


# ---------------- DOC (descriptive: no 7-day baseline exists, DOC was collected in event windows only) ----------------
def doc_view(p: PIT, arts: Sequence[Dict], t: int) -> Dict:
    out = {"timeline": {}, "articles": {}}
    for tp in DOC_TOPICS:
        key = f"gdelt_doc|topic:{tp}|article_count"
        w24, w6 = p.window(key, t - DAY, t), p.window(key, t - 6 * HOUR, t)
        prev = [o for o in w24 if o["available_at"] <= t - 6 * HOUR]
        s6, sp = sum(o["value"] for o in w6), sum(o["value"] for o in prev)
        out["timeline"][tp] = {"buckets_24h": len(w24), "expected_buckets_24h": 96, "articles_24h": sum(o["value"] for o in w24),
                               "articles_last6h": s6, "articles_prev18h": sp, "buckets_last6h": len(w6), "buckets_prev18h": len(prev),
                               "rate_ratio_last6h_vs_prev18h": round((s6 / max(len(w6), 1)) / (sp / len(prev)), 2) if prev and sp else None}
    tot = sum(v["articles_24h"] for v in out["timeline"].values())
    out["topic_share_24h"] = {tp: round(v["articles_24h"] / tot, 3) for tp, v in out["timeline"].items()} if tot else None
    uniq = {}
    for a in arts:
        if a["available_at"] <= t and a["first_seen"] > t - DAY:
            uniq[(a["topic"], a["url"])] = a
    for tp in DOC_TOPICS:
        xs = [a for (tt, _), a in uniq.items() if tt == tp]
        dom = Counter(a["domain"] for a in xs)
        last3 = sum(1 for a in xs if a["first_seen"] > t - 3 * HOUR)
        out["articles"][tp] = {"unique_articles_24h": len(xs), "domains": len(dom),
                               "top_domain_share": round(dom.most_common(1)[0][1] / len(xs), 3) if xs else None,
                               "top_domains": dom.most_common(3), "last_first_seen": iso(max((a["first_seen"] for a in xs), default=None)),
                               "articles_last3h": last3, "articles_prev21h": len(xs) - last3}
    return out


def gdelt_accel(p: PIT, t: int) -> Dict:
    out = {}
    for m in ("total_events", "escalation_events", "corridor_escalation_events"):
        l3, p21 = m_gdelt_sum(p, m, t, 3 * HOUR), m_gdelt_sum(p, m, t, DAY)
        out[m] = {"last3h": l3, "prev21h": None if p21 is None or l3 is None else p21 - l3,
                  "rate_ratio_last3h_vs_prev21h": round((l3 / 3) / ((p21 - l3) / 21), 2) if l3 is not None and p21 and p21 > l3 else None}
    return out


# ---------------- episodes ----------------
def episode_boundaries(episodes: Sequence[Dict]) -> List[Dict]:
    """Boundary = time of the (first) failing call. PRED: its own ts. PREDRUN: first prediction. V2_FAILURE_CLUSTER:
    first prediction of the matching run. LARGE_MOVE / VOLATILITY_EXPANSION / REGIME_REVERSAL: event_ts - 24 h (the
    triggers are measured over the 24 h / 7 d up to event_ts; disclosed approximation)."""
    P = {e["id"]: e for e in episodes}
    out = []
    for e in episodes:
        h = 24
        if e["type"] in ("PRED_UP_BTC_DOWN", "PRED_DOWN_BTC_UP", "PRED_CONSECUTIVE_FAILURES"):
            b, h = e["anchor_ts"], int(e["subtype"].split("_")[1][:-1])
            rule = "prediction time" if e["type"] != "PRED_CONSECUTIVE_FAILURES" else "first prediction of the run"
        elif e["type"] == "V2_FAILURE_CLUSTER":
            h = int(e["subtype"].split("_")[1][:-1])
            runs = [r for r in episodes if r["type"] == "PRED_CONSECUTIVE_FAILURES" and r["subtype"] == e["subtype"]
                    and any(P.get(f"PRED-{i}", {}).get("anchor_ts") == e["anchor_ts"] for i in r["prediction_ids"])]
            b, rule = (runs[0]["anchor_ts"], f"first prediction of {runs[0]['id']}") if runs else (e["anchor_ts"] - h * HOUR, "event_ts - horizon")
        else:
            b, rule = e["anchor_ts"] - DAY, "event_ts - 24 h"
        if e["id"] == "RE-15":
            b, rule = EVENT15["boundary_ms"], "LOCKED: PRED-1125"
        out.append({"id": e["id"], "type": e["type"], "boundary_ms": b, "boundary": iso(b), "horizon_h": h, "boundary_rule": rule})
    return out


def rates(flag_rows: Sequence[Dict[int, Optional[bool]]], times: Optional[Sequence[int]] = None) -> Dict[int, Dict]:
    """Share of rows where the dimension was abnormal. With `times`, also the number of distinct UTC days involved
    (rows are strongly autocorrelated: many rows on one day are not independent evidence)."""
    out = {}
    for d in EVIDENCE_DIMS:
        idx = [i for i, r in enumerate(flag_rows) if r[d] is not None]
        k = [i for i in idx if flag_rows[i][d]]
        out[d] = {"n": len(idx), "abnormal": len(k), "rate": round(len(k) / len(idx), 3) if idx else None}
        if times is not None:
            out[d]["days"] = len({times[i] // DAY for i in idx})
            out[d]["abnormal_days"] = len({times[i] // DAY for i in k})
    return out


def pair_rates(flag_rows: Sequence[Dict[int, Optional[bool]]]) -> Dict[str, Dict]:
    out = {}
    for a, b in combinations(EVIDENCE_DIMS, 2):
        known = [r for r in flag_rows if r[a] is not None and r[b] is not None]
        k = sum(1 for r in known if r[a] and r[b])
        out[f"{a}+{b}"] = {"n": len(known), "both": k, "rate": round(k / len(known), 3) if known else None}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    R = Path(args.results)
    with gzip.open(R / "risk_regime_raw" / "observations.jsonl.gz", "rt") as f:
        p = PIT([json.loads(line) for line in f])
    with gzip.open(R / "risk_regime_raw" / "gdelt_doc_articles.jsonl.gz", "rt") as f:
        arts = [json.loads(line) for line in f]
    hist = json.load(open(R / "risk_regime_history.json"))
    v1 = V1(hist["rows"])
    episodes = json.load(open(R / "risk_regime_episodes.json"))["episodes"]

    # --- Event #15 ---
    b, e, h = EVENT15["boundary_ms"], EVENT15["event_ms"], EVENT15["horizon_h"]
    pre = assess_all(p, v1, b)
    ev15 = {"event": EVENT15, "boundary": iso(b), "event_time": iso(e),
            "v1_call_at_boundary": {"v1_obs": iso(v1.at(b)["ts"]), "score": v1.at(b)["v1_score"], "direction": v1.at(b)["v1_direction"],
                                    "realized_24h": v1.at(b)["btc_outcome_24h"], "sources": v1.at(b)["v1_sources"]},
            "PRE_EVENT": pre, "PRE_EVENT_dimension_flags": dim_flags(pre),
            "PRE_EVENT_doc": doc_view(p, arts, b), "PRE_EVENT_gdelt_acceleration": gdelt_accel(p, b),
            "sensitivity_0901": {"boundary": iso(EVENT15["sensitivity_boundary_ms"]),
                                 "flags": dim_flags(assess_all(p, v1, EVENT15["sensitivity_boundary_ms"]))},
            "CONTEMPORANEOUS_at_event": assess_all(p, v1, e), "CONTEMPORANEOUS_at_boundary_plus_24h": assess_all(p, v1, b + h * HOUR),
            "POST_EVENT_excluded_event_plus_24h": assess_all(p, v1, e + DAY)}
    ev15["CONTEMPORANEOUS_dimension_flags"] = dim_flags(ev15["CONTEMPORANEOUS_at_event"])
    ev15["POST_EVENT_dimension_flags"] = dim_flags(ev15["POST_EVENT_excluded_event_plus_24h"])
    # Hourly path boundary -> event, per measure: first hour at which it became abnormal (CONTEMPORANEOUS, descriptive).
    first_abn = {}
    for m in all_measures():
        for k in range(1, int((e - b) / HOUR) + 2):
            a = assess(p, v1, m, b + k * HOUR)
            if a["abnormal"]:
                first_abn[m] = {"t": iso(b + k * HOUR), "value": a["value"], "pct_rank": a["pct_rank"]}
                break
    ev15["CONTEMPORANEOUS_first_abnormal_hour"] = first_abn

    # --- base rates at every V1 observation (same rule) and V1 failure vs correct ---
    base_rows, base_t, per_obs = [], [], []
    groups = defaultdict(lambda: {"pre": [], "cont": [], "t": []})
    for r in v1.rows:
        fl = dim_flags(assess_all(p, v1, r["ts"]))
        base_rows.append(fl)
        base_t.append(r["ts"])
        o = r["btc_outcome_24h"]
        per_obs.append({"t": iso(r["ts"]), "flags": fl})
        if o.get("outcome_status") == "RESOLVED" and o.get("realized_direction") not in (None, "FLAT") and r["v1_direction"]:
            ok = r["v1_direction"] == o["realized_direction"]
            cont = dim_flags(assess_all(p, v1, r["ts"] + DAY))
            for g in ("v1_failures" if not ok else "v1_correct", f"v1_{r['v1_direction']}_{'correct' if ok else 'failed'}"):
                groups[g]["pre"].append(fl)
                groups[g]["cont"].append(cont)
                groups[g]["t"].append(r["ts"])

    # --- 123 episodes ---
    eps = episode_boundaries(episodes)
    ep_rows = []
    for x in eps:
        t = x["boundary_ms"]
        ms = assess_all(p, v1, t)
        fl = dim_flags(ms)
        cont = dim_flags(assess_all(p, v1, t + x["horizon_h"] * HOUR))
        vr = v1.at(t)
        x.update({"pre_flags": fl, "pre_abnormal_dims": [d for d, f in fl.items() if f], "contemporaneous_flags": cont,
                  "contemporaneous_only_dims": [d for d in EVIDENCE_DIMS if cont[d] and fl[d] is False],
                  "v1_call": None if vr is None else vr["v1_direction"],
                  "v1_realized_24h": None if vr is None else vr["btc_outcome_24h"].get("realized_direction"),
                  "pre_measures": {m: v for m, v in ms.items() if v["abnormal"]}})
        ep_rows.append(x)
    uniq = {}
    for x in ep_rows:                                     # one row per distinct boundary hour (12h/24h twins, runs)
        uniq.setdefault(x["boundary_ms"] // HOUR, x)
    uniq_rows = list(uniq.values())
    combos = Counter()
    for x in uniq_rows:
        ab = sorted(x["pre_abnormal_dims"])
        for k in (2, 3):
            for c in combinations(ab, k):
                combos[c] += 1
    base_combo = Counter()
    for fl in base_rows:
        ab = sorted(d for d, f in fl.items() if f)
        for k in (2, 3):
            for c in combinations(ab, k):
                base_combo[c] += 1

    out = {"artifact": "risk-regime-event-research", "rule": "abnormal = outside own trailing 7-day 5th-95th percentile "
           "(daily DeFiLlama: previous 30 daily values); existing convention of hyperliquid_leverage_stress.py; no score, no weights",
           "dimensions": {d: n for d, (n, _) in DIMENSIONS.items()}, "event15": ev15,
           "base_rate_all_v1_obs": rates(base_rows, base_t), "n_v1_obs": len(base_rows),
           "v1_groups": {g: {"n": len(x["pre"]), "PRE_EVENT": rates(x["pre"], x["t"]), "CONTEMPORANEOUS_t_plus_24h": rates(x["cont"], x["t"])}
                         for g, x in sorted(groups.items())},
           "episodes": ep_rows, "episode_rates_all": rates([x["pre_flags"] for x in ep_rows]),
           "episode_rates_unique_boundaries": rates([x["pre_flags"] for x in uniq_rows], [x["boundary_ms"] for x in uniq_rows]),
           "n_unique_boundaries": len(uniq_rows),
           "episode_contemporaneous_rates_unique": rates([x["contemporaneous_flags"] for x in uniq_rows], [x["boundary_ms"] for x in uniq_rows]),
           "episode_rates_by_type_unique": {ty: rates([x["pre_flags"] for x in uniq_rows if x["type"] in tys])
                                            for ty, tys in (("UP_call_failed", ("PRED_UP_BTC_DOWN",)), ("DOWN_call_failed", ("PRED_DOWN_BTC_UP",)),
                                                            ("runs_and_clusters", ("PRED_CONSECUTIVE_FAILURES", "V2_FAILURE_CLUSTER")),
                                                            ("market_events", ("LARGE_MOVE", "VOLATILITY_EXPANSION", "REGIME_REVERSAL")))},
           "pair_rates_unique_episodes": pair_rates([x["pre_flags"] for x in uniq_rows]),
           "pair_rates_base": pair_rates(base_rows),
           "top_combinations_unique_episodes": [{"dims": list(c), "episodes": n, "base_obs": base_combo.get(c, 0)}
                                                for c, n in combos.most_common(25)],
           "per_v1_obs_flags": per_obs}
    Path(args.out).write_text(json.dumps(out, indent=1, default=str) + "\n")
    print("OK", len(ep_rows), "episodes", len(uniq_rows), "unique boundaries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
