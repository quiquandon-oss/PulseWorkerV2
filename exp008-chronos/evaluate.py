"""
EXP-008 descriptive comparison: Chronos-2 vs TimesFM (EXP-004), production k-NN, always-UP and the
provisional last-tick persistence rule, on the same forecast origins.

DESCRIPTIVE ONLY. EXP-008 has no frozen evaluation plan (see EVALUATION_PLAN.md, status PROPOSED), so
this script reports counts, hit rates, Wilson 95% intervals and exact McNemar tests, never a verdict.

Inputs (all read-only):
  --store      checkout of research-data/exp008-chronos (exp008_chronos/)
  --timesfm    JSON files from GET /research/timesfm-recent?horizon=H&limit=N (one per horizon)
  --knn-csv    data-exports/btc_predictions.csv

Population: Chronos-2 forecasts with a resolution file, origin at or after the end of the sealed
period (2026-10-09T21:00Z), paired on (horizon, input_end_ts) with a resolved TimesFM row, and with a
k-NN prediction from the same horizon logged within [-3h, +10min] of the origin (nearest). k-NN rows
before the seal end are dropped while parsing.
Ground truth: the Chronos-2 resolution (same rule as EXP-004). Pairs where it disagrees with the
TimesFM row's actual_direction are reported and excluded.
"""
import argparse
import csv
import json
import math
from datetime import datetime, timezone
from math import comb
from pathlib import Path

SEAL_END = int(datetime(2026, 10, 9, 21, tzinfo=timezone.utc).timestamp() * 1000)
KNN_WINDOW = (-3 * 3600000, 10 * 60000)


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - m, 4), round(c + m, 4)]


def mcnemar_exact(b, c):
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n)


def load_knn(path):
    out = {12: [], 24: []}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = int(float(row["ts"]))
            if ts < SEAL_END:
                continue                         # sealed-window rows never enter the analysis
            h = int(float(row["horizon_hours"]))
            if h in out and row.get("p_up") not in (None, ""):
                out[h].append((ts, float(row["p_up"])))
    return out


def evaluate(store, timesfm_files, knn_csv):
    store = Path(store)
    tfm = {}
    for f in timesfm_files:
        for r in json.loads(Path(f).read_text())["forecasts"]:
            if r.get("correct") is not None:
                tfm[(int(r["horizon_hours"]), int(r["input_end_ts"]))] = r
    knn = load_knn(knn_csv)
    report = {"population": "post-seal origins, paired on (horizon, input_end_ts)", "status": "DESCRIPTIVE (no frozen plan)"}
    for h in (12, 24):
        rows, excluded = [], []
        for fp in sorted((store / "forecasts" / "BTC" / f"{h}h").glob("*.json")):
            fc = json.loads(fp.read_text())
            rp = store / "resolutions" / "BTC" / f"{h}h" / fp.name
            if fc["input_end_ts"] < SEAL_END:
                excluded.append((fc["id"], "origin before seal end")); continue
            if not rp.exists():
                excluded.append((fc["id"], "unresolved")); continue
            res = json.loads(rp.read_text())
            t = tfm.get((h, fc["input_end_ts"]))
            if t is None:
                excluded.append((fc["id"], "no resolved TimesFM row at this origin")); continue
            if t["actual_direction"] != res["actual_direction"]:
                excluded.append((fc["id"], "ground truth differs from TimesFM")); continue
            cands = [k for k in knn[h] if KNN_WINDOW[0] <= k[0] - fc["input_end_ts"] <= KNN_WINDOW[1]]
            if not cands:
                excluded.append((fc["id"], "no k-NN prediction near origin")); continue
            p_up = min(cands, key=lambda k: abs(k[0] - fc["input_end_ts"]))[1]
            up = res["actual_direction"] == "UP"
            rows.append({"chronos": res["correct"], "timesfm": int(t["correct"]), "knn": int((p_up >= 0.5) == up),
                         "always_up": int(up), "persistence": res["persistence_correct"]})
        n = len(rows)
        out = {"n_shared": n, "excluded": excluded, "approaches": {}, "paired_vs_chronos": {}}
        for m in ("chronos", "timesfm", "knn", "always_up", "persistence"):
            vals = [r[m] for r in rows if r[m] is not None]
            k = sum(vals)
            out["approaches"][m] = {"n": len(vals), "correct": k, "incorrect": len(vals) - k,
                                    "hit_rate": round(k / len(vals), 4) if vals else None, "wilson95": wilson(k, len(vals))}
        for m in ("timesfm", "knn", "always_up", "persistence"):
            pairs = [(r["chronos"], r[m]) for r in rows if r[m] is not None]
            b = sum(1 for x, y in pairs if x == 1 and y == 0)
            c = sum(1 for x, y in pairs if x == 0 and y == 1)
            out["paired_vs_chronos"][m] = {"n": len(pairs), "chronos_only_right": b, "other_only_right": c,
                                           "diff_pts": round(100 * (b - c) / len(pairs), 2) if pairs else None,
                                           "mcnemar_exact_p": round(mcnemar_exact(b, c), 4)}
        report[f"h{h}"] = out
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--timesfm", nargs="+", required=True)
    ap.add_argument("--knn-csv", required=True)
    a = ap.parse_args(argv)
    print(json.dumps(evaluate(a.store, a.timesfm, a.knn_csv), indent=1))


if __name__ == "__main__":
    main()
