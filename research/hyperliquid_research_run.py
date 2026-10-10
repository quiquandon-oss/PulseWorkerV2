"""One research run of the Hyperliquid leverage / market-stress component (Risk Regime Shock, input 2).

Read-only everywhere: reads the public Hyperliquid Info API (no key, no cost) and a read-only extract of
the stored V1 observations, and writes a deterministic research artifact. It never writes to any database,
never calls an LLM and never touches V1.

Usage:
  python3 research/hyperliquid_research_run.py --v1 <v1_observations.json> --cache <dir> \
      --out research/results/hyperliquid_leverage_stress.json

<v1_observations.json>: JSON list of {"ts": <ms>, "fd": funding, "ls": longshort, "hf": hypefunding, ...}
(the stored V1 0-100 source scores). If Hyperliquid cannot be reached the artifact says so
(status LIVE_FETCH_FAILED) and contains no Hyperliquid results.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hyperliquid_leverage_stress as h  # noqa: E402

UTC = timezone.utc
HOUR = 3_600_000
EVENT15 = {"event_id": 15, "event_ts_ms": 1790564484213, "label": "BTC 84,429 -> 83,480 (-1.1%) in the 24 h to 2026-09-28 03:01 UTC; V1 predicted UP"}
# First failed V1 prediction of the Event #15 cluster (predictions.id 1125, p_up 0.73, issued at the 84,925 high).
PRE_BOUNDARY_MS = 1790510487481
FUNDING_PAGE_SLEEP_S = 0.5      # stay well below any rate limit
USER_AGENT = "CryptoPulse-research/1.0 (read-only)"


def post(body: Dict, cache: Path, retries: int = 3, timeout: int = 30):
    """POST to the Info API; responses are cached by request body so a rerun makes no network call."""
    key = cache / (json.dumps(body, sort_keys=True).replace("/", "_").replace(" ", "")[:180] + ".json")
    if key.exists():
        return json.loads(key.read_text()), "CACHED"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(h.INFO_URL, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            key.write_text(json.dumps(data))
            return data, "FETCHED"
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
        except Exception as e:  # network errors: retry with backoff, then give up
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"POST {h.INFO_URL} {body.get('type')} failed after {retries} attempts: {last}")


def fetch_funding(start_ms: int, end_ms: int, cache: Path, log: Counter) -> List[Dict]:
    """Paginates fundingHistory forward from start_ms until end_ms or until the API returns nothing new."""
    out: List[Dict] = []
    cursor = start_ms
    for _ in range(200):  # hard cap on pages
        page_raw, how = post({"type": "fundingHistory", "coin": h.COIN, "startTime": cursor, "endTime": end_ms}, cache)
        log["fundingHistory_" + how] += 1
        page = h.parse_funding_history(page_raw)
        log["fundingHistory_page_max_rows"] = max(log["fundingHistory_page_max_rows"], len(page))
        new = [r for r in page if r["t"] >= cursor]
        if not new:
            break
        out.extend(new)
        cursor = new[-1]["t"] + 1
        if cursor >= end_ms:
            break
        if how == "FETCHED":
            time.sleep(FUNDING_PAGE_SLEEP_S)
    return h.parse_funding_history([{"time": r["t"], "fundingRate": r["funding"], "premium": r["premium"]} for r in out])


def fetch_candles(start_ms: int, end_ms: int, cache: Path, log: Counter) -> List[Dict]:
    out: List[Dict] = []
    step = 30 * 24 * HOUR
    for lo in range(start_ms, end_ms, step):
        raw, how = post({"type": "candleSnapshot", "req": {"coin": h.COIN, "interval": "1h", "startTime": lo, "endTime": min(lo + step, end_ms)}}, cache)
        log["candleSnapshot_" + how] += 1
        out.extend(h.parse_candles(raw))
    return h.parse_candles([{"t": c["t"], "T": c["T"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"], "n": c["n"] or 0} for c in out])


# ---------------- V1-only findings (no network needed) ----------------
def v1_only_findings(v1: List[Dict]) -> Dict:
    def stats(key):
        vals = [r.get(key) for r in v1]
        present = [v for v in vals if v is not None]
        return {"present": len(present), "missing": len(vals) - len(present), "exactly_50": sum(1 for v in present if v == 50),
                "exactly_50_share": round(sum(1 for v in present if v == 50) / len(present), 4) if present else None,
                "distinct": len(set(present)), "min": min(present) if present else None, "max": max(present) if present else None}
    e = EVENT15["event_ts_ms"]
    before = [r for r in v1 if r["ts"] < e]
    after = [r for r in v1 if r["ts"] > e]
    pick = lambda r: {"t": datetime.fromtimestamp(r["ts"] / 1000, UTC).isoformat(), "funding": r.get("fd"), "longshort": r.get("ls"), "hypefunding": r.get("hf")}
    return {
        "sources": {"funding": stats("fd"), "longshort": stats("ls"), "hypefunding": stats("hf")},
        "note": "V1 subtracts Hyperliquid's fixed funding floor before scoring, so funding sitting at the floor scores exactly 50.",
        "around_event15": {"last_before": [pick(r) for r in before[-3:]], "first_after": [pick(r) for r in after[:3]]},
    }


# ---------------- analysis on live data ----------------
def first_true(points: List[Dict], key: str) -> Optional[int]:
    for p in points:
        if p.get("flags", {}).get(key):
            return p["t_ms"]
    return None


def analyse_event(candles, funding) -> Dict:
    e = EVENT15["event_ts_ms"]
    grid = [e - 48 * HOUR + i * HOUR for i in range(73)]
    pts = []
    for t in grid:
        f = h.features_at(candles, funding, t)
        f["t_ms"] = t
        f["flags"] = h.abnormal_flags(f) if f["status"] == "OK" else {}
        pts.append(f)
    keys = sorted({k for p in pts for k in p["flags"]})
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, UTC).isoformat() if ms else None
    boundary_t = max(t for t in grid if t <= PRE_BOUNDARY_MS)
    timing = {}
    for k in keys:
        first = first_true(pts, k)
        episodes, cur = [], None
        for p in pts:
            if p["flags"].get(k):
                cur = cur or {"start": p["t_ms"], "hours": 0}
                cur["end"] = p["t_ms"]
                cur["hours"] += 1
            elif cur:
                episodes.append(cur)
                cur = None
        if cur:
            episodes.append(cur)
        # A flag is PRE-EVENT only if it is on at the point the first failed prediction was issued; an earlier
        # episode that cleared before then could not have warned that prediction.
        active = any(ep["start"] <= boundary_t <= ep["end"] for ep in episodes)
        later = [ep["start"] for ep in episodes if ep["start"] > boundary_t]
        timing[k] = {"first_seen": iso(first), "active_at_pre_boundary": active,
                     "classification": "PRE-EVENT" if active else h.classify_timing(later[0] if later else None, PRE_BOUNDARY_MS, e),
                     "longest_run_hours": max((ep["hours"] for ep in episodes), default=0),
                     "points": sum(1 for p in pts if p["flags"].get(k)),
                     "episodes": [{"start": iso(ep["start"]), "end": iso(ep["end"]), "hours": ep["hours"],
                                   "class": h.classify_timing(ep["start"], PRE_BOUNDARY_MS, e)} for ep in episodes]}
    at = next((p for p in pts if p["t_ms"] == e), None)
    pre = next((p for p in pts if p["t_ms"] == max(t for t in grid if t <= PRE_BOUNDARY_MS)), None)
    slim = lambda p: {k: v for k, v in p.items() if k not in ("t_ms",)} if p else None
    return {"grid": "hourly, Event #15 -48h..+24h", "pre_event_boundary": datetime.fromtimestamp(PRE_BOUNDARY_MS / 1000, UTC).isoformat(),
            "at_pre_boundary": slim(pre), "at_event": slim(at), "flag_timing": timing,
            "series": [{"t": p["t"], "status": p["status"], "price": p.get("price"), "funding": p.get("D_funding"), "premium": p.get("D2_premium"),
                        "premium_pctile": p.get("F_premium_pctile_7d"), "ret_6h": p.get("G_ret_6h"), "vol_ratio": p.get("H_volume_6h_vs_7d_median"),
                        "flags": [k for k, v in p["flags"].items() if v]} for p in pts]}


def spearman(x, y):
    def rank(v):
        o = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(o):
            j = i
            while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
                j += 1
            for k in range(i, j + 1):
                r[o[k]] = (i + j) / 2
            i = j + 1
        return r
    if len(x) < 3:
        return None
    rx, ry = rank(x), rank(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(num / den, 3) if den else None


def analyse_history(candles, funding, v1) -> Dict:
    rows = []
    for r in v1:
        f = h.features_at(candles, funding, r["ts"])
        rows.append((r, f))
    ok = [(r, f) for r, f in rows if f["status"] == "OK"]
    with_base = [(r, f) for r, f in ok if f.get("F_premium_pctile_7d") is not None]
    floor = [f["funding_at_floor"] for _, f in ok]
    agree = [(r["fd"] == 50) == f["funding_at_floor"] for r, f in ok if r.get("fd") is not None]
    pairs = [(f["D2_premium"], r["fd"]) for r, f in ok if r.get("fd") is not None]
    flag_counts = Counter(k for _, f in with_base for k, v in h.abnormal_flags(f).items() if v)
    return {
        "v1_observations": len(v1), "usable": len(ok), "usable_with_7d_baseline": len(with_base),
        "coverage_pct": round(100 * len(ok) / len(v1), 2) if v1 else None,
        "excluded": dict(Counter(f["status"] for _, f in rows if f["status"] != "OK")),
        "btc_funding_at_floor_share": round(sum(floor) / len(floor), 4) if floor else None,
        "v1_funding_50_agrees_with_hl_floor": round(sum(agree) / len(agree), 4) if agree else None,
        "spearman_premium_vs_v1_funding": spearman(*map(list, zip(*pairs))) if pairs else None,
        "abnormal_flag_rates": {k: round(v / len(with_base), 4) for k, v in sorted(flag_counts.items())} if with_base else {},
        "weekend_usable": sum(1 for r, _ in ok if datetime.fromtimestamp(r["ts"] / 1000, UTC).weekday() >= 5),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    v1 = sorted(json.load(open(args.v1)), key=lambda r: r["ts"])
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    out = Path(args.out)
    previous = []
    if out.exists():
        try:
            old = json.loads(out.read_text())
            previous = old.get("previous_runs", []) + [{k: old.get(k) for k in ("status", "error", "run_ts") if old.get(k) is not None}]
        except (ValueError, AttributeError):
            previous = []
    start = v1[0]["ts"] - 9 * 24 * HOUR             # 7-day baseline + 24 h returns + margin before the first V1 observation
    end = max(v1[-1]["ts"], EVENT15["event_ts_ms"] + 24 * HOUR) + HOUR
    artifact = {
        "artifact": "hyperliquid-leverage-stress-research", "source": h.SOURCE, "requests": h.REQUESTS,
        "dimensions_expected": h.DIMENSIONS, "event": EVENT15, "previous_runs": previous,
        "required_range": [datetime.fromtimestamp(start / 1000, UTC).isoformat(), datetime.fromtimestamp(end / 1000, UTC).isoformat()],
        "v1_observations_input": len(v1), "v1_only_findings": v1_only_findings(v1),
        "v1_impact": "NONE: research only; no weight, no methodology version, V1 unchanged.",
        "lookahead": "A 1h candle is used only after its close time T; a funding record only after its time.",
    }
    log: Counter = Counter()
    try:
        snap_raw, how = post({"type": "metaAndAssetCtxs"}, cache)
        log["metaAndAssetCtxs_" + how] += 1
        artifact["live_snapshot"] = h.parse_asset_ctx(snap_raw)
        funding = fetch_funding(start, end, cache, log)
        candles = fetch_candles(start, end, cache, log)
        artifact["data"] = {
            "funding_records": len(funding), "funding_range": [funding[0]["t"], funding[-1]["t"]] if funding else None,
            "candles_1h": len(candles), "candle_range": [candles[0]["t"], candles[-1]["T"]] if candles else None,
            "requests": dict(log)}
        artifact.update(status="OK", event15=analyse_event(candles, funding), history=analyse_history(candles, funding, v1))
    except Exception as e:  # do not fake success
        artifact.update(status="LIVE_FETCH_FAILED", error=str(e)[:500], data={"requests": dict(log)},
                        history={"measured": False, "v1_observations": len(v1), "reason": "Hyperliquid could not be read"})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(h.dumps_deterministic(artifact))
    print(artifact["status"], artifact.get("error", ""))
    return 0 if artifact["status"] == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())
