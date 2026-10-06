"""One research run of the GDELT geopolitical component (Risk Regime Shock). Read-only everywhere.

Reads official GDELT 2.0 export files (only the batches the analysis needs), scores the point-in-time
geopolitical shock component around Event #15, measures how many stored V1 observations could carry a
GDELT reading, and writes a deterministic research artifact. It never writes to any database, never calls
an LLM, and never touches V1.

Usage:
  python3 research/gdelt_research_run.py --v1 <v1_observations.json> --cache <dir> --out research/results/gdelt_risk_regime_shock.json

<v1_observations.json> is a read-only extract of stored V1 observations: a JSON list of
{"ts": <ms>, "g": geopolitics, "m": macrogeo, "o": oil, "y": yield10y, "n": nasdaq, "s": sp500, "u": usd, "score": V1}.
If GDELT cannot be reached the artifact says so (status LIVE_FETCH_FAILED) and contains no GDELT results.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gdelt_geo_shock as g  # noqa: E402

EVENT15 = {"event_id": 15, "event_ts_ms": 1790564484213, "label": "BTC fell 4.1% in 24h (V1 predicted UP)"}
MAX_FILES_PER_RUN = 700          # hard cap: a run can never turn into an archive download
MASTER_TAIL_BYTES = (3_000_000, 12_000_000, 24_000_000)
USER_AGENT = "CryptoPulse-research/1.0 (read-only; GDELT open data)"


def http_get(url: str, headers: Optional[Dict[str, str]] = None, retries: int = 3, timeout: int = 60, max_bytes: Optional[int] = None) -> Tuple[int, bytes]:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, (r.read(max_bytes) if max_bytes else r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, b""
            last = e
        except Exception as e:  # network errors: retry with backoff, then give up
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last}")


def master_index(earliest: datetime) -> Dict[str, Tuple[int, str]]:
    """Tail of masterfilelist.txt (HTTP Range) reaching back to `earliest`; grows only if needed."""
    for n in MASTER_TAIL_BYTES:
        status, body = http_get(g.GDELT_SOURCE["master_file_list"], {"Range": f"bytes=-{n}"}, max_bytes=n + 1)
        text = body.decode("utf-8", "replace")
        if status == 206:
            text = text.split("\n", 1)[-1]          # first line may be cut by the range
        idx = g.parse_master_list(text)
        stamps = sorted(g.batch_ts_from_name(u) for u in idx)
        if stamps and stamps[0] <= earliest:
            return idx
        if status == 200:
            return idx                               # server ignored Range and sent what it sent (capped)
    return idx


def fetch_batch(b: datetime, idx: Dict[str, Tuple[int, str]], cache: Path) -> Tuple[str, Optional[bytes], Optional[str]]:
    """(status, bytes, md5). Cached by deterministic name; size + MD5 checked against the master list."""
    url = g.export_url(b)
    name = url.rsplit("/", 1)[-1]
    meta = idx.get(url)
    if meta is None:
        return "NOT_LISTED", None, None
    path = cache / name
    if path.exists():
        data = path.read_bytes()
        if len(data) == meta[0] and g.md5_hex(data) == meta[1]:
            return "CACHED", data, meta[1]
        path.unlink()                                 # corrupt cache entry: refetch once
    status, data = http_get(url)
    if status == 404:
        return "MISSING_404", None, None
    if len(data) != meta[0] or g.md5_hex(data) != meta[1]:
        return "INTEGRITY_FAILED", None, None
    path.write_bytes(data)
    return "FETCHED", data, meta[1]


def spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 5:
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
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(num / den, 3) if den else None


def analyse(series: Dict[datetime, Dict], top_events: List[Dict], v1: List[Dict], event_ts: datetime) -> Dict:
    """Event #15 analysis on an already point-in-time-safe batch series."""
    grid = [event_ts - timedelta(hours=24) + timedelta(minutes=15 * i) for i in range(4 * 48 + 1)]
    scores = [g.score_at(series, t) for t in grid]
    ok = [s for s in scores if s["status"] == "OK"]
    elevated = [s for s in ok if s["elevated"]]
    before = [s for s in ok if datetime.fromisoformat(s["t"]) <= event_ts]
    decline = [s for s in before if datetime.fromisoformat(s["t"]) >= event_ts - timedelta(hours=24)]
    after = [s for s in ok if datetime.fromisoformat(s["t"]) > event_ts]
    at_event = g.score_at(series, event_ts)
    acc = g.acceleration(series, datetime.fromisoformat(at_event["batch_used"])) if at_event.get("batch_used") else None

    def window_counts(lo: datetime, hi: datetime) -> Dict:
        c = Counter()
        cats = Counter()
        for b, v in series.items():
            if lo < b + g.AVAILABILITY_LAG <= hi:
                for k in ("geo_events", "geo_mentions", "geo_articles", "escalation_events", "corridor_escalation_events",
                          "negative_direction_events", "total_events"):
                    c[k] += v.get(k, 0)
                cats.update(v.get("categories", {}))
        return {**{k: int(c[k]) for k in sorted(c)}, "categories": dict(cats.most_common())}

    v1_rows = []
    for r in v1:
        t = datetime.fromtimestamp(r["ts"] / 1000, tz=timezone.utc)
        if abs((t - event_ts).total_seconds()) <= 36 * 3600:
            s = g.score_at(series, t)
            v1_rows.append({"t": t.isoformat(), "v1_score": r.get("score"), "geopolitics": r.get("g"), "macrogeo": r.get("m"),
                            "oil": r.get("o"), "yield10y": r.get("y"), "nasdaq": r.get("n"), "sp500": r.get("s"), "usd": r.get("u"),
                            "gdelt_status": s["status"], "geo_shock_score": s.get("geo_shock_score"), "gdelt_batch_used": s.get("batch_used")})
    pairs = [(r["geo_shock_score"], r["geopolitics"]) for r in v1_rows if r["geo_shock_score"] is not None and r["geopolitics"] is not None]
    pairs_m = [(r["geo_shock_score"], r["macrogeo"]) for r in v1_rows if r["geo_shock_score"] is not None and r["macrogeo"] is not None]
    first = elevated[0]["t"] if elevated else None
    return {
        "event": {**EVENT15, "event_ts": event_ts.isoformat()},
        "grid_points": len(grid), "scored_points": len(ok),
        "status_counts": dict(Counter(s["status"] for s in scores)),
        "detected": bool(elevated),
        "first_elevated": first,
        "elevated_before_event": any(s["elevated"] for s in before),
        "elevated_during_decline_24h": any(s["elevated"] for s in decline),
        "elevated_points_after_event_24h": sum(1 for s in after if s["elevated"]),
        "persistence_hours_longest_run": g.persistence_hours(ok),
        "at_event": at_event,
        "acceleration_at_event": acc,
        "counts_6h_before_event": window_counts(event_ts - timedelta(hours=6), event_ts),
        "counts_24h_before_event": window_counts(event_ts - timedelta(hours=24), event_ts),
        "top_corridor_escalation_events_24h_before": top_events[:15],
        "score_series": [{"t": s["t"], "status": s["status"], "geo_shock_score": s.get("geo_shock_score")} for s in scores],
        "v1_comparison": v1_rows,
        "spearman_vs_v1_geopolitics": spearman(*map(list, zip(*pairs))) if pairs else None,
        "spearman_vs_v1_macrogeo": spearman(*map(list, zip(*pairs_m))) if pairs_m else None,
        "lookahead": "Every score uses only batches with batch_ts + 15 min <= the scored time; events are placed at DATEADDED, never at SQLDATE.",
    }


