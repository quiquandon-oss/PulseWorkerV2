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
import json
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


# ---- offline mode: an archived subset of masterfilelist.txt replaces the live index, and nothing is downloaded ----
INDEX_FORMAT = "gdelt-master-index-subset-v1"
OFFLINE: Dict[str, object] = {"index": None}      # set by use_offline_index(); None = online (original behaviour)
# NOT_LISTED is not a problem: inside the index's coverage it means what it means online (GDELT never published it)
OFFLINE_PROBLEMS = ("MISSING_OFFLINE", "CORRUPT_OFFLINE", "OUTSIDE_INDEX")


class OfflineError(RuntimeError):
    """Raised when an offline run would need the network or meets a missing / corrupt archived input."""


def archive_master_index(master_text: str, lo: datetime, fetched: Dict) -> Dict:
    """The verbatim export lines of masterfilelist.txt from batch `lo` to the end of the list, with the fetch's
    provenance. The index covers [lo, covered_until]: a batch in that span that is absent was never published
    (NOT_LISTED, as online); a batch outside it is OUTSIDE_INDEX.

    `fetched` = {"retrieved_at": ISO, "bytes": n, "sha256": hex, "range": "bytes=-n", "http_status": n}.
    """
    lines = []
    for line in master_text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].endswith(".export.CSV.zip") and g.batch_ts_from_name(parts[2]) >= lo:
            lines.append(line.strip())
    lines.sort(key=lambda ln: ln.split()[2])
    if not lines:
        raise OfflineError("no export lines of the requested range in the master list")
    last = g.batch_ts_from_name(lines[-1].split()[2])
    until = datetime.fromisoformat(fetched["retrieved_at"].replace("Z", "+00:00")) if fetched.get("retrieved_at") else last
    return {"format": INDEX_FORMAT, "source_url": g.GDELT_SOURCE["master_file_list"], "fetched": fetched,
            "first_batch": g.stamp(g.batch_ts_from_name(lines[0].split()[2])), "last_batch": g.stamp(last),
            "covered_until": until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "lines": lines}


def load_offline_index(path: Path) -> Tuple[Dict[str, Tuple[int, str]], Dict]:
    doc = json.loads(Path(path).read_text())
    if doc.get("format") != INDEX_FORMAT:
        raise OfflineError(f"{path}: not a {INDEX_FORMAT} file")
    idx = g.parse_master_list("\n".join(doc["lines"]))
    if len(idx) != len(doc["lines"]):
        raise OfflineError(f"{path}: {len(doc['lines']) - len(idx)} unparseable index lines")
    return idx, doc


def use_offline_index(path: Optional[Path]) -> None:
    """Switch this module to offline mode (path) or back to online mode (None)."""
    if path is None:
        OFFLINE["index"] = None
        return
    idx, doc = load_offline_index(path)
    OFFLINE["index"] = {"idx": idx, "doc": doc, "path": str(path),
                        "lo": g.batch_ts_from_name(doc["lines"][0].split()[2]),
                        "hi": datetime.fromisoformat(doc["covered_until"].replace("Z", "+00:00"))}


def offline_problems(fetch_log: Dict) -> List[Dict]:
    """Batches an offline run could not read from the archive (empty list = complete)."""
    return [f for f in fetch_log.get("files", []) if f["status"] in OFFLINE_PROBLEMS]


def http_get(url: str, headers: Optional[Dict[str, str]] = None, retries: int = 3, timeout: int = 60, max_bytes: Optional[int] = None) -> Tuple[int, bytes]:
    if OFFLINE["index"] is not None:
        raise OfflineError(f"offline mode: refusing network access to {url}")
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
    """Tail of masterfilelist.txt (HTTP Range) reaching back to `earliest`; grows only if needed.
    Offline: the archived index, which must reach back to `earliest`."""
    off = OFFLINE["index"]
    if off is not None:
        if off["lo"] > earliest:
            raise OfflineError(f"archived index starts {g.stamp(off['lo'])}, after the required {g.stamp(earliest)}")
        return off["idx"]
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
    off = OFFLINE["index"]
    if off is not None and not off["lo"] <= b <= off["hi"]:
        return "OUTSIDE_INDEX", None, None
    meta = idx.get(url)
    if meta is None:
        return "NOT_LISTED", None, None
    path = cache / name
    if off is not None:                               # offline: read only, never delete, never download
        if not path.exists():
            return "MISSING_OFFLINE", None, None
        data = path.read_bytes()
        if len(data) != meta[0] or g.md5_hex(data) != meta[1]:
            return "CORRUPT_OFFLINE", None, None
        return "CACHED", data, meta[1]
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


