"""EXP-23 BTC 12-hour early warning. RESEARCH ONLY; never read by production.

Implements spec `btc-12h-early-warning` v0.4.0 (research/exp23/, sha256 82315687...1ba4; decisions D1-D10):
  * events: market-moves definition v1.0.0 (canonical sha256 c3ef6583...0b92), unchanged: hourly Hyperliquid BTC
    closes, R24 > +3% UP / < -3% DOWN (strict), episode / onset / resolution rules;
  * labels, outcomes and evaluation observations are POST-SEAL ONLY (available_at > SEAL_END, the plan's
    2026-10-09T21:00Z end treated as inclusive); the first eligible decision time is 2026-10-09T22:00Z;
  * pre-seal and sealed-period prices, OI and funding are allowed as point-in-time LOOKBACK only;
  * warning: issue time t0 in [T_on - 36h, T_on - 12h] (inclusive; T_on = v1.0.0 estimated onset, a proxy for the
    true onset); first crossing T_x is reported as a secondary reference; warning rate q = 5% (2.5% / 10% sensitivity);
  * checkpoint: >= 60 evaluable forward events and >= 25 per direction before any rate, p-value or comparison.

Because no label may come from before the seal, the warning rules are fitted WALK-FORWARD on post-seal labels that
were already final at decision time (label of hour h is final at h + 61 h), and the alert threshold is the causal
(1 - q) quantile of the model's scores on past eligible hours. No data is ever filled or synthesised.

stdlib only (CI installs pytest alone). Usage:
  python3 research/early_warning_12h.py --prices market_moves/hourly/BTC/observations.jsonl \
      --forward-store risk_regime_forward --oi-funding risk_regime_oi/observations.jsonl.gz --out report.json
"""
import argparse
import bisect
import glob
import gzip
import hashlib
import json
import math
import os
from decimal import Decimal

H = 3_600_000
DEV_END = 1_784_678_400_000          # 2026-07-22T00:00:00Z
SEAL_END = 1_791_579_600_000         # 2026-10-09T21:00:00Z, sealed (inclusive)
T1 = SEAL_END + H                    # 2026-10-09T22:00:00Z, first eligible decision time
DEFINITION_SHA256 = "c3ef65830c3bf729354749a94c2038c072fa72f99b5977608f41aa6bd19c0b92"
PLAN_SHA256 = "51d94913b6011a966ffd7b881e3f47509e20dc176b1c6ba4c821dfd084d2e41d"
SPEC_SHA256 = "8231568780906804663e6ba8687cc1a7bb6d867f9fbaed133a0876c5e9851ba4"   # spec v0.4.0

THRESHOLD = Decimal("0.03")          # v1.0.0, strict
RESOLVE_BAND = Decimal("0.02")
RESOLVE_HOURS = 6
REF_TOL = H
MAX_GAP = 2 * H
MERGE_WINDOW = 24 * H
LEAD_MIN, LEAD_MAX = 12 * H, 36 * H  # qualifying window, inclusive (exactly 12 h qualifies)
LABEL_FINAL_LAG = LEAD_MAX + 24 * H + H   # onset <= h+36h is final once crossings to h+60h are known
Q_MAIN, Q_SENS = 0.05, (0.025, 0.10)
CHECKPOINT_TOTAL, CHECKPOINT_PER_DIR = 60, 25
OI_STALE = 15 * 60_000
OI_REF_TOL = 10 * 60_000
FUND_STALE = 8 * H + 15 * 60_000
MIN_TRAIN_EPISODES, MIN_TRAIN_HOURS = 5, 200
F0_SHIFTS = 1000                     # chance reference: deterministic circular shifts
BOOT_N = 2000                        # day-block bootstrap resamples
BOOT_SEED = 23_012                   # fixed seed (EXP-23); reported with every bootstrap
MIN_CELL_EPISODES = 20               # plan small-sample rule: fewer episodes in a cell -> counts only
WARN_TYPES = ("UP", "DOWN", "ANY")
FEATURE_SETS = {
    "F1": ("P",), "F2": ("P", "O"), "F3": ("P", "U"), "F4": ("P", "O", "U"),
}


class SealedDataError(ValueError):
    """A sealed-period (or pre-seal) row reached the post-seal label builder."""


class LookAheadError(AssertionError):
    """A feature used data that was not available at its decision time."""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ----------------------------------------------------------------------------------------------------- loading
def _open(path):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path)


def load_market_moves(path):
    """market-moves hourly candles -> {available_at: (o, h, l, c)} as Decimals."""
    out = {}
    with _open(path) as f:
        for line in f:
            r = json.loads(line)
            if r["available_at"] != r["close_ts"] + 1 or r["close_ts"] != r["open_ts"] + H - 1:
                raise ValueError(f"bad candle timing {r.get('record_id')}")
            out[r["available_at"]] = tuple(Decimal(str(r[k])) for k in ("o", "h", "l", "c"))
    return out


def load_forward_prices(store_dir):
    """Forward store, file hyperliquid_hip3, instrument BTC (the Hyperliquid main perpetual)."""
    return load_forward_price_files(sorted(glob.glob(os.path.join(store_dir, "hyperliquid_hip3", "*", "*.jsonl.gz"))))


