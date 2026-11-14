"""Chronological prediction evaluation harness, protocol PEV-1 (research/plans/2026-11-01-prediction-eval-protocol.md).

Offline and read-only: reads candle JSONL files and a sentiment archive JSON, and writes one JSON report. It never
touches D1 or production. Pure Python (no numpy) so it runs in the research CI job as is.

  python3 research/prediction_eval.py fetch --out DIR                 # post-seal Hyperliquid candles (public API)
  python3 research/prediction_eval.py run --candles DIR [DIR ...] --sentiment FILE --out report.json

Models: A (legacy pipeline, flagged defective), B (corrected, no sentiment), C (corrected, with sentiment), Phase 4
candidates P4-LR and P4-KNN-SHRINK, baselines always_up / always_down / climatology / persistence.
"""
import argparse
import bisect
import hashlib
import json
import math
import random
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pev1p_window  # noqa: E402  PEV-1P no-peeking boundary: no window outcome before 2027-05-01T00:00Z

PROTOCOL = "PEV-1"
H = 3600000
STEP_MS = 3 * H
SEAL_START_TS = 1784678400000          # 2026-07-22T00:00:00Z
SEAL_END_TS = 1791579600000            # 2026-10-09T21:00:00Z
DEV_END_TS = 1781481600000             # 2026-06-15T00:00:00Z
CONTEXT_MAX_AGE_MS = int(24 * H * 0.2)  # 4.8 h, production TOL_MS
TECH_WINDOW = 30
MA_SHORT, MA_LONG = 8, 21
HORIZONS_H = (12, 24)
COINS = ("BTC", "ETH", "LINK")
LARGE_MOVE = 0.03
MIN_RESOLVED = 5
MIN_UNITS_FOR_LABEL = 20
BOOT_N, BOOT_SEED = 2000, 20261101
LR_LAMBDAS = (0.1, 1.0, 10.0)
SHRINK_MS = (0, 10, 30)
SHRINK_K = 50


# ---------------------------------------------------------------- seal rules
def price_usable(available_at):
    """A close is usable iff it became available at or before the seal start or strictly after the seal end."""
    return available_at <= SEAL_START_TS or available_at > SEAL_END_TS


def reading_usable(ts):
    return ts < SEAL_START_TS or ts > SEAL_END_TS


# ---------------------------------------------------------------- features (ported from worker.js)
def simple_technical_score(prices):
    """Exact port of computeSimpleTechnicalScore (worker.js), including Math.round on .5 (rounds half up)."""
    if len(prices) < 6:
        return 50
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]
    avg_gain = sum(gains) / len(changes) if gains else 0
    avg_loss = sum(losses) / len(changes) if losses else 0
    if avg_gain + avg_loss == 0:
        return 50
    if avg_loss == 0:
        return 100
    rsi = 100 - 100 / (1 + avg_gain / avg_loss)
    return math.floor(rsi + 0.5)


def regime_mag(closes):
    if len(closes) < MA_LONG:
        return None
    recent = closes[-MA_LONG:]
    long_ma = sum(recent) / MA_LONG
    short_ma = sum(recent[-MA_SHORT:]) / MA_SHORT
    return (short_ma - long_ma) / long_ma * 100 if long_ma else None


# ---------------------------------------------------------------- loading
def load_candles(paths, coin):
    """{availability_ts: close} for one coin, sealed closes dropped. Later files do not override earlier ones."""
    closes = {}
    for d in paths:
        f = Path(d) / coin / "observations.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            avail = int(c["close_ts"]) + 1
            if not price_usable(avail):
                continue                    # sealed: never parsed further
            closes.setdefault(avail, pev1p_window.Price(float(c["c"]), avail))   # keeps its availability time
    return closes


