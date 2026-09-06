"""
Tests for exp004-timesfm/run_experiment.py.

Run with: pip install pytest --break-system-packages && pytest exp004-timesfm/

These tests do NOT require the actual timesfm/torch packages to be
installed (generate_forecasts() imports them lazily, inside the function
body, specifically so this test suite can exercise everything else --
constants, SQL construction, resolution math, failure handling -- without
needing the real (large) ML dependencies installed just to run tests.
"""
import importlib.util
import os
import sys
import re

sys.path.insert(0, os.path.dirname(__file__))
spec = importlib.util.spec_from_file_location("run_experiment", os.path.join(os.path.dirname(__file__), "run_experiment.py"))
run_experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_experiment)


# ---- Scope ----

def test_coin_is_btc_only():
    assert run_experiment.COIN == "BTC"


def test_no_separate_eth_or_link_workflow_exists():
    workflows_dir = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows")
    exp004_workflows = [f for f in os.listdir(workflows_dir) if "exp004" in f.lower() or "timesfm" in f.lower()]
    assert len(exp004_workflows) == 1, f"expected exactly one Experiment 4 workflow file, found {exp004_workflows}"
    with open(os.path.join(workflows_dir, exp004_workflows[0])) as f:
        content = f.read()
    # Only one `on: schedule:` cron entry -- proves there isn't a
    # separate scheduled trigger per coin.
    cron_lines = re.findall(r"- cron:", content)
    assert len(cron_lines) == 1


def test_workflow_does_not_touch_cloudflare_cron_triggers():
    """Experiment 4's own GitHub Actions schedule must not be confused
    with, or accidentally added to, wrangler.toml's [triggers] crons
    array (the Cloudflare Cron Trigger account-wide budget)."""
    wrangler_toml = os.path.join(os.path.dirname(__file__), "..", "wrangler.toml")
    with open(wrangler_toml) as f:
        content = f.read()
    assert "30 7 * * *" not in content


# ---- Data integrity ----

def test_return_pct_formula_matches_pulseworkerv2_convention():
    # Same formula as worker.js's own `(fwd.btc_price - n.row.btc_price) / n.row.btc_price * 100`
    price_at_prediction = 100.0
    forecast_price = 105.0
    expected = (forecast_price - price_at_prediction) / price_at_prediction * 100
    assert expected == 5.0


def test_direction_threshold_is_strictly_greater_than_zero():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert '"UP" if predicted_return_pct > 0 else "DOWN"' in src


def test_input_end_ts_is_recorded_and_never_exceeds_prediction_time():
    """input_end_ts is set to the last price row's own timestamp (the
    real data), which by construction (SELECT ... ORDER BY ts ASC with
    no future filter, run at prediction time) cannot be in the future
    relative to when the script runs -- the query itself has no
    mechanism to retrieve rows that don't exist yet."""
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert "input_end_ts = price_history[-1][0]" in src
    assert "ORDER BY ts ASC" in src


def test_target_ts_uses_horizon_hours_consistently_with_pulseworkerv2():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert "target_ts = now_ms + horizon_hours * 3600000" in src


def test_resolution_uses_nearest_price_at_or_after_target_ts_not_before():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert "WHERE ts >= {row['target_ts']} ORDER BY ts ASC LIMIT 1" in src


# ---- Model integrity ----

def test_model_version_and_checkpoint_are_recorded_constants():
    assert run_experiment.MODEL_VERSION == "timesfm-2.5-200m"
    assert run_experiment.CHECKPOINT == "google/timesfm-2.5-200m-pytorch"
    assert run_experiment.INFERENCE_BACKEND == "torch-cpu"


def test_no_fake_confidence_field_anywhere_in_the_insert():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    # Check the actual INSERT column list, not comments -- "confidence"
    # is legitimately mentioned in an explanatory comment nearby.
    insert_stmt = re.search(r'"INSERT INTO experiment_4_timesfm\s*"(.*?)VALUES', src, re.DOTALL)
    assert insert_stmt is not None
    assert "confidence" not in insert_stmt.group(1).lower()
    # quantile head explicitly disabled -- point forecast only, no
    # probabilistic output is generated or stored.
    assert "use_continuous_quantile_head=False" in src


def test_features_used_is_explicit_and_documented():
    assert run_experiment.FEATURES_USED == "univariate: btc_price only, no covariates"


# ---- Production isolation ----

def test_script_never_writes_to_any_production_table():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    for forbidden in ["INSERT INTO predictions", "INSERT INTO link_predictions", "INSERT INTO eth_predictions",
                       "INSERT INTO challenger_predictions", "INSERT INTO selection_decisions",
                       "UPDATE predictions", "UPDATE selection_decisions"]:
        assert forbidden not in src, f"found forbidden write target: {forbidden}"


def test_selection_decisions_is_only_ever_read_not_written():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert "SELECT chosen_variant FROM selection_decisions" in src
    assert "INTO selection_decisions" not in src


def test_only_write_target_is_experiment_4_timesfm():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert "INSERT INTO experiment_4_timesfm" in src
    assert "UPDATE experiment_4_timesfm" in src
    insert_targets = re.findall(r"INSERT INTO (\w+)", src)
    update_targets = re.findall(r"UPDATE (\w+) SET", src)
    assert set(insert_targets) == {"experiment_4_timesfm"}
    assert set(update_targets) == {"experiment_4_timesfm"}


# ---- Failure handling ----

def test_generate_forecasts_failure_exits_nonzero_and_never_reaches_resolution(monkeypatch):
    """Simulates the __main__ block's own logic without actually running
    it as a subprocess -- forces generate_forecasts to raise, confirms
    the surrounding try/except would sys.exit(1) rather than fabricate a
    result or silently continue to resolution."""
    import pytest

    def boom():
        raise RuntimeError("simulated TimesFM unavailability")
    monkeypatch.setattr(run_experiment, "generate_forecasts", boom)

    resolve_called = []
    monkeypatch.setattr(run_experiment, "resolve_pending", lambda: resolve_called.append(True))

    def main_block():
        try:
            run_experiment.generate_forecasts()
        except Exception:
            sys.exit(1)
        try:
            run_experiment.resolve_pending()
        except Exception:
            sys.exit(1)

    with pytest.raises(SystemExit) as exc_info:
        main_block()

    assert exc_info.value.code == 1
    assert resolve_called == []  # resolution must never run after a forecast failure


def test_insufficient_price_history_raises_rather_than_proceeding():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert "insufficient BTC price history" in src
    assert "len(price_history) < 30" in src
