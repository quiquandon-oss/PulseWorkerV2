"""Append-only archive of post-seal Hyperliquid hourly candles for PEV-1P (and any later re-run or audit).

Why: Hyperliquid's public candleSnapshot serves only the most recent ~5,000 hourly candles (208.5 days measured
2026-11-02: oldest 2026-04-07T23:00Z). The PEV-1P analog pool starts at the seal end (2026-10-09T21:00Z); those
candles stop being servable around 2027-05-05, days after the 2027-05-01 checkpoint, and every re-run after that
would be impossible. Pre-seal candles are already preserved on the research-data/market-moves branch.

Layout (same convention as research-data/market-moves):
  <dir>/<COIN>/observations.jsonl  first-seen closed candles, one per open_ts, append-only, sorted
  <dir>/<COIN>/revisions.jsonl     a later retrieval whose OHLCV differs from the first-seen one (both kept)
  <dir>/runs/<run_id>.json         one manifest per run: request ranges, counts, sha256 of every file after the run
Only candles that closed after the seal end and before the retrieval are written: nothing sealed, nothing open.
This module stores raw prices only; it computes no label, prediction or metric.

  python3 research/candle_archive.py update --dir ARCHIVE
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prediction_eval as pe  # noqa: E402
import prospective_pev as pp  # noqa: E402

VERSION = "pev1p-candle-archive-v1"
FIELDS = ("o", "h", "l", "c", "v", "n")


def _read_jsonl(p):
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _record(coin, c, retrieved):
    t = int(c["t"])
    return {"record_id": f"{coin}:1h:{t}", "asset": coin, "open_ts": t, "close_ts": int(c["T"]), "available_at": int(c["T"]) + 1,
            **{k: c.get(k) for k in FIELDS}, "retrieved_at": retrieved, "source": "hyperliquid", "collector_version": VERSION}


def update(archive, post=None, now_ts=None, page_hours=pp.CANDLE_PAGE_HOURS):
    archive = Path(archive)
    now_ts = now_ts or int(time.time() * 1000)
    post = post or pp._hl_post
    run = {"run_id": f"run-{now_ts}", "collector_version": VERSION, "started_at": now_ts, "assets": {}}
    for coin in pe.COINS:
        d = archive / coin
        d.mkdir(parents=True, exist_ok=True)
        obs_p, rev_p = d / "observations.jsonl", d / "revisions.jsonl"
        existing = {r["open_ts"]: r for r in _read_jsonl(obs_p)}
        start = pe.SEAL_END_TS                     # never request sealed candles
        if existing:                               # re-check the last 48 h for revisions, then continue
            start = max(start, max(existing) - 48 * pe.H)
        got, lo = {}, start
        while lo < now_ts:
            hi = min(now_ts, lo + page_hours * pe.H)
            for c in post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": lo, "endTime": hi}}):
                got[int(c["t"])] = c
            lo = hi
        retrieved = now_ts
        new, revisions = [], []
        for t in sorted(got):
            rec = _record(coin, got[t], retrieved)
            if rec["available_at"] <= pe.SEAL_END_TS or rec["available_at"] > now_ts or not pe.price_usable(rec["available_at"]):
                continue                           # sealed or still open
            if t in existing:
                old = existing[t]
                if any(str(old.get(k)) != str(rec.get(k)) for k in FIELDS):
                    revisions.append({"record_id": rec["record_id"], "accepted": {k: old.get(k) for k in FIELDS + ("retrieved_at",)},
                                      "revised": {k: rec.get(k) for k in FIELDS + ("retrieved_at",)}})
            else:
                new.append(rec)
        if new:                                    # append-only: existing lines are never rewritten
            with obs_p.open("a") as f:
                for r in new:
                    f.write(json.dumps(r, sort_keys=True) + "\n")
        if revisions:
            with rev_p.open("a") as f:
                for r in revisions:
                    f.write(json.dumps(r, sort_keys=True) + "\n")
        all_obs = _read_jsonl(obs_p)
        ts = [r["open_ts"] for r in all_obs]
        gaps = sum(1 for a, b in zip(ts, ts[1:]) if b - a != pe.H)
        run["assets"][coin] = {"requested_from": start, "requested_to": now_ts, "received": len(got), "appended": len(new),
                               "revisions": len(revisions), "total": len(all_obs), "gaps": gaps,
                               "first_open_ts": ts[0] if ts else None, "last_open_ts": ts[-1] if ts else None}
    run["finished_at"] = int(time.time() * 1000)
    run["sha256"] = {str(p.relative_to(archive)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(archive.glob("*/*.jsonl"))}
    (archive / "runs").mkdir(exist_ok=True)
    (archive / "runs" / f"{run['run_id']}.json").write_text(json.dumps(run, indent=1, sort_keys=True))
    return run


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update")
    u.add_argument("--dir", required=True)
    a = ap.parse_args(argv)
    run = update(a.dir)
    print(json.dumps(run["assets"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
