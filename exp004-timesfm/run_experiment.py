#!/usr/bin/env python3
"""
Experiment 4 -- Google TimesFM research challenger for CryptoPulse V2.

RESEARCH ONLY. This script:
  1. Fetches BTC's own price history from production D1 (read-only).
  2. Runs a univariate TimesFM 2.5 forecast for BTC/12h and BTC/24h.
  3. Logs the forecast to the separate experiment_4_timesfm D1 table.
  4. Separately, resolves any past forecast whose horizon has now passed,
     using PulseWorkerV2's own authoritative return-percentage formula
     (NOT log-return, NOT invented here -- matches worker.js's existing
     `(fwd.btc_price - n.row.btc_price) / n.row.btc_price * 100`).

It NEVER writes to predictions, link_predictions, eth_predictions,
challenger_predictions, or selection_decisions. Its only write target is
experiment_4_timesfm. It reads selection_decisions once, read-only, to
record production_chosen_variant for later comparison -- it never writes
back to that table and cannot influence it.

Model choice: TimesFM 2.5 (google/timesfm-2.5-200m-pytorch), not 3.0.
TimesFM 3.0's pretrained weights are distributed under a separate
non-commercial/non-production-use-only license (confirmed directly from
Google's own repository and PyPI page, 2026-09-06) -- an unresolved
licensing ambiguity for use inside a continuously-running system, however
clearly the *usage* here is research-only. TimesFM 2.5 is Apache-2.0
(source and weights), is Google's own currently-recommended default
("Always use TimesFM 2.5 unless you have a specific reason to use an
older checkpoint" -- Google's own SKILL.md), needs less RAM (~1.5GB vs an
extrapolated ~2.5-3GB for 3.0's 330M params), and is fully sufficient for
this experiment's univariate, non-covariate use case -- 3.0's headline
new capability (native multivariate/covariate forecasting) is not needed
here. This is a documented, justified choice, not an oversight.

Failure handling: if TimesFM cannot execute (install failure, forecast
exception, anything), this script exits non-zero and writes NOTHING. It
never falls back to a fake/copied/synthetic forecast. A failed run here
has zero effect on pulseworker-v2, which is a completely separate,
unaffected system.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

DATABASE_NAME = "sentiment-history"
COIN = "BTC"  # Experiment 4 is BTC-only -- see the PR description for why
              # (TimesFM cannot run inside Cloudflare's global cron at all,
              # so it structurally requires its own separate schedule,
              # which per the explicit spec rule mandates BTC-only scope).
HORIZONS_HOURS = [12, 24]  # same two horizons PulseWorkerV2 already supports for BTC
MODEL_VERSION = "timesfm-2.5-200m"
CHECKPOINT = "google/timesfm-2.5-200m-pytorch"
INFERENCE_BACKEND = "torch-cpu"
CONTEXT_LENGTH = 512  # conservative, well within TimesFM 2.5's supported context; see report for rationale
FEATURES_USED = "univariate: btc_price only, no covariates"


def run_d1(sql: str):
    """Executes a SQL statement against production D1 via wrangler, exactly
    the same mechanism .github/workflows/export-learning-data.yml already
    uses. Returns the parsed JSON result. Raises on any failure -- this
    script does not swallow D1 errors."""
    result = subprocess.run(
        ["wrangler", "d1", "execute", DATABASE_NAME, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    return parsed[0]["results"] if parsed and parsed[0].get("results") is not None else []


def sql_escape(value):
    """Minimal, explicit SQL string escaping for the values this script
    generates itself (never raw user input) -- single-quote doubling,
    the standard SQLite/D1 convention."""
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def fetch_price_history():
    """Full BTC price history, oldest first -- same table PulseWorkerV2's
    own runPrediction reads from (btc_data). No WHERE clause needed since
    we want everything available AS OF NOW; input_end_ts is recorded
    explicitly below to prove exactly what was actually used."""
    rows = run_d1("SELECT ts, btc_price FROM btc_data ORDER BY ts ASC")
    return [(int(r["ts"]), float(r["btc_price"])) for r in rows if r.get("btc_price") is not None]


def fetch_production_chosen_variant(horizon_hours: int):
    rows = run_d1(
        f"SELECT chosen_variant FROM selection_decisions WHERE coin='{COIN}' "
        f"AND horizon_hours={horizon_hours} ORDER BY ts DESC LIMIT 1"
    )
    return rows[0]["chosen_variant"] if rows else None


def median_delta_ms(price_history):
    deltas = sorted(b[0] - a[0] for a, b in zip(price_history, price_history[1:]) if b[0] > a[0])
    if not deltas:
        raise RuntimeError("insufficient price history to determine sampling interval")
    return deltas[len(deltas) // 2]


def generate_forecasts():
    import torch
    import timesfm

    torch.set_float32_matmul_precision("high")
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(CHECKPOINT)
    model.compile(timesfm.ForecastConfig(
        max_context=CONTEXT_LENGTH,
        max_horizon=256,
        normalize_inputs=True,
        use_continuous_quantile_head=False,  # point forecast only -- see NO FAKE CONFIDENCE in the PR description
        force_flip_invariance=True,
        infer_is_positive=True,   # price series are strictly positive
        fix_quantile_crossing=True,
    ))

    price_history = fetch_price_history()
    if len(price_history) < 30:
        raise RuntimeError(f"insufficient BTC price history ({len(price_history)} rows) for a TimesFM context")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    input_end_ts = price_history[-1][0]
    context_prices = [p for _, p in price_history[-CONTEXT_LENGTH:]]
    step_ms = median_delta_ms(price_history)

    for horizon_hours in HORIZONS_HOURS:
        horizon_steps = max(1, round(horizon_hours * 3600000 / step_ms))
        point_forecast, _ = model.forecast(horizon=horizon_steps, inputs=[context_prices])
        forecast_price = float(point_forecast[0][-1])  # last forecasted step = the target horizon
        price_at_prediction = context_prices[-1]
        # SAME formula as worker.js's own return_pct -- not invented here.
        predicted_return_pct = (forecast_price - price_at_prediction) / price_at_prediction * 100
        direction = "UP" if predicted_return_pct > 0 else "DOWN"  # documented threshold: > 0, not >=
        production_variant = fetch_production_chosen_variant(horizon_hours)
        target_ts = now_ms + horizon_hours * 3600000

        sql = (
            "INSERT INTO experiment_4_timesfm "
            "(coin, horizon_hours, ts, target_ts, input_end_ts, price_at_prediction, forecast_price, "
            "predicted_return_pct, direction, model_version, checkpoint, inference_backend, context_length, "
            "features_used, production_chosen_variant) VALUES ("
            f"{sql_escape(COIN)}, {horizon_hours}, {now_ms}, {target_ts}, {input_end_ts}, "
            f"{price_at_prediction}, {forecast_price}, {predicted_return_pct}, {sql_escape(direction)}, "
            f"{sql_escape(MODEL_VERSION)}, {sql_escape(CHECKPOINT)}, {sql_escape(INFERENCE_BACKEND)}, "
            f"{horizon_steps}, {sql_escape(FEATURES_USED)}, {sql_escape(production_variant)})"
        )
        run_d1(sql)
        print(f"[exp004] logged BTC/{horizon_hours}h forecast: price_now={price_at_prediction} "
              f"forecast={forecast_price:.2f} return_pct={predicted_return_pct:.3f} direction={direction}")


def resolve_pending():
    """Resolves any past forecast whose target_ts has already passed and
    hasn't been resolved yet. Uses the nearest actual price at/after
    target_ts, same nearest-match convention used throughout worker.js's
    own backfill functions."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    pending = run_d1(
        f"SELECT id, target_ts, price_at_prediction, predicted_return_pct, direction "
        f"FROM experiment_4_timesfm WHERE resolved_ts IS NULL AND target_ts <= {now_ms}"
    )
    if not pending:
        print("[exp004] no pending resolutions")
        return

    for row in pending:
        actual_rows = run_d1(
            f"SELECT btc_price FROM btc_data WHERE ts >= {row['target_ts']} ORDER BY ts ASC LIMIT 1"
        )
        if not actual_rows:
            continue  # not resolvable yet -- no price data at/after target_ts; try again next run
        actual_price = float(actual_rows[0]["btc_price"])
        price_then = float(row["price_at_prediction"])
        actual_return_pct = (actual_price - price_then) / price_then * 100
        actual_direction = "UP" if actual_return_pct > 0 else "DOWN"
        predicted = float(row["predicted_return_pct"])
        correct = 1 if row["direction"] == actual_direction else 0
        absolute_error = abs(predicted - actual_return_pct)
        signed_error = predicted - actual_return_pct

        run_d1(
            "UPDATE experiment_4_timesfm SET "
            f"actual_return_pct={actual_return_pct}, actual_direction={sql_escape(actual_direction)}, "
            f"correct={correct}, absolute_error={absolute_error}, signed_error={signed_error}, "
            f"resolved_ts={now_ms} WHERE id={row['id']}"
        )
        print(f"[exp004] resolved id={row['id']}: predicted={row['direction']} actual={actual_direction} correct={correct}")


if __name__ == "__main__":
    try:
        generate_forecasts()
    except Exception as e:
        print(f"[exp004] FORECAST GENERATION FAILED (writing nothing): {e}", file=sys.stderr)
        sys.exit(1)

    try:
        resolve_pending()
    except Exception as e:
        print(f"[exp004] RESOLUTION PASS FAILED: {e}", file=sys.stderr)
        sys.exit(1)
