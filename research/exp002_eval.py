"""EXP-002 evaluator (anomaly-day shadow selection vs production selection), frozen plan section B of
research/plans/2026-11-01-prospective-plans-h2.md. The plan is not changed by this file.

What the plan fixes and this file applies literally: population (BTC, anomaly rows with ts >= 2026-11-01), pairing by
coin, horizon and prediction_ts, the stored fields `chosen_p_up_anomaly` and `chosen_p_up` (call UP at >= 0.5),
informative units, the non-overlap rule, the price and resolution rule, the exact two-sided sign test, Holm over two
horizons at alpha 0.05, and the checkpoint of 111 kept informative units per horizon.

What the plan did not anticipate, and how it is handled here (PAIRING RULE "cron-identified-v1", PROPOSED: its
adoption is an owner decision and is reported with every output):
- several `selection_decisions` rows for one cycle: the production row is the one recorded with trigger "cron" in
  `selection_provenance` (available from 2026-11-12); for earlier cycles, the earliest row whose prediction was
  written in a scheduled hour (divisible by 3) no more than 300 s after the hour and whose own ts is no more than
  60 s after the prediction;
- no such row: the cycle is excluded and counted;
- several anomaly rows for one cycle: the earliest is used, the others are counted.

Below the checkpoint only counts are produced: no outcome is read and no test is run. Every result carries the
disclosure that the two stored calls are each the chosen variant's probability on its latest resolved prediction at
its own computation time, so the comparison is between stored selection outputs, not two predictions of one cycle.

  python3 research/exp002_eval.py --anomaly-csv data-exports/selection_decisions_anomaly.csv \
      --selection-csv data-exports/selection_decisions.csv --provenance-csv data-exports/selection_provenance.csv \
      --price-csv data-exports/btc_data.csv
"""
import argparse
import bisect
import csv
import json
from pathlib import Path

from prospective_eval import ALPHA, exact_two_sided, holm

POPULATION_START = 1793491200000      # 2026-11-01T00:00:00Z
CHECKPOINT = 111                      # kept informative units per horizon
H = 3600 * 1000
TICK_OFFSET_MAX_MS = 300 * 1000
SELECTION_LAG_MAX_MS = 60 * 1000
RESOLUTION_LATE_MAX_MS = 60 * 60 * 1000
PAIRING_RULE = "cron-identified-v1 (PROPOSED, pending owner adoption)"
DISCLOSURE = ("The production call and the shadow call are the stored fields chosen_p_up and chosen_p_up_anomaly. Each is the "
              "chosen variant's probability on its latest resolved prediction at the time that selection was computed, not "
              "necessarily the prediction of the cycle being scored. The two can rest on different resolved predictions and "
              "different information cutoffs. This comparison does not measure two predictions for the same cycle.")


def _i(x):
    return int(float(x))


