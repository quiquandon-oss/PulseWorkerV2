"""Prospective evaluator for the frozen plans in research/plans/2026-11-01-prospective-evaluation-plans.md.

EXP-004 (TimesFM) and EXP-003 (momentum overlay). Reads only rows at or after the population start (2026-11-01T00:00Z),
so no sealed-period or pilot outcome enters. Until a checkpoint is reached it reports progress only: it never reports a
test result on a sample smaller than the frozen checkpoint.

Usage:
  python3 research/prospective_eval.py exp004 --timesfm tfm12.json tfm24.json --chronos-store <dir> --knn-csv btc_predictions.csv
  python3 research/prospective_eval.py exp003 --momentum-csv selection_decisions_momentum.csv
"""
import argparse
import csv
import json
from math import comb
from pathlib import Path

POPULATION_START = 1793491200000          # 2026-11-01T00:00:00Z
ALPHA = 0.05
EXP004_CHECKPOINT = 100                    # resolved origins per horizon
EXP003_CHECKPOINT = 60                     # non-tied rows per horizon
KNN_WINDOW = (-3 * 3600000, 10 * 60000)


def exact_two_sided(k, n):
    """Exact two-sided binomial test of k successes in n trials at p = 0.5 (sign test / McNemar on discordant pairs)."""
    if n == 0:
        return 1.0
    k = min(k, n - k)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def holm(pvals):
    """Holm step-down: {name: p} -> {name: rejected?} at ALPHA."""
    order = sorted(pvals, key=pvals.get)
    out, m, stop = {}, len(order), False
    for i, name in enumerate(order):
        if stop or pvals[name] > ALPHA / (m - i):
            stop = True
            out[name] = False
        else:
            out[name] = True
    return out


def mcnemar(a, b):
    """Paired correctness vectors (1/0). Returns discordant counts and exact p."""
    a_only = sum(1 for x, y in zip(a, b) if x == 1 and y == 0)
    b_only = sum(1 for x, y in zip(a, b) if x == 0 and y == 1)
    return {"a_only": a_only, "b_only": b_only, "p": exact_two_sided(a_only, a_only + b_only)}


# ------------------------------------------------------------------ EXP-004

def exp004(tfm_rows, persistence_by_origin, knn_rows, start=POPULATION_START, checkpoint=EXP004_CHECKPOINT):
    """tfm_rows: TimesFM forecasts (dicts from /research/timesfm-recent). persistence_by_origin: {(h, input_end_ts): 'UP'|'DOWN'}
    from EXP-008 forecast files. knn_rows: {h: [(ts, p_up)]}."""
    report = {"plan": "research/plans/2026-11-01-prospective-evaluation-plans.md#exp-004", "population_start": start}
    for h in (12, 24):
        rows = sorted((r for r in tfm_rows if int(r["horizon_hours"]) == h and int(r["input_end_ts"]) >= start), key=lambda r: r["input_end_ts"])
        excluded = {"unresolved": 0, "no_persistence": 0}
        resolved = [r for r in rows if r.get("correct") is not None]
        excluded["unresolved"] = len(rows) - len(resolved)
        n = len(resolved)
        h_out = {"n_resolved": n, "checkpoint": checkpoint, "excluded": excluded}
        if n < checkpoint:
            h_out["status"] = f"COLLECTING ({n}/{checkpoint})"
            report[f"h{h}"] = h_out
            continue
        window = resolved[:checkpoint]                       # the frozen first window
        tfm = [int(r["correct"]) for r in window]
        up = [int(r["actual_direction"] == "UP") for r in window]
        pers_pairs = [(int(r["correct"]), int(persistence_by_origin[(h, int(r["input_end_ts"]))] == r["actual_direction"]))
                      for r in window if (h, int(r["input_end_ts"])) in persistence_by_origin]
        excluded["no_persistence"] = checkpoint - len(pers_pairs)
        knn = []
        for r in window:
            c = [k for k in knn_rows.get(h, []) if KNN_WINDOW[0] <= k[0] - int(r["input_end_ts"]) <= KNN_WINDOW[1]]
            if c:
                p_up = min(c, key=lambda k: abs(k[0] - int(r["input_end_ts"])))[1]
                knn.append((int(r["correct"]), int((p_up >= 0.5) == (r["actual_direction"] == "UP"))))
        h_out.update(status="CHECKPOINT REACHED", hits={"timesfm": sum(tfm), "always_up": sum(up)},
                     vs_always_up=mcnemar(tfm, up),
                     vs_persistence=mcnemar([a for a, _ in pers_pairs], [b for _, b in pers_pairs]) | {"n": len(pers_pairs)},
                     descriptive_vs_knn=mcnemar([a for a, _ in knn], [b for _, b in knn]) | {"n": len(knn)})
        report[f"h{h}"] = h_out
    tests = {f"h{h}_{c}": report[f"h{h}"][c]["p"] for h in (12, 24) for c in ("vs_always_up", "vs_persistence")
             if c in report[f"h{h}"]}
    if tests:
        rej = holm(tests)
        for h in (12, 24):
            d = report[f"h{h}"]
            if "vs_always_up" in d:
                better = d["vs_always_up"]["a_only"] > d["vs_always_up"]["b_only"] and d["vs_persistence"]["a_only"] > d["vs_persistence"]["b_only"]
                d["first_window_meets_criterion"] = bool(better and rej[f"h{h}_vs_always_up"] and rej[f"h{h}_vs_persistence"])
                d["note"] = "Success also requires the same result in the next non-overlapping window (replication)."
    return report


