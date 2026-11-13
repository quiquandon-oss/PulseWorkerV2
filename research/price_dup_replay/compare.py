"""Compares the two replay arms (stored series vs one row per run) on the cycles production predicted.

  python3 research/price_dup_replay/compare.py --stored stored.json --one-row onerow.json --exports data-exports \
      [--from 2026-10-09T21:00:00Z] [--out report.json]

Outcomes are production's own stored `realized_up` for each cycle, so both arms are scored against the same
outcome. Only cycles at or after --from are reported. It changes nothing.
"""
import argparse
import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

PRED_CSV = {"BTC": "btc_predictions.csv", "ETH": "eth_predictions.csv", "LINK": "link_predictions.csv"}
CORE_VARIANTS = ["p_up", "p_up_experimental", "calibrated_p_up", "calibrated_conditional_p_up"]
CHALLENGER_VARIANTS = ["p_up_flat", "p_up_tilted", "calibrated_p_up_flat"]


def metrics(pairs):
    """pairs: (p_up, realized_up). Call is UP when p_up >= 0.5."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    tp = sum(1 for p, y in pairs if p >= 0.5 and y == 1)
    fp = sum(1 for p, y in pairs if p >= 0.5 and y == 0)
    tn = sum(1 for p, y in pairs if p < 0.5 and y == 0)
    fn = sum(1 for p, y in pairs if p < 0.5 and y == 1)
    div = lambda a, b: round(a / b, 4) if b else None
    return {"n": n, "up_outcomes": tp + fn, "down_outcomes": tn + fp, "up_calls": tp + fp, "down_calls": tn + fn,
            "accuracy": div(tp + tn, n), "up_precision": div(tp, tp + fp), "up_recall": div(tp, tp + fn),
            "down_precision": div(tn, tn + fn), "down_recall": div(tn, tn + fp),
            "balanced_accuracy": (round((tp / (tp + fn) + tn / (tn + fp)) / 2, 4) if (tp + fn) and (tn + fp) else None),
            "brier": round(sum((p - y) ** 2 for p, y in pairs) / n, 4), "mean_p_up": round(sum(p for p, _ in pairs) / n, 4)}


def compare_variant(a, b, truth):
    """a, b: {key: p_up} per arm; truth: {key: realized_up}. Uses keys present in both arms with a value."""
    keys = sorted(k for k in a if k in b and a[k] is not None and b[k] is not None)
    if not keys:
        return {"compared": 0}
    diffs = sorted(abs(a[k] - b[k]) for k in keys)
    changed = [k for k in keys if (a[k] >= 0.5) != (b[k] >= 0.5)]
    res = [k for k in keys if truth.get(k) in (0, 1)]
    ch_res = [k for k in changed if truth.get(k) in (0, 1)]
    return {
        "compared": len(keys), "changed_calls": len(changed), "changed_calls_share": round(len(changed) / len(keys), 4),
        "abs_p_difference": {"mean": round(statistics.fmean(diffs), 4), "median": round(statistics.median(diffs), 4),
                             "p90": round(diffs[int(0.9 * (len(diffs) - 1))], 4), "max": round(diffs[-1], 4),
                             "share_identical": round(sum(1 for d in diffs if d < 1e-12) / len(diffs), 4)},
        "resolved": len(res),
        "stored_series": metrics([(a[k], truth[k]) for k in res]),
        "one_row_per_run": metrics([(b[k], truth[k]) for k in res]),
        "changed_calls_resolved": len(ch_res),
        "changed_calls_where_stored_series_was_right": sum(1 for k in ch_res if (a[k] >= 0.5) == (truth[k] == 1)),
        "changed_calls_where_one_row_per_run_was_right": sum(1 for k in ch_res if (b[k] >= 0.5) == (truth[k] == 1)),
    }


def non_overlapping(keys, horizon_h):
    kept, last = 0, None
    for ts in sorted(keys):
        if last is None or ts >= last + horizon_h * 3600 * 1000:
            kept, last = kept + 1, ts
    return kept


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stored", required=True); ap.add_argument("--one-row", required=True)
    ap.add_argument("--exports", required=True); ap.add_argument("--from", dest="start", default="2026-10-09T21:00:00Z")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    start = int(datetime.fromisoformat(a.start.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)
    A, B = json.loads(Path(a.stored).read_text()), json.loads(Path(a.one_row).read_text())
    report = {"reported_from": a.start, "arms": {"stored": {k: A[k] for k in ("cycles", "price_rows", "failures")},
                                                 "one_row_per_run": {k: B[k] for k in ("cycles", "price_rows", "failures")}}, "results": {}}
    for coin, name in PRED_CSV.items():
        prod = {}
        with open(Path(a.exports) / name, newline="") as f:
            for r in csv.DictReader(f):
                ts, h = int(r["ts"]), int(r.get("horizon_hours") or 24)
                if ts >= start:
                    prod[(h, ts)] = {"p_up": float(r["p_up"]) if r["p_up"] != "" else None,
                                     "realized_up": int(float(r["realized_up"])) if r["realized_up"] != "" else None}
        for h in sorted({k[0] for k in prod}):
            cyc = {k[1]: v for k, v in prod.items() if k[0] == h}
            truth = {ts: v["realized_up"] for ts, v in cyc.items()}
            block = {"production_cycles": len(cyc), "resolved_production_cycles": sum(1 for v in truth.values() if v in (0, 1)),
                     "non_overlapping_windows": non_overlapping(cyc, h)}
            pa = {r["ts"]: r for r in A["predictions"][coin] if r["horizon_hours"] == h and r["ts"] in cyc}
            pb = {r["ts"]: r for r in B["predictions"][coin] if r["horizon_hours"] == h and r["ts"] in cyc}
            block["cycles_predicted"] = {"stored": len(pa), "one_row_per_run": len(pb)}
            # Fidelity: does the stored-series arm reproduce what production stored?
            fid = [ts for ts in pa if cyc[ts]["p_up"] is not None and pa[ts]["p_up"] is not None]
            if fid:
                d = sorted(abs(pa[ts]["p_up"] - cyc[ts]["p_up"]) for ts in fid)
                block["fidelity_stored_arm_vs_production"] = {
                    "compared": len(fid), "identical_p_up_share": round(sum(1 for x in d if x < 1e-9) / len(d), 4),
                    "median_abs_p_difference": round(statistics.median(d), 4), "p90_abs_p_difference": round(d[int(0.9 * (len(d) - 1))], 4),
                    "same_call_share": round(sum(1 for ts in fid if (pa[ts]["p_up"] >= 0.5) == (cyc[ts]["p_up"] >= 0.5)) / len(fid), 4)}
            res = [ts for ts, y in truth.items() if y in (0, 1) and cyc[ts]["p_up"] is not None]
            block["production_stored_p_up"] = metrics([(cyc[ts]["p_up"], truth[ts]) for ts in res])
            block["variants"] = {}
            for v in CORE_VARIANTS:
                block["variants"][v] = compare_variant({ts: r.get(v) for ts, r in pa.items()}, {ts: r.get(v) for ts, r in pb.items()}, truth)
            ca = {r["ts"]: r for r in A["challenger_predictions"] if r["coin"] == coin and r["horizon_hours"] == h and r["ts"] in cyc}
            cb = {r["ts"]: r for r in B["challenger_predictions"] if r["coin"] == coin and r["horizon_hours"] == h and r["ts"] in cyc}
            for v in CHALLENGER_VARIANTS:
                block["variants"]["challenger_" + v] = compare_variant({ts: r.get(v) for ts, r in ca.items()}, {ts: r.get(v) for ts, r in cb.items()}, truth)
            sa = {r["prediction_ts"]: r for r in A["selection_decisions"] if r["coin"] == coin and r["horizon_hours"] == h and r["prediction_ts"] in cyc}
            sb = {r["prediction_ts"]: r for r in B["selection_decisions"] if r["coin"] == coin and r["horizon_hours"] == h and r["prediction_ts"] in cyc}
            both = sorted(set(sa) & set(sb))
            block["selection"] = {
                "cycles_with_a_selection": {"stored": len(sa), "one_row_per_run": len(sb), "both": len(both)},
                "different_chosen_variant": sum(1 for t in both if sa[t]["chosen_variant"] != sb[t]["chosen_variant"]),
                "different_cleared_gate": sum(1 for t in both if sa[t]["cleared_gate"] != sb[t]["cleared_gate"]),
                "cleared_gate": {"stored": sum(1 for t in both if sa[t]["cleared_gate"]), "one_row_per_run": sum(1 for t in both if sb[t]["cleared_gate"])},
                "chosen_variants": {"stored": _count(sa[t]["chosen_variant"] for t in both), "one_row_per_run": _count(sb[t]["chosen_variant"] for t in both)},
                "chosen_p_up": compare_variant({t: sa[t]["chosen_p_up"] for t in both}, {t: sb[t]["chosen_p_up"] for t in both}, truth),
            }
            report["results"][f"{coin}_{h}h"] = block
    text = json.dumps(report, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0


def _count(it):
    out = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())
