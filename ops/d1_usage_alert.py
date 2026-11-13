"""Decides whether the /d1-usage payload shows D1 consumption heading for the daily read limit.

D1's free plan stops serving reads after 5,000,000 rows read in a UTC day; that stalled predictions on
2026-11-11. Read-only; it only interprets the public usage payload, whose totals are a lower bound (requests
reading fewer than 50 rows are not recorded).

Levels
  critical  the rows read recorded today have reached the limit
  warning   more than 60% of the limit is used, or a RELIABLE projection for the day exceeds 85%
  stale     no measurement in the last 2 hours (every hourly scheduled run records one)
  unknown   the payload cannot be interpreted (not ok, not recording, no limit)
  healthy   none of the above

Projection and coverage
  Every hourly scheduled run ("0 * * * *") is recorded, so the number of such runs recorded today is the number
  of hours the day's total actually covers. When that matches the hours elapsed, the day is fully measured and the
  projection is total * 24 / hours elapsed (RELIABLE, from 3 h into the day). When recording started part-way
  through the day (first deployment, or recording was off), the total misses the earlier hours: the projection is
  then the measured hourly rate * 24, labelled PROVISIONAL, and it is reported but does not raise the alert.

  python3 ops/d1_usage_alert.py usage.json [now_epoch_seconds]
"""
import json
import sys
import time

DEFAULT_LIMIT = 5000000
MAX_SHARE_SO_FAR = 0.60      # recorded rows read today, as a share of the daily limit
MAX_SHARE_PROJECTED = 0.85   # reliable projection for the day, as a share of the daily limit
MIN_HOURS_FOR_PROJECTION = 3.0
STALE_HOURS = 2.0
HOURLY_CRON = "0 * * * *"


def coverage(day, now):
    """(hours elapsed today, hours the recorded total covers, whether the whole elapsed day is covered)."""
    elapsed = (now % 86400) / 3600.0
    runs = sum((e.get("invocations") or 0) for e in day.get("entries") or []
               if e.get("kind") == "scheduled" and e.get("key") == HOURLY_CRON)
    return elapsed, min(elapsed, float(runs)), runs >= int(elapsed)


def projection(read, elapsed, covered, complete):
    """(projected rows read for a full day or None, 'reliable' | 'provisional' | 'none')."""
    if complete:
        return (read * 24.0 / elapsed, "reliable") if elapsed >= MIN_HOURS_FOR_PROJECTION else (None, "none")
    if covered < 1.0:
        return None, "none"
    return read / covered * 24.0, "provisional"


def assess(payload, now):
    """Return {'level', 'problems', 'lines', ...figures}. `now` is epoch seconds."""
    out = {"level": "unknown", "problems": [], "lines": []}
    if not payload.get("ok"):
        out["problems"].append("usage endpoint did not return ok")
        return out
    if not payload.get("recording"):
        out["problems"].append("usage is not being recorded (table missing)")
        return out
    limit = payload.get("daily_read_limit")
    if not isinstance(limit, (int, float)) or limit <= 0:
        out["problems"].append("daily read limit not reported")
        return out
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    day = next((d for d in payload.get("days") or [] if d.get("day") == today), None)
    elapsed = (now % 86400) / 3600.0
    if day is None:
        out["lines"].append(f"{today}: nothing recorded yet")
        if elapsed >= STALE_HOURS:   # every hourly scheduled run is recorded, so an empty day means recording stopped
            out.update(level="stale")
            out["problems"].append("no usage recorded today")
        else:
            out.update(level="healthy")
        return out
    read = day.get("rows_read") or 0
    elapsed, covered, complete = coverage(day, now)
    projected, quality = projection(read, max(elapsed, 1e-9), covered, complete)
    stamps = [e.get("last_ts") for e in day.get("entries") or [] if isinstance(e.get("last_ts"), (int, float))]
    last_ts = max(stamps) / 1000.0 if stamps else None
    out.update(read=read, limit=limit, remaining=max(0, limit - read), share=read / limit, covered_hours=covered,
               elapsed_hours=elapsed, coverage_complete=complete, projected=projected, projection_quality=quality, last_ts=last_ts)
    out["lines"].append(f"{today}: {read:,} rows read recorded ({read / limit:.1%} of {limit:,}), {day.get('rows_written') or 0:,} written, "
                        f"{max(0, limit - read):,} remaining")
    out["lines"].append(f"  measured {covered:.1f} h of the {elapsed:.1f} h elapsed today" + ("" if complete else " (partial-day recording)"))
    for e in (day.get("entries") or [])[:5]:
        out["lines"].append(f"  {e.get('kind')} {e.get('key')}: {e.get('rows_read') or 0:,} rows in {e.get('invocations')} invocations")
    if projected is not None:
        out["lines"].append(f"  projected for a full day at this rate: {projected:,.0f} ({projected / limit:.0%}), {quality}")
        if quality == "provisional" and projected > MAX_SHARE_PROJECTED * limit:
            out["lines"].append("  the provisional projection is above the alert threshold; it does not raise the alert until the day is fully measured")
    if last_ts is None or now - last_ts > STALE_HOURS * 3600:
        out.update(level="stale")
        out["problems"].append(f"no usage measurement in the last {STALE_HOURS:g} hours")
        return out
    if read >= limit:
        out.update(level="critical")
        out["problems"].append("rows read today have reached the daily limit")
        return out
    if read > MAX_SHARE_SO_FAR * limit:
        out["problems"].append(f"rows read today are above {MAX_SHARE_SO_FAR:.0%} of the daily limit")
    if quality == "reliable" and projected > MAX_SHARE_PROJECTED * limit:
        out["problems"].append(f"projected rows read for the day are above {MAX_SHARE_PROJECTED:.0%} of the daily limit")
    out.update(level="warning" if out["problems"] else "healthy")
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    now = float(argv[1]) if len(argv) > 1 else time.time()
    result = assess(json.load(open(argv[0])), now)
    for line in result["lines"]:
        print(line)
    for p in result["problems"]:
        print(f"- {p}")
    print("D1 consumption: " + result["level"].upper())
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