def breadth(events: List[Dict]) -> Dict:
    """Where the corridor escalation events came from, and whether one story dominates them."""
    if not events:
        return {"events": 0}
    by_url = Counter(r["url"] for r in events)
    n = len(events)
    return {
        "events": n,
        "mentions": sum(r["mentions"] for r in events),
        "distinct_articles": len(by_url),
        "distinct_domains": len({g.source_domain(u) for u in by_url if u}),
        "top_article_share": round(by_url.most_common(1)[0][1] / n, 4),
        "top10_articles_share": round(sum(c for _, c in by_url.most_common(10)) / n, 4),
        "by_action_country": dict(sorted(Counter(r.get("action_country") or "??" for r in events).most_common(10), key=lambda kv: (-kv[1], kv[0]))),
        "by_category": dict(sorted(Counter(r["category"] for r in events).items(), key=lambda kv: (-kv[1], kv[0]))),
    }


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
    # Detection needs a persistent elevated period (>= 1 h). On real data single 15-minute top-decile points occur
    # in about 2% of readings, so "any elevated point in a 48 h window" is true almost by construction.
    periods = g.elevated_periods(scores)
    persistent = [p for p in periods if p["persistent"]]
    return {
        "event": {**EVENT15, "event_ts": event_ts.isoformat()},
        "grid_points": len(grid), "scored_points": len(ok),
        "status_counts": dict(Counter(s["status"] for s in scores)),
        "detected": bool(persistent),
        "detection_rule": "detected = at least one persistent elevated period (>= 1 h of consecutive readings >= 90) in Event #15 -24h..+24h",
        "any_elevated_point": bool(elevated),
        "elevated_periods": periods,
        "first_persistent_elevated": persistent[0]["start"] if persistent else None,
        "first_elevated": first,
        "elevated_before_event": any(s["elevated"] for s in before),
        "elevated_during_decline_24h": any(s["elevated"] for s in decline),
        "elevated_points_after_event_24h": sum(1 for s in after if s["elevated"]),
        "persistence_hours_longest_run": g.persistence_hours(ok),
        "at_event": at_event,
        "acceleration_at_event": acc,
        "counts_6h_before_event": window_counts(event_ts - timedelta(hours=6), event_ts),
        "counts_24h_before_event": window_counts(event_ts - timedelta(hours=24), event_ts),
        "top_corridor_escalation_events_24h_before": [{k: v for k, v in r.items() if k != "action_country"} for r in top_events[:15]],
        "corridor_escalation_24h_before": breadth(top_events),
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
                                "action_country": e.action_country,
                                "mentions": e.mentions, "sources": e.sources, "articles": e.articles, "url": e.url})
    top.sort(key=lambda r: (-r["mentions"], -r["sources"], r["event_id"]))
    return series, top, {"fetch_status_counts": dict(fetch_log), "row_rejects": dict(rejects), "files": files}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--scope", choices=("event", "history"), default="event",
                    help="event: Event #15 window only; history: also score every stored V1 observation")
    ap.add_argument("--max-files", type=int, default=MAX_FILES_PER_RUN, help="hard cap on export files for this run")
    ap.add_argument("--offline-index", help="archived master-list subset (archive_gdelt_index.py); no network access")
    args = ap.parse_args(argv)
    use_offline_index(Path(args.offline_index) if args.offline_index else None)
    v1 = json.load(open(args.v1))
    v1_ts = sorted(r["ts"] for r in v1)
    event_ts = datetime.fromtimestamp(EVENT15["event_ts_ms"] / 1000, tz=timezone.utc)
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    grid = [event_ts - timedelta(hours=24) + timedelta(minutes=15 * i) for i in range(4 * 48 + 1)]
    v1_window = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in v1_ts if abs(t - EVENT15["event_ts_ms"]) <= 36 * 3600e3]
    v1_all = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in v1_ts]
    batches = g.required_batches(grid + v1_window + (v1_all if args.scope == "history" else []))
    previous = _previous_runs(Path(args.out))
    artifact = {
        "scope": args.scope, "max_files": args.max_files, "previous_runs": previous,
        "artifact": "gdelt-risk-regime-shock-research", "filter_version": g.FILTER_VERSION, "feature_version": g.FEATURE_VERSION,
        "source": g.GDELT_SOURCE, "fields_used": list(g.FIELDS_USED), "event": EVENT15,
        "required_batches": len(batches), "required_range": [g.stamp(batches[0]), g.stamp(batches[-1])],
        "v1_observations_input": len(v1_ts), "v1_range": [v1_ts[0], v1_ts[-1]],
        "v1_only_findings": v1_only_findings(v1, event_ts),
        "v1_impact": "NONE: research feature only; no weight, no methodology version, V1 unchanged.",
    }
    if len(batches) > args.max_files:
        artifact["status"] = "REFUSED_TOO_MANY_FILES"
    else:
        try:
            first_v1 = datetime.fromtimestamp(v1_ts[0] / 1000, tz=timezone.utc)
            idx = master_index(min(batches[0], first_v1 - timedelta(hours=g.BASELINE_HOURS + 2)))
            listed = [g.batch_ts_from_name(u) for u in idx]
            artifact["download_bytes_listed"] = sum(idx[g.export_url(b)][0] for b in batches if g.export_url(b) in idx)
            series, top, log = build_series(batches, idx, cache, event_ts)
            if OFFLINE["index"] is not None and offline_problems(log):
                bad = offline_problems(log)
                raise OfflineError(f"{len(bad)} archived batches unusable, first {bad[0]}")
            ev = analyse(series, top, v1, event_ts)
            win = (event_ts - timedelta(hours=24), event_ts + timedelta(hours=24))
            if args.scope == "history":
                hgrid = _grid(v1_all[0], v1_all[-1])
                hscores = [g.score_at(series, t) for t in hgrid]
                artifact["false_positives"] = g.false_positive_summary(hscores, series, exclude=win)
                artifact["false_positives"]["grid"] = "every 15 minutes across the stored V1 history"
                artifact["v1_historical_scores"] = g.historical_scores(series, v1_ts)
            else:
                scores = [g.score_at(series, t) for t in _grid(event_ts - timedelta(hours=48), event_ts + timedelta(hours=24))]
                artifact["false_positives"] = g.false_positive_summary(scores, series, exclude=win)
                artifact["false_positives"]["grid"] = "Event #15 -48h..+24h only (run --scope history for the full V1 history)"
            artifact.update(status="OK", fetch=log, event15=ev,
                            v1_coverage=g.v1_coverage(v1_ts, listed),
                            coverage_basis="file availability from the official masterfilelist.txt (size + MD5 per 15-minute export)")
            if OFFLINE["index"] is not None:   # recorded only for offline runs, so online artifacts are unchanged
                artifact["offline_index"] = {"path": Path(OFFLINE["index"]["path"]).name,
                                             "fetched": OFFLINE["index"]["doc"]["fetched"]}
        except Exception as e:  # do not fake success
            artifact.update(status="OFFLINE_INPUT_FAILED" if OFFLINE["index"] is not None else "LIVE_FETCH_FAILED", error=str(e)[:500],
                            v1_coverage={"v1_observations": len(v1_ts), "measured": False,
                                         "reason": "GDELT file availability could not be read"})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    split_sidecars(artifact, Path(args.out))
    Path(args.out).write_text(g.dumps_deterministic(artifact))
    print(artifact["status"])
    return 0 if artifact["status"] == "OK" else 2