def load_forward_price_files(paths):
    """Same as load_forward_prices, for an explicit (already verified) list of partition files."""
    import ast
    out = {}
    for p in sorted(paths):
        for line in _open(p):
            r = json.loads(line)
            if r["instrument"] != "BTC" or r["metric"] != "close":
                continue
            raw = r["raw"] if isinstance(r["raw"], dict) else ast.literal_eval(r["raw"])
            if r["available_at"] != r["timestamp"] + H or raw["t"] != r["timestamp"]:
                raise ValueError(f"bad forward candle timing at {r['timestamp']}")
            out[r["available_at"]] = tuple(Decimal(str(raw[k])) for k in ("o", "h", "l", "c"))
    return out


def merge_prices(*sources):
    """Union by available_at. Disagreeing closes are conflicts: the hour is dropped (never resolved by choice)."""
    merged, conflicts = {}, []
    for src in sources:
        for t, v in src.items():
            if t in merged and merged[t] is not None and merged[t][3] != v[3]:
                conflicts.append(t)
                merged[t] = None
            elif t not in merged:
                merged[t] = v
    return {t: v for t, v in merged.items() if v is not None}, sorted(set(conflicts))


def load_venue_series(paths):
    """Binance OI (5 min) and funding (8 h) rows from frozen and forward files.
    Returns ({ts: (available_at, oi_btc, oi_usd)}, {ts: (available_at, rate)}, conflicts)."""
    oi, fund, conflicts = {}, {}, []
    for p in paths:
        for line in _open(p):
            r = json.loads(line)
            src, metric, ts, av, val = r["source"], r["metric"], r["timestamp"], r["available_at"], r["value"]
            if src == "binance_oi_archive" and metric in ("open_interest", "open_interest_usd"):
                cur = list(oi.get(ts, (av, None, None)))
                i = 1 if metric == "open_interest" else 2
                if cur[i] is not None and cur[i] != val:
                    conflicts.append(("oi", ts))
                cur[0] = max(cur[0], av)
                cur[i] = val
                oi[ts] = tuple(cur)
            elif src == "binance_funding_archive" and metric == "funding_rate":
                if ts in fund and fund[ts][1] != val:
                    conflicts.append(("funding", ts))
                fund[ts] = (av, val)
    for kind, ts in conflicts:            # conflicting snapshots are removed, never chosen
        (oi if kind == "oi" else fund).pop(ts, None)
    oi = {t: v for t, v in oi.items() if v[1] is not None and v[2] is not None}
    return oi, fund, sorted(set(conflicts))


def forward_venue_files(store_dir):
    pats = ("binance_oi_archive", "binance_funding_archive")
    return sorted(p for s in pats for p in glob.glob(os.path.join(store_dir, s, "*", "*.jsonl.gz")))


# ------------------------------------------------------------------------------------------------------ labels
def _nearest(ts_sorted, target, tol):
    i = bisect.bisect_left(ts_sorted, target)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(ts_sorted) and abs(ts_sorted[j] - target) <= tol:
            d = abs(ts_sorted[j] - target)
            if best is None or d < best[0] or (d == best[0] and ts_sorted[j] < best[1]):
                best = (d, ts_sorted[j])
    return None if best is None else best[1]


def _max_gap(ts_sorted, lo, hi):
    i, j = bisect.bisect_left(ts_sorted, lo), bisect.bisect_right(ts_sorted, hi)
    w = ts_sorted[i:j]
    return max((b - a for a, b in zip(w, w[1:])), default=0), len(w)


def r24_valid(closes, ts_sorted, t):
    """v1.0.0 R24 at t (Decimal) or None when LOW_COVERAGE."""
    ref = _nearest(ts_sorted, t - 24 * H, REF_TOL)
    if ref is None:
        return None
    gap, n = _max_gap(ts_sorted, t - 24 * H - REF_TOL, t)
    if gap > MAX_GAP:
        return None
    return closes[t] / closes[ref] - 1


def build_labels(post_seal_closes):
    """Post-seal v1.0.0 episodes with burn-in. Input: {available_at: close}, ALL > SEAL_END (else SealedDataError)."""
    bad = [t for t in post_seal_closes if t <= SEAL_END]
    if bad:
        raise SealedDataError(f"{len(bad)} rows at or before SEAL_END reached the label builder (first {min(bad)})")
    ts = sorted(post_seal_closes)
    c = post_seal_closes
    first_labelable = SEAL_END + 26 * H            # whole window [t-25h, t] post-seal
    episodes, burn_in_crossings, hours = [], [], []
    burn_in, calm_run, calm, active, last_resolved = True, 0, 0, None, None
    burn_in_end = None
    for t in ts:
        if t < first_labelable:
            continue
        R = r24_valid(c, ts, t)
        hours.append((t, None if R is None else str(R)))
        if R is None:
            calm_run = 0
            calm = 0
            continue
        d = "UP" if R > THRESHOLD else "DOWN" if R < -THRESHOLD else None
        if burn_in:
            if d is not None:
                burn_in_crossings.append({"t": t, "direction": d})
                calm_run = 0
            else:
                calm_run = calm_run + 1 if abs(R) < RESOLVE_BAND else 0
                if calm_run >= RESOLVE_HOURS:
                    burn_in, burn_in_end = False, t
            continue
        if active is not None:
            if d == active["direction"]:
                active["last_crossing"], active["n_crossings"] = t, active["n_crossings"] + 1
                calm = 0
                continue
            if d is not None:                          # opposite crossing: end and start new
                active["resolution"], active["resolution_reason"] = t, "OPPOSITE_CROSSING"
                last_resolved = active
                active = _new_episode(c, ts, t, d)
                episodes.append(active)
                calm = 0
                continue
            calm = calm + 1 if abs(R) < RESOLVE_BAND else 0
            if calm >= RESOLVE_HOURS:
                active["resolution"], active["resolution_reason"] = t, "CALM"
                last_resolved, active, calm = active, None, 0
            continue
        if d is not None:
            if (last_resolved is not None and last_resolved["direction"] == d
                    and t - last_resolved["last_crossing"] <= MERGE_WINDOW):
                active = last_resolved
                active["resolution"] = active["resolution_reason"] = None
                active["merged"] += 1
                active["last_crossing"], active["n_crossings"] = t, active["n_crossings"] + 1
                calm = 0
                continue
            active = _new_episode(c, ts, t, d)
            episodes.append(active)
            calm = 0
    for e in episodes:
        if e["resolution"] is None:
            e["resolution_reason"] = "OPEN"
    return {
        "first_labelable": first_labelable,
        "last_labelled": hours[-1][0] if hours else None,
        "labelled_hours": len(hours),
        "valid_hours": sum(1 for _, r in hours if r is not None),
        "burn_in_complete": not burn_in,
        "burn_in_end": burn_in_end,
        "burn_in_crossings": burn_in_crossings,
        "episodes": episodes,
    }


