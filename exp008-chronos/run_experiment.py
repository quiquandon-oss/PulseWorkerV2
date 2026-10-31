"""
Experiment 8 -- Amazon Chronos-2 research challenger for CryptoPulse V2.

RESEARCH ONLY. Each run:
  1. Resolves past Chronos-2 forecasts whose target time has passed, with
     EXP-004's rule: the first btc_data price at or after target_ts, compared
     with price_at_prediction (`(fwd - then) / then * 100`, UP if > 0).
  2. For each horizon (12h, 24h), takes the forecast origin of the newest
     EXP-004 TimesFM forecast (experiment_4_timesfm.input_end_ts, read-only).
     It forecasts from exactly that origin, using only btc_data points at or
     before it, with the same deduplication, 128-point context and cadence
     rule as EXP-004. So every Chronos-2 forecast has the same origin, target
     timestamp and ground truth as a TimesFM forecast. A run refuses to
     forecast when its last context point differs from the TimesFM row.

Storage: one immutable JSON file per forecast and per resolution, under
--store (a checkout of the research-data/exp008-chronos branch). Nothing is
written to production D1. Reads from production D1 are limited to btc_data
(at most one full read per run) and the newest experiment_4_timesfm row per
horizon.

It never writes to, and cannot influence, predictions, challenger_predictions,
selection_decisions, source weights, or EXP-004's table.

Failure handling: if the model cannot be loaded or a forecast fails, nothing
is written for that forecast and the run exits non-zero. There is no fallback
to a fake, copied or synthetic forecast.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATABASE_NAME = "sentiment-history"
COIN = "BTC"
HORIZONS_HOURS = [12, 24]                 # same as EXP-004
MODEL_ID = "amazon/chronos-2"
MODEL_VERSION = "chronos-2"
INFERENCE_BACKEND = "torch-cpu"
CONTEXT_LENGTH = 128                      # same as EXP-004, so both models see the same inputs
RECENT_CADENCE_WINDOW = 60                # same as EXP-004
DEDUP_TOLERANCE_MS = 60000                # same as EXP-004
QUANTILE_LEVELS = [0.1, 0.5, 0.9]         # all are Chronos-2 training quantiles: no interpolation
FEATURES_USED = "univariate: btc_price only, no covariates"
MAX_ANCHOR_AGE_MS = 48 * 3600000          # do not forecast from an origin older than this (no backfill)
LOAD_ATTEMPTS = 3
STORE_FORMAT = "exp008-chronos-store-v1"


# ---------------------------------------------------------------- D1 access (injectable for tests)

def run_d1(sql: str):
    """Executes a read-only SQL statement against production D1 via wrangler, the same
    mechanism EXP-004 uses. Raises on any failure."""
    result = subprocess.run(
        ["wrangler", "d1", "execute", DATABASE_NAME, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    return parsed[0]["results"] if parsed and parsed[0].get("results") is not None else []


# ---------------------------------------------------------------- input preparation (identical to EXP-004)

def dedupe_near_simultaneous(price_history):
    """Copied unchanged from exp004-timesfm/run_experiment.py (test_run_experiment.py checks
    that both behave identically). price_history must already be sorted by ts ascending."""
    if not price_history:
        return []
    result = [price_history[0]]
    for ts, price in price_history[1:]:
        if ts - result[-1][0] > DEDUP_TOLERANCE_MS:
            result.append((ts, price))
    return result


def median_delta_ms(price_history):
    """Copied unchanged from exp004-timesfm/run_experiment.py: median spacing of the most
    recent RECENT_CADENCE_WINDOW points of an already-deduplicated series."""
    recent = price_history[-RECENT_CADENCE_WINDOW:]
    deltas = sorted(b[0] - a[0] for a, b in zip(recent, recent[1:]) if b[0] > a[0])
    if not deltas:
        raise RuntimeError("insufficient recent price history to determine sampling interval")
    return deltas[len(deltas) // 2]


def fetch_price_history(d1, end_ts):
    """btc_data up to and including end_ts, sorted and deduplicated as in EXP-004.
    Same validity guards as EXP-004's resolver (numeric ts, non-null price)."""
    rows = d1(f"SELECT ts, btc_price FROM btc_data WHERE ts <= {int(end_ts)} "
              f"AND typeof(ts) IN ('integer', 'real') AND btc_price IS NOT NULL ORDER BY ts ASC")
    raw = [(int(r["ts"]), float(r["btc_price"])) for r in rows]
    raw.sort(key=lambda p: p[0])
    return dedupe_near_simultaneous(raw)


