"""Market-move detection evaluator for evaluation plan v1.0.0. RESEARCH ONLY.

Implements research/market_moves/definition.json (v1.0.0) and evaluation_plan.json (v1.0.0) exactly as frozen:
reference episodes from B1 on hourly candle closes, the B0h / B1 / B2 / B3 detectors, alert-to-episode matching and
the plan's metrics and breakdowns. Nothing here touches production (no D1, no Worker, no secret, no network).

Isolation and sealing (plan "split"):
  * A development run loads only observations with available_at < 2026-07-22T00:00:00Z; nothing in the evaluation
    period is loaded, replayed or computed.
  * Evaluation-period metrics are computed only when --authorize-evaluation is passed the plan's sha256 (an explicit,
    separately authorised run). Without it this program refuses.

B0 (production LARGE_MOVE on a read-only extract of production btc_data) needs that extract. It is not part of the
candle snapshot, so B0 is reported as NOT_RUN with the reason, never approximated.

Usage:
  python3 research/market_moves/evaluate.py --store <market_moves dir> --period development --out <report.json>
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import sqlite3
import statistics
import sys
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # research/ for the unchanged production detector (B0h)

H = 3600_000
D = 24 * H
ASSETS = ("BTC", "ETH", "LINK")
EVALUATOR_VERSION = "market-moves-evaluate-v1"


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso_ms(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def ms_iso(ms: Optional[int]) -> Optional[str]:
    from datetime import datetime, timezone
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- frozen inputs

def load_frozen(root: Path = HERE):
    definition = json.loads((root / "definition.json").read_text())
    plan = json.loads((root / "evaluation_plan.json").read_text())
    def_sha = sha256_text(canonical(definition))
    plan_sha = sha256_text(canonical(plan))
    pinned_def = (root / "definition.sha256").read_text().split()[0]
    pinned_plan = (root / "evaluation_plan.sha256").read_text().split()[0]
    if def_sha != pinned_def or def_sha != plan["definition_sha256"]:
        raise SystemExit(f"definition.json sha256 {def_sha} does not match the pinned value; refusing")
    if plan_sha != pinned_plan:
        raise SystemExit(f"evaluation_plan.json sha256 {plan_sha} does not match the pinned value; refusing")
    return definition, plan, def_sha, plan_sha


class Series:
    """Hourly closes of one asset, indexed by observation time t = available_at."""

    def __init__(self, rows: List[dict]):
        rows = sorted(rows, key=lambda r: r["available_at"])
        self.t = [r["available_at"] for r in rows]
        self.c = [Decimal(r["c"]) for r in rows]
        self.open_ts = [r["open_ts"] for r in rows]
        self.close_ts = [r["close_ts"] for r in rows]
        self.idx = {t: i for i, t in enumerate(self.t)}

    def nearest(self, target: int, tol: int = H) -> Optional[int]:
        """Index of the observation nearest to target within +/- tol; ties go to the earlier one."""
        j = bisect.bisect_left(self.t, target)
        best = None
        for k in (j - 1, j):
            if 0 <= k < len(self.t) and abs(self.t[k] - target) <= tol:
                if best is None or abs(self.t[k] - target) < abs(self.t[best] - target):
                    best = k
        return best

    def gaps_ok(self, lo: int, i_hi: int, max_gap: int = 2 * H) -> bool:
        """No gap between consecutive available observations in [lo, t[i_hi]] exceeds max_gap."""
        i_lo = bisect.bisect_left(self.t, lo)
        for k in range(i_lo, i_hi):
            if self.t[k + 1] - self.t[k] > max_gap:
                return False
        return True


def load_store(store: Path, cutoff: Optional[int], expected_sha: Dict[str, str]) -> Dict[str, Series]:
    out = {}
    for a in ASSETS:
        p = store / "hourly" / a / "observations.jsonl"
        raw = p.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if expected_sha.get(a) and sha != expected_sha[a]:
            raise SystemExit(f"{a} observations sha256 {sha} does not match the plan's snapshot; refusing")
        rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        if cutoff is not None:
            rows = [r for r in rows if r["available_at"] < cutoff]  # development isolation
        out[a] = Series(rows)
    return out


# ---------------------------------------------------------------- per-hour classifiers

VALID, LOW, INSUF = "VALID", "LOW_COVERAGE", "INSUFFICIENT_HISTORY"


def make_return_classifier(lookback: int, thr: Decimal, confirm: Decimal, calm: Decimal):
    """B1 (24 h, 3%) and B2 (4 h, 2.5%): exact Decimal ratio against 1 +/- thr, strict inequalities."""
    up, dn = Decimal(1) + thr, Decimal(1) - thr

    def classify(s: Series, i: int) -> dict:
        t = s.t[i]
        j = s.nearest(t - lookback)
        if j is None or j >= i or not s.gaps_ok(t - lookback - H, i):
            return {"status": LOW}
        ratio = s.c[i] / s.c[j]
        r = ratio - 1
        direction = "UP" if ratio > up else "DOWN" if ratio < dn else "NONE"
        return {"status": VALID, "dir": direction, "value": float(r), "ref_elapsed_ms": t - s.t[j],
                "confirm_mag": abs(r) >= confirm, "calm": abs(r) < calm}
    return classify


def sigma_series(s: Series) -> List[Optional[float]]:
    """sigma_1h at each observation: sample std of the 720 hourly log returns ending at that observation, using only
    returns between consecutive closes exactly 1 h apart; None below 648 valid returns (90%)."""
    n = len(s.t)
    ret_at = [None] * n
    for k in range(1, n):
        if s.t[k] - s.t[k - 1] == H:
            ret_at[k] = math.log(float(s.c[k] / s.c[k - 1]))
    out: List[Optional[float]] = [None] * n
    for k in range(n):
        lo = bisect.bisect_right(s.t, s.t[k] - 720 * H)  # returns in (t - 720 h, t]
        vals = [ret_at[m] for m in range(lo, k + 1) if ret_at[m] is not None]
        out[k] = statistics.stdev(vals) if len(vals) >= 648 else None
    return out


def make_vol_classifier(k: float, sigma: List[Optional[float]]):
    """B3: L24 = ln(C(t)/C(t-24h)) against T = k * sigma_1h(at the 24 h reference) * sqrt(24); sigma is the
    asset's sigma_series."""
    def classify(s: Series, i: int) -> dict:
        t = s.t[i]
        j = s.nearest(t - D)
        if j is None or j >= i or not s.gaps_ok(t - D - H, i):
            return {"status": LOW}
        sig = sigma[j]
        if sig is None:
            return {"status": INSUF}
        L = math.log(float(s.c[i] / s.c[j]))
        T = k * sig * math.sqrt(24)
        direction = "UP" if L > T else "DOWN" if L < -T else "NONE"
        return {"status": VALID, "dir": direction, "value": L, "threshold": T,
                "confirm_mag": abs(L) >= 1.5 * T, "calm": abs(L) < (2.0 / 3.0) * T}
    return classify