def _new_episode(c, ts, t, d):
    """v1.0.0 onset: lowest (UP) / highest (DOWN) close among observations in [T_x - 24h, T_x]; ties -> latest."""
    i, j = bisect.bisect_left(ts, t - 24 * H), bisect.bisect_right(ts, t)
    window = ts[i:j]
    key = (lambda u: (c[u], -u)) if d == "UP" else (lambda u: (-c[u], -u))
    onset = min(window, key=key)
    return {"episode_id": f"B1:BTC:{t - 1}:{d}", "direction": d, "T_x": t, "T_on": onset,
            "onset_close": str(c[onset]), "last_crossing": t, "n_crossings": 1, "merged": 0,
            "resolution": None, "resolution_reason": None}


# ---------------------------------------------------------------------------------------------------- features
class FeatureBuilder:
    """Point-in-time features at decision time t. Every lookup is restricted to available_at <= t."""

    def __init__(self, prices, oi=None, funding=None):
        self.p = prices                                   # {available_at: (o, h, l, c)}
        self.pt = sorted(prices)
        self.oi = oi or {}
        self.oi_ts = sorted(self.oi)
        self.f = funding or {}
        self.f_ts = sorted(self.f)

    def _check(self, used, t):
        if used and max(used) > t:
            raise LookAheadError(f"feature at {t} used data available at {max(used)}")

    def _ret(self, t, k, used):
        ref = _nearest(self.pt, t - k * H, REF_TOL)
        if ref is None or ref > t:
            return None, None
        gap, _ = _max_gap(self.pt, ref, t)
        if gap > MAX_GAP:
            return None, None
        used += [ref, t]
        return float(self.p[t][3] / self.p[ref][3] - 1), ref

    def _sigma(self, end, hours, min_frac, used):
        lo = end - hours * H
        i, j = bisect.bisect_right(self.pt, lo), bisect.bisect_right(self.pt, end)
        w = self.pt[i - 1 if i > 0 else 0:j]
        rets = [math.log(self.p[b][3] / self.p[a][3]) for a, b in zip(w, w[1:]) if b - a == H and a >= lo]
        if len(rets) < math.ceil(min_frac * hours) or len(rets) < 2:
            return None
        used += [end]
        m = sum(rets) / len(rets)
        return math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))

    def price(self, t, used):
        if t not in self.p:
            return None
        out = {}
        for k in (1, 4, 12, 24):
            out[f"R{k}"], ref = self._ret(t, k, used)
            if k == 24:
                ref24 = ref
        out["sig24"] = self._sigma(t, 24, 0.9, used)
        out["sig168"] = self._sigma(t, 168, 0.9, used)
        out["sig720"] = self._sigma(ref24, 720, 0.9, used) if ref24 is not None else None
        out["sig_ratio"] = (out["sig24"] / out["sig720"]) if out["sig24"] and out["sig720"] else None
        for hours in (24, 168):
            lo = t - hours * H
            w = self.pt[bisect.bisect_right(self.pt, lo):bisect.bisect_right(self.pt, t)]
            sig = out["sig24"] if hours == 24 else out["sig168"]
            if len(w) < math.ceil(0.9 * hours) or not sig:
                out[f"dist_hi{hours}"] = out[f"dist_lo{hours}"] = None
                if hours == 24:
                    out["range24"] = None
                continue
            c = self.p[t][3]
            hi, lo_ = max(self.p[u][1] for u in w), min(self.p[u][2] for u in w)
            out[f"dist_hi{hours}"] = math.log(hi / c) / sig
            out[f"dist_lo{hours}"] = math.log(c / lo_) / sig
            if hours == 24:
                out["range24"] = sum(float((self.p[u][1] - self.p[u][2]) / self.p[u][3]) for u in w) / len(w)
            used += w[-1:]
        return None if any(v is None for v in out.values()) else out

    def _oi_at(self, t, target_ts, tol, used):
        """Snapshot nearest target_ts (+-tol) that was available by t."""
        snap = _nearest(self.oi_ts, target_ts, tol)
        if snap is None or self.oi[snap][0] > t:
            return None
        used.append(self.oi[snap][0])
        return self.oi[snap]

    def oi_features(self, t, price, used):
        if not self.oi_ts:
            return None
        i = bisect.bisect_right(self.oi_ts, t) - 1          # candidates with ts <= t, then availability
        while i >= 0 and self.oi[self.oi_ts[i]][0] > t:
            i -= 1
        if i < 0 or t - self.oi[self.oi_ts[i]][0] > OI_STALE:
            return None
        cur_ts = self.oi_ts[i]
        cur = self.oi[cur_ts]
        used.append(cur[0])
        out = {"oi_btc": cur[1], "oi_usd": cur[2]}
        for k in (1, 4, 12, 24, 72):
            past = self._oi_at(t, cur_ts - k * H, OI_REF_TOL, used)
            out[f"dOI{k}"] = None if past is None else cur[1] / past[1] - 1
        lo = cur_ts - 7 * 24 * H
        j0 = bisect.bisect_right(self.oi_ts, lo)
        win = [self.oi[u][1] for u in self.oi_ts[j0:i + 1] if self.oi[u][0] <= t]
        out["oi_pct7d"] = (sum(1 for v in win if v <= cur[1]) / len(win)) if len(win) >= 0.9 * 7 * 288 else None
        for k in (4, 24):
            out[f"div{k}"] = None if out[f"dOI{k}"] is None or price is None else out[f"dOI{k}"] - price[f"R{k}"]
        return None if any(v is None for v in out.values()) else out

    def funding_features(self, t, used):
        i = bisect.bisect_right(self.f_ts, t) - 1
        while i >= 0 and self.f[self.f_ts[i]][0] > t:
            i -= 1
        if i < 0 or t - self.f[self.f_ts[i]][0] > FUND_STALE:
            return None
        cur = self.f[self.f_ts[i]]
        used.append(cur[0])
        last9 = [self.f[u][1] for u in self.f_ts[max(0, i - 8):i + 1] if self.f_ts[i] - u <= 3 * 24 * H]
        win = [self.f[u][1] for u in self.f_ts[:i + 1] if self.f_ts[i] - u < 30 * 24 * H]
        if len(last9) < 9 or len(win) < 80 or i < 1:
            return None
        return {"fund": cur[1], "dfund": cur[1] - self.f[self.f_ts[i - 1]][1], "fund_mean3d": sum(last9) / 9,
                "fund_pct30d": sum(1 for v in win if v <= cur[1]) / len(win)}

    def build(self, t):
        """Returns {'P': dict|None, 'O': dict|None, 'U': dict|None, 'max_available_at': int}."""
        used = []
        p = self.price(t, used)
        o = self.oi_features(t, p, used)
        u = self.funding_features(t, used)
        self._check(used, t)
        return {"P": p, "O": o, "U": u, "max_available_at": max(used) if used else None}


