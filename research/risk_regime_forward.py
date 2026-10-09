"""Risk Regime forward collection: append-only, idempotent, gap-aware store of NEW observations. RESEARCH ONLY.

Collects, for times after the frozen historical datasets end:
  binance_oi_archive       Binance BTCUSDT perpetual OI, 5-minute (Binance public data archive, daily files, SHA-256 checked)
  binance_funding_archive  Binance BTCUSDT funding settlements (archive, monthly files)
  hyperliquid_hip3         Hyperliquid cross-asset hourly candles (established implementation: candles + hyperliquid_obs)
  hyperliquid_btc_funding  Hyperliquid BTC hourly funding/premium (established implementation: fetch_funding)

Storage (research/results/risk_regime_forward/):
  <source>/<YYYY>/<partition>.jsonl.gz      normalised observations of one UTC day (funding archive: one month)
  <source>/<YYYY>/<partition>.meta.json     fingerprint, file sha256, counts, expected/missing slots, collection times
  raw/<source>/<YYYY>/<partition>.jsonl.gz  raw provider records behind that partition (archive CSV text + checksum;
                                            Hyperliquid candle / funding rows as returned)
  runs/<UTC time>_<run id>.json            one record per run: every attempt, status, error, conflict, staleness
  index.json                               rebuilt from the partitions on every run (deterministic)

Rules:
- Nothing already in the frozen datasets is stored again: each series has a cutoff (its last timestamp in
  results/risk_regime_raw/observations.jsonl.gz and results/risk_regime_oi/observations.jsonl.gz); only later
  timestamps are kept. The frozen files are read, never written.
- A partition is written once, atomically. Re-collecting the same content is a no-op (the first collection time is
  kept). A strict superset (an earlier gap now filled, every earlier value identical) supersedes it. Any differing
  value is a CONFLICT: the stored partition is kept, nothing is overwritten, the run records the difference.
- Missing slots are listed, never filled. A partition with no data is not written; the run records MISSING /
  NOT_YET_PUBLISHED. Partitions that are not COMPLETE are retried by the next run (safe: same rules apply).
- Only closed periods are collected (a day after it ends; a month after it ends).
- No proxy, no credentials, no paid source, no substitution. Bybit and liquidations are not collected (missing).

Usage: python3 research/risk_regime_forward.py --store research/results/risk_regime_forward [--sources ...] [--max-days 60]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import risk_regime_data as d  # noqa: E402
import risk_regime_oi_collect as oc  # noqa: E402
import risk_regime_sources as s  # noqa: E402

UTC = timezone.utc
MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
RESEARCH = Path(__file__).resolve().parent
FROZEN = [RESEARCH / "results" / "risk_regime_raw" / "observations.jsonl.gz",
          RESEARCH / "results" / "risk_regime_oi" / "observations.jsonl.gz"]

# Expected grid per source (slot = observation timestamp rounded to the grid), publication delay and retention.
SOURCE_SPECS = {
    "binance_oi_archive": {"period": "day", "grid_ms": 5 * MIN, "metrics": ("open_interest", "open_interest_usd"),
                           "available_after_ms": DAY, "stale_after_ms": 3 * DAY,
                           "retention": "archive keeps daily files back to 2021-12; no expiry is documented",
                           "units": {"open_interest": "BTC", "open_interest_usd": "USD"}},
    "binance_funding_archive": {"period": "month", "grid_ms": 8 * HOUR, "metrics": ("funding_rate",),
                                "available_after_ms": 0, "stale_after_ms": 7 * DAY,
                                "retention": "monthly files only: a month is published after it ends (up to ~31 days research latency)",
                                "units": {"funding_rate": "rate per funding interval (8h)"}},
    "hyperliquid_hip3": {"period": "day", "grid_ms": HOUR, "metrics": ("close", "volume"), "available_after_ms": HOUR,
                         "stale_after_ms": 2 * DAY,
                         "retention": "candleSnapshot serves only the latest 5,000 candles (1h ~ 208 days): collect well within that",
                         "units": {"close": "price", "volume": "base units"}},
    "hyperliquid_btc_funding": {"period": "day", "grid_ms": HOUR, "metrics": ("funding_rate", "premium"),
                                "available_after_ms": HOUR, "stale_after_ms": 2 * DAY,
                                "retention": "fundingHistory: full history", "units": {"funding_rate": "rate/1h", "premium": "fraction"}},
}
HL_SYMBOLS = s.HL_SYMBOLS


# ---------------- helpers ----------------
def iso(ms: Optional[int]) -> Optional[str]:
    return d.iso(ms)


def day_start(ms: int) -> int:
    return ms - ms % DAY


def partition_bounds(period: str, label: str) -> Tuple[int, int]:
    if period == "day":
        lo = int(datetime.strptime(label, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
        return lo, lo + DAY
    y, m = map(int, label.split("-"))
    lo = datetime(y, m, 1, tzinfo=UTC)
    hi = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=UTC)
    return int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)


def partition_of(period: str, ts: int) -> str:
    t = datetime.fromtimestamp(ts / 1000, UTC)
    return t.strftime("%Y-%m-%d") if period == "day" else t.strftime("%Y-%m")


def labels_between(period: str, lo_ms: int, hi_ms: int) -> List[str]:
    """Closed partitions whose start is >= the partition of lo_ms and whose end is <= hi_ms."""
    out, cur = [], partition_of(period, lo_ms)
    while True:
        a, b = partition_bounds(period, cur)
        if b > hi_ms:
            break
        out.append(cur)
        cur = partition_of(period, b)
    return out


def series_key(o: Dict) -> str:
    return f"{o['source']}|{o['instrument']}|{o['metric']}"


def content_fingerprint(obs: Iterable[Dict]) -> str:
    """Identity of a partition's content: everything except this run's retrieval time."""
    h = hashlib.sha256()
    for o in sorted(obs, key=lambda x: (series_key(x), x["timestamp"])):
        x = {k: v for k, v in o.items() if k != "retrieved_at"}
        h.update(json.dumps(x, sort_keys=True).encode())
        h.update(b"\n")
    return h.hexdigest()


def measurement(o: Dict) -> Dict:
    """What a conflict is judged on: the measured value and its definition, not this run's retrieval time and not
    file-level provenance in `raw` (a re-published archive file has a new sha256 but may carry identical values)."""
    return {k: v for k, v in o.items() if k not in ("retrieved_at", "raw")}


def _gz_bytes(lines: Iterable[str]) -> bytes:
    buf = tempfile.SpooledTemporaryFile()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:      # mtime=0: identical content -> identical bytes
        for line in lines:
            g.write((line + "\n").encode())
    buf.seek(0)
    return buf.read()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def read_partition(path: Path) -> List[Dict]:
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


def frozen_cutoffs(paths: Sequence[Path] = FROZEN) -> Dict[str, int]:
    """Last timestamp per series in the frozen historical datasets (read-only)."""
    cut: Dict[str, int] = {}
    for p in paths:
        if not p.exists():
            continue
        with gzip.open(p, "rt") as f:
            for line in f:
                o = json.loads(line)
                k = series_key(o)
                cut[k] = max(cut.get(k, 0), o["timestamp"])
    return cut


def slot_of(ts: int, grid: int) -> int:
    return (ts + grid // 2) // grid * grid          # funding timestamps carry ms jitter around the grid


def expected_slots(lo: int, hi: int, grid: int, after: Optional[int]) -> List[int]:
    start = lo if after is None else max(lo, slot_of(after, grid) + grid)
    return list(range(start, hi, grid))


def compress_ranges(slots: Sequence[int], grid: int) -> List[List[Optional[str]]]:
    out: List[List[int]] = []
    for t in sorted(slots):
        if out and t - out[-1][1] == grid:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [[iso(a), iso(b)] for a, b in out]


# ---------------- store ----------------
class ForwardStore:
    def __init__(self, root: Path, cutoffs: Dict[str, int]):
        self.root = Path(root)
        self.cutoffs = cutoffs

    def paths(self, source: str, label: str) -> Tuple[Path, Path, Path]:
        y = label[:4]
        base = self.root / source / y / label
        return base.with_suffix(".jsonl.gz"), Path(str(base) + ".meta.json"), self.root / "raw" / source / y / f"{label}.jsonl.gz"

    def meta(self, source: str, label: str) -> Optional[Dict]:
        mp = self.paths(source, label)[1]
        return json.loads(mp.read_text()) if mp.exists() else None

    def expected(self, source: str, label: str, series: Sequence[str]) -> Dict[str, List[int]]:
        spec = SOURCE_SPECS[source]
        lo, hi = partition_bounds(spec["period"], label)
        return {k: expected_slots(lo, hi, spec["grid_ms"], self.cutoffs.get(k)) for k in series}

    def write(self, source: str, label: str, obs: List[Dict], raw: List[Dict], series: Sequence[str], run: Dict) -> Dict:
        """Applies the write rules to one partition. Returns the attempt record."""
        spec = SOURCE_SPECS[source]
        lo, hi = partition_bounds(spec["period"], label)
        # keep only this partition, only after the frozen cutoff, deterministic de-duplication
        kept = [o for o in obs if lo <= o["timestamp"] < hi and o["timestamp"] > self.cutoffs.get(series_key(o), -1)]
        store = d.ObservationStore()
        store.add(kept)
        rows = [o for k in sorted(store.keys(), key="|".join) for o in store.series(k)]
        internal_conflicts = {"|".join(k): v[:5] for k, v in store.conflicts.items()}
        exp = self.expected(source, label, series)
        present = defaultdict(set)
        for o in rows:
            present[series_key(o)].add(slot_of(o["timestamp"], spec["grid_ms"]))
        missing = {k: sorted(set(v) - present[k]) for k, v in exp.items()}
        n_exp = sum(len(v) for v in exp.values())
        n_miss = sum(len(v) for v in missing.values())
        rec = {"source": source, "partition": label, "observations": len(rows), "expected_slots": n_exp, "missing_slots": n_miss,
               "missing_ranges": {k: compress_ranges(v, spec["grid_ms"]) for k, v in missing.items() if v},
               "duplicates_dropped": sum(store.duplicates.values()), "internal_conflicts": internal_conflicts}
        if not rows:
            rec["status"] = "NO_DATA"
            return rec
        completeness = "COMPLETE" if n_miss == 0 else "GAPS"
        fp = content_fingerprint(rows)
        dp, mp, rp = self.paths(source, label)
        old = self.meta(source, label)
        if old is not None and old["fingerprint"] == fp:
            rec.update(status="UNCHANGED", completeness=old["completeness"])
            return rec
        if old is not None:
            prev = {(series_key(o), o["timestamp"]): o for o in read_partition(dp)}
            new = {(series_key(o), o["timestamp"]): o for o in rows}
            diffs = [k for k in prev if k in new and measurement(prev[k]) != measurement(new[k])]
            lost = [k for k in prev if k not in new]
            if diffs or lost:
                rec.update(status="CONFLICT", completeness=old["completeness"], kept_fingerprint=old["fingerprint"],
                           candidate_fingerprint=fp, differing=[[k[0], iso(k[1])] for k in diffs[:20]],
                           not_in_candidate=[[k[0], iso(k[1])] for k in lost[:20]])
                return rec
            # strict superset: keep earlier rows (and their first retrieval time), add the new ones
            rows = sorted(list(prev.values()) + [o for k, o in new.items() if k not in prev], key=lambda o: (series_key(o), o["timestamp"]))
            fp = content_fingerprint(rows)
        data = _gz_bytes(json.dumps(o, sort_keys=True) for o in rows)
        raw_bytes = _gz_bytes(json.dumps(r, sort_keys=True) for r in raw)
        atomic_write(rp, raw_bytes)
        atomic_write(dp, data)
        meta = {"source": source, "partition": label, "fingerprint": fp, "file_sha256": hashlib.sha256(data).hexdigest(),
                "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(), "observations": len(rows), "expected_slots": n_exp,
                "missing_slots": n_miss, "missing_ranges": rec["missing_ranges"], "completeness": completeness,
                "first_collected_at": old["first_collected_at"] if old else run["started_at"], "last_written_at": run["started_at"],
                "last_run_id": run["run_id"], "superseded": (old.get("superseded", []) + [old["fingerprint"]]) if old else [],
                "units": SOURCE_SPECS[source]["units"]}
        atomic_write(mp, (json.dumps(meta, indent=1, sort_keys=True) + "\n").encode())   # meta last: a crash before this leaves a retryable state
        rec.update(status="SUPERSEDED" if old else "WRITTEN", completeness=completeness, fingerprint=fp)
        return rec

    def todo(self, source: str, labels: Sequence[str]) -> List[str]:
        """Partitions still to attempt: absent, or stored with gaps."""
        out = []
        for lab in labels:
            m = self.meta(source, lab)
            if m is None or m["completeness"] != "COMPLETE":
                out.append(lab)
        return out

    def rebuild_index(self, now: int) -> Dict:
        idx = {"artifact": "risk-regime-forward-index", "sources": {}}
        for source, spec in SOURCE_SPECS.items():
            metas = sorted((json.loads(p.read_text()) for p in (self.root / source).glob("*/*.meta.json")), key=lambda m: m["partition"])
            complete = [m["partition"] for m in metas if m["completeness"] == "COMPLETE"]
            latest_end = partition_bounds(spec["period"], complete[-1])[1] if complete else None
            age = None if latest_end is None else now - latest_end
            idx["sources"][source] = {
                "partitions": len(metas), "complete": len(complete), "with_gaps": [m["partition"] for m in metas if m["completeness"] != "COMPLETE"],
                "first": metas[0]["partition"] if metas else None, "last": metas[-1]["partition"] if metas else None,
                "latest_complete": complete[-1] if complete else None,
                "stale": None if age is None else age > spec["stale_after_ms"] + spec["available_after_ms"],
                "observations": sum(m["observations"] for m in metas), "retention": spec["retention"], "units": spec["units"],
                "files": {m["partition"]: m["file_sha256"] for m in metas}}
        return idx


# ---------------- collectors (reuse existing implementations) ----------------
def collect_binance_archive(store: ForwardStore, source: str, labels: Sequence[str], run: Dict, fetch: Callable = oc.get_bytes,
                            now: Optional[int] = None) -> List[Dict]:
    spec = SOURCE_SPECS[source]
    kind = "metrics" if source == "binance_oi_archive" else "fundingRate"
    series = [f"{source}|BTCUSDT|{m}" for m in spec["metrics"]]
    out = []
    for lab in labels:
        r = oc.collect_archive(kind, [lab], fetch=fetch, pause_s=0.2)
        if not r["observations"]:
            end = partition_bounds(spec["period"], lab)[1]
            late = (now or int(time.time() * 1000)) - end > spec["stale_after_ms"]
            status = ("MISSING" if late else "NOT_YET_PUBLISHED") if r["files"].get("http_404") else ("CHECKSUM_FAILED" if r["files"].get("checksum_failed") else "FETCH_FAILED")
            out.append({"source": source, "partition": lab, "status": status, "files": r["files"],
                        "error": next((x.get("body") for x in r["raw"] if x.get("status") != 200), None)})
            continue
        rec = store.write(source, lab, r["observations"], r["raw"], series, run)
        rec["files"] = r["files"]
        out.append(rec)
    return out


def _hl_default_candles(sym: str, lo: int, hi: int, cache: Path, log: Counter) -> List[Dict]:
    import hyperliquid_asset_universe_run as ur
    return ur.candles(sym, "1h", lo, hi, cache, log)


def _hl_default_funding(lo: int, hi: int, cache: Path, log: Counter) -> List[Dict]:
    from hyperliquid_research_run import fetch_funding
    return fetch_funding(lo, hi, cache, log)


def collect_hyperliquid(store: ForwardStore, labels: Sequence[str], run: Dict, now: int,
                        candles_fn: Callable = _hl_default_candles, funding_fn: Callable = _hl_default_funding) -> List[Dict]:
    """One request window covering all pending days; observations split into day partitions."""
    out = []
    if not labels:
        return out
    lo, hi = partition_bounds("day", labels[0])[0], partition_bounds("day", labels[-1])[1]
    cache = Path(tempfile.mkdtemp(prefix="hl_forward_"))
    log: Counter = Counter()
    for source in ("hyperliquid_hip3", "hyperliquid_btc_funding"):
        try:
            if source == "hyperliquid_hip3":
                by_sym = {sym: [c for c in candles_fn(sym, lo, hi, cache, log) if c["T"] < now] for sym in HL_SYMBOLS}
                obs = s.hyperliquid_obs(by_sym, now)
                raw_rows = [dict(c, coin=sym) for sym, cs in by_sym.items() for c in cs]
                series = [f"hyperliquid_hip3|{sym}|{m}" for sym in HL_SYMBOLS for m in ("close", "volume")]
                raw_ts = lambda r: r["t"]
            else:
                fund = [r for r in funding_fn(lo, hi, cache, log) if r["t"] < now]
                obs = s.hyperliquid_funding_obs(fund, now)
                raw_rows = list(fund)
                series = [f"hyperliquid_btc_funding|BTC|{m}" for m in ("funding_rate", "premium")]
                raw_ts = lambda r: r["t"]
        except Exception as e:  # recorded, not hidden; next run retries the same partitions
            out += [{"source": source, "partition": lab, "status": "FETCH_FAILED", "error": f"{type(e).__name__}: {e}"} for lab in labels]
            continue
        for lab in labels:
            a, b = partition_bounds("day", lab)
            part = [o for o in obs if a <= o["timestamp"] < b]
            raw = [dict(r, provenance="https://api.hyperliquid.xyz/info") for r in raw_rows if a <= raw_ts(r) < b]
            rec = store.write(source, lab, part, raw, series, run)
            if rec["status"] == "NO_DATA":
                rec["status"] = "MISSING"
            out.append(rec)
    out.append({"source": "hyperliquid", "partition": None, "status": "REQUEST_LOG", "requests": dict(log)})
    return out


# ---------------- run ----------------
def run_collection(root: Path, sources: Sequence[str], now: Optional[int] = None, max_days: int = 60,
                   cutoffs: Optional[Dict[str, int]] = None, fetch: Callable = oc.get_bytes,
                   candles_fn: Callable = _hl_default_candles, funding_fn: Callable = _hl_default_funding) -> Dict:
    now = now or int(time.time() * 1000)
    cut = frozen_cutoffs() if cutoffs is None else cutoffs
    store = ForwardStore(root, cut)
    run = {"artifact": "risk-regime-forward-run", "run_id": os.environ.get("GITHUB_RUN_ID") or uuid.uuid4().hex[:12],
           "started_at": iso(now), "sources": list(sources), "attempts": [], "errors": []}
    try:
        run["location"] = oc.location(fetch)
    except Exception as e:
        run["location"] = {"error": str(e)}
    horizon = now - max_days * DAY
    try:
        for source in ("binance_oi_archive", "binance_funding_archive"):
            if source not in sources:
                continue
            spec = SOURCE_SPECS[source]
            key = f"{source}|BTCUSDT|{spec['metrics'][0]}"
            start = max(cut[key] + spec["grid_ms"] if key in cut else horizon, horizon)     # first expected slot after the frozen data
            labels = store.todo(source, labels_between(spec["period"], start, now))
            run["attempts"] += collect_binance_archive(store, source, labels, run, fetch=fetch, now=now)
        if {"hyperliquid_hip3", "hyperliquid_btc_funding"} & set(sources):
            start = max(min(cut[k] + HOUR if (k := f"hyperliquid_hip3|{sym}|close") in cut else horizon for sym in HL_SYMBOLS), horizon)
            labels = sorted(set(store.todo("hyperliquid_hip3", labels_between("day", start, now - HOUR)))
                            | set(store.todo("hyperliquid_btc_funding", labels_between("day", start, now - HOUR))))
            run["attempts"] += collect_hyperliquid(store, labels, run, now, candles_fn=candles_fn, funding_fn=funding_fn)
    except Exception as e:  # anything unexpected is recorded; partitions already written are complete and atomic
        run["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        idx = store.rebuild_index(now)
        atomic_write(Path(root) / "index.json", (json.dumps(idx, indent=1, sort_keys=True) + "\n").encode())
        run["finished_at"] = iso(int(time.time() * 1000))
        run["summary"] = dict(Counter(a["status"] for a in run["attempts"]))
        run["stale_sources"] = [k for k, v in idx["sources"].items() if v["stale"]]
        name = f"{datetime.fromtimestamp(now / 1000, UTC).strftime('%Y%m%dT%H%M%SZ')}_{run['run_id']}.json"
        atomic_write(Path(root) / "runs" / name, (json.dumps(run, indent=1, sort_keys=True, default=str) + "\n").encode())
    return run


def load_forward(root: Path) -> List[Dict]:
    """All stored forward observations (for analysis); frozen data is loaded separately and never mixed on disk."""
    out = []
    for p in sorted(Path(root).glob("*/*/*.jsonl.gz")):
        if p.parts[-3] in SOURCE_SPECS:
            out += read_partition(p)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--sources", nargs="*", default=list(SOURCE_SPECS))
    ap.add_argument("--max-days", type=int, default=60)
    args = ap.parse_args(argv)
    run = run_collection(Path(args.store), args.sources, max_days=args.max_days)
    print(json.dumps({"location": run.get("location"), "summary": run["summary"], "errors": run["errors"], "stale": run["stale_sources"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