def _grid(lo: datetime, hi: datetime) -> List[datetime]:
    start = lo.replace(minute=(lo.minute // 15) * 15, second=0, microsecond=0)
    return [start + timedelta(minutes=15 * i) for i in range(int((hi - start).total_seconds() // 900) + 1)]


def split_sidecars(artifact: Dict, out: Path) -> None:
    """The main artifact is bundled into the Worker, so the long per-file manifest and the per-observation V1
    scores go to sidecar files next to it; the artifact keeps their path and SHA-256."""
    import hashlib
    parts = []
    if isinstance(artifact.get("fetch"), dict) and "files" in artifact["fetch"]:
        parts.append(("files", artifact["fetch"], "files", "_files.json"))
    if isinstance(artifact.get("v1_historical_scores"), dict) and "rows" in artifact["v1_historical_scores"]:
        parts.append(("v1_scores", artifact["v1_historical_scores"], "rows", "_v1_scores.json"))
    for _, holder, key, suffix in parts:
        path = out.with_name(out.stem + suffix)
        text = g.dumps_deterministic(holder.pop(key))
        path.write_text(text)
        holder[key + "_sidecar"] = {"path": path.name, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _previous_runs(path: Path) -> List[Dict]:
    """Earlier runs written to the same artifact are kept (status, error, scope) as an audit trail."""
    if not path.exists():
        return []
    import json
    try:
        old = json.loads(path.read_text())
    except ValueError:
        return []
    keep = {k: old.get(k) for k in ("status", "error", "scope", "required_batches", "required_range") if k in old}
    return list(old.get("previous_runs", [])) + [keep]


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