def vector(row, groups):
    vals = []
    for g in groups:
        if row[g] is None:
            return None
        vals += [row[g][k] for k in sorted(row[g])]
    return vals


# -------------------------------------------------------------------------------------------- warning rules
def targets(episodes, h):
    """1 if an episode of the type has T_on in [h+12h, h+36h]."""
    hit = {"UP": 0, "DOWN": 0, "ANY": 0}
    for e in episodes:
        if h + LEAD_MIN <= e["T_on"] <= h + LEAD_MAX:
            hit[e["direction"]] = 1
            hit["ANY"] = 1
    return hit


try:                                   # optional speed-up; stdlib fallback keeps CI (pytest only) working
    import numpy as _np
except ImportError:                    # pragma: no cover
    _np = None
BACKEND = "numpy" if _np is not None else "stdlib"
_FIT_CACHE = {}


def set_backend(name):
    """Force the fit backend ('stdlib' or 'numpy'); the automated evaluator pins 'stdlib' so that reports are
    byte-identical whether or not numpy is installed."""
    global BACKEND
    if name not in ("stdlib", "numpy") or (name == "numpy" and _np is None):
        raise ValueError(f"backend {name!r} unavailable")
    BACKEND = name
    _FIT_CACHE.clear()


def fit_logistic(X, y, l2=1.0, iters=25):
    """L2 logistic regression by Newton/IRLS on standardised features. Memoised on the exact training data."""
    key = hashlib.sha256(json.dumps([X, y, l2, iters]).encode()).hexdigest()
    if key not in _FIT_CACHE:
        _FIT_CACHE[key] = (_fit_numpy if BACKEND == "numpy" else _fit_stdlib)(X, y, l2, iters)
    return _FIT_CACHE[key]


def _fit_numpy(X, y, l2, iters):
    A = _np.asarray(X, dtype=float)
    mu, sd = A.mean(0), A.std(0)
    sd[sd == 0] = 1.0
    Z = _np.hstack([_np.ones((len(A), 1)), (A - mu) / sd])
    yv, w = _np.asarray(y, dtype=float), _np.zeros(Z.shape[1])
    R = l2 * _np.eye(Z.shape[1])
    R[0, 0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + _np.exp(-_np.clip(Z @ w, -30, 30)))
        g = Z.T @ (p - yv) + R @ w
        Hm = (Z * (p * (1 - p))[:, None]).T @ Z + R + 1e-12 * _np.eye(Z.shape[1])
        step = _np.linalg.solve(Hm, g)
        w = w - step
        if _np.max(_np.abs(step)) < 1e-8:
            break
    return {"w": w.tolist(), "mu": mu.tolist(), "sd": sd.tolist()}


