"""Risk Regime research data layer: the common raw-observation contract, a point-in-time store and data-quality
reporting. RESEARCH ONLY: no score, no weights, no thresholds, no V1 change.

Every collector in `risk_regime_sources.py` emits observations in this one shape:

    source, provider, instrument, metric, timestamp, value, unit, interval, retrieved_at, historical_or_live,
    source_url, coverage_status, available_at, raw

- `timestamp` is the observation time the provider assigns (UTC ms).
- `available_at` is when the value was publicly obtainable, per a documented rule for each source (e.g. the
  close of an hourly bar, the end of a daily bucket, a GDELT batch time plus its 15-minute lag, an article's
  first-seen time). It is never earlier than `timestamp`.
- `retrieved_at` is when this research run fetched it. For a historical backfill it is later than the
  prediction being studied; that is expected and is NOT what availability is judged on. For a LIVE-ONLY
  snapshot (no history exists) `available_at` = `retrieved_at`, so it can never be used for a past timestamp.
- `raw` keeps the provider's original fields for audit. Values are never overwritten by normalised scores.

Nothing is interpolated or forward-filled. "Latest available value" lookups return the value with its own
timestamp and age, so staleness stays visible.
"""
from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

UTC = timezone.utc
HOUR = 3_600_000
DAY = 24 * HOUR

FIELDS = ("source", "provider", "instrument", "metric", "timestamp", "value", "unit", "interval", "retrieved_at",
          "historical_or_live", "source_url", "coverage_status", "available_at")
HISTORICAL, LIVE = "historical", "live"
INTERVAL_MS = {"15m": 15 * 60_000, "1h": HOUR, "8h": 8 * HOUR, "1d": DAY, "event": None, "snapshot": None}


class ContractError(ValueError):
    pass


def make_obs(*, source: str, provider: str, instrument: str, metric: str, timestamp: int, value, unit: str,
             interval: str, retrieved_at: int, historical_or_live: str, source_url: str, available_at: int,
             coverage_status: str = "OK", raw: Optional[Dict] = None) -> Dict:
    """Builds one validated observation. Raises ContractError rather than guessing."""
    if not isinstance(timestamp, int) or timestamp < 946684800000:          # after 2000-01-01, in ms
        raise ContractError(f"{source}/{metric}: timestamp must be UTC epoch ms, got {timestamp!r}")
    if not isinstance(available_at, int) or available_at < timestamp:
        raise ContractError(f"{source}/{metric}: available_at {available_at!r} precedes timestamp {timestamp}")
    if historical_or_live not in (HISTORICAL, LIVE):
        raise ContractError(f"{source}/{metric}: historical_or_live must be '{HISTORICAL}' or '{LIVE}'")
    if historical_or_live == LIVE and available_at < retrieved_at:
        raise ContractError(f"{source}/{metric}: a live snapshot is only available from its retrieval time")
    if interval not in INTERVAL_MS:
        raise ContractError(f"{source}/{metric}: unknown interval {interval!r}")
    if value is not None and not isinstance(value, (int, float, str)):
        raise ContractError(f"{source}/{metric}: value must be a number, string or None")
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError(f"{source}/{metric}: non-finite value")
    return {"source": source, "provider": provider, "instrument": instrument, "metric": metric, "timestamp": timestamp,
            "value": value, "unit": unit, "interval": interval, "retrieved_at": retrieved_at,
            "historical_or_live": historical_or_live, "source_url": source_url, "coverage_status": coverage_status,
            "available_at": available_at, "raw": raw or {}}


def series_key(o: Dict) -> Tuple[str, str, str]:
    return o["source"], o["instrument"], o["metric"]


class ObservationStore:
    """In-memory, append-only store. Exact duplicates (same series and timestamp, same value) are dropped and
    counted; conflicting duplicates (same series and timestamp, different value) keep the first and are
    recorded as anomalies, never silently merged."""

    def __init__(self):
        self._series: Dict[Tuple[str, str, str], List[Dict]] = defaultdict(list)
        self._index: Dict[Tuple[str, str, str], Dict[int, Dict]] = defaultdict(dict)
        self.duplicates: Counter = Counter()
        self.conflicts: Dict[Tuple[str, str, str], List[Dict]] = defaultdict(list)
        self._sorted = True
        self._ts: Dict[Tuple[str, str, str], List[int]] = {}

    def add(self, obs: Iterable[Dict]) -> int:
        n = 0
        for o in obs:
            k = series_key(o)
            prev = self._index[k].get(o["timestamp"])
            if prev is not None:
                if prev["value"] == o["value"]:
                    self.duplicates[k] += 1
                else:
                    self.conflicts[k].append({"timestamp": o["timestamp"], "kept": prev["value"], "dropped": o["value"]})
                continue
            self._index[k][o["timestamp"]] = o
            self._series[k].append(o)
            n += 1
        self._sorted = False
        return n

    def _sort(self):
        if not self._sorted:
            for k in self._series:
                self._series[k].sort(key=lambda o: o["timestamp"])
            self._ts = {k: [o["timestamp"] for o in v] for k, v in self._series.items()}
            self._sorted = True

    def keys(self) -> List[Tuple[str, str, str]]:
        return sorted(self._series)

    def series(self, key: Tuple[str, str, str]) -> List[Dict]:
        self._sort()
        return list(self._series.get(key, []))

    def latest_available(self, key: Tuple[str, str, str], t_ms: int) -> Optional[Dict]:
        """Newest observation of `key` whose `available_at` <= t_ms."""
        o, _ = self.latest_available_with_previous(key, t_ms)
        return o

    def latest_available_with_previous(self, key: Tuple[str, str, str], t_ms: int) -> Tuple[Optional[Dict], Optional[Dict]]:
        """(latest observation available at t, the observation just before it in the series). Because
        available_at >= timestamp, the scan starts at the last timestamp <= t_ms and walks back."""
        self._sort()
        s = self._series.get(key, [])
        i = bisect.bisect_right(self._ts.get(key, []), t_ms) - 1
        while i >= 0:
            if s[i]["available_at"] <= t_ms:
                return s[i], (s[i - 1] if i > 0 else None)
            i -= 1
        return None, None

    def __len__(self):
        return sum(len(v) for v in self._series.values())