# ---------------------------------------------------------------- episode engine (definition "episode")

def run_episodes(s: Series, classify: Callable, period_end: int) -> dict:
    episodes: List[dict] = []
    active: Optional[dict] = None
    hours = []
    for i, t in enumerate(s.t):
        cl = classify(s, i)
        hours.append((t, cl))
        crossing = cl["status"] == VALID and cl["dir"] in ("UP", "DOWN")
        if crossing:
            if active and active["direction"] == cl["dir"]:
                active["crossings"].append(t)
            else:
                if active:  # opposite direction ends the active episode at this hour
                    _resolve(s, active, t, "OPPOSITE_CROSSING")
                    active = None
                last = episodes[-1] if episodes else None
                if (last and last["resolution_ts"] is not None and last["resolution_reason"] != "OPPOSITE_CROSSING"
                        and last["direction"] == cl["dir"] and t - last["crossings"][-1] <= D):
                    last["merged_reopenings"].append(t)  # re-opened, no new alert
                    last["resolution_ts"] = None
                    last["resolution_reason"] = None
                    last["crossings"].append(t)
                    active = last
                else:
                    active = _start(s, i, cl["dir"])
                    episodes.append(active)
        if active:
            # confirmation: persistence (3 hourly closes after first detection) or magnitude, whichever first
            if active["confirmation_ts"] is None:
                t0 = active["first_crossing_ts"]
                same_dir_mag = cl["status"] == VALID and cl.get("confirm_mag") and _sign(cl.get("value")) == active["direction"]
                if same_dir_mag:
                    active["confirmation_ts"], active["confirmation_rule"] = t, "magnitude"
                elif t0 < t <= t0 + 3 * H and not active["_persist_failed"]:
                    # each of the 3 hourly closes t0+1h, t0+2h, t0+3h must exist and be a VALID same-direction crossing
                    active["_persist_seen"] += 1
                    ok = crossing and cl["dir"] == active["direction"] and t == t0 + active["_persist_seen"] * H
                    if not ok:
                        active["_persist_failed"] = True
                    elif t == t0 + 3 * H:
                        active["confirmation_ts"], active["confirmation_rule"] = t, "persistence"
                elif t > t0 + 3 * H and not active["_persist_failed"]:
                    active["_persist_failed"] = True  # an hour was missing: persistence cannot be shown
            # resolution: 6th consecutive VALID calm close; LOW_COVERAGE/INSUFFICIENT_HISTORY reset the run
            if cl["status"] == VALID and cl.get("calm") and not crossing:
                active["_calm_run"] += 1
            else:
                active["_calm_run"] = 0
            if active["_calm_run"] >= 6:
                _resolve(s, active, t, "CALM")
                active = None
    for e in episodes:
        if e["resolution_ts"] is None:
            e["resolution_reason"] = "OPEN"
            e["window_end_ts"] = period_end
            e["magnitude"] = _magnitude(s, e, period_end)
        else:
            e["window_end_ts"] = e["resolution_ts"]
        e.pop("_calm_run", None)
        e.pop("_persist_failed", None)
        e.pop("_persist_seen", None)
    return {"episodes": episodes, "hours": hours}