def fetch_anchor(d1, horizon_hours):
    """Newest EXP-004 forecast for this horizon (read-only), or None."""
    rows = d1("SELECT ts, input_end_ts, target_ts, price_at_prediction FROM experiment_4_timesfm "
              f"WHERE coin='{COIN}' AND horizon_hours={int(horizon_hours)} ORDER BY ts DESC LIMIT 1")
    return rows[0] if rows else None


# ---------------------------------------------------------------- store

def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def forecast_path(store, h, input_end_ts):
    return Path(store) / "forecasts" / COIN / f"{h}h" / f"{iso(input_end_ts)}.json"


def resolution_path(store, h, input_end_ts):
    return Path(store) / "resolutions" / COIN / f"{h}h" / f"{iso(input_end_ts)}.json"


def write_once(path, obj):
    """Immutable store: never overwrites. Returns False if the file already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode()
    try:
        with open(path, "xb") as f:
            f.write(data)
    except FileExistsError:
        return False
    return True


# ---------------------------------------------------------------- model

def load_pipeline(attempts=LOAD_ATTEMPTS, sleep=time.sleep):
    """Loads Chronos-2 on CPU, retrying transient download failures with backoff."""
    from chronos import Chronos2Pipeline   # imported lazily so tests run without the package
    revision = os.environ.get("CHRONOS_REVISION") or None
    last = None
    for i in range(attempts):
        try:
            return Chronos2Pipeline.from_pretrained(MODEL_ID, device_map="cpu", revision=revision)
        except Exception as e:   # network/hub errors: retry, then fail the run
            last = e
            if i + 1 < attempts:
                sleep(2 ** (i + 1))
    raise RuntimeError(f"could not load {MODEL_ID}: {last}")


def runtime_versions():
    out = {}
    for name in ("chronos", "torch", "transformers"):
        try:
            out[name] = __import__(name).__version__
        except Exception:
            out[name] = None
    return out


def forecast_point(pipeline, context_prices, horizon_steps):
    """Returns (median, q10, q90) of the last forecast step. Chronos-2's point forecast is
    its median (predict_quantiles returns the median as the 'mean')."""
    import numpy as np
    quantiles, _ = pipeline.predict_quantiles(
        [np.asarray(context_prices, dtype="float32")],
        prediction_length=horizon_steps,
        quantile_levels=QUANTILE_LEVELS,
    )
    last = quantiles[0][0, -1, :]          # (n_variates=1, horizon, quantiles) -> final step
    q10, q50, q90 = (float(v) for v in last)
    return q50, q10, q90


# ---------------------------------------------------------------- run

def resolve_pending(store, d1, now_ms):
    resolved, pending = 0, 0
    for fp in sorted((Path(store) / "forecasts").glob(f"{COIN}/*h/*.json")):
        fc = json.loads(fp.read_text())
        rp = resolution_path(store, fc["horizon_hours"], fc["input_end_ts"])
        if rp.exists() or fc["target_ts"] > now_ms:
            pending += int(not rp.exists())
            continue
        rows = d1(f"SELECT ts, btc_price FROM btc_data WHERE ts >= {int(fc['target_ts'])} "
                  f"AND typeof(ts) IN ('integer', 'real') AND btc_price IS NOT NULL ORDER BY ts ASC LIMIT 1")
        if not rows:
            pending += 1
            continue                            # no valid price yet: retried next run, never fabricated
        actual_price = float(rows[0]["btc_price"])
        actual_return = (actual_price - fc["price_at_prediction"]) / fc["price_at_prediction"] * 100
        actual_dir = "UP" if actual_return > 0 else "DOWN"
        persistence_dir = fc.get("persistence_direction")
        write_once(rp, {
            "format": STORE_FORMAT, "id": fc["id"], "horizon_hours": fc["horizon_hours"],
            "input_end_ts": fc["input_end_ts"], "target_ts": fc["target_ts"],
            "resolution_price_ts": int(rows[0]["ts"]), "actual_price": actual_price,
            "actual_return_pct": actual_return, "actual_direction": actual_dir,
            "correct": int(fc["direction"] == actual_dir),
            "persistence_correct": None if persistence_dir is None else int(persistence_dir == actual_dir),
            "absolute_error": abs(fc["predicted_return_pct"] - actual_return),
            "signed_error": fc["predicted_return_pct"] - actual_return,
            "resolved_ts": now_ms,
        })
        resolved += 1
    return resolved, pending


def make_forecasts(store, d1, pipeline_loader, now_ms, code_sha=None):
    made, notes, errors = [], [], []
    anchors = {h: fetch_anchor(d1, h) for h in HORIZONS_HOURS}
    todo = []
    for h, a in anchors.items():
        if not a:
            notes.append(f"{h}h: no EXP-004 forecast to anchor to")
        elif forecast_path(store, h, int(a["input_end_ts"])).exists():
            notes.append(f"{h}h: origin {iso(int(a['input_end_ts']))} already forecast")
        elif now_ms - int(a["input_end_ts"]) > MAX_ANCHOR_AGE_MS:
            notes.append(f"{h}h: newest EXP-004 origin {iso(int(a['input_end_ts']))} is older than 48h; not backfilled")
        else:
            todo.append((h, a))
    if not todo:
        return made, notes, errors
    pipeline = pipeline_loader()
    versions = runtime_versions()
    history_cache = {}
    for h, a in todo:
        end = int(a["input_end_ts"])
        try:
            if end not in history_cache:
                history_cache[end] = fetch_price_history(d1, end)
            hist = history_cache[end]
            if len(hist) < 30:
                raise RuntimeError(f"insufficient history ({len(hist)} points)")
            last_ts, last_price = hist[-1]
            if last_ts != end or abs(last_price - float(a["price_at_prediction"])) > 1e-9:
                raise RuntimeError(f"context does not end at the EXP-004 origin (last {last_ts}/{last_price}, "
                                   f"anchor {end}/{a['price_at_prediction']}): refusing an unpaired forecast")
            context = [p for _, p in hist[-CONTEXT_LENGTH:]]
            step_ms = median_delta_ms(hist)
            steps = max(1, round(h * 3600000 / step_ms))
            median, q10, q90 = forecast_point(pipeline, context, steps)
            ret = (median - last_price) / last_price * 100
            prev = hist[-2][1]
            last_tick_ret = (last_price - prev) / prev * 100
            rec = {
                "format": STORE_FORMAT, "id": f"{COIN}_{h}h_{iso(end)}", "coin": COIN, "horizon_hours": h,
                "issued_ts": now_ms, "input_end_ts": end, "target_ts": end + h * 3600000,
                "price_at_prediction": last_price, "forecast_median": median, "forecast_q10": q10, "forecast_q90": q90,
                "predicted_return_pct": ret, "direction": "UP" if ret > 0 else "DOWN",
                "last_tick_return_pct": last_tick_ret,
                "persistence_direction": "UP" if last_tick_ret > 0 else "DOWN",
                "persistence_rule": "provisional: direction of the last deduplicated tick (not pre-registered)",
                "model_id": MODEL_ID, "model_version": MODEL_VERSION,
                "model_revision": os.environ.get("CHRONOS_REVISION") or "main (unpinned)",
                "inference_backend": INFERENCE_BACKEND, "runtime": versions, "quantile_levels": QUANTILE_LEVELS,
                "context_length": len(context), "forecast_horizon_steps": steps, "step_ms": step_ms,
                "context_sha256": hashlib.sha256(json.dumps(context).encode()).hexdigest(),
                "features_used": FEATURES_USED,
                "anchor": {"source": "experiment_4_timesfm", "timesfm_ts": int(a["ts"]), "timesfm_target_ts": int(a["target_ts"])},
                "code_sha": code_sha,
            }
            if write_once(forecast_path(store, h, end), rec):
                made.append(rec["id"])
        except Exception as e:
            errors.append(f"{h}h: {type(e).__name__}: {e}")
    return made, notes, errors


def main(argv=None, d1=run_d1, pipeline_loader=load_pipeline, now_ms=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True, help="directory of the exp008_chronos store")
    a = ap.parse_args(argv)
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    store = Path(a.store)
    record = {"format": STORE_FORMAT, "run_ts": now_ms, "code_sha": os.environ.get("GITHUB_SHA"),
              "run_id": os.environ.get("GITHUB_RUN_ID")}
    try:
        record["resolved"], record["pending"] = resolve_pending(store, d1, now_ms)
    except Exception as e:
        record["resolve_error"] = f"{type(e).__name__}: {e}"
    try:
        made, notes, errors = make_forecasts(store, d1, pipeline_loader, now_ms, record["code_sha"])
    except Exception as e:
        made, notes, errors = [], [], [f"{type(e).__name__}: {e}"]
    record.update(forecasts=made, notes=notes, errors=errors)
    write_once(store / "runs" / f"{iso(now_ms)}.json", record)
    print(json.dumps(record, indent=1))
    return 1 if (errors or "resolve_error" in record) else 0


if __name__ == "__main__":
    sys.exit(main())
