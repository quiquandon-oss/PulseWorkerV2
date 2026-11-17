"""Read-only audit of the forecast resolution rule (EXP-004 TimesFM and EXP-008 Chronos-2).

Frozen rule (research/plans/2026-11-01-prospective-evaluation-plans.md): the actual price is the first btc_data price
at or after target_ts = input_end_ts + horizon. btc_data is written once per 3-hourly prediction tick, a few seconds
after the hour, with seconds of jitter. input_end_ts is itself one of those tick timestamps, so target_ts lands a few
seconds after or before the tick that belongs to it. When the tick's price is written even 1 ms before target_ts, the
frozen rule skips it and takes the NEXT tick, about 3 h later: a "12 h" forecast is then scored on a ~15 h outcome.

This module computes, for each forecast, both the frozen-rule price and the slot price (first price at or after
target_ts - SLOT_TOLERANCE_MS) and reports where they differ and whether direction correctness changes. It reads only
the inputs it is given, writes nothing, and changes no resolution: adopting a different rule is a plan amendment.

Usage: python research/resolution_slot_audit.py --ledger ledger.json --prices btc.json  (prices: [{"ts":..,"btc_price":..}])
"""
import argparse
import bisect
import json

SLOT_TOLERANCE_MS = 120_000          # observed early arrivals 1.8-50.4 s (2026-11-17); ticks are 3 h apart
TICK_MS = 3 * 3600 * 1000


def first_at_or_after(prices, t):
    """prices: list of (ts, price) sorted by ts. First entry with ts >= t, or None."""
    i = bisect.bisect_left(prices, (t, float("-inf")))
    return prices[i] if i < len(prices) else None


def audit(forecasts, prices, tolerance_ms=SLOT_TOLERANCE_MS):
    px = sorted((int(p["ts"]), float(p["btc_price"])) for p in prices if p.get("btc_price") is not None)
    rows, summary = [], {"n": 0, "differs": 0, "correctness_changes": 0, "unresolvable_frozen": 0, "late_over_1h_frozen": 0}
    for f in forecasts:
        t, p0 = int(f["target_ts"]), float(f["price_at_prediction"])
        frozen = first_at_or_after(px, t)
        slot = first_at_or_after(px, t - tolerance_ms)
        if frozen is None:
            summary["unresolvable_frozen"] += 1
            continue
        summary["n"] += 1

        def call(price):
            return "UP" if (price - p0) / p0 * 100 > 0 else "DOWN"

        frozen_dir, slot_dir = call(frozen[1]), call(slot[1])
        late_ms = frozen[0] - t
        if late_ms > 3600_000:
            summary["late_over_1h_frozen"] += 1
        differs = slot[0] != frozen[0]
        changed = differs and (f["direction"] == frozen_dir) != (f["direction"] == slot_dir)
        summary["differs"] += int(differs)
        summary["correctness_changes"] += int(changed)
        rows.append({"id": f.get("id"), "horizon_hours": f.get("horizon_hours"), "input_end_ts": f.get("input_end_ts"),
                     "target_ts": t, "frozen_price_ts": frozen[0], "frozen_late_ms": late_ms, "slot_price_ts": slot[0],
                     "slot_early_ms": t - slot[0], "direction": f["direction"], "frozen_actual": frozen_dir,
                     "slot_actual": slot_dir, "differs": differs, "correctness_changes": changed})
    return {"tolerance_ms": tolerance_ms, "summary": summary, "rows": rows}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--prices", required=True)
    a = ap.parse_args(argv)
    ledger = json.load(open(a.ledger, encoding="utf-8"))
    forecasts = ledger["forecasts"] if isinstance(ledger, dict) else ledger
    out = audit(forecasts, json.load(open(a.prices, encoding="utf-8")))
    print(json.dumps(out["summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
