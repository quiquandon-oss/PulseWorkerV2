"""Historical hourly candles for BTC, ETH and LINK from Hyperliquid's free candle endpoint. RESEARCH ONLY.

Retrospective reference data: these candles were retrieved now, after the fact. They show what the market did; they
are NOT a record of what CryptoPulse knew at the time. Nothing here touches production (no D1, no Worker, no secret).

Store layout (a research-data branch, see MARKET_MOVES_DATA.md):
  hourly/<ASSET>/observations.jsonl  first-seen accepted candles, one JSON object per line, sorted by open_ts.
                                     Never rewritten: a later retrieval that differs is a revision, not an overwrite.
  hourly/<ASSET>/revisions.jsonl     later retrievals whose candle differs from the accepted one (both kept).
  runs/<run_id>.json                 one manifest per collection run (request, counts, exclusions, file sha256s).

Point-in-time rule: a candle is known only after it closes. available_at = close_ts + 1 ms. A candle whose close_ts is
not before the retrieval time is still forming; it is excluded and counted, never stored.

Usage:
  python3 research/market_moves/collect.py collect --store <dir> [--assets BTC ETH LINK] [--days 200]
  python3 research/market_moves/collect.py quality --store <dir> --out <report.json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

ENDPOINT = "https://api.hyperliquid.xyz/info"
SOURCE = "hyperliquid"
INTERVAL = "1h"
HOUR_MS = 3600_000
ASSETS = ("BTC", "ETH", "LINK")
CHUNK_HOURS = 1000              # candles per request, well under the provider's 5000-candle response cap
REQUEST_SPACING_S = 1.5         # polite spacing between requests (free endpoint)
VOLUME_MEANING = ("Hyperliquid candle field 'v': traded volume of the perpetual during the candle, in units of the "
                  "base asset (e.g. BTC), on Hyperliquid only; 'n': number of trades. Venue-specific, not market-wide.")
COLLECTOR_VERSION = "market-moves-collect-v1"


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def http_post_json(url: str, body: dict, timeout: float = 30.0):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def normalize(raw: dict, asset: str, retrieved_at: int, request: dict) -> dict:
    """One provider candle -> one store record. Prices and volume stay the provider's decimal strings (no float)."""
    return {
        "record_id": f"{asset}:{INTERVAL}:{int(raw['t'])}",
        "asset": asset,
        "market": f"{SOURCE}:perp:{raw.get('s', asset)}",
        "interval": raw.get("i", INTERVAL),
        "open_ts": int(raw["t"]),
        "close_ts": int(raw["T"]),
        "available_at": int(raw["T"]) + 1,
        "o": str(raw["o"]), "h": str(raw["h"]), "l": str(raw["l"]), "c": str(raw["c"]),
        "v": str(raw["v"]), "n": int(raw["n"]) if raw.get("n") is not None else None,
        "source": SOURCE, "endpoint": f"POST {ENDPOINT} candleSnapshot",
        "request": request,
        "retrieved_at": retrieved_at,
        "raw": raw,
        "raw_sha256": sha256_text(canonical(raw)),
        "collector_version": COLLECTOR_VERSION,
    }


def candle_problems(r: dict) -> List[str]:
    """Per-candle validity checks. Empty list = valid."""
    p = []
    if r["open_ts"] % HOUR_MS != 0:
        p.append("open_ts_not_hour_aligned")
    if r["close_ts"] != r["open_ts"] + HOUR_MS - 1:
        p.append("close_ts_not_open_plus_1h_minus_1ms")
    try:
        o, h, l, c, v = (Decimal(r[k]) for k in ("o", "h", "l", "c", "v"))
    except (InvalidOperation, KeyError):
        return p + ["unparsable_number"]
    if min(o, h, l, c) <= 0:
        p.append("non_positive_price")
    if not (l <= min(o, c) and max(o, c) <= h and l <= h):
        p.append("ohlc_inconsistent")
    if v < 0:
        p.append("negative_volume")
    if r.get("n") is not None and r["n"] < 0:
        p.append("negative_trade_count")
    return p