# ------------------------------------------------------------------ EXP-003

def exp003(rows, start=POPULATION_START, checkpoint=EXP003_CHECKPOINT):
    """rows: dicts of selection_decisions_momentum (CSV export)."""
    report = {"plan": "research/plans/2026-11-01-prospective-evaluation-plans.md#exp-003", "population_start": start}
    seen, unique, duplicates = set(), [], 0
    for r in sorted(rows, key=lambda r: int(float(r["ts"]))):
        if int(float(r["ts"])) < start:
            continue
        key = (str(r["horizon_hours"]), str(r["prediction_ts"]))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(r)
    pvals = {}
    for h in ("12", "24"):
        above = below = ties = unscored = 0
        for r in (x for x in unique if str(x["horizon_hours"]) == h):
            scores = {s["variant"]: s["lca"] for s in json.loads(r["scores_json"])}
            chosen = scores.get(r["production_chosen_variant"])
            if chosen is None or "challenger_momentum" not in scores:
                unscored += 1
                continue
            m = scores["challenger_momentum"]
            if m > chosen: above += 1
            elif m < chosen: below += 1
            else: ties += 1
        n = above + below
        d = {"non_tied": n, "above": above, "below": below, "excluded": {"ties": ties, "chosen_variant_unscored": unscored},
             "checkpoint": checkpoint}
        if n < checkpoint:
            d["status"] = f"COLLECTING ({n}/{checkpoint})"
        else:
            # first `checkpoint` non-tied rows in time order form the frozen first window
            win_above = 0
            counted = 0
            for r in (x for x in unique if str(x["horizon_hours"]) == h):
                scores = {s["variant"]: s["lca"] for s in json.loads(r["scores_json"])}
                c = scores.get(r["production_chosen_variant"])
                if c is None or "challenger_momentum" not in scores or scores["challenger_momentum"] == c:
                    continue
                win_above += scores["challenger_momentum"] > c
                counted += 1
                if counted == checkpoint:
                    break
            d.update(status="CHECKPOINT REACHED", first_window_above=win_above, p=exact_two_sided(win_above, checkpoint))
            pvals[h] = d["p"]
        report[f"h{h}"] = d
    report["duplicates_dropped"] = duplicates
    if pvals:
        rej = holm(pvals)
        for h, ok in rej.items():
            d = report[f"h{h}"]
            d["first_window_meets_criterion"] = bool(ok and d["first_window_above"] * 2 > checkpoint)
            d["note"] = "Success also requires the same result in the next non-overlapping window (replication)."
    return report


# ------------------------------------------------------------------ CLI

def _load_knn(path, start):
    out = {12: [], 24: []}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            ts = int(float(r["ts"]))
            if ts < start - 3 * 3600000 or r.get("p_up") in (None, ""):
                continue
            h = int(float(r["horizon_hours"]))
            if h in out:
                out[h].append((ts, float(r["p_up"])))
    return out


def _load_persistence(store):
    out = {}
    for fp in Path(store).glob("forecasts/BTC/*h/*.json"):
        f = json.loads(fp.read_text())
        out[(int(f["horizon_hours"]), int(f["input_end_ts"]))] = f["persistence_direction"]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a4 = sub.add_parser("exp004")
    a4.add_argument("--timesfm", nargs="+", required=True)
    a4.add_argument("--chronos-store", required=True)
    a4.add_argument("--knn-csv", required=True)
    a3 = sub.add_parser("exp003")
    a3.add_argument("--momentum-csv", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "exp004":
        rows = [r for f in a.timesfm for r in json.loads(Path(f).read_text())["forecasts"]]
        rep = exp004(rows, _load_persistence(a.chronos_store), _load_knn(a.knn_csv, POPULATION_START))
    else:
        with open(a.momentum_csv, newline="") as f:
            rep = exp003(list(csv.DictReader(f)))
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
