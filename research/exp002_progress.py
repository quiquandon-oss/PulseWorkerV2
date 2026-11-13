"""EXP-002 progress: counts only. No call, pairing decision, outcome or test is computed here.

The frozen plan (research/plans/2026-11-01-prospective-plans-h2.md, section B) pairs each
`selection_decisions_anomaly` row (BTC, ts >= 2026-11-01) with "the" `selection_decisions` row of the same coin,
horizon and prediction_ts. Production data has cycles with several rows and cycles with none, and how those are
handled is an open owner decision. Until it is taken, this reports only how much material exists and how much of
it is affected, so the distance to the checkpoint (111 kept informative units per horizon) can be judged.

  python3 research/exp002_progress.py --anomaly-csv data-exports/selection_decisions_anomaly.csv \
      --selection-csv data-exports/selection_decisions.csv
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path

POPULATION_START = 1793491200000   # 2026-11-01T00:00:00Z
CHECKPOINT = 111                   # kept informative units per horizon (frozen)
H = 3600 * 1000


def progress(anomaly_rows, selection_rows, start=POPULATION_START):
    sel = Counter()
    for r in selection_rows:
        if r.get("coin") == "BTC" and r.get("prediction_ts") not in (None, ""):
            sel[(int(r["horizon_hours"]), int(float(r["prediction_ts"])))] += 1
    out = {"population_start_ms": start, "checkpoint_kept_informative_units_per_horizon": CHECKPOINT, "horizons": {}}
    by_h = {}
    for r in anomaly_rows:
        if r.get("coin") != "BTC" or int(float(r["ts"])) < start:
            continue
        by_h.setdefault(int(r["horizon_hours"]), []).append(int(float(r["prediction_ts"])))
    for h, pts in sorted(by_h.items()):
        cycles = Counter(pts)
        n_sel = Counter(min(sel.get((h, p), 0), 2) for p in cycles)
        kept, last = 0, None
        for p in sorted(cycles):                      # upper bound: cycles whose windows do not overlap
            if last is None or p >= last + h * H:
                kept, last = kept + 1, p
        out["horizons"][f"{h}h"] = {
            "anomaly_rows": len(pts), "cycles": len(cycles),
            "cycles_logged_more_than_once": sum(1 for c in cycles.values() if c > 1),
            "cycles_with_no_selection_row": n_sel.get(0, 0),
            "cycles_with_one_selection_row": n_sel.get(1, 0),
            "cycles_with_several_selection_rows": n_sel.get(2, 0),
            "non_overlapping_cycles_upper_bound_on_kept_units": kept,
            "checkpoint_reached": False if kept < CHECKPOINT else None,   # None: cannot be decided from counts alone
        }
    return out


def _read(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--anomaly-csv", required=True)
    ap.add_argument("--selection-csv", required=True)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    text = json.dumps(progress(_read(a.anomaly_csv), _read(a.selection_csv)), indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