def build_series(batches: List[datetime], idx, cache: Path, event_ts: datetime) -> Tuple[Dict, List[Dict], Dict]:
    series: Dict[datetime, Dict] = {}
    seen: set = set()
    rejects: Counter = Counter()
    fetch_log = Counter()
    files = []
    top: List[Dict] = []
    for b in batches:
        status, data, md5 = fetch_batch(b, idx, cache)
        fetch_log[status] += 1
        files.append({"batch": g.stamp(b), "status": status, "md5": md5})
        if data is None:
            continue
        rows = g.parse_export_zip(data, g.export_url(b), rejects)
        series[b] = g.aggregate_batch(rows, seen)
        if event_ts - timedelta(hours=24) < b + g.AVAILABILITY_LAG <= event_ts:
            for e in rows:
                c = g.classify(e)
                if c.relevant and c.corridor and c.escalation:
                    top.append({"event_id": e.event_id, "date_added": e.date_added, "cameo": e.code, "category": c.category,
                                "actor1": e.actor1_country, "actor2": e.actor2_country, "location": e.action_name,
                                "mentions": e.mentions, "sources": e.sources, "articles": e.articles, "url": e.url})
    top.sort(key=lambda r: (-r["mentions"], -r["sources"], r["event_id"]))
    return series, top, {"fetch_status_counts": dict(fetch_log), "row_rejects": dict(rejects), "files": files}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    import json
    v1 = json.load(open(args.v1))
    v1_ts = sorted(r["ts"] for r in v1)
    event_ts = datetime.fromtimestamp(EVENT15["event_ts_ms"] / 1000, tz=timezone.utc)
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    grid = [event_ts - timedelta(hours=24) + timedelta(minutes=15 * i) for i in range(4 * 48 + 1)]
    v1_window = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in v1_ts if abs(t - EVENT15["event_ts_ms"]) <= 36 * 3600e3]
    batches = g.required_batches(grid + v1_window)
    artifact = {
        "artifact": "gdelt-risk-regime-shock-research", "filter_version": g.FILTER_VERSION, "feature_version": g.FEATURE_VERSION,
        "source": g.GDELT_SOURCE, "fields_used": list(g.FIELDS_USED), "event": EVENT15,
        "required_batches": len(batches), "required_range": [g.stamp(batches[0]), g.stamp(batches[-1])],
        "v1_observations_input": len(v1_ts), "v1_range": [v1_ts[0], v1_ts[-1]],
        "v1_only_findings": v1_only_findings(v1, event_ts),
        "v1_impact": "NONE: research feature only; no weight, no methodology version, V1 unchanged.",
    }
    if len(batches) > MAX_FILES_PER_RUN:
        artifact["status"] = "REFUSED_TOO_MANY_FILES"
    else:
        try:
            first_v1 = datetime.fromtimestamp(v1_ts[0] / 1000, tz=timezone.utc)
            idx = master_index(min(batches[0], first_v1 - timedelta(hours=g.BASELINE_HOURS + 2)))
            listed = [g.batch_ts_from_name(u) for u in idx]
            series, top, log = build_series(batches, idx, cache, event_ts)
            artifact.update(status="OK", fetch=log, event15=analyse(series, top, v1, event_ts),
                            v1_coverage=g.v1_coverage(v1_ts, listed),
                            coverage_basis="file availability from the official masterfilelist.txt (size + MD5 per 15-minute export)")
        except Exception as e:  # do not fake success
            artifact.update(status="LIVE_FETCH_FAILED", error=str(e)[:500],
                            v1_coverage={"v1_observations": len(v1_ts), "measured": False,
                                         "reason": "GDELT file availability could not be read"})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(g.dumps_deterministic(artifact))
    print(artifact["status"])
    return 0 if artifact["status"] == "OK" else 2


