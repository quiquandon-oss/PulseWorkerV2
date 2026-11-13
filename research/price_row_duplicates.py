"""Measures duplicate price rows per scheduled run and what they do to row-counted look-backs.

Reads the exported price series (data-exports/btc_data.csv, eth_data.csv, link_data.csv) exactly as stored.
It changes nothing and recomputes no prediction: it describes the series and compares the model's own
row-counted quantities on the stored series against the same series with one row per run (the first).

  python3 research/price_row_duplicates.py --dir data-exports --out report.json

A "run" is a group of rows whose timestamps are within RUN_WINDOW_MS of the group's first row.
"""
import argparse
import csv
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

RUN_WINDOW_MS = 10 * 60 * 1000
VOL_LOOKBACK_ROWS = 14     # worker.js trailingVolatility default lookback
TECH_LOOKBACK_ROWS = 30    # worker.js log*Data: technical score from the last 30 rows
SERIES = {"BTC": ("btc_data.csv", "btc_price"), "ETH": ("eth_data.csv", "eth_price"), "LINK": ("link_data.csv", "link_price")}
H = 3600 * 1000


def load(path, price_col):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("ts") and r.get(price_col) not in (None, ""):
                rows.append({"ts": int(float(r["ts"])), "price": float(r[price_col]),
                             "tech": float(r["technical_score"]) if r.get("technical_score") not in (None, "") else None})
    rows.sort(key=lambda r: r["ts"])
    return rows


def group_runs(rows):
    runs = []
    for r in rows:
        if runs and r["ts"] - runs[-1][0]["ts"] <= RUN_WINDOW_MS:
            runs[-1].append(r)
        else:
            runs.append([r])
    return runs


def trailing_vol(prices, end, lookback=VOL_LOOKBACK_ROWS):
    """Same formula as worker.js trailingVolatility: population std of period-over-period % moves."""
    window = prices[max(0, end - lookback + 1): end + 1]
    if len(window) < 4:
        return None
    rets = [(window[i] - window[i - 1]) / window[i - 1] * 100 for i in range(1, len(window))]
    return statistics.pstdev(rets)


def median_span_hours(ts, lookback):
    spans = [(ts[i] - ts[i - lookback + 1]) / H for i in range(lookback - 1, len(ts))]
    return statistics.median(spans) if spans else None


def month(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m")


def analyse(rows):
    runs = group_runs(rows)
    sizes = Counter(len(g) for g in runs)
    by_month = {}
    for g in runs:
        by_month.setdefault(month(g[0]["ts"]), Counter())[min(len(g), 3)] += 1
    multi = [g for g in runs if len(g) > 1]
    collapsed = [g[0] for g in runs]
    stored_p, coll_p = [r["price"] for r in rows], [r["price"] for r in collapsed]
    # volatility at the last row of each run, on both series
    idx_last, i = [], -1
    for g in runs:
        i += len(g)
        idx_last.append(i)
    pairs = []
    for k, end in enumerate(idx_last):
        a, b = trailing_vol(stored_p, end), trailing_vol(coll_p, k)
        if a is not None and b is not None and b > 0:
            pairs.append(a / b)
    first_multi = next((g[0]["ts"] for g in runs if len(g) > 1), None)
    return {
        "rows": len(rows), "runs": len(runs), "rows_per_run": {str(k): v for k, v in sorted(sizes.items())},
        "first_run_with_more_than_one_row": datetime.fromtimestamp(first_multi / 1000, timezone.utc).isoformat() if first_multi else None,
        "runs_per_month_by_rows": {m: {("3+" if k == 3 else str(k)): v for k, v in sorted(c.items())} for m, c in sorted(by_month.items())},
        "multi_row_runs": len(multi),
        "multi_row_runs_with_different_prices": sum(1 for g in multi if len({r["price"] for r in g}) > 1),
        "multi_row_runs_with_different_technical_score": sum(1 for g in multi if len({r["tech"] for r in g}) > 1),
        "max_seconds_between_rows_of_one_run": max(((g[-1]["ts"] - g[0]["ts"]) / 1000 for g in multi), default=0),
        "share_of_rows_that_repeat_an_earlier_row_of_the_same_run": round((len(rows) - len(runs)) / len(rows), 4) if rows else None,
        "median_hours_covered": {
            f"volatility_lookback_{VOL_LOOKBACK_ROWS}_rows": {"stored": median_span_hours([r["ts"] for r in rows], VOL_LOOKBACK_ROWS),
                                                               "one_row_per_run": median_span_hours([r["ts"] for r in collapsed], VOL_LOOKBACK_ROWS)},
            f"technical_lookback_{TECH_LOOKBACK_ROWS}_rows": {"stored": median_span_hours([r["ts"] for r in rows], TECH_LOOKBACK_ROWS),
                                                               "one_row_per_run": median_span_hours([r["ts"] for r in collapsed], TECH_LOOKBACK_ROWS)},
        },
        "trailing_volatility_stored_over_one_row_per_run": {
            "n": len(pairs), "median_ratio": round(statistics.median(pairs), 4) if pairs else None,
            "p10": round(sorted(pairs)[int(0.1 * (len(pairs) - 1))], 4) if pairs else None,
            "p90": round(sorted(pairs)[int(0.9 * (len(pairs) - 1))], 4) if pairs else None,
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    report = {"run_window_ms": RUN_WINDOW_MS, "series": {}}
    for coin, (name, col) in SERIES.items():
        p = Path(a.dir) / name
        report["series"][coin] = analyse(load(p, col)) if p.exists() else {"missing": str(p)}
    text = json.dumps(report, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
