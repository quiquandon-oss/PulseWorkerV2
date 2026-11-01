"""Does the k-NN feature set carry information about direction? (PEV-1 follow-up, pre-declared, no model selection.)

Pre-seal data only (DEV + VAL of PEV-1; TEST and the sealed period are not used). Non-overlapping inferential units
(00:00 UTC for 24 h, 00:00/12:00 for 12 h). For each coin, horizon and feature:
  - Spearman rank correlation with the forward return, Fisher-z 95% CI;
  - the same within low / high trailing-volatility regimes (median split of the past-24 h realised volatility, a
    quantity known at decision time);
  - class balance (UP share) and feature stability (mean and sd, DEV vs VAL).
Reported only; nothing here is tuned or selected. Multiple comparisons: 3 coins x 2 horizons x 2 features x 3 slices.

  python3 research/feature_information.py --candles <market-moves hourly dir> --out report.json
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prediction_eval as pe  # noqa: E402

FEATURES = ("technical_score", "regime_mag")


def ranks(x):
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman_ci(x, y):
    n = len(x)
    if n < 10:
        return {"n": n, "rho": None, "ci95": None}
    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    rho = num / den if den else 0.0
    z, se = math.atanh(max(-0.999999, min(0.999999, rho))), 1 / math.sqrt(n - 3)
    return {"n": n, "rho": round(rho, 4), "ci95": [round(math.tanh(z - 1.96 * se), 4), round(math.tanh(z + 1.96 * se), 4)]}


def trailing_vol(closes_by_ts, t, hours=24):
    rets = []
    for k in range(hours):
        a, b = closes_by_ts.get(t - (k + 1) * pe.H), closes_by_ts.get(t - k * pe.H)
        if a is None or b is None:
            return None
        rets.append(math.log(b / a))
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets))


def analyse(closes, horizons=pe.HORIZONS_H):
    rows = pe.build_rows(closes, horizons)
    out = {}
    for h in horizons:
        units = [r for r in pe.unit_times(rows, h, 0, pe.SEAL_START_TS) if pe._has_features(r) and r["labels"][h] is not None]
        for u in units:
            u["vol"] = trailing_vol(closes, u["ts"])
        units = [u for u in units if u["vol"] is not None]
        med = sorted(u["vol"] for u in units)[len(units) // 2] if units else None
        block = {"n_units": len(units), "up_share": round(sum(u["labels"][h] > 0 for u in units) / len(units), 4) if units else None,
                 "features": {}}
        for f in FEATURES:
            y = [u["labels"][h] for u in units]
            block["features"][f] = {
                "all": spearman_ci([u[f] for u in units], y),
                "low_vol": spearman_ci([u[f] for u in units if u["vol"] <= med], [u["labels"][h] for u in units if u["vol"] <= med]),
                "high_vol": spearman_ci([u[f] for u in units if u["vol"] > med], [u["labels"][h] for u in units if u["vol"] > med]),
                "stability": {p: (lambda v: {"mean": round(sum(v) / len(v), 3), "sd": round(math.sqrt(sum((a - sum(v) / len(v)) ** 2 for a in v) / len(v)), 3)} if v else None)(
                    [u[f] for u in units if (u["ts"] < pe.DEV_END_TS) == (p == "dev")]) for p in ("dev", "val")},
            }
        out[f"{h}h"] = block
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rep = {"analysis": "feature-information-v1", "data": "pre-seal only (PEV-1 DEV+VAL)",
           "coins": {c: analyse(pe.load_candles([a.candles], c)) for c in pe.COINS}}
    Path(a.out).write_text(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