def _sign(v) -> Optional[str]:
    if v is None:
        return None
    return "UP" if v > 0 else "DOWN" if v < 0 else None


def _start(s: Series, i: int, direction: str) -> dict:
    t0 = s.t[i]
    lo = bisect.bisect_left(s.t, t0 - D)
    best = None
    for k in range(lo, i + 1):  # lowest close (UP) / highest close (DOWN); ties go to the latest hour
        if best is None or (s.c[k] <= s.c[best] if direction == "UP" else s.c[k] >= s.c[best]):
            best = k
    return {"direction": direction, "first_crossing_ts": t0, "first_detection_ts": t0, "crossings": [t0],
            "onset_ts": s.t[best], "onset_candle": [s.open_ts[best], s.close_ts[best]], "onset_close": str(s.c[best]),
            "confirmation_ts": None, "confirmation_rule": None, "resolution_ts": None, "resolution_reason": None,
            "merged_reopenings": [], "magnitude": None, "_calm_run": 0, "_persist_failed": False, "_persist_seen": 0}


def _resolve(s: Series, e: dict, t: int, reason: str):
    e["resolution_ts"], e["resolution_reason"] = t, reason
    e["magnitude"] = _magnitude(s, e, t)


def _magnitude(s: Series, e: dict, end: int) -> Optional[float]:
    lo, hi = bisect.bisect_left(s.t, e["onset_ts"]), bisect.bisect_right(s.t, end)
    closes = s.c[lo:hi]
    if not closes:
        return None
    onset = Decimal(e["onset_close"])
    ext = max(closes) if e["direction"] == "UP" else min(closes)
    return float(ext / onset - 1)


# ---------------------------------------------------------------- B0h: unchanged production LARGE_MOVE