def _fit_stdlib(X, y, l2, iters):
    n, d = len(X), len(X[0])
    mu = [sum(r[j] for r in X) / n for j in range(d)]
    sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / n) or 1.0 for j in range(d)]
    Z = [[1.0] + [(r[j] - mu[j]) / sd[j] for j in range(d)] for r in X]
    w = [0.0] * (d + 1)
    for _ in range(iters):
        g = [0.0] * (d + 1)
        Hm = [[0.0] * (d + 1) for _ in range(d + 1)]
        for z, yi in zip(Z, y):
            s = sum(a * b for a, b in zip(w, z))
            p = 1 / (1 + math.exp(-max(-30, min(30, s))))
            for a in range(d + 1):
                g[a] += (p - yi) * z[a]
                pa = p * (1 - p) * z[a]
                for b in range(a, d + 1):
                    Hm[a][b] += pa * z[b]
        for a in range(d + 1):
            for b in range(a):
                Hm[a][b] = Hm[b][a]
            if a:
                g[a] += l2 * w[a]
                Hm[a][a] += l2
        step = _solve(Hm, g)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return {"w": w, "mu": mu, "sd": sd}


def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            M[c][c] = 1e-12
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def score(model, x):
    z = [1.0] + [(v - m) / s for v, m, s in zip(x, model["mu"], model["sd"])]
    return sum(a * b for a, b in zip(model["w"], z))


def quantile(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def walk_forward_warnings(hours, feats, episodes, groups, wtype, qs, label_ok):
    """Causal warnings for one feature set and warning type, for every warning rate in qs.
    Retrained once per 24 h on hours whose labels were final by t (h + LABEL_FINAL_LAG <= t) and lie in the
    burn-in-complete labelled span (label_ok). The threshold is the (1 - q) quantile of the model's scores on its own
    past training hours, so the warning rate is set without any future information. Returns {q: {t: bool|None}}."""
    out = {q: {} for q in qs}
    model, thresholds, last_fit = None, {}, None
    for t in hours:
        x = vector(feats[t], groups)
        if x is None:
            for q in qs:
                out[q][t] = None                               # NOT_EVALUABLE
            continue
        if last_fit is None or t - last_fit >= 24 * H:
            train = [h for h in hours if h + LABEL_FINAL_LAG <= t and label_ok(h) and vector(feats[h], groups)]
            pos_eps = {e["episode_id"] for e in episodes
                       if e["T_x"] + 24 * H <= t and (wtype == "ANY" or e["direction"] == wtype)}
            if len(train) >= MIN_TRAIN_HOURS and len(pos_eps) >= MIN_TRAIN_EPISODES:
                X = [vector(feats[h], groups) for h in train]
                y = [targets(episodes, h)[wtype] for h in train]
                if 0 < sum(y) < len(y):
                    model = fit_logistic(X, y)
                    s = [score(model, xi) for xi in X]
                    thresholds = {q: quantile(s, 1 - q) for q in qs}
            last_fit = t
        sc = score(model, x) if model is not None else None
        for q in qs:
            out[q][t] = bool(sc is not None and sc > thresholds[q])
    return out


# ------------------------------------------------------------------------------------------------------ scoring
def runs_from(warn, hours):
    """Consecutive warning hours; broken by a non-warning or NOT_EVALUABLE hour or a gap in the decision grid."""
    runs, cur, prev = [], None, None
    for t in hours:
        w = warn.get(t)
        if w and cur is not None and prev is not None and t - prev == H:
            cur["end"] = t
        elif w:
            cur = {"t0": t, "end": t}
            runs.append(cur)
        else:
            cur = None
        prev = t
    return runs


def score_runs(runs, episodes, wtype, ref, period_end, eligible, evaluable):
    """Event-level scoring at reference ref ('T_on' primary, 'T_x' secondary). Returns counts only."""
    compat = [e for e in episodes if wtype == "ANY" or e["direction"] == wtype]
    eval_eps = [e for e in compat if episode_evaluable(e, ref, eligible, evaluable)]
    hit_by, false_t0 = {}, []
    res = {"hits": 0, "duplicates": 0, "inside_move": 0, "false_warnings": 0, "censored_runs": 0}
    for r in runs:
        t0 = r["t0"]
        if t0 > period_end - LEAD_MAX:
            res["censored_runs"] += 1
            continue
        q = sorted((e for e in eval_eps if e[ref] - LEAD_MAX <= t0 <= e[ref] - LEAD_MIN), key=lambda e: e[ref])
        fresh = [e for e in q if e["episode_id"] not in hit_by]
        if fresh:
            hit_by[fresh[0]["episode_id"]] = t0
            res["hits"] += 1
        elif q:
            res["duplicates"] += 1
        elif any(e[ref] - LEAD_MIN < t0 <= (e["resolution"] or period_end) for e in compat):
            res["inside_move"] += 1
        else:
            res["false_warnings"] += 1
            false_t0.append(t0)
    res["evaluable_episodes"] = len(eval_eps)
    res["false_warning_t0"] = false_t0
    res["misses"] = len(eval_eps) - len(hit_by)
    res["hit_episode_ids"] = sorted(hit_by)
    res["hit_lead_hours"] = sorted((e[ref] - hit_by[e["episode_id"]]) / H for e in eval_eps if e["episode_id"] in hit_by)
    return res


def episode_evaluable(e, ref, eligible, evaluable):
    lo, hi = e[ref] - LEAD_MAX, e[ref] - LEAD_MIN
    window = [t for t in range(lo, hi + 1, H)]
    if lo < T1 or any(t not in eligible for t in window):
        return False
    return sum(1 for t in window if evaluable.get(t)) >= 0.9 * len(window)


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p, d = k / n, 1 + z * z / n
    c, hw = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - hw, 4), round(c + hw, 4)]