def fetch_asset(asset: str, start_ms: int, end_ms: int, post: Callable = http_post_json, clock: Callable = lambda: int(time.time() * 1000),
                sleep: Callable = time.sleep) -> Dict:
    """All closed 1h candles for asset with open_ts in [start_ms, end_ms), fetched in chunks. Forming candles excluded."""
    accepted, excluded_forming, duplicates_in_response, requests = [], 0, 0, []
    seen = set()
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + CHUNK_HOURS * HOUR_MS)
        req = {"type": "candleSnapshot", "req": {"coin": asset, "interval": INTERVAL, "startTime": cursor, "endTime": chunk_end - 1}}
        data = post(ENDPOINT, req)
        retrieved_at = clock()
        requests.append({"request": req, "retrieved_at": retrieved_at, "candles": len(data) if isinstance(data, list) else None})
        if not isinstance(data, list):
            raise RuntimeError(f"{asset}: unexpected response type {type(data).__name__}")
        for raw in data:
            if int(raw["T"]) >= retrieved_at:
                excluded_forming += 1          # not closed when retrieved: never stored
                continue
            if not (cursor <= int(raw["t"]) < chunk_end):
                continue                       # outside the requested chunk (provider may pad); the owning chunk keeps it
            if int(raw["t"]) in seen:
                duplicates_in_response += 1
                continue
            seen.add(int(raw["t"]))
            accepted.append(normalize(raw, asset, retrieved_at, req["req"]))
        cursor = chunk_end
        if cursor < end_ms:
            sleep(REQUEST_SPACING_S)
    accepted.sort(key=lambda r: r["open_ts"])
    return {"records": accepted, "excluded_forming": excluded_forming, "duplicates_in_response": duplicates_in_response, "requests": requests}


def read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def merge_into_store(store: Path, asset: str, fetched: List[dict], run_id: str) -> Dict:
    """Append-only merge. New candle -> appended. Same raw -> confirmation only. Different raw -> revision recorded,
    the originally accepted candle and its retrieval time are kept unchanged."""
    adir = store / "hourly" / asset
    adir.mkdir(parents=True, exist_ok=True)
    obs_path, rev_path = adir / "observations.jsonl", adir / "revisions.jsonl"
    existing = {r["record_id"]: r for r in read_jsonl(obs_path)}
    new, confirmed, revisions = [], 0, []
    for r in fetched:
        old = existing.get(r["record_id"])
        if old is None:
            new.append(r)
        elif old["raw_sha256"] == r["raw_sha256"]:
            confirmed += 1
        else:
            revisions.append({"record_id": r["record_id"], "run_id": run_id, "accepted_raw_sha256": old["raw_sha256"],
                              "accepted_retrieved_at": old["retrieved_at"], "revised_raw_sha256": r["raw_sha256"],
                              "revised_retrieved_at": r["retrieved_at"], "revised_raw": r["raw"]})
    if new:
        merged = sorted(list(existing.values()) + new, key=lambda r: r["open_ts"])
        # The file is re-written in sorted order (a backfilled earlier hour lands in place), but every pre-existing
        # line is kept byte-identical, verified here; nothing accepted earlier can change.
        lines = [canonical(r) for r in merged]
        before = {canonical(r) for r in existing.values()}
        assert before <= set(lines), "append-only violation: an accepted record would change"
        obs_path.write_text("\n".join(lines) + "\n")
    if revisions:
        with rev_path.open("a") as fh:
            for rv in revisions:
                fh.write(canonical(rv) + "\n")
    return {"new": len(new), "confirmed_identical": confirmed, "revisions": len(revisions)}


