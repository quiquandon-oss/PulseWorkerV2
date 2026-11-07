"""PEV-1P: prospective, untouched test window for the PEV-1 models (protocol research/plans/2026-11-02-pev1p-protocol.md).

PEV-1's TEST period (2026-10-09 -> 2026-11-01) had already been looked at in the sentiment-weighting audit, so it can
not support a confirmatory claim. PEV-1P evaluates the same models, metrics and decision rule on a window that starts
2026-11-01T00:00Z and was frozen before any of its outcomes were examined. Hyperparameters are frozen to PEV-1's VAL
choices; nothing is re-tuned.

Two modes:
  counts  - allowed at any time. Reports only how many units exist / are resolved / are eligible per model. It never
            reads a label's value, so it reveals nothing about outcomes (no peeking).
  final   - the single confirmatory analysis. Refuses to run before CHECKPOINT_TS.

  python3 research/prospective_pev.py fetch --out DIR
  python3 research/prospective_pev.py counts --candles PRE_SEAL_DIR DIR --sentiment FILE --out counts.json
  python3 research/prospective_pev.py final  --candles PRE_SEAL_DIR DIR --sentiment FILE --out final.json
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prediction_eval as pe  # noqa: E402

PROTOCOL = "PEV-1P"
TEST_START_TS = 1793491200000          # 2026-11-01T00:00:00Z
CHECKPOINT_TS = 1809129600000          # 2027-05-01T00:00:00Z: the one confirmatory analysis
FROZEN = {"P4-LR_lambda": 10.0, "P4-KNN-SHRINK_m": 30}   # PEV-1 VAL selection, identical in all 6 cells
CANDLE_PAGE_HOURS = 4000                # below Hyperliquid's 5000-candle response cap


def test_units(rows, h, now_ts):
    """Inferential units of the prospective window whose label time has passed (ts + h <= now)."""
    return [u for u in pe.unit_times(rows, h, TEST_START_TS, now_ts) if u["ts"] + h * pe.H <= now_ts]


def final_units(rows, h, now_ts):
    """The frozen confirmatory population: test_units capped at the checkpoint, however late final runs."""
    return test_units(rows, h, min(now_ts, CHECKPOINT_TS))


def models(sent_ts, sentiment):
    base = {"always_up": lambda up, u, h: 1.0, "always_down": lambda up, u, h: 0.0,
            "climatology": pe.climatology, "persistence": lambda up, u, h: pe.persistence(u, h)}
    return dict(base,
                A=lambda up, u, h: pe.model_a(up, u, h, sent_ts, sentiment),
                B=lambda up, u, h: pe.model_corrected(up, u, h),
                C=lambda up, u, h: (pe.model_corrected(up, u, h, sent_ts, sentiment)
                                    if pe.asof_reading(sent_ts, sentiment, u["ts"]) is not None else None),
                **{"P4-LR": lambda up, u, h: pe.model_lr(up, u, h, FROZEN["P4-LR_lambda"]),
                   "P4-KNN-SHRINK": lambda up, u, h: pe.model_shrink(up, u, h, FROZEN["P4-KNN-SHRINK_m"])})


def counts_for_coin(closes, sentiment, now_ts, horizons=pe.HORIZONS_H):
    """Eligibility counts only. Rows omit labels; 'resolved' checks for a future close by key."""
    rows = pe.build_rows(closes, horizons=())
    sent_ts = [s[0] for s in sentiment]
    out = {}
    for h in horizons:
        units = test_units(rows, h, now_ts)
        resolved = lambda u: u["ts"] + h * pe.H in closes
        out[f"{h}h"] = {
            "units_elapsed": len(units),
            "resolved": sum(1 for u in units if resolved(u)),
            "with_price_features": sum(1 for u in units if pe._has_features(u)),
            "with_asof_sentiment": sum(1 for u in units if pe.asof_reading(sent_ts, sentiment, u["ts"]) is not None),
            "eligible_all_models": sum(1 for u in units if resolved(u) and pe._has_features(u)
                                       and pe.asof_reading(sent_ts, sentiment, u["ts"]) is not None),
        }
    return out


def final_for_coin(closes, sentiment, now_ts, horizons=pe.HORIZONS_H):
    rows = pe.build_rows(closes, horizons)
    sent_ts = [s[0] for s in sentiment]
    out = {}
    for h in horizons:
        units = final_units(rows, h, now_ts)
        ms = models(sent_ts, sentiment)
        preds = pe.predict_all(rows, units, h, ms)
        pairs = [(m, b) for m in ("A", "B", "C", "P4-LR", "P4-KNN-SHRINK")
                 for b in ("always_up", "always_down", "persistence", "climatology")] + [("C", "B"), ("C", "A"), ("B", "A")]
        block, keep = pe.score_block(list(ms), preds, units, h, compare_to=pairs)
        block["decision"] = {m: pe.decision_label(block, m) for m in ("A", "B", "C", "P4-LR", "P4-KNN-SHRINK")}
        if h == 24:
            block["goal_a"] = pe.goal_a(["A", "B", "C", "P4-LR", "P4-KNN-SHRINK", "persistence"], preds, units, keep, h)
        out[f"{h}h"] = block
    return out


def fetch(out_dir, start_ts=pe.SEAL_END_TS, end_ts=None, post=None, page_hours=CANDLE_PAGE_HOURS):
    """Post-seal hourly candles, paginated so the window can outgrow one Hyperliquid response."""
    end_ts = end_ts or int(time.time() * 1000)
    post = post or _hl_post
    for coin in pe.COINS:
        seen = {}
        lo = start_ts
        while lo < end_ts:
            hi = min(end_ts, lo + page_hours * pe.H)
            for c in post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": lo, "endTime": hi}}):
                seen[int(c["t"])] = c
            lo = hi
        retrieved = int(time.time() * 1000)
        lines = []
        for t in sorted(seen):
            c = seen[t]
            avail = int(c["T"]) + 1
            if avail > retrieved or avail > end_ts or not pe.price_usable(avail) or avail <= pe.SEAL_END_TS:
                continue                   # still open, or sealed
            lines.append(json.dumps({"asset": coin, "open_ts": t, "close_ts": int(c["T"]), "c": c["c"],
                                     "available_at": avail, "retrieved_at": retrieved, "source": "hyperliquid"}))
        d = Path(out_dir) / coin
        d.mkdir(parents=True, exist_ok=True)
        (d / "observations.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))
        print(coin, len(lines), "closed post-seal candles")


def _hl_post(body):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "cryptopulse-research/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main(argv=None, now_ts=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", required=True)
    for name in ("counts", "final"):
        r = sub.add_parser(name)
        r.add_argument("--candles", nargs="+", required=True)
        r.add_argument("--sentiment", required=True)
        r.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    now_ts = now_ts or int(time.time() * 1000)
    if a.cmd == "fetch":
        fetch(a.out)
        return 0
    if a.cmd == "final" and now_ts < CHECKPOINT_TS:
        print(f"refusing: the confirmatory analysis runs once, at or after {CHECKPOINT_TS} (2027-05-01T00:00Z)", file=sys.stderr)
        return 2
    sentiment = pe.load_sentiment(a.sentiment)
    inputs = {a.sentiment: pe.sha256_of(a.sentiment)}
    for d in a.candles:
        for coin in pe.COINS:
            p = Path(d) / coin / "observations.jsonl"
            if p.exists():
                inputs[str(p)] = pe.sha256_of(p)
    fn = counts_for_coin if a.cmd == "counts" else final_for_coin
    report = {"protocol": PROTOCOL, "mode": a.cmd, "generated_at": now_ts, "test_start_ts": TEST_START_TS,
              "checkpoint_ts": CHECKPOINT_TS, "frozen_hyperparameters": FROZEN, "inputs_sha256": inputs,
              "coins": {c: fn(pe.load_candles(a.candles, c), sentiment, now_ts) for c in pe.COINS}}
    Path(a.out).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