def mcnemar_exact(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def shifted_warnings(warn, hours, shift):
    """F0 helper: circularly shift the warning flags over the EVALUABLE hours only (NOT_EVALUABLE hours keep None),
    so the chance series has exactly the same number of warning hours and the same run-length structure."""
    ev = [t for t in hours if warn.get(t) is not None]
    if not ev:
        return dict(warn)
    flags = [warn[t] for t in ev]
    s = shift % len(flags)
    rot = flags[-s:] + flags[:-s] if s else flags
    out = {t: None for t in hours if warn.get(t) is None}
    out.update(zip(ev, rot))
    return out


def f0_shifts(n_evaluable, n_shifts=F0_SHIFTS):
    """Deterministic, distinct non-zero shifts (no random state)."""
    if n_evaluable < 2:
        return []
    return sorted({1 + (i * 7919) % (n_evaluable - 1) for i in range(n_shifts)})


def poisson_ci(k, alpha=0.05):
    """Exact (Garwood) two-sided interval for a Poisson count k, by bisection on the Poisson CDF (stdlib)."""
    def cdf(x, lam):
        if x < 0:
            return 0.0
        term = total = math.exp(-lam)
        for i in range(1, x + 1):
            term *= lam / i
            total += term
        return total

    def solve(f, lo, hi):
        for _ in range(200):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) else (lo, mid)
        return (lo + hi) / 2
    hi_bound = max(10.0, 10.0 * (k + 1))
    lower = 0.0 if k == 0 else solve(lambda lam: 1 - cdf(k - 1, lam) < alpha / 2, 0.0, hi_bound)
    upper = solve(lambda lam: cdf(k, lam) > alpha / 2, 0.0, hi_bound)
    return [lower, upper]


def day_block_bootstrap_diff(times_a, times_b, scored_hours, seed, n_boot=BOOT_N):
    """Difference in false warnings per 30 days (a - b) with a UTC-day block bootstrap.
    Independent unit = the UTC day of the scored hours (warnings inside one day are correlated); days are resampled
    with replacement using random.Random(seed), so the result is deterministic."""
    import random
    days = sorted({t // (24 * H) for t in scored_hours})
    if not days:
        return None
    ca, cb = {}, {}
    for t in times_a:
        ca[t // (24 * H)] = ca.get(t // (24 * H), 0) + 1
    for t in times_b:
        cb[t // (24 * H)] = cb.get(t // (24 * H), 0) + 1
    diffs = [ca.get(d, 0) - cb.get(d, 0) for d in days]
    point = sum(diffs) / len(days) * 30
    rnd = random.Random(seed)
    stats = sorted(sum(diffs[rnd.randrange(len(days))] for _ in days) / len(days) * 30 for _ in range(n_boot))
    return {"point": point, "ci95": [stats[int(0.025 * n_boot)], stats[int(0.975 * n_boot) - 1]],
            "unit": "UTC day", "n_units": len(days), "n_boot": n_boot, "seed": seed}


def holm(pvals):
    """Holm step-down adjustment; returns adjusted p-values in the input order."""
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    adj, running = [0.0] * len(pvals), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvals) - rank) * pvals[i]))
        adj[i] = running
    return adj


