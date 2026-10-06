"""One research run of the Risk Regime Shock convergence experiment (GDELT x Hyperliquid). RESEARCH ONLY.

Read-only everywhere. Reuses the frozen GDELT component (gdelt_geo_shock / gdelt_research_run: official GDELT 2.0
15-minute exports, MD5-verified cache, no look-ahead) and the Hyperliquid asset-universe audit (public Info API,
cached hourly candles). Writes a deterministic artifact; never writes to any database, never calls an LLM,
never touches V1, produces no score and no weights.

Usage:
  python3 research/risk_regime_shock_run.py --v1 <v1_full.json> --predictions <predictions.json> \
      --gdelt-cache <dir> --hl-cache <dir> --out research/results/risk_regime_shock.json

<v1_full.json>: the 575 read-only V1 observations: {"ts", "fd", "ls", "hf", "g", "m", "o", "y", "n", "s", "u", "score"}.
<predictions.json>: read-only V1 predictions in the window: {"id", "ts", "horizon_h", "p_up", "realized_up", "realized_return"}.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gdelt_geo_shock as g  # noqa: E402
import gdelt_research_run as gr  # noqa: E402
import hyperliquid_asset_universe as u  # noqa: E402
import hyperliquid_asset_universe_run as ur  # noqa: E402
import risk_regime_shock as r  # noqa: E402
from hyperliquid_research_run import EVENT15, PRE_BOUNDARY_MS  # noqa: E402

HOUR = r.HOUR
UTC = timezone.utc
V1_SOURCE_KEYS = {"g": "geopolitics", "m": "macrogeo", "o": "oil", "y": "yield10y", "n": "nasdaq", "s": "sp500",
                  "u": "usd", "fd": "funding", "ls": "longshort", "hf": "hypefunding"}


def gdelt_grid(v1_ts: List[int], cache: Path) -> Dict:
    """15-minute geo_shock grid over the V1 history, computed by the frozen GDELT module from the MD5-verified
    export cache. The computed grid is cached (keyed by range and module versions) so reruns are offline."""
    lo = datetime.fromtimestamp(v1_ts[0] / 1000, UTC)
    hi = datetime.fromtimestamp(v1_ts[-1] / 1000, UTC)
    key = cache / f"_geo_grid_{g.FILTER_VERSION}_{g.FEATURE_VERSION}_{v1_ts[0]}_{v1_ts[-1]}.json"
    if key.exists():
        return json.loads(key.read_text())
    times = gr._grid(lo, hi)
    batches = g.required_batches(times)
    idx = gr.master_index(min(batches[0], lo - timedelta(hours=g.BASELINE_HOURS + 2)))
    series, _, log = gr.build_series(batches, idx, cache, datetime.fromtimestamp(EVENT15["event_ts_ms"] / 1000, UTC))
    rows = []
    for t in times:
        s = g.score_at(series, t)
        rows.append({"t_ms": int(t.timestamp() * 1000), "status": s["status"], "geo_shock_score": s.get("geo_shock_score"),
                     "elevated": bool(s.get("elevated"))})
    out = {"rows": rows, "fetch_status_counts": log["fetch_status_counts"], "batches": len(batches)}
    key.write_text(json.dumps(out))
    return out


def load_instruments(v1_ts: List[int], cache: Path):
    """Same request bodies as the asset-universe run, so its cache serves them."""
    start = v1_ts[0] - 9 * u.DAY
    end = max(v1_ts[-1], EVENT15["event_ts_ms"] + 24 * HOUR) + HOUR
    log = Counter()
    syms = ["BTC"] + list(r.BASKET)
    out = {s: r.Instrument(s, ur.candles(s, "1h", start, end, cache, log)) for s in syms}
    return out, dict(log)


def btc_forward_return(btc: r.Instrument, t_ms: int, hours: int = 24):
    """Outcome only (never a feature): BTC % change from the last close before t to the last close before t+hours."""
    a, b = u.last_closed(btc.candles, t_ms), u.last_closed(btc.candles, t_ms + hours * HOUR)
    if not a or not b or b["T"] <= a["T"]:
        return None
    return round((b["c"] / a["c"] - 1) * 100, 4)


def snapshot(t_ms: int, inst: Dict[str, r.Instrument], grid: List[Dict]) -> Dict:
    feats = {s: i.features(t_ms) for s, i in inst.items() if s != "BTC"}
    btc = inst["BTC"].features(t_ms)
    m = r.market_state(feats)
    return {"t": r.iso(t_ms), "geo": r.geo_at(grid, t_ms), "market": m, "btc": {k: btc.get(k) for k in ("ret_1h", "ret_3h", "ret_6h", "pct_6h")},
            "btc_pairs": r.btc_pairs(btc, feats),
            "instruments": {s: {k: f.get(k) for k in ("status", "underlying_open", "ret_1h", "ret_3h", "ret_6h", "pct_1h", "pct_3h", "pct_6h")}
                            for s, f in feats.items()}}


def spearman(a, b):
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    return (u.spearman([p[0] for p in pairs], [p[1] for p in pairs]), len(pairs))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1", required=True)
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--gdelt-cache", required=True)
    ap.add_argument("--hl-cache", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    v1 = sorted(json.load(open(args.v1)), key=lambda x: x["ts"])
    preds = sorted(json.load(open(args.predictions)), key=lambda x: x["ts"])
    v1_ts = [x["ts"] for x in v1]
    art = {"artifact": "risk-regime-shock-convergence-research", "candidate": "NEW_SIGNAL / Risk Regime Shock / REGIME_MODIFIER / DATA_COLLECTION_REQUIRED",
           "v1_impact": "NONE: research only. No Risk Regime Shock score, no weights, no V1 change, Candidate #1 not activated.",
           "event": EVENT15, "pre_event_boundary": r.iso(PRE_BOUNDARY_MS),
           "declared_rules": {"market_abnormal": "6h return outside own trailing 7-day 5th-95th percentile (1h/3h also reported)",
                              "market_elevated": f">= {r.MARKET_ELEVATED_MIN} of {len(r.BREADTH_BASKET)} basket instruments abnormal (sensitivity {list(r.MARKET_ELEVATED_SENSITIVITY)})",
                              "geo_elevated": f"GDELT geo_shock_score >= {r.GEO_ELEVATED_PCT} (frozen GDELT component)",
                              "inactive": f"no candle moved or traded in the last {r.ACTIVE_LOOKBACK_H} h -> never abnormal",
                              "windows_h": list(r.WINDOWS_H), "basket": r.BASKET, "breadth_basket": list(r.BREADTH_BASKET),
                              "btc_aligned_directions_in_sample": r.BTC_ALIGNED_IN_SAMPLE},
           "inputs": {"v1_observations": len(v1), "predictions": len(preds),
                      "v1_sha256": hashlib.sha256(json.dumps(v1, sort_keys=True).encode()).hexdigest(),
                      "predictions_sha256": hashlib.sha256(json.dumps(preds, sort_keys=True).encode()).hexdigest()},
           "open_interest": "FUTURE LEVERAGE COMPONENT: PROSPECTIVE COLLECTION REQUIRED (no free history); not used."}
    try:
        geo = gdelt_grid(v1_ts, Path(args.gdelt_cache))
        grid = geo["rows"]
        inst, hl_log = load_instruments(v1_ts, Path(args.hl_cache))
        art["data"] = {"gdelt_grid_points": len(grid), "gdelt_batches": geo["batches"], "gdelt_fetch": geo["fetch_status_counts"],
                       "gdelt_status": dict(Counter(x["status"] for x in grid)), "hl_requests": hl_log,
                       "hl_candles": {s: len(i.candles) for s, i in inst.items()}}
        # GDELT consistency: the grid must reproduce the committed GDELT V1 scores exactly.
        committed = Path(__file__).resolve().parent / "results" / "gdelt_risk_regime_shock_v1_scores.json"
        if committed.exists():
            ref = {x["ts"]: x for x in json.loads(committed.read_text())}
            mism = sum(1 for t in v1_ts if t in ref and r.geo_at(grid, t)["geo_shock_score"] != ref[t]["geo_shock_score"])
            art["data"]["gdelt_reproduces_committed_v1_scores"] = {"compared": len(ref), "mismatches": mism}

        # ---- hourly grid ----
        h0 = (v1_ts[0] // HOUR + 1) * HOUR + 60_000
        hours = list(range(h0, v1_ts[-1] + 1, HOUR))
        snaps = [snapshot(t, inst, grid) for t in hours]
        geo_s = [(t, s["geo"]["elevated"]) for t, s in zip(hours, snaps)]
        mkt_s = [(t, s["market"]["elevated"]) for t, s in zip(hours, snaps)]
        n = len(snaps)
        p_geo = sum(f for _, f in geo_s) / n
        p_mkt = sum(f for _, f in mkt_s) / n
        both = sum(1 for (_, a), (_, b) in zip(geo_s, mkt_s) if a and b)
        art["hourly"] = {
            "points": n, "geo_elevated_rate": round(p_geo, 4), "market_elevated_rate": round(p_mkt, 4),
            "market_elevated_rate_by_threshold": {k: round(sum(s["market"]["elevated_sensitivity"][k] for s in snaps) / n, 4)
                                                  for k in map(str, r.MARKET_ELEVATED_SENSITIVITY)},
            "both_same_hour": both, "both_expected_if_independent": round(p_geo * p_mkt * n, 2),
            "classes": dict(Counter(r.classify_state(a, b) for (_, a), (_, b) in zip(geo_s, mkt_s))),
            "market_elevated_by_session": {
                "underlying_open": sum(1 for t, f in mkt_s if f and r.underlying_open(t)),
                "underlying_shut": sum(1 for t, f in mkt_s if f and not r.underlying_open(t)),
                "open_hours": sum(1 for t in hours if r.underlying_open(t))},
            "abnormal_rate_by_instrument": {s: round(sum(1 for x in snaps if s in x["market"]["abnormal"]) / n, 4) for s in r.BASKET},
            "inactive_rate_by_instrument": {s: round(sum(1 for x in snaps if x["instruments"][s]["status"] == "INACTIVE") / n, 4) for s in r.BASKET},
            "breadth_distribution": {f: dict(sorted(Counter(x["market"][f] for x in snaps).items()))
                                     for f in ("abnormal_any_count", "classic_risk_off_count", "btc_aligned_count_in_sample")},
            "geo_vs_market_spearman": spearman([x["geo"]["geo_shock_score"] for x in snaps], [x["market"]["abnormal_any_count"] for x in snaps])[0],
            "cooccurrence_vs_shift_null": {f"{w}h": r.shift_null(geo_s, mkt_s, w) for w in r.WINDOWS_H},
            "geo_episodes": len(r.episodes(geo_s)), "market_episodes": len(r.episodes(mkt_s)),
            "convergence_threshold_sensitivity": {
                k: {f"{w}h": r.shift_null(geo_s, [(t, x["market"]["elevated_sensitivity"][k]) for t, x in zip(hours, snaps)], w)
                    for w in r.WINDOWS_H} for k in map(str, r.MARKET_ELEVATED_SENSITIVITY)},
        }
        # ---- BTC stress episodes and what preceded them ----
        btc_s = [(t, s["btc_pairs"]["btc_abnormal"] == "down") for t, s in zip(hours, snaps)]
        eps = r.episodes(btc_s)
        rows = []
        for e in eps:
            st = e["start"]
            rows.append({"start": r.iso(st), "hours": e["hours"],
                         "geo_trailing_24h_before": r.any_in(geo_s, st - 24 * HOUR, st - HOUR),
                         "market_trailing_24h_before": r.any_in(mkt_s, st - 24 * HOUR, st - HOUR),
                         "geo_at_start": dict(geo_s).get(st), "market_at_start": dict(mkt_s).get(st),
                         "proximity": r.proximity(st, geo_s, mkt_s)})
        base_before = lambda s_: round(sum(1 for t, _ in s_ if r.any_in(s_, t - 24 * HOUR, t - HOUR)) / n, 4)
        art["btc_stress_episodes"] = {
            "definition": "BTC 6h return <= own trailing 7-day 5th percentile, hourly; contiguous hours = one episode",
            "count": len(eps), "episodes": rows,
            "share_with_geo_in_prior_24h": round(sum(x["geo_trailing_24h_before"] for x in rows) / len(rows), 4) if rows else None,
            "share_with_market_in_prior_24h": round(sum(x["market_trailing_24h_before"] for x in rows) / len(rows), 4) if rows else None,
            "base_rate_any_hour_geo_in_prior_24h": base_before(geo_s), "base_rate_any_hour_market_in_prior_24h": base_before(mkt_s),
            "both_in_prior_24h": sum(1 for x in rows if x["geo_trailing_24h_before"] and x["market_trailing_24h_before"])}

        # ---- Event #15 ----
        e_ms, b_ms = EVENT15["event_ts_ms"], PRE_BOUNDARY_MS
        win = [(t, s) for t, s in zip(hours, snaps) if e_ms - 48 * HOUR <= t <= e_ms + 24 * HOUR]
        first = lambda pred: next((r.iso(t) for t, s in win if pred(s)), None)
        art["event15"] = {
            "at_boundary": snapshot(b_ms, inst, grid), "at_event": snapshot(e_ms, inst, grid),
            "first_geo_elevated_in_window": first(lambda s: s["geo"]["elevated"]),
            "first_market_elevated_in_window": first(lambda s: s["market"]["elevated"]),
            "first_both_same_hour": first(lambda s: s["geo"]["elevated"] and s["market"]["elevated"]),
            "proximity_at_boundary": r.proximity(b_ms, geo_s, mkt_s), "proximity_at_event": r.proximity(e_ms, geo_s, mkt_s),
            "hourly": [{"t": s["t"], "geo": s["geo"]["geo_shock_score"], "geo_elevated": s["geo"]["elevated"],
                        "breadth": s["market"]["abnormal_any_count"], "classic": s["market"]["classic_risk_off_count"],
                        "market_elevated": s["market"]["elevated"], "abnormal": s["market"]["abnormal"],
                        "btc_ret_6h": s["btc"]["ret_6h"], "btc_pct_6h": s["btc"]["pct_6h"],
                        "underlying_open": r.underlying_open(t)} for t, s in win]}

        # ---- 575 observations ----
        obs = []
        for x in v1:
            s = snapshot(x["ts"], inst, grid)
            cls = r.classify_state(s["geo"]["elevated"], s["market"]["elevated"])
            obs.append({"ts": x["ts"], "class": cls, "geo_shock_score": s["geo"]["geo_shock_score"], "geo_elevated": s["geo"]["elevated"],
                        "breadth": s["market"]["abnormal_any_count"], "classic": s["market"]["classic_risk_off_count"],
                        "market_elevated": s["market"]["elevated"], "equity_stress": s["market"]["equity_stress"],
                        "rates_stress": s["market"]["rates_stress"], "commodity_stress": s["market"]["commodity_stress"],
                        "fx_stress": s["market"]["fx_stress"], "btc_down_equity_down": s["btc_pairs"]["btc_down_equity_down"],
                        "btc_down_10y_up": s["btc_pairs"]["btc_down_10y_up"],
                        "trailing": {w: v["trailing"] for w, v in r.proximity(x["ts"], geo_s, mkt_s).items() if w in ("6h", "24h")},
                        "v1_score": x.get("score"), "btc_fwd_24h_pct": btc_forward_return(inst["BTC"], x["ts"]),
                        **{k: x.get(k) for k in V1_SOURCE_KEYS}})
        def v1_dir_perf(key):
            out = {}
            for o in obs:
                if o["btc_fwd_24h_pct"] is None or o["v1_score"] is None or o["v1_score"] == 50:
                    continue
                k = o["class"] if key == "class" else o["trailing"][key]
                gp = out.setdefault(k, {"n": 0, "hits": 0, "fwd": 0.0})
                gp["n"] += 1
                gp["hits"] += int((o["v1_score"] > 50) == (o["btc_fwd_24h_pct"] > 0))
                gp["fwd"] += o["btc_fwd_24h_pct"]
            for gp in out.values():
                gp.update(hit_rate=round(gp["hits"] / gp["n"], 4), mean_btc_fwd_24h_pct=round(gp.pop("fwd") / gp["n"], 4), small_sample=gp["n"] < 30)
            return dict(sorted(out.items()))
        art["v1_observations"] = {
            "classes_same_time": dict(Counter(o["class"] for o in obs)),
            "classes_trailing_6h": dict(Counter(o["trailing"]["6h"] for o in obs)),
            "classes_trailing_24h": dict(Counter(o["trailing"]["24h"] for o in obs)),
            "v1_score_direction_vs_btc_fwd_24h": {"same_time": v1_dir_perf("class"), "trailing_6h": v1_dir_perf("6h"),
                                                  "trailing_24h": v1_dir_perf("24h")},
            "rows_sidecar": "risk_regime_shock_v1_rows.json"}
        # ---- V1 predictions ----
        prow = []
        for p in preds:
            s = snapshot(p["ts"], inst, grid)
            px = r.proximity(p["ts"], geo_s, mkt_s)
            prow.append({**p, "same_time": r.classify_state(s["geo"]["elevated"], s["market"]["elevated"]),
                         "trailing_6h": px["6h"]["trailing"], "trailing_24h": px["24h"]["trailing"]})
        art["v1_predictions"] = {k: r.group_performance(prow, k) for k in ("same_time", "trailing_6h", "trailing_24h")}
        art["v1_predictions"]["all"] = r.group_performance([{**p, "all": "ALL"} for p in prow], "all")
        art["v1_predictions"]["event15_cluster"] = [{k: p[k] for k in ("id", "ts", "horizon_h", "p_up", "realized_up", "same_time", "trailing_6h", "trailing_24h")}
                                                    for p in prow if b_ms - HOUR <= p["ts"] <= e_ms + HOUR]
        # ---- incremental value vs V1 ----
        geo_scores = [o["geo_shock_score"] for o in obs]
        breadth = [o["breadth"] for o in obs]
        art["incremental_vs_v1"] = {
            "spearman_geo_shock_vs_v1_source": {V1_SOURCE_KEYS[k]: spearman(geo_scores, [o[k] for o in obs])[0] for k in V1_SOURCE_KEYS},
            "spearman_market_breadth_vs_v1_source": {V1_SOURCE_KEYS[k]: spearman(breadth, [o[k] for o in obs])[0] for k in V1_SOURCE_KEYS},
            "v1_source_mean_by_class": {cls: {V1_SOURCE_KEYS[k]: _mean([o[k] for o in obs if o["class"] == cls]) for k in V1_SOURCE_KEYS}
                                        for cls in sorted({o["class"] for o in obs})},
            "v1_extreme_share_by_class": {cls: {V1_SOURCE_KEYS[k]: _extreme([o[k] for o in obs if o["class"] == cls]) for k in V1_SOURCE_KEYS}
                                          for cls in sorted({o["class"] for o in obs})},
            "v1_extreme_definition": "share of readings <= 20 or >= 80 on V1's 0-100 scale"}
        art["status"] = "OK"
    except Exception as e:  # do not fake success
        import traceback
        art.update(status="FAILED", error=f"{type(e).__name__}: {e}"[:500], trace=traceback.format_exc()[-1500:])
        obs = None
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if obs is not None:
        side = out.with_name("risk_regime_shock_v1_rows.json")
        side.write_text(json.dumps(obs, sort_keys=True, indent=1) + "\n")
        art["v1_observations"]["rows_sha256"] = hashlib.sha256(side.read_bytes()).hexdigest()
    out.write_text(g.dumps_deterministic(art))
    print(art["status"], art.get("error", ""))
    return 0 if art["status"] == "OK" else 2


def _mean(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 2) if v else None


def _extreme(vals):
    v = [x for x in vals if x is not None]
    return round(sum(1 for x in v if x <= 20 or x >= 80) / len(v), 4) if v else None


if __name__ == "__main__":
    sys.exit(main())