def v1_only_findings(v1: List[Dict], event_ts: datetime) -> Dict:
    """Facts about the stored V1 inputs around Event #15 that do not need GDELT."""
    ms = int(event_ts.timestamp() * 1000)
    before = [r for r in v1 if r["ts"] <= ms]
    after = [r for r in v1 if r["ts"] > ms]
    last, nxt = (before[-1] if before else None), (after[0] if after else None)
    win = [r for r in v1 if ms - 72 * 3600e3 <= r["ts"] <= ms]

    def stuck(key):
        vals = [r[key] for r in win if r.get(key) is not None]
        return {"distinct_values_72h_before": len(set(vals)), "min": min(vals) if vals else None, "max": max(vals) if vals else None}

    gaps = [(b["ts"] - a["ts"]) / 3600e3 for a, b in zip(v1, v1[1:])]
    return {
        "last_v1_before_event": datetime.fromtimestamp(last["ts"] / 1000, tz=timezone.utc).isoformat() if last else None,
        "next_v1_after_event": datetime.fromtimestamp(nxt["ts"] / 1000, tz=timezone.utc).isoformat() if nxt else None,
        "gap_hours_around_event": round((nxt["ts"] - last["ts"]) / 3600e3, 2) if last and nxt else None,
        "readings_72h_before_event": {k: stuck(c) for k, c in (("geopolitics", "g"), ("macrogeo", "m"), ("oil", "o"), ("yield10y", "y"),
                                                               ("nasdaq", "n"), ("sp500", "s"), ("usd", "u"))},
        "v1_cadence_hours": {"median": round(sorted(gaps)[len(gaps) // 2], 2) if gaps else None, "max": round(max(gaps), 2) if gaps else None,
                             "gaps_over_6h": sum(1 for x in gaps if x > 6)},
    }


if __name__ == "__main__":
    sys.exit(main())