def production_row(rows, cron_ids):
    """rows: selection_decisions rows of one cycle. Returns (row, how) or (None, reason)."""
    by_prov = sorted((r for r in rows if _i(r["id"]) in cron_ids), key=lambda r: _i(r["ts"]))
    if by_prov:
        return by_prov[0], "provenance"
    timed = []
    for r in rows:
        p, ts = _i(r["prediction_ts"]), _i(r["ts"])
        on_tick = (p // H) % 3 == 0 and p % H <= TICK_OFFSET_MAX_MS
        if on_tick and 0 <= ts - p <= SELECTION_LAG_MAX_MS:
            timed.append(r)
    if timed:
        return min(timed, key=lambda r: _i(r["ts"])), "timing"
    return None, ("no_selection_row" if not rows else "no_identifiable_cron_row")


def resolve(prices_ts, prices, t, horizon_h):
    """Frozen rule: price at t = last price at or before t; resolution = first price at or after t + h, excluded if
    more than 60 minutes late. Returns 1 (UP, strictly above 0), 0 (DOWN) or None (unresolved)."""
    a = bisect.bisect_right(prices_ts, t) - 1
    b = bisect.bisect_left(prices_ts, t + horizon_h * H)
    if a < 0 or b >= len(prices_ts) or prices_ts[b] - (t + horizon_h * H) > RESOLUTION_LATE_MAX_MS:
        return None
    return 1 if prices[b] - prices[a] > 0 else 0


def evaluate(anomaly_rows, selection_rows, provenance_rows, price_rows, start=POPULATION_START, checkpoint=CHECKPOINT):
    cron_ids = {_i(r["selection_row_id"]) for r in provenance_rows
                if r.get("selection_table") == "selection_decisions" and r.get("selection_trigger") == "cron" and r.get("selection_row_id") not in (None, "")}
    sel = {}
    for r in selection_rows:
        if r.get("coin") == "BTC" and r.get("prediction_ts") not in (None, ""):
            sel.setdefault((_i(r["horizon_hours"]), _i(r["prediction_ts"])), []).append(r)
    by_h = {}
    for r in anomaly_rows:
        if r.get("coin") == "BTC" and _i(r["ts"]) >= start:
            by_h.setdefault(_i(r["horizon_hours"]), {}).setdefault(_i(r["prediction_ts"]), []).append(r)
    out = {"experiment": "EXP-002", "pairing_rule": PAIRING_RULE, "disclosure": DISCLOSURE, "alpha": ALPHA,
           "checkpoint_kept_informative_units_per_horizon": checkpoint, "horizons": {}}
    kept_by_h = {}
    for h, cycles in sorted(by_h.items()):
        c = {"anomaly_rows": sum(len(v) for v in cycles.values()), "cycles": len(cycles), "repeated_anomaly_rows_excluded": 0,
             "excluded_no_selection_row": 0, "excluded_no_identifiable_cron_row": 0, "excluded_missing_stored_value": 0,
             "paired_by_provenance": 0, "paired_by_timing": 0, "same_call_pairs_excluded": 0, "informative_units": 0,
             "informative_units_dropped_for_overlap": 0, "kept_informative_units": 0}
        kept, last = [], None
        for p in sorted(cycles):
            rows = sorted(cycles[p], key=lambda r: _i(r["ts"]))
            c["repeated_anomaly_rows_excluded"] += len(rows) - 1
            prod, how = production_row(sel.get((h, p), []), cron_ids)
            if prod is None:
                c["excluded_" + how] += 1
                continue
            if rows[0].get("chosen_p_up_anomaly") in (None, "") or prod.get("chosen_p_up") in (None, ""):
                c["excluded_missing_stored_value"] += 1
                continue
            c["paired_by_" + how] += 1
            shadow_up = float(rows[0]["chosen_p_up_anomaly"]) >= 0.5
            prod_up = float(prod["chosen_p_up"]) >= 0.5
            if shadow_up == prod_up:
                c["same_call_pairs_excluded"] += 1
                continue
            c["informative_units"] += 1
            if last is not None and p < last + h * H:
                c["informative_units_dropped_for_overlap"] += 1
                continue
            last = p
            kept.append((p, shadow_up))
        c["kept_informative_units"] = len(kept)
        c["checkpoint_reached"] = len(kept) >= checkpoint
        out["horizons"][f"{h}h"] = c
        kept_by_h[h] = kept
    if not kept_by_h or not all(len(k) >= checkpoint for k in kept_by_h.values()) or len(kept_by_h) < 2:
        out["status"] = "INSUFFICIENT_SAMPLE"
        out["note"] = "Counts only. No outcome was read and no test was run below the checkpoint."
        return out
    # Checkpoint reached at both horizons: the one confirmatory analysis.
    pts = sorted((_i(r["ts"]), float(r["btc_price"])) for r in price_rows if r.get("btc_price") not in (None, ""))
    prices_ts, prices = [t for t, _ in pts], [v for _, v in pts]
    pvals = {}
    for h, kept in kept_by_h.items():
        right = unresolved = 0
        n = 0
        for p, shadow_up in kept:
            y = resolve(prices_ts, prices, p, h)
            if y is None:
                unresolved += 1
                continue
            n += 1
            right += (y == 1) == shadow_up
        pvals[f"{h}h"] = exact_two_sided(right, n)
        out["horizons"][f"{h}h"].update({"resolved_kept_units": n, "unresolved_kept_units_excluded": unresolved,
                                         "shadow_right": right, "shadow_right_share": round(right / n, 4) if n else None,
                                         "p_value": pvals[f"{h}h"]})
    rejected = holm(pvals)
    for k, rej in rejected.items():
        hz = out["horizons"][k]
        hz["significant_after_holm"] = rej
        hz["supported"] = bool(rej and hz["shadow_right_share"] is not None and hz["shadow_right_share"] > 0.5)
    out["status"] = "EVALUATED"
    return out


def _read(path):
    if not path or not Path(path).exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--anomaly-csv", required=True); ap.add_argument("--selection-csv", required=True)
    ap.add_argument("--provenance-csv"); ap.add_argument("--price-csv"); ap.add_argument("--out")
    a = ap.parse_args(argv)
    text = json.dumps(evaluate(_read(a.anomaly_csv), _read(a.selection_csv), _read(a.provenance_csv), _read(a.price_csv)), indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