def cell_statistics(prim, f0_null, scored, met):
    """Intervals and the chance comparison for one cell -- only at the checkpoint and with >= 20 episodes."""
    n_ep = prim["evaluable_episodes"]
    if not met:
        return {"status": "NOT_COMPUTED", "reason": "below the D5 checkpoint (counts only)"}
    if n_ep < MIN_CELL_EPISODES:
        return {"status": "NOT_COMPUTED", "reason": f"cell has {n_ep} < {MIN_CELL_EPISODES} episodes"}
    days = len({u // (24 * H) for u in scored})
    denom = prim["hits"] + prim["false_warnings"] + prim["inside_move"]
    lo, hi = poisson_ci(prim["false_warnings"])
    return {"status": "COMPUTED",
            "recall": {"value": prim["hits"] / n_ep, "wilson95": wilson(prim["hits"], n_ep), "unit": "episode"},
            "precision": {"value": prim["hits"] / denom if denom else None,
                          "wilson95": wilson(prim["hits"], denom), "unit": "warning run"},
            "false_warnings_per_30d": {"value": prim["false_warnings"] / days * 30,
                                       "poisson95": [lo / days * 30, hi / days * 30], "scored_days": days},
            "vs_F0": {"p_one_sided": (1 + sum(1 for x in f0_null if x >= prim["hits"])) / (1 + len(f0_null)),
                      "n_shifts": len(f0_null)}}


def checkpoint(episodes_eval):
    up = sum(1 for e in episodes_eval if e["direction"] == "UP")
    down = len(episodes_eval) - up
    return {"evaluable_events": len(episodes_eval), "up": up, "down": down,
            "met": len(episodes_eval) >= CHECKPOINT_TOTAL and up >= CHECKPOINT_PER_DIR and down >= CHECKPOINT_PER_DIR}


# ----------------------------------------------------------------------------------------------------- pipeline
def run(prices, oi, funding, data_end=None, conflicts=None):
    """Full pipeline on loaded data. Deterministic; returns the report dict."""
    post = {t: v[3] for t, v in prices.items() if t > SEAL_END}            # labels: post-seal only
    labels = build_labels(post)
    episodes = labels["episodes"]
    fb = FeatureBuilder(prices, oi, funding)
    hours = sorted(t for t in prices if t >= T1)                           # eligible decision grid
    feats = {}
    for t in hours:
        row = fb.build(t)
        if row["max_available_at"] is not None and row["max_available_at"] > t:
            raise LookAheadError(t)
        feats[t] = row
    eligible = set(hours)
    period_end = data_end or (max(hours) if hours else T1)
    burn_end = labels["burn_in_end"]

    def label_ok(h):
        return (burn_end is not None and h + LEAD_MIN >= burn_end and labels["last_labelled"] is not None
                and h + LABEL_FINAL_LAG <= labels["last_labelled"])

    coverage = {}
    for name, groups in FEATURE_SETS.items():
        ok = [t for t in hours if vector(feats[t], groups) is not None]
        coverage[name] = {"evaluable_hours": len(ok), "first_evaluable": ok[0] if ok else None,
                          "last_evaluable": ok[-1] if ok else None}
    common = [t for t in hours if all(vector(feats[t], g) is not None for g in FEATURE_SETS.values())]
    blocks = {"P": [t for t in hours if feats[t]["P"] is None], "O": [t for t in hours if feats[t]["O"] is None],
              "U": [t for t in hours if feats[t]["U"] is None]}

    def evaluate(groups, scored, met):
        """Warnings, event-level counts and the F0 chance reference for one feature set on fixed scored hours."""
        sset, evaluable = set(scored), {u: True for u in scored}
        out, internal = {}, {}
        for wtype in WARN_TYPES:
            per_q = {}
            warns = walk_forward_warnings(scored, feats, episodes, groups, wtype, (Q_MAIN,) + Q_SENS, label_ok)
            for q, warn in warns.items():
                runs = runs_from(warn, scored)
                prim = score_runs(runs, episodes, wtype, "T_on", period_end, sset, evaluable)
                sec = score_runs(runs, episodes, wtype, "T_x", period_end, sset, evaluable)
                n_ev = sum(1 for u in scored if warn.get(u) is not None)
                null = [score_runs(runs_from(shifted_warnings(warn, scored, s), scored), episodes, wtype, "T_on",
                                   period_end, sset, evaluable)["hits"] for s in f0_shifts(n_ev)]
                internal[(wtype, q)] = {"hits": set(prim.pop("hit_episode_ids")),
                                        "false_t0": prim.pop("false_warning_t0")}
                sec.pop("hit_episode_ids")
                sec.pop("false_warning_t0")
                if not met:
                    prim.pop("hit_lead_hours")
                    sec.pop("hit_lead_hours")
                ns = sorted(null)
                per_q[str(q)] = {
                    "warning_hours": sum(1 for v in warn.values() if v), "runs": len(runs),
                    "primary_T_on": prim, "secondary_T_x": sec,
                    "F0_chance_reference": {
                        "type": "chance reference, NOT a predictive model",
                        "generation": "the set's own warning flags circularly shifted over its evaluable scored hours "
                                      "(same warning-hour count and run lengths); deterministic shifts "
                                      "1 + (i*7919) mod (n-1)",
                        "evaluation": "identical scorer, episodes, T_on window [T_on-36h, T_on-12h] and censoring",
                        "n_shifts": len(null),
                        "null_hits_median": ns[len(ns) // 2] if ns else None,
                        "null_hits_p95": ns[min(len(ns) - 1, math.ceil(0.95 * len(ns)) - 1)] if ns else None},
                    "statistics": cell_statistics(prim, null, scored, met)}
            out[wtype] = per_q
        return out, internal

    # Each comparison Fk vs F1 is scored on the hours where Fk is evaluable (F1 is then evaluable too), so a set
    # starts as soon as its own data genuinely exists; the all-sets intersection is reported as coverage only.
    comparisons, pending_tests = {}, []
    for name in ("F2", "F3", "F4"):
        scored = [t for t in hours if vector(feats[t], FEATURE_SETS[name]) is not None]
        eps_eval = [e for e in episodes if episode_evaluable(e, "T_on", set(scored), {u: True for u in scored})]
        cp_k = checkpoint(eps_eval)
        res_k, int_k = evaluate(FEATURE_SETS[name], scored, cp_k["met"])
        res_1, int_1 = evaluate(FEATURE_SETS["F1"], scored, cp_k["met"])
        entry = {"scored_hours": len(scored), "checkpoint": cp_k, name: res_k, "F1": res_1}
        if cp_k["met"]:
            inc = {}
            for (w, q) in int_k:
                b = len(int_k[(w, q)]["hits"] - int_1[(w, q)]["hits"])
                c = len(int_1[(w, q)]["hits"] - int_k[(w, q)]["hits"])
                inc[f"{w}@{q}"] = {"only_" + name: b, "only_F1": c, "mcnemar_p": mcnemar_exact(b, c),
                                   "delta_false_warnings_per_30d": day_block_bootstrap_diff(
                                       int_k[(w, q)]["false_t0"], int_1[(w, q)]["false_t0"], scored, BOOT_SEED)}
                pending_tests.append((name, f"{w}@{q}", inc[f"{w}@{q}"]))
            entry["incremental_vs_F1"] = inc
        else:
            entry["incremental_vs_F1"] = {"status": "NOT_COMPUTED", "reason": "below the D5 checkpoint"}
        comparisons[f"{name}_vs_F1"] = entry
    # Holm over the comparisons (F2/F3/F4 vs F1) that reached the checkpoint, per warning type and rate
    for key in sorted({k for _, k, _ in pending_tests}):
        group = [r for _, k, r in pending_tests if k == key]
        for r, a in zip(group, holm([r["mcnemar_p"] for r in group])):
            r["mcnemar_p_holm"] = a
    f1_hours = [t for t in hours if vector(feats[t], FEATURE_SETS["F1"]) is not None]
    eval_eps = [e for e in episodes if episode_evaluable(e, "T_on", set(f1_hours), {u: True for u in f1_hours})]
    cp = checkpoint(eval_eps)
    f1_res, _ = evaluate(FEATURE_SETS["F1"], f1_hours, cp["met"])
    status = "CHECKPOINT_MET" if cp["met"] else "INSUFFICIENT_SAMPLE"
    results = {"F1_own_hours": f1_res, "comparisons": comparisons}
    blockers = []
    for name in ("F2", "F3", "F4"):
        if comparisons[f"{name}_vs_F1"]["scored_hours"] == 0:
            blockers.append(f"{name}: no eligible decision hour has all {name} features; {name} vs F1 not evaluable yet")
    if blocks["U"] and funding:
        last_fund = max(v[0] for v in funding.values())
        blockers.append(f"funding: last settlement available_at {last_fund}; stale (> 8h15m) at "
                        f"{len(blocks['U'])} of {len(hours)} eligible hours (Binance monthly archive not yet published)")
    return {
        "artifact": "exp23-btc-12h-early-warning-report", "fit_backend": BACKEND,
        "spec_sha256": SPEC_SHA256, "definition_sha256": DEFINITION_SHA256, "plan_sha256": PLAN_SHA256,
        "boundary": {"DEV_END": DEV_END, "SEAL_END_inclusive": SEAL_END, "first_decision_T1": T1},
        "parameters": {"lead_window_hours": [LEAD_MIN // H, LEAD_MAX // H], "q": Q_MAIN, "q_sensitivity": list(Q_SENS),
                       "checkpoint": {"total": CHECKPOINT_TOTAL, "per_direction": CHECKPOINT_PER_DIR},
                       "oi_staleness_ms": OI_STALE, "funding_staleness_ms": FUND_STALE},
        "data": {"price_hours_total": len(prices), "price_hours_post_seal": len(post),
                 "oi_snapshots": len(oi), "funding_settlements": len(funding),
                 "last_price_available_at": max(prices) if prices else None,
                 "last_oi_available_at": max((v[0] for v in oi.values()), default=None),
                 "last_funding_available_at": max((v[0] for v in funding.values()), default=None),
                 "conflicts_dropped": conflicts or []},
        "labels": {k: v for k, v in labels.items() if k != "episodes"} | {
            "episodes": len(episodes), "episodes_up": sum(1 for e in episodes if e["direction"] == "UP"),
            "episodes_down": sum(1 for e in episodes if e["direction"] == "DOWN"),
            "burn_in_crossings": len(labels["burn_in_crossings"])},
        "decision_hours": {"eligible": len(hours), "common_F1_F4": len(common), "coverage": coverage,
                           "not_evaluable_by_group": {g: len(v) for g, v in blocks.items()}},
        "checkpoint": cp, "status": status,
        "metrics": None if not cp["met"] else "see results_counts[...].statistics and incremental_vs_F1",
        "statistics_method": {
            "recall": "Wilson 95%, unit = evaluable episode (episodes are separated by v1.0.0 resolution)",
            "precision": "Wilson 95%, unit = warning run",
            "false_warnings_per_30d": "exact Poisson (Garwood) 95% on the count, scaled by scored UTC days",
            "delta_false_warnings_vs_F1": f"UTC-day block bootstrap, {BOOT_N} resamples, seed {BOOT_SEED}",
            "incremental_hits_vs_F1": "exact McNemar on discordant episodes, Holm over F2/F3/F4",
            "vs_F0": f"one-sided empirical p over {F0_SHIFTS} deterministic circular shifts",
            "gating": f"nothing computed below the checkpoint ({CHECKPOINT_TOTAL} events, {CHECKPOINT_PER_DIR}/direction)"
                      f" or in cells with < {MIN_CELL_EPISODES} episodes"},
        "results_counts": results,
        "blockers": blockers,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prices", required=True, help="market-moves BTC observations.jsonl")
    ap.add_argument("--forward-store", required=True, help="risk_regime_forward directory")
    ap.add_argument("--oi-funding", nargs="*", default=[], help="frozen risk_regime_oi observations.jsonl.gz")
    ap.add_argument("--definition", help="optional definition.json to verify the pinned hash")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.definition:
        with open(a.definition) as f:
            got = canonical_sha256(json.load(f))
        if got != DEFINITION_SHA256:
            raise SystemExit(f"definition hash mismatch: {got}")
    prices, conflicts = merge_prices(load_market_moves(a.prices), load_forward_prices(a.forward_store))
    venue_files = list(a.oi_funding) + forward_venue_files(a.forward_store)
    oi, funding, vconf = load_venue_series(venue_files)
    report = run(prices, oi, funding, conflicts=[["price", t] for t in conflicts] + [list(c) for c in vconf])
    files = [a.prices] + venue_files + sorted(glob.glob(os.path.join(a.forward_store, "hyperliquid_hip3", "*", "*.jsonl.gz")))
    root = os.path.abspath(a.forward_store)
    report["inputs"] = {("forward/" + os.path.relpath(os.path.abspath(p), root)) if os.path.abspath(p).startswith(root + os.sep)
                        else os.path.basename(p): sha256_file(p) for p in sorted(set(files))}
    report["code_sha256"] = sha256_file(__file__)
    with open(a.out, "w") as f:
        json.dump(report, f, indent=1, sort_keys=True)
        f.write("\n")
    print(json.dumps({"status": report["status"], "checkpoint": report["checkpoint"],
                      "coverage": report["decision_hours"]["coverage"], "blockers": report["blockers"]}, indent=1))


if __name__ == "__main__":
    main()