def load_sentiment(path):
    data = json.loads(Path(path).read_text())
    rows = data["series"] if isinstance(data, dict) else data
    out = sorted((int(r["observation_ts"]), float(r["score"])) for r in rows
                 if r.get("score") is not None and reading_usable(int(r["observation_ts"])))
    return out


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- rows
def build_rows(closes, horizons=HORIZONS_H):
    """One row per 3-hourly grid time with a close. Features use closes at or before t only."""
    if not closes:
        return []
    pev1p_window.check_prices(closes)       # no shifted or re-filed close, for labels, prev_ret and features alike
    start = min(closes) // STEP_MS * STEP_MS
    end = max(closes)
    rows, hist = [], []
    t = start
    while t <= end:
        p = closes.get(t)
        if p is None:
            hist = []                         # a gap (or the seal) breaks every trailing window
            t += STEP_MS
            continue
        hist.append(p)
        row = {"ts": t, "price": p, "technical_score": None, "regime_mag": None, "labels": {}, "prev_ret": {}}
        if len(hist) >= TECH_WINDOW:
            row["technical_score"] = simple_technical_score(hist[-TECH_WINDOW:])
            row["regime_mag"] = regime_mag(hist)
        for h in horizons:
            fut = closes.get(t + h * H)
            if fut is not None:
                pev1p_window.check_label(t, p, fut, t + h * H)
            row["labels"][h] = None if fut is None else pev1p_window.Label(fut / p - 1, t)
            past = closes.get(t - h * H)
            row["prev_ret"][h] = None if past is None else p / past - 1
        rows.append(row)
        t += STEP_MS
    return rows


def nearest_reading(readings_ts, readings, ts, avail_until, tol=CONTEXT_MAX_AGE_MS):
    """Legacy fp-nearest-v0: nearest reading within +-tol, among readings that existed at avail_until."""
    hi = bisect.bisect_right(readings_ts, avail_until)
    i = bisect.bisect_left(readings_ts, ts, 0, hi)
    best, best_d = None, None
    for j in (i - 1, i):
        if 0 <= j < hi:
            d = abs(readings_ts[j] - ts)
            if d <= tol and (best_d is None or d < best_d):
                best, best_d = readings[j][1], d
    return best


def asof_reading(readings_ts, readings, ts, max_age=CONTEXT_MAX_AGE_MS):
    """fp-asof-v1: latest reading at or before ts, no older than max_age."""
    j = bisect.bisect_right(readings_ts, ts) - 1
    if j < 0 or ts - readings_ts[j] > max_age:
        return None
    return readings[j][1]


# ---------------------------------------------------------------- models
def mean_std(vals):
    n = len(vals)
    if n == 0:
        return 0.0, 1.0
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / n)
    return m, (sd or 1.0)


