"""EXP-23 v0.5.0 intraday funding: observation latency and freshness coverage. RESEARCH ONLY, offline, read-only.

Reads a forward-store checkout (research-data/risk-regime-forward) and reports, per funding source (HL, OKX):
  - settlement -> first observation latency (causal availability minus settlement time), with the share of
    settlements seen within one hour (what an hourly decision grid needs) and within the source's staleness limit;
  - decision hours whose latest causally available settlement meets the unchanged staleness limit, and hours where
    U_<SRC> is computable (staleness plus the unchanged minimum counts), using the evaluator's own functions;
  - collection runs: hourly `--open-day` runs found, clock hours with no such run (missed), and start delay vs :17.
Nothing before T1 (2026-10-09T22:00Z) is read into any statistic, so the sealed period is never inspected. It
changes no rule; the evaluator's source_variants.json remains the record of evaluability with P and O.

Usage: python3 research/exp23_funding_latency.py --store <data>/risk_regime_forward [--until ISO]
"""
import argparse
import glob
import json
import os
from datetime import datetime, timezone

import early_warning_12h as ew

H = ew.H
SCHEDULE_MINUTE = 17          # research/scheduler/research-funding-intraday-schedule.yml


def ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def iso(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def pct(sorted_vals, q):
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))] if sorted_vals else None


def source_report(series, key, hours):
    spec = ew.SOURCE_FUNDING[key]
    lags = sorted(av - ts for ts, (av, _v) in series.items() if ts >= ew.T1)
    fb = ew.FeatureBuilder({}, None, None)
    ts_sorted = sorted(series)
    fresh = computable = 0
    for t in hours:
        avail = [u for u in ts_sorted if series[u][0] <= t]
        fresh += bool(avail) and t - avail[-1] <= spec["stale_ms"]
        computable += fb.source_funding_features(key, series, t, []) is not None
    n = len(hours)
    return {"settlements": len(lags), "latency_ms": {"p50": pct(lags, 0.5), "p90": pct(lags, 0.9), "max": lags[-1] if lags else None},
            "share_within_1h": sum(x <= H for x in lags) / len(lags) if lags else None,
            "share_within_staleness_limit": sum(x <= spec["stale_ms"] for x in lags) / len(lags) if lags else None,
            "staleness_limit_ms": spec["stale_ms"], "decision_hours": n,
            "fresh_hours": fresh, "fresh_share": fresh / n if n else None,
            "u_computable_hours": computable, "u_computable_share": computable / n if n else None}


def runs_report(store, until):
    starts = []
    for p in sorted(glob.glob(os.path.join(store, "runs", "*.json"))):
        r = json.load(open(p))
        if r.get("open_day") and ms(r["started_at"]) >= ew.T1:
            starts.append(ms(r["started_at"]))
    if not starts:
        return {"hourly_runs": 0, "first": None, "missed_hours": None, "start_delay_ms": None}
    first_hour = starts[0] - starts[0] % H
    last = until - SCHEDULE_MINUTE * 60_000                       # the last hour whose scheduled start has passed
    expected = range(first_hour, last - last % H + 1, H)
    seen = {s - s % H for s in starts}
    delays = sorted(s - (s - s % H) - SCHEDULE_MINUTE * 60_000 for s in starts)
    return {"hourly_runs": len(starts), "first": iso(starts[0]), "expected_hours": len(expected),
            "missed_hours": sum(h not in seen for h in expected),
            "start_delay_ms": {"p50": pct(delays, 0.5), "p90": pct(delays, 0.9), "max": delays[-1]}}


def report(store, until):
    out = {"artifact": "exp23-funding-latency", "until": iso(until), "runs": runs_report(store, until), "sources": {}}
    for key in ew.SOURCE_FUNDING:
        series, info = ew.load_source_funding(ew.forward_source_funding_files(store, key), key)
        series = {ts: v for ts, v in series.items() if v[0] <= until}
        start = max(ew.T1, min((ts for ts in series if ts >= ew.T1), default=until))
        hours = list(range(start - start % H + H, until - until % H + 1, H))
        out["sources"][key] = dict(source_report(series, key, hours), conflicts_dropped=info["conflicts_dropped"],
                                   non_causal_rows=info["non_causal_rows"])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--until", help="ISO time; default now")
    a = ap.parse_args(argv)
    until = ms(a.until) if a.until else int(datetime.now(timezone.utc).timestamp() * 1000)
    print(json.dumps(report(a.store, until), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
