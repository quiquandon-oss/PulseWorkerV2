"""Track A: exploratory analysis of V1 prediction failures on the FROZEN historical window. RESEARCH ONLY.

EXPLORATORY / HYPOTHESIS-GENERATING. Every result here is computed on the same 575 frozen V1 observations
(2026-08-27 -> 2026-10-06) that the earlier Risk Regime work also looked at. Nothing here is a validation; any
hypothesis it suggests must be tested on observations it has not seen (the 30+ later V1 observations are excluded
here on purpose and only counted).

Rules (fixed, existing ones where they exist):
- V1 call: score >= 50 -> UP (EXP-005); outcome: outcome_engine 24h, as stored in risk_regime_history.json.
- Independence: the unit is the FIRST resolved call of each UTC day (24h outcomes of calls on one day overlap).
  Uncertainty: 95 % day-block bootstrap intervals (resampling days), never per-observation p-values.
- Source "lean": the Research Lab's own display convention (learning-core ASSESSMENT_RULES): >= 55 up, <= 45 down.
- Point-in-time: every regime / market input is the latest value available at the V1 observation time.
- V1 reconstruction: learning-core v1Composite with V1_METHODOLOGY_V1 defaults (weights x confidence), JS rounding.

No score, no weights, no V1 change. Output: results/v1_failure_analysis.json.
Usage: python3 research/v1_failure_analysis.py --v1-full <archive extract json> --out research/results/v1_failure_analysis.json
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import risk_regime_event_research as er  # noqa: E402

UTC = timezone.utc
HOUR, DAY = er.HOUR, er.DAY
LEAN_UP, LEAN_DOWN = 55, 45                      # learning-core ASSESSMENT_RULES (existing display convention)
BOOT = 2000
# learning-core V1_METHODOLOGY_V1 (id: weight, confidence) -- copied verbatim, checked by the test against the JS file.
V1_DEFAULTS = {"fng": (14, 1.0), "funding": (15, 1.0), "longshort": (10, 0.5), "global": (6, 1.0), "cryptonews": (15, 0.7),
               "macrogeo": (8, 0.7), "geopolitics": (10, 0.6), "regulatory": (12, 0.6), "sosovalue": (15, 0.7), "onchain": (8, 0.5),
               "oil": (6, 0.6), "yield10y": (5, 0.6), "usd": (8, 0.6), "nasdaq": (6, 0.7), "sp500": (6, 0.7), "ninemag": (8, 0.6),
               "foufi": (6, 0.4), "etfflows": (10, 0.8), "hypefunding": (4, 0.4), "gold": (5, 0.5), "strc": (5, 0.35)}
GROUPS = {"fng": "BREADTH", "global": "BREADTH", "funding": "DERIVATIVES", "longshort": "DERIVATIVES", "hypefunding": "DERIVATIVES",
          "cryptonews": "NEWS", "geopolitics": "NEWS", "regulatory": "NEWS", "sosovalue": "NEWS", "foufi": "NEWS", "macrogeo": "MACRO",
          "oil": "MACRO", "yield10y": "MACRO", "usd": "MACRO", "gold": "MACRO", "nasdaq": "EQUITIES", "sp500": "EQUITIES",
          "ninemag": "EQUITIES", "onchain": "ONCHAIN", "etfflows": "FLOWS", "strc": "TREASURY"}


def js_round(x: float) -> int:
    return math.floor(x + 0.5)


def v1_composite(sources: Dict[str, float], drop: Sequence[str] = ()) -> Optional[int]:
    tot = acc = 0.0
    for k, (w, c) in V1_DEFAULTS.items():
        v = sources.get(k)
        if k in drop or not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        tot += w * c
        acc += v * w * c
    return js_round(acc / tot) if tot else None


def lean(v) -> Optional[str]:
    if not isinstance(v, (int, float)):
        return None
    return "UP" if v >= LEAN_UP else "DOWN" if v <= LEAN_DOWN else "NEUTRAL"


def day_of(ts: int) -> int:
    return ts // DAY


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) < 8:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def day_bootstrap(units: Sequence[Dict], stat: Callable[[Sequence[Dict]], Optional[float]], seed: int = 7) -> Optional[List[float]]:
    """95 % interval by resampling whole days (units are already one per day)."""
    if len(units) < 8:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(BOOT):
        s = stat([units[rng.randrange(len(units))] for _ in units])
        if s is not None:
            vals.append(s)
    vals.sort()
    return [round(vals[int(0.025 * len(vals))], 3), round(vals[int(0.975 * len(vals)) - 1], 3)] if vals else None


def rate(units: Sequence[Dict], key: str = "failed") -> Optional[float]:
    return round(sum(u[key] for u in units) / len(units), 3) if units else None


def first_per_day(rows: Sequence[Dict]) -> List[Dict]:
    seen, out = set(), []
    for r in rows:
        if day_of(r["ts"]) in seen:
            continue
        seen.add(day_of(r["ts"]))
        out.append(r)
    return out


def summarize(label: str, rows: Sequence[Dict]) -> Dict:
    units = first_per_day(rows)
    return {"label": label, "observations": len(rows), "obs_failure_rate": rate(rows), "day_units": len(units),
            "day_failure_rate": rate(units), "day_ci95": day_bootstrap(units, rate)}


def build_rows(frozen: Sequence[Dict], full: Dict[int, Dict], p: "er.PIT") -> List[Dict]:
    rows = []
    for r in frozen:
        o = r["btc_outcome_24h"]
        if o.get("outcome_status") != "RESOLVED" or o.get("realized_direction") not in ("UP", "DOWN") or r["v1_direction"] is None:
            continue
        ts = r["ts"]
        src = full[ts]["sources"]
        btc_now = p.asof(er.PRICE_SERIES["BTC"], ts, 2 * HOUR)
        btc_24 = p.asof(er.PRICE_SERIES["BTC"], ts - DAY, 2 * HOUR)
        btc_7d = p.asof(er.PRICE_SERIES["BTC"], ts - 7 * DAY, 2 * HOUR)
        dvol = p.asof(er.DVOL, ts, 2 * HOUR)
        dvol_hist = [o["value"] for o in p.window(er.DVOL, ts - 7 * DAY, ts)]
        t = datetime.fromtimestamp(ts / 1000, UTC)
        recon = v1_composite(src)
        rows.append({
            "ts": ts, "t": er.iso(ts), "score": r["v1_score"], "call": r["v1_direction"], "realized": o["realized_direction"],
            "fwd_ret": o["forward_return_pct"], "failed": r["v1_direction"] != o["realized_direction"], "sources": src,
            "past_ret_24h": None if not btc_now or not btc_24 else 100 * (btc_now["value"] / btc_24["value"] - 1),
            "trend_7d": None if not btc_now or not btc_7d else 100 * (btc_now["value"] / btc_7d["value"] - 1),
            "dvol_vs_7d_median": None if not dvol or len(dvol_hist) < 24 else dvol["value"] - statistics.median(dvol_hist),
            "weekday": t.weekday(), "weekend_boundary": t.weekday() >= 5,
            "spans_reopen": any(datetime.fromtimestamp((ts + k * HOUR) / 1000, UTC).weekday() == 6 and
                                datetime.fromtimestamp((ts + k * HOUR) / 1000, UTC).hour == 22 for k in range(25)),
            "recon_score": recon, "recon_call": None if recon is None else ("UP" if recon >= 50 else "DOWN"),
        })
    return rows


def source_profile(rows: Sequence[Dict], sid: str) -> Dict:
    vals = [r["sources"].get(sid) for r in rows]
    present = [v for v in vals if isinstance(v, (int, float))]
    runs, cur, prev = [], 0, object()
    for v in vals:
        if v == prev:
            cur += 1
        else:
            if cur:
                runs.append(cur)
            cur, prev = 1, v
    if cur:
        runs.append(cur)
    days_changed = len({day_of(r["ts"]) for r, a, b in zip(rows[1:], vals, vals[1:]) if a != b})
    # predictive (forward 24h return) vs contemporaneous (past 24h return), day units
    units = [r for r in first_per_day(rows) if isinstance(r["sources"].get(sid), (int, float))]
    pred = spearman([u["sources"][sid] for u in units], [u["fwd_ret"] for u in units])
    cont_units = [u for u in units if u["past_ret_24h"] is not None]
    cont = spearman([u["sources"][sid] for u in cont_units], [u["past_ret_24h"] for u in cont_units])
    lean_units = [dict(u, hit=lean(u["sources"][sid]) == u["realized"]) for u in units if lean(u["sources"][sid]) in ("UP", "DOWN")]
    w, c = V1_DEFAULTS[sid]
    return {"source": sid, "group": GROUPS[sid], "default_weight_x_conf": round(w * c, 2), "present": len(present),
            "share_exactly_50": round(sum(v == 50 for v in present) / len(present), 3) if present else None,
            "share_extreme_le10_ge90": round(sum(v <= 10 or v >= 90 for v in present) / len(present), 3) if present else None,
            "distinct_values": len(set(present)), "median_unchanged_run_obs": statistics.median(runs) if runs else None,
            "days_with_any_change": days_changed, "day_units": len(units),
            "spearman_vs_forward_24h": None if pred is None else round(pred, 3),
            "forward_ci95": day_bootstrap(units, lambda us: spearman([u["sources"][sid] for u in us], [u["fwd_ret"] for u in us])),
            "spearman_vs_past_24h": None if cont is None else round(cont, 3),
            "lean_day_units": len(lean_units), "lean_hit_rate": rate(lean_units, "hit"),
            "lean_hit_ci95": day_bootstrap(lean_units, lambda us: rate(us, "hit"))}


RESEARCH_MEASURES = ["BTC.chg24", "DVOL.level", "DVOL.chg24", "funding.level", "premium.level", "premium.chg24", "GOLD.chg24",
                     "SILVER.chg24", "COPPER.chg24", "XYZ100.chg24", "10Y.chg24", "stablecoins_total.dchg", "USDC.dchg", "USDT.dchg",
                     "DEX_volume.dchg", "TVL.dchg", "gdelt_total.sum24", "gdelt_escalation.share24", "gdelt_corridor.sum24",
                     "geo_shock.level", "oi.chg24", "oi.chg72", "oi.level", "bnfund.level"]
ARCHIVE_RATIOS = {"taker_buy_sell_vol_ratio": "sum_taker_long_short_vol_ratio", "top_trader_long_short_ratio": "sum_toptrader_long_short_ratio",
                  "account_long_short_ratio": "count_long_short_ratio"}


def research_dimensions(R: Path, rows: Sequence[Dict]) -> Dict:
    """Track B, EXPLORATORY: does each research dimension (ranked against its own trailing week, point-in-time) relate to
    the FORWARD 24h BTC return (advance warning) or only to the PAST 24h return (contemporaneous / lagging)? One unit
    per UTC day; 95 % day-block bootstrap. Also its overlap with the V1 score."""
    obs = []
    for f in ("risk_regime_raw/observations.jsonl.gz", "risk_regime_oi/observations.jsonl.gz"):
        with gzip.open(R / f, "rt") as fh:
            obs += [json.loads(line) for line in fh]
    # expose provider columns already present (raw) in the checksum-verified archive rows as their own series
    extra = []
    for o in obs:
        if o["source"] == "binance_oi_archive" and o["metric"] == "open_interest":
            for name, col in ARCHIVE_RATIOS.items():
                v = o["raw"].get(col)
                if v not in (None, ""):
                    extra.append(dict(o, metric=name, value=float(v), unit="ratio"))
    p = er.PIT(obs + extra)
    er.enable_oi()
    v1 = er.V1([])
    units = first_per_day(rows)
    out = {"label": "EXPLORATORY (same 41 frozen days used throughout this research)", "day_units": len(units), "dimensions": {}}

    def ranked(name, t):
        if name in ARCHIVE_RATIOS:
            key = f"binance_oi_archive|BTCUSDT|{name}"
            o = p.asof(key, t, 2 * HOUR)
            hist = [x["value"] for x in p.window(key, t - 7 * DAY, t)][:-1]
            return None if o is None or len(hist) < 100 else er.percentile_rank(o["value"], hist)
        return er.assess(p, v1, name, t)["pct_rank"]
    for name in RESEARCH_MEASURES + list(ARCHIVE_RATIOS):
        us = [dict(u, x=ranked(name, u["ts"])) for u in units]
        us = [u for u in us if u["x"] is not None]
        fwd = spearman([u["x"] for u in us], [u["fwd_ret"] for u in us])
        pu = [u for u in us if u["past_ret_24h"] is not None]
        past = spearman([u["x"] for u in pu], [u["past_ret_24h"] for u in pu])
        ov = spearman([u["x"] for u in us], [u["score"] for u in us])
        out["dimensions"][name] = {"day_units": len(us), "rho_forward_24h": None if fwd is None else round(fwd, 3),
                                   "forward_ci95": day_bootstrap(us, lambda s: spearman([u["x"] for u in s], [u["fwd_ret"] for u in s])),
                                   "rho_past_24h": None if past is None else round(past, 3),
                                   "rho_with_v1_score": None if ov is None else round(ov, 3)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1-full", required=True, help="read-only research_sentiment_archive extract: [{ts, score, sources}]")
    ap.add_argument("--results", default=str(Path(__file__).resolve().parent / "results"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    R = Path(args.results)
    frozen = json.load(open(R / "risk_regime_history.json"))["rows"]
    full_list = json.load(open(args.v1_full))
    full = {r["ts"]: r for r in full_list}
    with gzip.open(R / "risk_regime_raw" / "observations.jsonl.gz", "rt") as f:
        p = er.PIT([json.loads(line) for line in f])
    frozen_ts = {r["ts"] for r in frozen}
    assert all(t in full for t in frozen_ts), "archive extract must contain every frozen observation"
    mism = sum(1 for r in frozen if js_round(full[r["ts"]]["score"]) != js_round(r["v1_score"]) or
               any(full[r["ts"]]["sources"].get(k) != v for k, v in r["v1_sources"].items()))
    rows = build_rows(frozen, full, p)
    held_out = sorted(t for t in full if t > max(frozen_ts))
    out = {"artifact": "v1-failure-analysis", "label": "EXPLORATORY: discovery set = the 575 frozen observations; not a validation",
           "inputs": {"frozen_observations": len(frozen), "resolved_used": len(rows), "archive_rows": len(full_list),
                      "archive_vs_frozen_mismatches": mism, "held_out_later_observations_not_analysed": len(held_out),
                      "v1_full_sha256": hashlib.sha256(Path(args.v1_full).read_bytes()).hexdigest()}}
    # A1 baseline and direction
    out["baseline"] = [summarize("all calls", rows), summarize("UP calls", [r for r in rows if r["call"] == "UP"]),
                       summarize("DOWN calls", [r for r in rows if r["call"] == "DOWN"])]
    # A2 time period: chronological halves and calendar weeks
    half = rows[len(rows) // 2]["ts"]
    out["periods"] = [summarize("first half", [r for r in rows if r["ts"] < half]), summarize("second half", [r for r in rows if r["ts"] >= half])]
    out["weeks"] = [summarize(w, [r for r in rows if datetime.fromtimestamp(r["ts"] / 1000, UTC).strftime("%G-W%V") == w])
                    for w in sorted({datetime.fromtimestamp(r["ts"] / 1000, UTC).strftime("%G-W%V") for r in rows})]
    # A3 regimes (point-in-time)
    out["regimes"] = {
        "trend_7d": [summarize("BTC 7d up", [r for r in rows if r["trend_7d"] is not None and r["trend_7d"] > 0]),
                     summarize("BTC 7d down", [r for r in rows if r["trend_7d"] is not None and r["trend_7d"] <= 0])],
        "call_vs_trend": [summarize("UP call in 7d downtrend", [r for r in rows if r["call"] == "UP" and r["trend_7d"] is not None and r["trend_7d"] <= 0]),
                          summarize("UP call in 7d uptrend", [r for r in rows if r["call"] == "UP" and r["trend_7d"] is not None and r["trend_7d"] > 0]),
                          summarize("DOWN call in 7d uptrend", [r for r in rows if r["call"] == "DOWN" and r["trend_7d"] is not None and r["trend_7d"] > 0]),
                          summarize("DOWN call in 7d downtrend", [r for r in rows if r["call"] == "DOWN" and r["trend_7d"] is not None and r["trend_7d"] <= 0])],
        "call_vs_past_24h": [summarize("call agrees with past 24h move", [r for r in rows if r["past_ret_24h"] is not None and (r["past_ret_24h"] > 0) == (r["call"] == "UP")]),
                             summarize("call opposes past 24h move", [r for r in rows if r["past_ret_24h"] is not None and (r["past_ret_24h"] > 0) != (r["call"] == "UP")])],
        "implied_vol": [summarize("DVOL above own 7d median", [r for r in rows if r["dvol_vs_7d_median"] is not None and r["dvol_vs_7d_median"] > 0]),
                        summarize("DVOL at/below own 7d median", [r for r in rows if r["dvol_vs_7d_median"] is not None and r["dvol_vs_7d_median"] <= 0])],
    }
    # A4 weekend
    out["weekend"] = [summarize("boundary Sat/Sun", [r for r in rows if r["weekend_boundary"]]), summarize("boundary Mon-Fri", [r for r in rows if not r["weekend_boundary"]]),
                      summarize("24h window crosses Sunday 22:00 UTC reopen", [r for r in rows if r["spans_reopen"]]),
                      summarize("does not cross reopen", [r for r in rows if not r["spans_reopen"]])]
    # A5 agreement among V1's sources (lean convention)
    for r in rows:
        ls = [lean(v) for v in r["sources"].values()]
        n = sum(x in ("UP", "DOWN") for x in ls)
        r["agree_share"] = sum(x == r["call"] for x in ls) / n if n else None
        r["margin"] = abs(r["score"] - 50)
    cut = sorted(r["agree_share"] for r in rows if r["agree_share"] is not None)
    q1, q2 = cut[len(cut) // 3], cut[2 * len(cut) // 3]
    out["agreement"] = {"note": "tertile cut points computed on this same sample (descriptive only)", "cuts": [round(q1, 3), round(q2, 3)],
                        "bins": [summarize("low agreement", [r for r in rows if r["agree_share"] is not None and r["agree_share"] <= q1]),
                                 summarize("mid agreement", [r for r in rows if r["agree_share"] is not None and q1 < r["agree_share"] <= q2]),
                                 summarize("high agreement", [r for r in rows if r["agree_share"] is not None and r["agree_share"] > q2])],
                        "score_margin": [summarize("score within 3 of 50", [r for r in rows if r["margin"] <= 3]),
                                         summarize("score 4-10 from 50", [r for r in rows if 3 < r["margin"] <= 10]),
                                         summarize("score >10 from 50", [r for r in rows if r["margin"] > 10])]}
    # A6/A7 per source
    out["sources"] = sorted((source_profile(rows, s) for s in V1_DEFAULTS), key=lambda x: -(x["default_weight_x_conf"]))
    # A8 reconstruction of the stored score with the published defaults
    rec = [r for r in rows if r["recon_score"] is not None]
    out["reconstruction"] = {"observations": len(rec), "exact_score_matches": sum(r["recon_score"] == js_round(r["score"]) for r in rec),
                             "mean_abs_gap": round(statistics.fmean(abs(r["recon_score"] - r["score"]) for r in rec), 2),
                             "call_disagreements": sum(r["recon_call"] != r["call"] for r in rec),
                             "call_disagreement_days": len({day_of(r["ts"]) for r in rec if r["recon_call"] != r["call"]}),
                             "stored_failure_rate": rate(rec), "reconstructed_failure_rate": round(sum(r["recon_call"] != r["realized"] for r in rec) / len(rec), 3)}
    # A9 higher-frequency equivalents: Hyperliquid 24/7 perps vs V1's FRED daily readings (day units)
    hf = {}
    units = first_per_day(rows)
    for v1_src, hl in (("sp500", "SP500"), ("nasdaq", "XYZ100"), ("yield10y", "10Y")):
        us = []
        for u in units:
            m = er.measure(p, f"{hl}.chg24", u["ts"], er.V1([]))
            if m is not None and isinstance(u["sources"].get(v1_src), (int, float)):
                us.append(dict(u, hl=m))
        hf[v1_src] = {"day_units": len(us), "v1_reading_vs_fwd": None if len(us) < 8 else round(spearman([u["sources"][v1_src] for u in us], [u["fwd_ret"] for u in us]), 3),
                      "hl_24h_change_vs_fwd": None if len(us) < 8 else round(spearman([u["hl"] for u in us], [u["fwd_ret"] for u in us]), 3),
                      "v1_reading_vs_hl_24h_change": None if len(us) < 8 else round(spearman([u["sources"][v1_src] for u in us], [u["hl"] for u in us]), 3),
                      "v1_weekend_unchanged_share": round(statistics.fmean(
                          [1.0 if a["sources"].get(v1_src) == b["sources"].get(v1_src) else 0.0
                           for a, b in zip(rows, rows[1:]) if b["weekend_boundary"]]), 3)}
    out["higher_frequency"] = hf
    out["research_dimensions"] = research_dimensions(R, rows)
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=True, default=str) + "\n")
    print("OK", len(rows), "resolved rows;", len(first_per_day(rows)), "day units")
    return 0


if __name__ == "__main__":
    sys.exit(main())
