"""Decides whether the /prediction-freshness payload shows a stalled prediction pipeline.

Used by .github/workflows/prediction-staleness-alert.yml, which fails (so the run shows red and GitHub can notify)
when this returns problems. Read-only; it only interprets the public freshness payload.

  python3 ops/prediction_staleness.py fresh.json
"""
import json
import sys

MIN_CRON_ROWS_24H = 6        # 8 scheduled runs are expected per 24 h; fewer than 6 means at least three were missed
MAX_PRICE_AGE_HOURS = 4.0    # runs are 3 h apart


def problems(payload):
    out = []
    if not payload.get("ok"):
        return ["freshness endpoint did not return ok"]
    for coin, v in sorted((payload.get("coins") or {}).items()):
        age = (v.get("price_data") or {}).get("latest_age_hours")
        if age is None or age > MAX_PRICE_AGE_HOURS:
            out.append(f"{coin} price data: latest row is {age} h old (limit {MAX_PRICE_AGE_HOURS} h)")
        for h, x in sorted((v.get("horizons") or {}).items()):
            for path in ("core", "challenger"):
                y = x.get(path) or {}
                if y.get("status") != "FRESH":
                    out.append(f"{coin} {h}h {path}: status {y.get('status')}")
                elif (y.get("cron_rows_24h") or 0) < MIN_CRON_ROWS_24H:
                    out.append(f"{coin} {h}h {path}: {y.get('cron_rows_24h')} scheduled rows in 24 h (expected 8, minimum {MIN_CRON_ROWS_24H})")
    if not payload.get("coins"):
        out.append("no coins in the freshness payload")
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    found = problems(json.load(open(argv[0])))
    for p in found:
        print(f"- {p}")
    print("prediction pipeline: " + ("STALLED OR DEGRADED" if found else "healthy"))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