def get_observations_available_at(store: ObservationStore, prediction_ts: int,
                                  max_age_ms: Optional[Dict[str, int]] = None) -> Dict[Tuple[str, str, str], Dict]:
    """For every series, the latest observation that was available at `prediction_ts` (available_at <= ts),
    with its age. Never returns information published after `prediction_ts`. `max_age_ms` (per source) only
    labels an observation STALE; it never hides it and never substitutes a value."""
    out = {}
    for k in store.keys():
        o = store.latest_available(k, prediction_ts)
        if o is None:
            continue
        age = prediction_ts - o["timestamp"]
        limit = (max_age_ms or {}).get(k[0])
        out[k] = {**o, "age_ms": age, "stale": bool(limit is not None and age > limit)}
    return out


def change_over(store: ObservationStore, key: Tuple[str, str, str], t_ms: int, lookback_ms: int) -> Optional[Dict]:
    """Change between the latest value available at t and the latest value available at t - lookback, both
    point-in-time. Returns None (not 0) when either side is missing or non-numeric."""
    a = store.latest_available(key, t_ms)
    b = store.latest_available(key, t_ms - lookback_ms)
    if not a or not b or not isinstance(a["value"], (int, float)) or not isinstance(b["value"], (int, float)):
        return None
    d = a["value"] - b["value"]
    pct = (d / b["value"] * 100) if b["value"] else None
    return {"from_ts": b["timestamp"], "to_ts": a["timestamp"], "delta": d, "pct": pct}


# ---------------- data quality ----------------
def quality(store: ObservationStore, key: Tuple[str, str, str], window: Optional[Tuple[int, int]] = None) -> Dict:
    s = store.series(key)
    if window:
        s = [o for o in s if window[0] <= o["timestamp"] <= window[1]]
    if not s:
        return {"observations": 0, "coverage_status": "NO_DATA"}
    interval = s[0]["interval"]
    step = INTERVAL_MS.get(interval)
    ts = [o["timestamp"] for o in s]
    gaps = []
    anomalies = Counter()
    for a, b in zip(ts, ts[1:]):
        if step and b - a > step * 1.5:
            gaps.append({"from": iso(a), "to": iso(b), "missing_intervals": round((b - a) / step) - 1})
    for o in s:
        if o["available_at"] < o["timestamp"]:
            anomalies["available_before_timestamp"] += 1
        if o["historical_or_live"] == HISTORICAL and o["timestamp"] > o["retrieved_at"]:
            anomalies["timestamp_after_retrieval"] += 1
        if step and step >= HOUR and o["timestamp"] % step not in (0,) and interval in ("1h", "1d", "8h"):
            anomalies["not_on_interval_boundary"] += 1
        if o["value"] is None:
            anomalies["null_value"] += 1
    span_lo, span_hi = (window or (ts[0], ts[-1]))
    expected = (int((span_hi - span_lo) // step) + 1) if step else None
    own_expected = (round((ts[-1] - ts[0]) / step) + 1) if step else None
    spacing = sorted(b - a for a, b in zip(ts, ts[1:]))
    return {
        "first": iso(ts[0]), "last": iso(ts[-1]), "observations": len(s), "expected": expected,
        "coverage_pct": round(100 * len(s) / expected, 2) if expected else None,
        "missing_pct": round(100 * (1 - len(s) / expected), 2) if expected else None,
        "coverage_basis": "requested window" if window else "own first..last",
        "coverage_within_own_range_pct": round(100 * len(s) / own_expected, 2) if own_expected else None,
        "duplicates_dropped": store.duplicates.get(key, 0), "conflicting_duplicates": len(store.conflicts.get(key, [])),
        "timestamp_anomalies": dict(anomalies), "gaps": len(gaps), "largest_gaps": sorted(gaps, key=lambda g: -g["missing_intervals"])[:5],
        "resolution": interval, "median_spacing_min": round(spacing[len(spacing) // 2] / 60_000, 2) if spacing else None,
        "historical_depth_days": round((ts[-1] - ts[0]) / DAY, 2),
        "historical_or_live": sorted({o["historical_or_live"] for o in s}),
        "no_lookahead": "available_at rule applied per observation" if not anomalies.get("available_before_timestamp") else "VIOLATION",
    }


def iso(t_ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(t_ms / 1000, UTC).isoformat() if t_ms is not None else None