def knn_p_up(query, cands, keys, stats, k_override=None):
    """Production k-NN rule. cands: list of (feature_dict, up_bool). Returns (p_up, n_resolved) or (None, n)."""
    if not cands:
        return None, 0
    k = k_override or min(15, max(5, len(cands) // 3))
    def z(row, key):
        m, s = stats[key]
        return (row[key] - m) / s
    qz = [z(query, key) for key in keys]
    scored = sorted(((math.sqrt(sum((qz[i] - z(c, key)) ** 2 for i, key in enumerate(keys))), up) for c, up in cands),
                    key=lambda x: x[0])[:k]
    if len(scored) < MIN_RESOLVED:
        return None, len(scored)
    return sum(1 for _, up in scored if up) / len(scored), len(scored)


def _has_features(r):
    return r["technical_score"] is not None and r["regime_mag"] is not None


def _candidates(rows_upto, t, h):
    """Rows resolved early enough to be analogs at t (production: ts <= t - (h + 0.2h))."""
    cutoff = t - int(h * H * 1.2)
    cands = [r for r in rows_upto if r["ts"] <= cutoff and r["labels"].get(h) is not None
             and r["technical_score"] is not None and r["regime_mag"] is not None]
    pev1p_window.check_rows(cands)          # every model reads labels only through here: no window or re-filed label
    return cands


def model_a(rows_upto, query, h, sent_ts, sent):
    """Legacy: symmetric join (among readings existing at t), imputation/scaling over all rows INCLUDING the query."""
    pev1p_window.check_query(query)
    if not _has_features(query):
        return None                         # warm-up or gap: the model abstains (counted)
    t = query["ts"]
    feat = [r for r in rows_upto if r["technical_score"] is not None and r["regime_mag"] is not None]
    ctx = {r["ts"]: nearest_reading(sent_ts, sent, r["ts"], t) for r in feat}
    real = [v for v in ctx.values() if v is not None]
    mean_s = sum(real) / len(real) if real else 50.0
    full = {r["ts"]: dict(technical_score=r["technical_score"], regime_mag=r["regime_mag"],
                          sentiment=ctx[r["ts"]] if ctx[r["ts"]] is not None else mean_s) for r in feat}
    keys = ("technical_score", "regime_mag", "sentiment")
    stats = {k: mean_std([v[k] for v in full.values()]) for k in keys}
    cands = [(full[r["ts"]], r["labels"][h] > 0) for r in _candidates(feat, t, h)]
    return knn_p_up(full[t], cands, keys, stats)[0]


def model_corrected(rows_upto, query, h, sent_ts=None, sent=None, k_override=None):
    """B (sent_ts None) or C: as-of join, imputation and scaling fitted on rows before the query."""
    pev1p_window.check_query(query)
    if not _has_features(query):
        return None                         # warm-up or gap: the model abstains (counted)
    t = query["ts"]
    feat = [r for r in rows_upto if r["technical_score"] is not None and r["regime_mag"] is not None]
    train = [r for r in feat if r["ts"] < t]
    keys = ("technical_score", "regime_mag")
    full = {r["ts"]: dict(technical_score=r["technical_score"], regime_mag=r["regime_mag"]) for r in feat}
    if sent_ts is not None:
        keys = keys + ("sentiment",)
        ctx = {r["ts"]: asof_reading(sent_ts, sent, r["ts"]) for r in feat}
        real = [ctx[r["ts"]] for r in train if ctx[r["ts"]] is not None]
        mean_s = sum(real) / len(real) if real else 50.0
        for ts_, v in full.items():
            v["sentiment"] = ctx[ts_] if ctx[ts_] is not None else mean_s
    stats = {k: mean_std([full[r["ts"]][k] for r in train]) for k in keys}
    cands = [(full[r["ts"]], r["labels"][h] > 0) for r in _candidates(feat, t, h)]
    return knn_p_up(full[t], cands, keys, stats, k_override)[0]


def fit_logistic(xs, ys, lam, iters=60):
    """L2 logistic regression (intercept unpenalised) by Newton's method on standardised inputs. Pure Python."""
    d = len(xs[0]) + 1
    w = [0.0] * d
    for _ in range(iters):
        g = [0.0] * d
        hm = [[0.0] * d for _ in range(d)]
        for x, y in zip(xs, ys):
            xa = (1.0,) + tuple(x)
            s = sum(wi * xi for wi, xi in zip(w, xa))
            p = 1 / (1 + math.exp(-max(-30, min(30, s))))
            for i in range(d):
                g[i] += (p - y) * xa[i]
                for j in range(d):
                    hm[i][j] += p * (1 - p) * xa[i] * xa[j]
        for i in range(1, d):
            g[i] += lam * w[i]
            hm[i][i] += lam
        hm[0][0] += 1e-9
        step = _solve(hm, g)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    return w


def _solve(a, b):
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-15:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-15 else 0.0 for i in range(n)]


def model_lr(rows_upto, query, h, lam):
    pev1p_window.check_query(query)
    if not _has_features(query):
        return None                         # warm-up or gap: the model abstains (counted)
    t = query["ts"]
    keys = ("technical_score", "regime_mag")
    feat = [r for r in rows_upto if r["technical_score"] is not None and r["regime_mag"] is not None]
    train = _candidates(feat, t, h)
    if len(train) < 30:
        return None
    stats = {k: mean_std([r[k] for r in train]) for k in keys}
    zs = lambda r: [(r[k] - stats[k][0]) / stats[k][1] for k in keys]
    w = fit_logistic([zs(r) for r in train], [1 if r["labels"][h] > 0 else 0 for r in train], lam)
    s = w[0] + sum(wi * xi for wi, xi in zip(w[1:], zs(query)))
    return 1 / (1 + math.exp(-max(-30, min(30, s))))


def model_shrink(rows_upto, query, h, m):
    pev1p_window.check_query(query)
    p = model_corrected(rows_upto, query, h, k_override=SHRINK_K)
    if p is None:
        return None
    base = climatology(rows_upto, query, h)
    return (SHRINK_K * p + m * base) / (SHRINK_K + m)


def climatology(rows_upto, query, h):
    pev1p_window.check_query(query)
    train = _candidates(rows_upto, query["ts"], h)
    if not train:
        return None
    return sum(1 for r in train if r["labels"][h] > 0) / len(train)


def persistence(query, h):
    pev1p_window.check_query(query)
    r = query["prev_ret"].get(h)
    return None if r is None else (1.0 if r > 0 else 0.0)


# ---------------------------------------------------------------- metrics
def balanced_accuracy(ps, ys):
    up = [p >= 0.5 for p, y in zip(ps, ys) if y]
    dn = [p < 0.5 for p, y in zip(ps, ys) if not y]
    r_up = sum(up) / len(up) if up else None
    r_dn = sum(dn) / len(dn) if dn else None
    ba = None if r_up is None or r_dn is None else (r_up + r_dn) / 2
    return ba, r_up, r_dn


def brier(ps, ys):
    return sum((p - (1 if y else 0)) ** 2 for p, y in zip(ps, ys)) / len(ps) if ps else None


def calibration(ps, ys, bins=5):
    table, ece = [], 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(ps) if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if not idx:
            continue
        mp = sum(ps[i] for i in idx) / len(idx)
        fr = sum(1 for i in idx if ys[i]) / len(idx)
        table.append({"bin": [lo, hi], "n": len(idx), "mean_p": round(mp, 4), "up_rate": round(fr, 4)})
        ece += len(idx) / len(ps) * abs(mp - fr)
    return {"ece": round(ece, 4) if ps else None, "table": table}


def auc(scores, labels):
    """Mann-Whitney AUC with average ranks for ties; None when one class is empty."""
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return None
    allv = sorted(scores)
    def rank(v):
        lo, hi = bisect.bisect_left(allv, v), bisect.bisect_right(allv, v)
        return (lo + hi + 1) / 2
    return (sum(rank(s) for s in pos) - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def bootstrap_ci(n, stat, seed=BOOT_SEED, n_boot=BOOT_N):
    """Percentile 95% CI of stat(indices) over resampled unit indices; None values are skipped."""
    rng = random.Random(seed)
    vals = []
    for _ in range(n_boot):
        v = stat([rng.randrange(n) for _ in range(n)])
        if v is not None:
            vals.append(v)
    if len(vals) < n_boot * 0.5:
        return None
    vals.sort()
    return [round(vals[int(0.025 * len(vals))], 4), round(vals[min(len(vals) - 1, int(0.975 * len(vals)))], 4)]


# ---------------------------------------------------------------- evaluation
def unit_times(rows, h, lo, hi):
    """Non-overlapping inferential units: 00:00 UTC (24 h) or 00:00/12:00 UTC (12 h) within [lo, hi)."""
    period = h * H
    return [r for r in rows if lo <= r["ts"] < hi and r["ts"] % period == 0]


def predict_all(rows, units, h, models):
    """{model: [p or None per unit]}. rows_upto contains only rows at or before the unit (no future rows)."""
    pev1p_window.check_rows(rows)
    pev1p_window.check_rows(units)
    ts_index = [r["ts"] for r in rows]
    out = {name: [] for name in models}
    before = pev1p_window.before_checkpoint()
    for u in units:
        if before and pev1p_window.is_window_unit(u):
            for name in models:                 # PEV-1P window: no prediction before the checkpoint. Such units are
                out[name].append(None)          # unresolved then (build_rows refuses their labels), so every metric
            continue                            # already excludes them as "unresolved"
        upto = rows[:bisect.bisect_right(ts_index, u["ts"])]
        for name, fn in models.items():
            out[name].append(fn(upto, u, h))
    return out


def score_block(names, preds, units, h, compare_to=None):
    """Metrics on the common eligible set: units where every model in `names` predicted and the label resolved."""
    pev1p_window.check_rows(units)
    excluded = {"unresolved": 0, "model_abstained": 0}
    keep = []
    for i, u in enumerate(units):
        if u["labels"][h] is None:
            excluded["unresolved"] += 1
        elif any(preds[n][i] is None for n in names):
            excluded["model_abstained"] += 1
        else:
            keep.append(i)
    ys = [units[i]["labels"][h] > 0 for i in keep]
    res = {"n_units": len(keep), "excluded": excluded, "up_share": round(sum(ys) / len(ys), 4) if ys else None,
           "models": {}}
    for n in names:
        ps = [preds[n][i] for i in keep]
        ba, r_up, r_dn = balanced_accuracy(ps, ys) if ps else (None, None, None)
        res["models"][n] = {
            "balanced_accuracy": None if ba is None else round(ba, 4),
            "recall_up": None if r_up is None else round(r_up, 4),
            "recall_down": None if r_dn is None else round(r_dn, 4),
            "brier": None if not ps else round(brier(ps, ys), 4),
            "calibration": calibration(ps, ys),
            "ba_ci95": bootstrap_ci(len(ps), lambda ix, ps=ps: balanced_accuracy([ps[i] for i in ix], [ys[i] for i in ix])[0]) if ps else None,
            "brier_ci95": bootstrap_ci(len(ps), lambda ix, ps=ps: brier([ps[i] for i in ix], [ys[i] for i in ix])) if ps else None,
        }
    if compare_to and keep:
        res["paired"] = {}
        for a, b in compare_to:
            pa, pb = [preds[a][i] for i in keep], [preds[b][i] for i in keep]
            def dba(ix, pa=pa, pb=pb):
                x = balanced_accuracy([pa[i] for i in ix], [ys[i] for i in ix])[0]
                y = balanced_accuracy([pb[i] for i in ix], [ys[i] for i in ix])[0]
                return None if x is None or y is None else x - y
            dbr = lambda ix, pa=pa, pb=pb: brier([pa[i] for i in ix], [ys[i] for i in ix]) - brier([pb[i] for i in ix], [ys[i] for i in ix])
            full = list(range(len(keep)))
            res["paired"][f"{a}-{b}"] = {"d_balanced_accuracy": None if dba(full) is None else round(dba(full), 4),
                                         "d_ba_ci95": bootstrap_ci(len(keep), dba),
                                         "d_brier": round(dbr(full), 4), "d_brier_ci95": bootstrap_ci(len(keep), dbr)}
    return res, keep


def goal_a(names, preds, units, keep, h):
    pev1p_window.check_rows(units)
    big = [i for i in keep if abs(units[i]["labels"][h]) > LARGE_MOVE]
    out = {"threshold_abs_return": LARGE_MOVE, "n_large": len(big),
           "n_large_up": sum(1 for i in big if units[i]["labels"][h] > 0),
           "n_large_down": sum(1 for i in big if units[i]["labels"][h] <= 0), "models": {}}
    for n in names:
        _, r_up, r_dn = balanced_accuracy([preds[n][i] for i in big], [units[i]["labels"][h] > 0 for i in big]) if big else (None, None, None)
        out["models"][n] = {"recall_up_large": r_up, "recall_down_large": r_dn,
                            "auc_conviction": auc([abs(preds[n][i] - 0.5) for i in keep],
                                                  [abs(units[i]["labels"][h]) > LARGE_MOVE for i in keep])}
    return out


def decision_label(block, model, naive=("always_up", "always_down", "persistence")):
    if block["n_units"] < MIN_UNITS_FOR_LABEL:
        return f"NOT EVALUABLE (n < {MIN_UNITS_FOR_LABEL})"
    best = max((n for n in naive if n in block["models"]),
               key=lambda n: block["models"][n]["balanced_accuracy"] or 0)
    p_ba = block["paired"].get(f"{model}-{best}")
    p_br = block["paired"].get(f"{model}-climatology")
    if not p_ba or not p_br or not p_ba["d_ba_ci95"] or not p_br["d_brier_ci95"]:
        return "NOT DEMONSTRATED"
    if p_ba["d_ba_ci95"][0] > 0 and p_br["d_brier_ci95"][1] < 0:
        return "DEMONSTRATED"
    return "NOT DEMONSTRATED"


def evaluate_coin(closes, sentiment, horizons=HORIZONS_H):
    rows = build_rows(closes, horizons)
    sent_ts = [s[0] for s in sentiment]
    first_post = SEAL_END_TS + 1
    last_ts = rows[-1]["ts"] + 1 if rows else 0
    out = {}
    for h in horizons:
        base = {"always_up": lambda up, u, h: 1.0, "always_down": lambda up, u, h: 0.0,
                "climatology": climatology, "persistence": lambda up, u, h: persistence(u, h)}
        # Phase 4 selection: DEV rows are only analogs/training; hyperparameters chosen on VAL by Brier.
        val_units = unit_times(rows, h, DEV_END_TS, SEAL_START_TS)
        val_models = dict(base, B=lambda up, u, h: model_corrected(up, u, h))
        for lam in LR_LAMBDAS:
            val_models[f"P4-LR(l={lam})"] = (lambda lam: lambda up, u, h: model_lr(up, u, h, lam))(lam)
        for m in SHRINK_MS:
            val_models[f"P4-KNN-SHRINK(m={m})"] = (lambda m: lambda up, u, h: model_shrink(up, u, h, m))(m)
        vp = predict_all(rows, val_units, h, val_models)
        val_block, _ = score_block(list(val_models), vp, val_units, h,
                                   compare_to=[("B", n) for n in ("always_up", "always_down", "persistence", "climatology")])
        lr_names = [n for n in val_models if n.startswith("P4-LR")]
        sh_names = [n for n in val_models if n.startswith("P4-KNN-SHRINK")]
        pick = lambda names: min(names, key=lambda n: val_block["models"][n]["brier"] if val_block["models"][n]["brier"] is not None else 9)
        lr_pick, sh_pick = (pick(lr_names), pick(sh_names)) if val_block["n_units"] else (lr_names[1], sh_names[0])
        lam = float(lr_pick.split("=")[1].rstrip(")"))
        msh = int(sh_pick.split("=")[1].rstrip(")"))

        # TEST: post-seal, A/B/C on the common eligible set where C has a REAL as-of reading at the unit.
        test_units = unit_times(rows, h, first_post, last_ts)
        test_models = dict(base,
                           A=lambda up, u, h: model_a(up, u, h, sent_ts, sentiment),
                           B=lambda up, u, h: model_corrected(up, u, h),
                           C=lambda up, u, h: (model_corrected(up, u, h, sent_ts, sentiment)
                                               if asof_reading(sent_ts, sentiment, u["ts"]) is not None else None),
                           **{"P4-LR": lambda up, u, h: model_lr(up, u, h, lam),
                              "P4-KNN-SHRINK": lambda up, u, h: model_shrink(up, u, h, msh)})
        tp = predict_all(rows, test_units, h, test_models)
        names = list(test_models)
        pairs = [(m, b) for m in ("A", "B", "C", "P4-LR", "P4-KNN-SHRINK")
                 for b in ("always_up", "always_down", "persistence", "climatology")] + [("C", "B"), ("C", "A"), ("B", "A")]
        test_block, keep = score_block(names, tp, test_units, h, compare_to=pairs)
        test_block["decision"] = {m: decision_label(test_block, m) for m in ("A", "B", "C", "P4-LR", "P4-KNN-SHRINK")}
        if h == 24:
            test_block["goal_a"] = goal_a(["A", "B", "C", "P4-LR", "P4-KNN-SHRINK", "persistence"], tp, test_units, keep, h)
        dev_units = unit_times(rows, h, 0, DEV_END_TS)
        out[f"{h}h"] = {
            "selected": {"P4-LR_lambda": lam, "P4-KNN-SHRINK_m": msh},
            "counts": {"dev_units": len(dev_units), "val_units": len(val_units), "test_units": len(test_units)},
            "val": val_block, "test": test_block,
        }
    return out


# ---------------------------------------------------------------- CLI
def fetch(out_dir, start_ts=SEAL_END_TS, end_ts=None):
    """Post-seal hourly candles from Hyperliquid's public API; only closed candles after the seal are written."""
    end_ts = end_ts or int(time.time() * 1000)
    for coin in COINS:
        body = json.dumps({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                                                             "startTime": start_ts, "endTime": end_ts}}).encode()
        req = urllib.request.Request("https://api.hyperliquid.xyz/info", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            candles = json.loads(r.read())
        retrieved = int(time.time() * 1000)
        d = Path(out_dir) / coin
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        for c in sorted(candles, key=lambda c: c["t"]):
            avail = int(c["T"]) + 1
            if avail > retrieved or not price_usable(avail) or avail <= SEAL_END_TS:
                continue                       # still open, or sealed
            lines.append(json.dumps({"asset": coin, "open_ts": int(c["t"]), "close_ts": int(c["T"]), "c": c["c"],
                                     "available_at": avail, "retrieved_at": retrieved, "source": "hyperliquid"}))
        (d / "observations.jsonl").write_text("\n".join(lines) + "\n")
        print(coin, len(lines), "closed post-seal candles")


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--candles", nargs="+", required=True)
    r.add_argument("--sentiment", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--coins", nargs="+", default=list(COINS))
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        fetch(a.out)
        return 0
    sentiment = load_sentiment(a.sentiment)
    inputs = {a.sentiment: sha256_of(a.sentiment)}
    for d in a.candles:
        for coin in a.coins:
            p = Path(d) / coin / "observations.jsonl"
            if p.exists():
                inputs[str(p)] = sha256_of(p)
    report = {"protocol": PROTOCOL, "generated_at": int(time.time() * 1000), "inputs_sha256": inputs,
              "n_sentiment_readings_usable": len(sentiment), "coins": {}}
    for coin in a.coins:
        report["coins"][coin] = evaluate_coin(load_candles(a.candles, coin), sentiment)
        print(coin, "done", file=sys.stderr)
    Path(a.out).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
