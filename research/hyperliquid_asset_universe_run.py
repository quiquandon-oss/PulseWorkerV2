"""One research run of the Hyperliquid asset-universe audit (Candidate #1 / Risk Regime Shock). RESEARCH ONLY.

Read-only everywhere: discovers the current Hyperliquid universe from the public Info API (no key, no cost),
reads the read-only V1 observation extract, and writes a deterministic artifact. It never writes to any
database, never calls an LLM and never touches V1.

Usage:
  python3 research/hyperliquid_asset_universe_run.py --v1 <v1_observations.json> --cache <dir> \
      --out research/results/hyperliquid_asset_universe.json

<v1_observations.json>: the same 575-row extract as the BTC leverage run (list of {"ts": <ms>, ...}).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hyperliquid_asset_universe as u  # noqa: E402
import hyperliquid_leverage_stress as h  # noqa: E402
from hyperliquid_research_run import EVENT15, PRE_BOUNDARY_MS, post  # noqa: E402

HOUR = u.HOUR
REQUEST_SLEEP_S = 1.1          # 20-weight requests: ~55/min, under the documented 1,200 weight/min
HISTORY_FROM_MS = 1735689600000  # 2025-01-01: daily candles from here reveal each listing date
ETF_HINTS = ("ETF", "Treasury", "index", "Index")
RWA_SPOT = re.compile(r"(xau|gold|paxg|silver|slv|gld|oil|spy|qqq|sp500|nasdaq|treasury|t-?bill|bond|eur|jpy|"
                      r"xstock|tokenized|usdy|thbill)", re.I)

# What V1 already has, inspected in CryptoPulse/index.html (not assumed). All are 0-100 scores of a
# single point-in-time change; V1 stores only the score.
V1_SOURCES = {
    "oil": "xyz:BRENTOIL markPx vs prevDayPx (24h %), inverted, satur scale 3 (srcOilFromHyperliquid)",
    "usd": "xyz:EUR markPx vs prevDayPx (24h %), satur scale 1.5 (srcUsdFromHyperliquid); one-off backfill used xyz:DXY daily closes",
    "gold": "xyz:GOLD markPx vs prevDayPx (24h %), sign flipped in a 'competing-haven' regime (srcGoldFromHyperliquid)",
    "nasdaq": "FRED NASDAQCOM, latest vs previous daily observation (srcFredSeries)",
    "sp500": "FRED SP500, latest vs previous daily observation (srcFredSeries)",
    "yield10y": "FRED DGS10, latest vs previous daily observation, inverted (srcFredSeries)",
    "ninemag": "Yahoo quotes of a 9-stock tech/AI basket, cap-weighted day change",
    "strc": "STRC preferred price vs $100 par via Yahoo (/strc-proxy)",
    "global": "CoinGecko total crypto market cap 24h change",
    "funding/longshort/hypefunding": "Hyperliquid native funding (BTC/ETH, 5-asset, HYPE); researched separately",
}
# Hyperliquid instrument -> V1 source it duplicates or overlaps (None = no V1 counterpart).
V1_OVERLAP = {
    "xyz:GOLD": ("gold", "SAME INSTRUMENT: V1 already scores xyz:GOLD's 24h change"),
    "PAXG": ("gold", "Same exposure (gold) via a perp on the PAXG token; V1 gold covers the direction"),
    "xyz:BRENTOIL": ("oil", "SAME INSTRUMENT: V1 already scores xyz:BRENTOIL's 24h change"),
    "xyz:CL": ("oil", "WTI vs Brent: same oil factor, different benchmark"),
    "xyz:EUR": ("usd", "SAME INSTRUMENT: V1 usd is xyz:EUR's 24h change"),
    "xyz:GBP": ("usd", "Another USD cross; overlaps the USD factor V1 reads from EUR"),
    "xyz:JPY": ("usd", "USD/JPY: USD factor plus yen safe-haven/carry; partial overlap"),
    "xyz:SP500": ("sp500", "Same index; V1 reads FRED daily closes, Hyperliquid trades hourly incl. weekends"),
    "mkts:US500": ("sp500", "Same index family (price level differs from the index; deployer docs not in the API)"),
    "xyz:XYZ100": ("nasdaq", "Nasdaq-100-like vs V1's Nasdaq Composite daily close"),
    "mkts:USTECH": ("nasdaq", "Nasdaq-100-like vs V1's Nasdaq Composite daily close"),
    "mkts:SMALL2000": (None, "Small caps: no V1 counterpart"),
    "para:10Y": ("yield10y", "Same yield; V1 reads FRED DGS10 daily, Hyperliquid trades hourly incl. weekends"),
    "para:2Y": (None, "Front-end rates: no V1 counterpart"),
    "para:30Y": (None, "Long-end yield: no V1 counterpart (partly correlated with yield10y)"),
    "xyz:TLT": ("yield10y", "Long-bond price; inverse of long yields, overlaps yield10y direction"),
    "mkts:USBOND": ("yield10y", "Long-bond price; inverse of long yields, overlaps yield10y direction"),
    "xyz:SILVER": (None, "No V1 counterpart (correlated with gold)"),
    "xyz:COPPER": (None, "No V1 counterpart (industrial-demand barometer)"),
    "xyz:NATGAS": (None, "No V1 counterpart"),
    "xyz:HO": (None, "No V1 counterpart (diesel; correlated with oil)"),
    "xyz:XLE": ("oil", "Energy equities; partly the oil factor"),
    "xyz:STRC": ("strc", "SAME UNDERLYING: V1 strc reads STRC via Yahoo; Hyperliquid adds 24/7 trading of the perp"),
}
# Spot tokens whose names imply 1:1 exposure, checked against the matching HIP-3 perp's live mark price.
SPOT_REFERENCE = {"XAUT0": "xyz:GOLD", "XAUM": "xyz:GOLD", "SPY": "mkts:US500", "QQQ": "mkts:USTECH",
                  "SPYX": "mkts:US500", "QQQX": "mkts:USTECH", "USPYX": "mkts:US500"}


def fetch(body: Dict, cache: Path, log: Counter, sleep: bool = True):
    data, how = post(body, cache)
    log[f"{body['type']}_{how}"] += 1
    if how == "FETCHED" and sleep:
        time.sleep(REQUEST_SLEEP_S)
    return data


def discover(cache: Path, log: Counter) -> Dict:
    dexs = fetch({"type": "perpDexs"}, cache, log)
    names = [d["name"] for d in dexs if d]
    concise = dict(fetch({"type": "perpConciseAnnotations"}, cache, log))
    perps, dex_summary = [], []
    for dex in [None] + names:
        body = {"type": "metaAndAssetCtxs"} if dex is None else {"type": "metaAndAssetCtxs", "dex": dex}
        meta, ctxs = fetch(body, cache, log)
        info = next((d for d in dexs if d and d["name"] == dex), None)
        live = 0
        for a, c in zip(meta["universe"], ctxs):
            delisted = bool(a.get("isDelisted"))
            live += not delisted
            perps.append({"symbol": a["name"], "dex": dex, "delisted": delisted,
                          "concise_category": (concise.get(a["name"]) or {}).get("category"),
                          "day_ntl_volume": float(c.get("dayNtlVlm") or 0), "open_interest": _f(c.get("openInterest")),
                          "mark_px": _f(c.get("markPx")), "oracle_px": _f(c.get("oraclePx")), "mid_px": _f(c.get("midPx")),
                          "funding": _f(c.get("funding")), "max_leverage": a.get("maxLeverage")})
        dex_summary.append({"dex": dex or "(native)", "full_name": info and info.get("fullName"),
                            "deployer": info and info.get("deployer"), "oracle_updater": info and info.get("oracleUpdater"),
                            "assets": len(meta["universe"]), "live": live})
    return {"dexs": dex_summary, "perps": perps}


def _f(x):
    return None if x in (None, "") else float(x)


def is_macro_candidate(p: Dict) -> bool:
    if p["delisted"]:
        return False
    if p["dex"] is None:
        return p["symbol"] in ("PAXG", "SPX")          # PAXG: gold-token perp; SPX: ticker trap, checked below
    return p["concise_category"] in ("indices", "rates", "commodities", "fx", "crypto", None) or \
        p["symbol"].split(":")[1] in ("TLT", "XLE", "URNM", "EWJ", "EWY", "EWZ", "EWT", "SMH", "XBI", "MAGS",
                                       "SOXL", "KORU", "USBOND", "STRC")


def spot_rwa(cache: Path, log: Counter, perp_px: Dict[str, float]) -> List[Dict]:
    meta, ctxs = fetch({"type": "spotMetaAndAssetCtxs"}, cache, log)
    tokens = {t["index"]: t for t in meta["tokens"]}
    out = []
    for pair, c in zip(meta["universe"], ctxs):
        base = tokens[pair["tokens"][0]]
        label = f"{base['name']} {base.get('fullName') or ''}"
        if not RWA_SPOT.search(label) or base["name"] in ("USDC",):
            continue
        quote = tokens[pair["tokens"][1]]["name"]
        ref = SPOT_REFERENCE.get(base["name"])
        px = _f(c.get("markPx"))
        out.append({"pair": pair["name"], "token": base["name"], "quote": quote, "full_name": base.get("fullName"),
                    "reference_perp": ref,
                    "price_check": u.price_consistency(px, perp_px.get(ref)) if ref and quote.startswith("USD") else "NOT CHECKABLE",
                    "evm_contract": (base.get("evmContract") or {}).get("address"),
                    "day_ntl_volume": float(c.get("dayNtlVlm") or 0), "mark_px": px,
                    "type": u.SPOT_TOKEN})
    return sorted(out, key=lambda r: -r["day_ntl_volume"])


def candles(coin: str, interval: str, start: int, end: int, cache: Path, log: Counter) -> List[Dict]:
    out, cursor, step = [], start, (500 * HOUR if interval == "1h" else 500 * u.DAY)
    while cursor < end:
        raw = fetch({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": cursor,
                                                       "endTime": min(cursor + step, end)}}, cache, log)
        out.extend(raw)
        cursor += step
    return u.parse_candles(out) if out else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    v1 = sorted(json.load(open(args.v1)), key=lambda r: r["ts"])
    v1_ts = [r["ts"] for r in v1]
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    log: Counter = Counter()
    start = v1_ts[0] - 9 * u.DAY
    end = max(v1_ts[-1], EVENT15["event_ts_ms"] + 24 * HOUR) + HOUR
    art = {"artifact": "hyperliquid-asset-universe-audit", "event": EVENT15,
           "pre_event_boundary": u.iso(PRE_BOUNDARY_MS), "v1_observations_input": len(v1),
           "window": [u.iso(start), u.iso(end)], "v1_sources_inspected": V1_SOURCES,
           "v1_impact": "NONE: research only; no weight, no methodology version, no Risk Regime Shock score.",
           "lookahead": "A 1h candle is used only after its close time T; returns use closed candles only.",
           "api": {"url": h.INFO_URL, "auth": "none (public POST, no key)", "cost_eur": 0,
                   "documented_requests": ["perpDexs", "meta", "metaAndAssetCtxs", "spotMetaAndAssetCtxs",
                                           "candleSnapshot", "fundingHistory"],
                   "undocumented_requests_used": ["perpConciseAnnotations", "perpAnnotation"],
                   "rate_limit": "1,200 weight/min per IP; info requests weight 20 (candleSnapshot +1 per 60 rows, fundingHistory +1 per 20 rows)",
                   "candle_limit": "most recent 5,000 candles per interval", "page_rows": u.PAGE_ROWS,
                   "intervals": ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M"],
                   "open_interest_history": "none documented (current value only, via metaAndAssetCtxs)"}}
    try:
        disc = discover(cache, log)
        cands = [p for p in disc["perps"] if is_macro_candidate(p)]
        btc_c = candles("BTC", "1h", start, end, cache, log)
        btc_r6 = u.hourly_returns(btc_c, 6)
        btc_feats = [u.return_features(btc_c, t, btc_r6) for t in v1_ts]
        assets = []
        for p in cands:
            sym = p["symbol"]
            ann = fetch({"type": "perpAnnotation", "coin": sym}, cache, log) if p["dex"] else None
            cat = u.classify(sym, ann, p["concise_category"])
            if p["dex"] is None:
                cat = "GOLD" if sym == "PAXG" else "CRYPTO"
            daily = candles(sym, "1d", HISTORY_FROM_MS, end, cache, log)
            hourly = candles(sym, "1h", start, end, cache, log)
            fund = fetch({"type": "fundingHistory", "coin": sym, "startTime": end - 24 * HOUR, "endTime": end}, cache, log)
            r6 = u.hourly_returns(hourly, 6)
            feats = [u.return_features(hourly, t, r6) for t in v1_ts]
            overlap = V1_OVERLAP.get(sym, (None, "No V1 counterpart"))
            rec = {"symbol": sym, "dex": p["dex"] or "(native)", "native_or_hip3": "HIP-3" if p["dex"] else "native",
                   "category": cat, "instrument_type": u.instrument_type(sym, p["dex"]),
                   "display_name": (ann or {}).get("displayName"), "annotation_category": (ann or {}).get("category") or p["concise_category"],
                   "underlying_per_deployer": (ann or {}).get("description"),
                   "live": {k: p[k] for k in ("mark_px", "oracle_px", "mid_px", "open_interest", "funding", "day_ntl_volume", "max_leverage")},
                   "history": {"daily_candles": len(daily), "first_daily_candle": u.iso(daily[0]["t"]) if daily else None,
                               "hourly_candles_in_window": len(hourly),
                               "hourly_first": u.iso(hourly[0]["t"]) if hourly else None,
                               "funding_history_available": bool(fund), "open_interest_history": False},
                   "market_hours": u.market_hours_profile(hourly) if hourly else None,
                   "coverage_575": u.coverage(hourly, v1_ts, r6) if hourly else {"usable": 0, "missing": len(v1_ts), "coverage_pct": 0.0},
                   "event15": u.event_analysis(hourly, r6, EVENT15["event_ts_ms"], PRE_BOUNDARY_MS) if hourly else {"overall": "UNUSABLE"},
                   "v1_overlap": {"v1_source": overlap[0], "note": overlap[1]}}
            if cat not in ("CRYPTO", "UNKNOWN") and hourly:
                ok = [(b, f) for b, f in zip(btc_feats, feats) if b["status"] == "OK" and f["status"] == "OK"
                      and b.get("ret_24h") is not None and f.get("ret_24h") is not None]
                rec["cross_asset"] = {
                    "spearman_24h_ret_vs_btc": u.spearman([b["ret_24h"] for b, _ in ok], [f["ret_24h"] for _, f in ok]),
                    "n": len(ok),
                    "btc_down_and_asset_down": u.joint_condition(btc_feats, feats, "down", "down", v1_ts),
                    "btc_down_and_asset_up": u.joint_condition(btc_feats, feats, "down", "up", v1_ts)}
            assets.append(rec)
        art["btc_event15"] = u.event_analysis(btc_c, btc_r6, EVENT15["event_ts_ms"], PRE_BOUNDARY_MS)
        art["inventory"] = {"dexs": disc["dexs"], "live_perps": sum(1 for p in disc["perps"] if not p["delisted"]),
                            "delisted_perps": sum(1 for p in disc["perps"] if p["delisted"]),
                            "live_by_concise_category": dict(Counter(p["concise_category"] or "(none)" for p in disc["perps"]
                                                                     if not p["delisted"] and p["dex"])),
                            "delisted_macro_symbols": sorted(p["symbol"] for p in disc["perps"] if p["delisted"] and p["dex"]
                                                             and p["concise_category"] in ("indices", "rates", "commodities", "fx"))}
        art["candidates"] = assets
        art["spot_rwa_tokens"] = spot_rwa(cache, log, {p["symbol"]: p["mark_px"] for p in disc["perps"]})
        art["status"] = "OK"
    except Exception as e:  # do not fake success
        art.update(status="LIVE_FETCH_FAILED", error=str(e)[:500])
    art["requests"] = dict(log)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(h.dumps_deterministic(art))
    print(art["status"], art.get("error", ""))
    return 0 if art["status"] == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())
