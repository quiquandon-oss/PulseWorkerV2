"""Decides whether the /d1-usage payload shows D1 consumption heading for the daily read limit.

D1's free plan stops serving reads after 5,000,000 rows read in a UTC day; that stalled predictions on
2026-11-11. This fails (so the workflow run shows red) when the rows recorded so far today exceed a share of the
limit, either outright or projected to the end of the day at the rate so far. Read-only; it only interprets the
public usage payload, whose totals are a lower bound (small requests are not recorded).

  python3 ops/d1_usage_alert.py usage.json [now_epoch_seconds]
"""
import json
import sys
import time

MAX_SHARE_SO_FAR = 0.60      # recorded rows read today, as a share of the daily limit
MAX_SHARE_PROJECTED = 0.85   # the same, projected to 24 h at today's rate so far
MIN_HOURS_FOR_PROJECTION = 3.0


def assess(payload, now):
    """Return (problems, lines). `now` is epoch seconds."""
    if not payload.get("ok"):
        return ["usage endpoint did not return ok"], []
    if not payload.get("recording"):
        return ["usage is not being recorded (table missing)"], []
    limit = payload.get("daily_read_limit") or 5000000
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    day = next((d for d in payload.get("days") or [] if d.get("day") == today), None)
    if day is None:
        hours = (now % 86400) / 3600.0
        # every hourly scheduled run is recorded, so an empty day after the first hours means recording stopped
        return (["no usage recorded today"] if hours >= 2.0 else []), [f"{today}: nothing recorded yet"]
    read = day.get("rows_read") or 0
    hours = max((now % 86400) / 3600.0, 1e-9)
    lines = [f"{today}: {read:,} rows read recorded ({read / limit:.1%} of {limit:,}), {day.get('rows_written') or 0:,} written"]
    for e in (day.get("entries") or [])[:5]:
        lines.append(f"  {e.get('kind')} {e.get('key')}: {e.get('rows_read') or 0:,} rows in {e.get('invocations')} invocations")
    out = []
    if read > MAX_SHARE_SO_FAR * limit:
        out.append(f"rows read today are {read / limit:.0%} of the daily limit (alert above {MAX_SHARE_SO_FAR:.0%})")
    if hours >= MIN_HOURS_FOR_PROJECTION:
        projected = read * 24.0 / hours
        lines.append(f"  projected for the day at this rate: {projected:,.0f} ({projected / limit:.0%})")
        if projected > MAX_SHARE_PROJECTED * limit:
            out.append(f"projected rows read for the day are {projected / limit:.0%} of the limit (alert above {MAX_SHARE_PROJECTED:.0%})")
    return out, lines


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    now = float(argv[1]) if len(argv) > 1 else time.time()
    found, lines = assess(json.load(open(argv[0])), now)
    for line in lines:
        print(line)
    for p in found:
        print(f"- {p}")
    print("D1 consumption: " + ("AT RISK" if found else "within limits"))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