def b0h_alerts(s: Series, scored: tuple) -> List[dict]:
    """Production detect_large_moves, imported unchanged, on clean 3-hourly readings (close of the candle that closes
    at 00/03/.../21 UTC, timestamped at available_at), called every 6 h at 00/06/12/18 UTC with window [run-3d, run].
    An event fingerprint is emitted once; the alert time is the first run that emits it."""
    from event_detector import detect_large_moves  # research/event_detector.py, unchanged
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE btc_data (ts INTEGER, btc_price REAL)")
    conn.executemany("INSERT INTO btc_data VALUES (?, ?)",
                     [(t, float(c)) for t, c in zip(s.t, s.c) if (t // H) % 3 == 0 and t % H == 0])
    seen, alerts = set(), []
    if not s.t:
        return alerts
    first_run = ((max(s.t[0], scored[0] - 3 * D) + 6 * H - 1) // (6 * H)) * (6 * H)
    run = first_run
    while run < scored[1]:
        for ev in detect_large_moves(conn, run - 3 * D, run):
            if ev["fingerprint"] not in seen:
                seen.add(ev["fingerprint"])
                alerts.append({"alert_ts": run, "direction": ev["direction"], "event_ts": ev["event_ts"]})
        run += 6 * H
    return alerts


# ---------------------------------------------------------------- matching and metrics (plan "matching")

def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def match(alerts: List[dict], episodes: List[dict], scored: tuple) -> dict:
    ref = [e for e in episodes if scored[0] <= e["first_crossing_ts"] < scored[1]]
    other = [e for e in episodes if e not in ref]
    hit_by = {}
    rows = []
    for a in sorted(alerts, key=lambda x: x["alert_ts"]):
        if not (scored[0] <= a["alert_ts"] < scored[1]):
            continue
        cands = [e for e in ref if e["direction"] == a["direction"] and e["onset_ts"] <= a["alert_ts"] <= e["window_end_ts"]]
        unhit = [e for e in cands if id(e) not in hit_by]
        if unhit:
            e = min(unhit, key=lambda x: x["onset_ts"])
            hit_by[id(e)] = a
            rows.append({**a, "kind": "HIT", "episode_id": e["episode_id"]})
        elif cands:
            rows.append({**a, "kind": "DUPLICATE", "episode_id": cands[0]["episode_id"]})
        else:
            late = any(e["direction"] == a["direction"] and e["resolution_ts"] is not None
                       and e["resolution_ts"] < a["alert_ts"] <= e["resolution_ts"] + D for e in ref)
            warm = any(e["direction"] == a["direction"] and e["onset_ts"] <= a["alert_ts"] <= e["window_end_ts"] for e in other)
            rows.append({**a, "kind": "FALSE_ALARM", "late": late, "inside_warmup_episode": warm})
    inside = [r for r in rows if any(e["onset_ts"] <= r["alert_ts"] <= e["window_end_ts"] for e in ref)]
    dir_ok = [r for r in inside if any(e["direction"] == r["direction"] and e["onset_ts"] <= r["alert_ts"] <= e["window_end_ts"] for e in ref)]
    return {"ref": ref, "rows": rows, "hit_by": hit_by, "direction_inside": (len(dir_ok), len(inside))}


def _q(vals: List[float], p: float) -> Optional[float]:
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p
    f = math.floor(k)
    return v[f] if f == k else v[f] + (v[f + 1] - v[f]) * (k - f)


def summarise(ref: List[dict], rows: List[dict], hit_by: dict, scored_days: float) -> dict:
    hits = [r for r in rows if r["kind"] == "HIT"]
    fas = [r for r in rows if r["kind"] == "FALSE_ALARM"]
    dups = [r for r in rows if r["kind"] == "DUPLICATE"]
    hit_eps = [e for e in ref if id(e) in hit_by]
    missed = [e for e in ref if id(e) not in hit_by]
    n_ref = len(ref)
    out = {"reference_episodes": n_ref, "hits": len(hits), "misses": len(missed), "false_alarms": len(fas),
           "false_alarms_late": sum(1 for r in fas if r.get("late")), "duplicates": len(dups),
           "false_alarms_inside_warmup_episode": sum(1 for r in fas if r.get("inside_warmup_episode"))}
    if n_ref < 20:
        out["status"] = "INSUFFICIENT_SAMPLE"  # plan small_sample_rule: counts only, no rates emphasised
        return out
    delays = [(hit_by[id(e)]["alert_ts"] - e["onset_ts"]) / H for e in hit_eps]
    from_det = [(hit_by[id(e)]["alert_ts"] - e["first_detection_ts"]) / H for e in hit_eps]
    out.update({
        "status": "OK",
        "precision": round(len(hits) / (len(hits) + len(fas)), 4) if hits or fas else None,
        "precision_ci95": wilson(len(hits), len(hits) + len(fas)),
        "recall": round(len(hits) / n_ref, 4), "recall_ci95": wilson(len(hits), n_ref),
        "false_alarms_per_30_days": round(len(fas) / scored_days * 30, 3),
        "delay_from_onset_h": {"median": _q(delays, 0.5), "p90": _q(delays, 0.9)},
        "delay_from_reference_detection_h": {"median": _q(from_det, 0.5), "p90": _q(from_det, 0.9)},
        "duplicates_per_hit": round(len(dups) / len(hits), 3) if hits else None,
        "peak_magnitude_median": {"hit": _q([abs(e["magnitude"]) for e in hit_eps if e["magnitude"] is not None], 0.5),
                                  "missed": _q([abs(e["magnitude"]) for e in missed if e["magnitude"] is not None], 0.5)},
    })
    return out


# ---------------------------------------------------------------- run

def evaluate(store: Path, period: str, authorize: Optional[str] = None, root: Path = HERE) -> dict:
    definition, plan, def_sha, plan_sha = load_frozen(root)
    split = plan["split"]
    if period == "evaluation":
        if authorize != plan_sha:
            raise SystemExit("evaluation-period metrics are sealed: pass --authorize-evaluation <plan sha256> in an "
                             "explicit, separately authorised run; refusing")
        scored = (iso_ms(split["evaluation_scored_utc"][0]), iso_ms(split["evaluation_scored_utc"][1]))
        cutoff = None
    elif period == "development":
        scored = (iso_ms(split["development_scored_utc"][0]), iso_ms(split["development_scored_utc"][1]))
        cutoff = scored[1]  # nothing at or after 2026-07-22T00:00Z is loaded
    else:
        raise SystemExit(f"unknown period {period}")
    series = load_store(store, cutoff, plan["data_snapshot"]["observations_sha256"])
    scored_days = (scored[1] - scored[0]) / D

    sigmas = {a: sigma_series(series[a]) for a in ASSETS}
    b1 = make_return_classifier(D, Decimal("0.03"), Decimal("0.045"), Decimal("0.02"))
    b2 = make_return_classifier(4 * H, Decimal("0.025"), Decimal("0.0375"), Decimal("0.0167"))

    # volatility-regime cut points: 33rd/67th percentile of sigma_1h over all development scored hours, all assets
    dev = (iso_ms(split["development_scored_utc"][0]), iso_ms(split["development_scored_utc"][1]))
    dev_sig = [sg for a in ASSETS for t, sg in zip(series[a].t, sigmas[a]) if dev[0] <= t < dev[1] and sg is not None]
    cuts = [_q(dev_sig, 0.33), _q(dev_sig, 0.67)] if dev_sig else [None, None]

    report = {"evaluator_version": EVALUATOR_VERSION, "period": period, "plan_sha256": plan_sha, "definition_sha256": def_sha,
              "data_snapshot": plan["data_snapshot"], "scored_utc": [ms_iso(scored[0]), ms_iso(scored[1])],
              "scored_days": scored_days, "loaded_until_available_at_lt": ms_iso(cutoff),
              "volatility_tercile_cuts_sigma_1h": cuts, "assets": {}, "detectors": {}, "notes": []}
    all_ref, per_det_rows = {}, {}
    for a in ASSETS:
        s = series[a]
        dets = {"B1": b1, "B2": b2, "B3_k2.5": make_vol_classifier(2.5, sigmas[a]), "B3_k3.0": make_vol_classifier(3.0, sigmas[a])}
        runs = {name: run_episodes(s, f, scored[1]) for name, f in dets.items()}
        ref_run = runs["B1"]
        for e in ref_run["episodes"]:
            e["episode_id"] = f"B1:{a}:{e['first_crossing_ts'] - 1}:{e['direction']}"
            e["asset"] = a
            i0 = s.idx[e["first_crossing_ts"]]
            sg = sigmas[a][i0]
            e["vol_tercile"] = None if sg is None or cuts[0] is None else ("low" if sg <= cuts[0] else "mid" if sg <= cuts[1] else "high")
            lo, hi = e["onset_ts"] - D, e["window_end_ts"]
            e["low_coverage_in_window"] = any(cl["status"] != VALID for t, cl in ref_run["hours"] if lo <= t <= hi)
        cov = {VALID: 0, LOW: 0, INSUF: 0}
        for t, cl in ref_run["hours"]:
            if scored[0] <= t < scored[1]:
                cov[cl["status"]] += 1
        ref_eps = [e for e in ref_run["episodes"] if scored[0] <= e["first_crossing_ts"] < scored[1]]
        confirmed = [e for e in ref_eps if e["confirmation_ts"] is not None]
        report["assets"][a] = {
            "observations_loaded": len(s.t), "b1_hour_coverage": cov,
            "b3_insufficient_history_hours": sum(1 for t, sg in zip(s.t, sigmas[a]) if scored[0] <= t < scored[1] and sg is None),
            "reference_episodes": len(ref_eps),
            "reference_by_direction": {d: sum(1 for e in ref_eps if e["direction"] == d) for d in ("UP", "DOWN")},
            "reference_open_at_period_end": sum(1 for e in ref_eps if e["resolution_reason"] == "OPEN"),
            "confirmation_share": round(len(confirmed) / len(ref_eps), 4) if ref_eps else None,
            "confirmation_delay_h_median": _q([(e["confirmation_ts"] - e["first_detection_ts"]) / H for e in confirmed], 0.5),
        }
        all_ref[a] = ref_run["episodes"]
        alerts = {name: [{"alert_ts": e["first_detection_ts"], "direction": e["direction"], "asset": a}
                         for e in r["episodes"]] for name, r in runs.items()}
        alerts["B0h"] = [{**x, "asset": a} for x in b0h_alerts(s, scored)]
        for name, al in alerts.items():
            per_det_rows.setdefault(name, {})[a] = (al, runs.get(name, {}).get("episodes"))

    det_names = ["B0h", "B1", "B2", "B3_k2.5", "B3_k3.0"]
    for name in det_names:
        all_rows, all_ref_eps, all_hits = [], [], {}
        by_asset, dir_counts = {}, [0, 0]
        for a in ASSETS:
            al, own = per_det_rows[name][a]
            m = match(al, all_ref[a], scored)
            all_rows += m["rows"]; all_ref_eps += m["ref"]; all_hits.update(m["hit_by"])
            dir_counts[0] += m["direction_inside"][0]; dir_counts[1] += m["direction_inside"][1]
            by_asset[a] = summarise(m["ref"], m["rows"], m["hit_by"], scored_days)
            if own is not None and name != "B1":
                by_asset[a]["own_episode_count"] = sum(1 for e in own if scored[0] <= e["first_crossing_ts"] < scored[1])
        overall = summarise(all_ref_eps, all_rows, all_hits, scored_days)
        overall["direction_accuracy"] = round(dir_counts[0] / dir_counts[1], 4) if dir_counts[1] else None
        overall["direction_accuracy_n"] = dir_counts[1]
        breakdowns = {}
        for d in ("UP", "DOWN"):
            eps = [e for e in all_ref_eps if e["direction"] == d]
            rws = [r for r in all_rows if r["direction"] == d]
            breakdowns[f"direction={d}"] = summarise(eps, rws, {k: v for k, v in all_hits.items() if any(id(e) == k for e in eps)}, scored_days)
        for key, fn in (("vol_tercile", lambda e: e["vol_tercile"]), ("low_coverage_in_window", lambda e: e["low_coverage_in_window"])):
            for val in sorted({str(fn(e)) for e in all_ref_eps}):
                eps = [e for e in all_ref_eps if str(fn(e)) == val]
                ids = {id(e) for e in eps}
                hits = {k: v for k, v in all_hits.items() if k in ids}
                cell = {"reference_episodes": len(eps), "hits": len(hits), "misses": len(eps) - len(hits)}
                if len(eps) >= 20:
                    cell.update({"recall": round(len(hits) / len(eps), 4), "recall_ci95": wilson(len(hits), len(eps))})
                else:
                    cell["status"] = "INSUFFICIENT_SAMPLE"
                breakdowns[f"{key}={val}"] = cell
        report["detectors"][name] = {"overall": overall, "by_asset": by_asset, "breakdowns": breakdowns}
    report["detectors"]["B0"] = {"status": "NOT_RUN", "reason": "B0 needs a read-only extract of production btc_data "
                                 "(plan detector B0 input); it is not part of the candle snapshot and production D1 was "
                                 "not queried. B0h covers the same algorithm on clean candle readings."}
    report["reference_episodes"] = [
        {k: (ms_iso(v) if k.endswith("_ts") and isinstance(v, int) else v) for k, v in e.items() if k not in ("crossings", "merged_reopenings")}
        | {"crossing_count": len(e["crossings"]), "merged_reopenings": len(e["merged_reopenings"])}
        for a in ASSETS for e in all_ref[a] if scored[0] <= e["first_crossing_ts"] < scored[1]]
    report["notes"] = [
        "Development period only: no claim is made from it (plan claims_rule). Evaluation-period metrics remain sealed.",
        "B1 scored against its own reference episodes is an implementation-consistency check, not a performance result.",
        "B2 targets 4-hour moves; its alerts are scored against the 24-hour reference episodes and labelled as such.",
        "Retrospective candle replay: collection latency is 0 by construction; live detection latency is not measured here.",
    ]
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", required=True, type=Path, help="market_moves directory of research-data/market-moves")
    ap.add_argument("--period", choices=["development", "evaluation"], default="development")
    ap.add_argument("--authorize-evaluation", default=None)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    report = evaluate(args.store, args.period, args.authorize_evaluation)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(f"wrote {args.out} ({args.period}); sha256 {hashlib.sha256(args.out.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