def quality_report(records: List[dict], revisions: List[dict]) -> Dict:
    """Coverage and data-quality summary for one asset (counts only; no returns or performance)."""
    if not records:
        return {"candles": 0}
    ts = [r["open_ts"] for r in records]
    dup = len(ts) - len(set(ts))
    gaps = []
    for a, b in zip(ts, ts[1:]):
        if b - a > HOUR_MS:
            gaps.append({"after_open_ts": a, "next_open_ts": b, "missing_hours": (b - a) // HOUR_MS - 1})
    problems = {}
    for r in records:
        for p in candle_problems(r):
            problems[p] = problems.get(p, 0) + 1
    vols = [float(Decimal(r["v"])) for r in records]
    med = statistics.median(vols)
    zero_vol = sum(1 for v in vols if v == 0)
    extreme = [r["record_id"] for r, v in zip(records, vols) if med > 0 and v > 20 * med]
    span_h = (ts[-1] - ts[0]) // HOUR_MS + 1
    return {
        "candles": len(records), "first_open_ts": ts[0], "last_open_ts": ts[-1], "span_hours": span_h,
        "coverage_pct": round(100 * len(set(ts)) / span_h, 3), "duplicate_open_ts": dup,
        "gaps": gaps, "missing_hours_total": sum(g["missing_hours"] for g in gaps),
        "invalid_candles": problems, "zero_volume_candles": zero_vol,
        "volume_median_base_units": med, "volume_over_20x_median": extreme,
        "revisions_recorded": len(revisions),
        "utc_hour_alignment_ok": all(t % HOUR_MS == 0 for t in ts),
    }


def run_collect(store: Path, assets: Iterable[str], days: int, post=http_post_json, clock=lambda: int(time.time() * 1000), sleep=time.sleep) -> Dict:
    started = clock()
    end_ms = (started // HOUR_MS) * HOUR_MS + HOUR_MS        # through the current (forming) hour; forming excluded
    start_ms = end_ms - days * 24 * HOUR_MS
    run_id = f"run-{started}"
    manifest = {"run_id": run_id, "collector_version": COLLECTOR_VERSION, "source": SOURCE, "endpoint": ENDPOINT,
                "interval": INTERVAL, "requested_open_ts_range": [start_ms, end_ms], "requested_days": days,
                "started_at": started, "volume_meaning": VOLUME_MEANING, "assets": {}}
    for asset in assets:
        got = fetch_asset(asset, start_ms, end_ms, post=post, clock=clock, sleep=sleep)
        merged = merge_into_store(store, asset, got["records"], run_id)
        adir = store / "hourly" / asset
        manifest["assets"][asset] = {
            "fetched_closed_candles": len(got["records"]), "excluded_forming": got["excluded_forming"],
            "duplicates_in_response": got["duplicates_in_response"], "requests": got["requests"], **merged,
            "first_open_ts": got["records"][0]["open_ts"] if got["records"] else None,
            "last_open_ts": got["records"][-1]["open_ts"] if got["records"] else None,
            "observations_sha256": sha256_file(adir / "observations.jsonl"),
            "revisions_sha256": sha256_file(adir / "revisions.jsonl"),
        }
        sleep(REQUEST_SPACING_S)
    manifest["finished_at"] = clock()
    (store / "runs").mkdir(parents=True, exist_ok=True)
    (store / "runs" / f"{run_id}.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect"); c.add_argument("--store", required=True); c.add_argument("--assets", nargs="+", default=list(ASSETS)); c.add_argument("--days", type=int, default=200)
    q = sub.add_parser("quality"); q.add_argument("--store", required=True); q.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    store = Path(a.store)
    if a.cmd == "collect":
        m = run_collect(store, a.assets, a.days)
        for asset, s in m["assets"].items():
            print(f"{asset}: {s['fetched_closed_candles']} closed candles, new {s['new']}, identical {s['confirmed_identical']}, revisions {s['revisions']}, forming excluded {s['excluded_forming']}")
    else:
        rep = {"collector_version": COLLECTOR_VERSION, "volume_meaning": VOLUME_MEANING, "assets": {}}
        for asset in ASSETS:
            adir = store / "hourly" / asset
            rep["assets"][asset] = quality_report(read_jsonl(adir / "observations.jsonl"), read_jsonl(adir / "revisions.jsonl"))
        Path(a.out).write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n")
        for asset, s in rep["assets"].items():
            print(asset, {k: s.get(k) for k in ("candles", "coverage_pct", "missing_hours_total", "duplicate_open_ts", "invalid_candles", "revisions_recorded")})


if __name__ == "__main__":
    sys.exit(main())
