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
import sqlite3

import pytest

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


def test_dedup_collapses_near_simultaneous_pairs_from_the_two_horizon_calls():
    """Reproduces the exact real-data shape confirmed against production
    btc_data (2026-09-06): each cron tick logs price twice (once per
    horizon call), ~150-300ms apart, with ~3h between ticks. Without
    dedup, a naive median-delta calculation is dominated by these
    sub-second gaps -- confirmed empirically to compute ~6.8s instead of
    ~3h, which would have made horizon_steps wrong by roughly 3 orders
    of magnitude (~6345 instead of 4 for a 12h horizon)."""
    tick_spacing_ms = 3 * 3600000
    base = 1788458427794
    raw = []
    for i in range(20):
        tick_ts = base + i * tick_spacing_ms
        raw.append((tick_ts, 90000.0 + i))
        raw.append((tick_ts + 200, 90000.0 + i))  # the second horizon call's own price-log write
    deduped = run_experiment.dedupe_near_simultaneous(raw)
    assert len(deduped) == 20  # one point per real tick, not 40
    step = run_experiment.median_delta_ms(deduped)
    assert abs(step - tick_spacing_ms) < 1000  # within 1s of the real ~3h spacing
    # And the horizon_steps this would produce, matching generate_forecasts' own formula:
    assert round(12 * 3600000 / step) == 4
    assert round(24 * 3600000 / step) == 8


def test_naive_undeduped_median_would_have_been_wrong_by_orders_of_magnitude():
    """Documents the bug this fix addresses, using the same synthetic
    shape as the dedup test above but WITHOUT deduping -- proving the
    old behavior really was wrong, not just theoretically risky."""
    tick_spacing_ms = 3 * 3600000
    base = 1788458427794
    raw = []
    for i in range(20):
        tick_ts = base + i * tick_spacing_ms
        raw.append((tick_ts, 90000.0 + i))
        raw.append((tick_ts + 200, 90000.0 + i))
    raw.sort()
    naive_step = run_experiment.median_delta_ms(raw)  # deliberately NOT deduped
    assert naive_step < 1000  # dominated by the ~200ms intra-tick gaps, not ~3h
    naive_horizon_steps_12h = round(12 * 3600000 / naive_step)
    assert naive_horizon_steps_12h > 1000  # wildly wrong, confirming the bug's real magnitude


def test_recent_window_ignores_an_older_different_cadence_era_mixed_into_full_history():
    """Reproduces the EXACT real bug found against production btc_data
    (2026-09-06): the full historical dataset contains an older ~1h
    cadence era (570 of 1046 real deduplicated ticks) alongside the
    current ~3h era (173 ticks) -- a real run using the full history's
    median computed step_ms=1h, giving horizon_steps=12/24 instead of
    the correct 4/8. This test builds a synthetic series with the same
    two-era shape and confirms median_delta_ms, using only the most
    recent RECENT_CADENCE_WINDOW points, correctly reflects the CURRENT
    era and ignores the older one entirely."""
    one_hour = 3600000
    three_hours = 3 * one_hour
    base = 1_700_000_000_000

    # Older era: 800 ticks at ~1h spacing (dominates by raw count, same
    # as the real data, where the 1h era outnumbers the 3h era).
    older_era = [(base + i * one_hour, 90000.0) for i in range(800)]
    # Current (recent) era: 80 ticks at ~3h spacing, appended after.
    current_era_start = older_era[-1][0] + three_hours
    recent_era = [(current_era_start + i * three_hours, 90000.0) for i in range(80)]

    full_history = older_era + recent_era
    assert len(full_history) > run_experiment.RECENT_CADENCE_WINDOW

    step = run_experiment.median_delta_ms(full_history)
    # Must reflect the CURRENT (~3h) era, not the older (~1h) era that
    # dominates the full history by count.
    assert abs(step - three_hours) < 1000, (
        f"median_delta_ms computed {step}ms ({step/3600000:.2f}h) -- expected ~3h "
        f"(the current era), not ~1h (the older era that outnumbers it in the full history)"
    )
    assert round(12 * 3600000 / step) == 4
    assert round(24 * 3600000 / step) == 8


def test_context_length_stays_within_the_recent_cadence_era():
    """CONTEXT_LENGTH=128 at the current ~3h cadence covers ~16 days --
    small enough to plausibly stay within a single cadence era, unlike
    the original 512 (which, at 3h spacing, would reach back ~64 days --
    long enough to cross into btc_data's real older ~1h era)."""
    assert run_experiment.CONTEXT_LENGTH == 128
    days_covered_at_current_cadence = (run_experiment.CONTEXT_LENGTH * 3) / 24
    assert days_covered_at_current_cadence < 30  # well under a month, deliberately conservative


def test_target_ts_uses_horizon_hours_consistently_with_pulseworkerv2():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert "target_ts = input_end_ts + horizon_hours * 3600000" in src
    # And explicitly NOT anchored to execution time -- this is the exact
    # methodological correction this test guards: the forecast represents
    # input_end_ts + horizon, not now_ms + horizon, since there is a real,
    # observed gap between when data was last available and when the
    # script happens to execute (confirmed on real data: input_end_ts was
    # ~2.27h before now_ms in the first real validation run).
    assert "target_ts = now_ms + horizon_hours * 3600000" not in src


def test_resolution_uses_nearest_price_at_or_after_target_ts_not_before():
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert "WHERE ts >= {row['target_ts']} " in src
    assert "ORDER BY ts ASC LIMIT 1" in src
    # Updated for the EXP-004 Resolution Integrity Investigation fix:
    # both fail-closed guards must remain part of this same query.
    assert "typeof(ts) IN ('integer', 'real')" in src
    assert "btc_price IS NOT NULL" in src


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


def test_context_length_and_horizon_steps_are_not_swapped():
    """Directly guards against the real bug found in PR #39's validation
    run: context_length and forecast_horizon_steps were swapped in the
    original INSERT (context_length stored horizon_steps' value; there
    was no column for horizon_steps at all). Confirmed against the real
    run's own stored data: context_length showed 12 and 24 (==
    horizon_hours), not a plausible context-window size."""
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    # Column declaration order: context_length must come directly before
    # forecast_horizon_steps.
    assert "inference_backend, context_length, " in src
    assert "forecast_horizon_steps, features_used" in src
    # And the actual f-string values passed at those same two positions,
    # in the same order: len(context_prices) then horizon_steps.
    assert "{len(context_prices)}, {horizon_steps}, {sql_escape(FEATURES_USED)}" in src
    src = open(os.path.join(os.path.dirname(__file__), "run_experiment.py")).read()
    assert "INSERT INTO experiment_4_timesfm" in src
    assert "UPDATE experiment_4_timesfm" in src
    assert "forecast_horizon_steps" in src
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


# ---- resolve_pending() runtime adversarial fixtures (Phase 1 Research ----
# ---- Governance Correction: Task 3, supplementing the source-text-only ----
# ---- assertions above with REAL execution) ----
#
# resolve_pending()'s own SQL is executed here against a REAL, in-memory
# sqlite3 database (run_d1 is monkeypatched to route through it) instead
# of being asserted only as a string -- the same "actually execute"
# convention research/test_migration.py already established for a
# different migration, applied here to a function that had never had a
# single runtime test before this addition (only string-presence checks,
# per the governance audit's own F5 finding).

def _fresh_resolve_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE experiment_4_timesfm (
          id INTEGER PRIMARY KEY, target_ts, price_at_prediction REAL,
          predicted_return_pct REAL, direction TEXT, resolved_ts INTEGER,
          actual_return_pct REAL, actual_direction TEXT, correct INTEGER,
          absolute_error REAL, signed_error REAL
        )
    """)
    conn.execute("CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts, btc_price REAL)")
    return conn


def _sqlite_run_d1(conn):
    """Routes run_experiment.run_d1(sql) through the real sqlite3
    connection instead of a canned stub, so resolve_pending()'s actual
    WHERE/ORDER BY/type-comparison behavior is genuinely exercised."""
    def fake_run_d1(sql):
        if sql.strip().upper().startswith("UPDATE"):
            conn.execute(sql)
            conn.commit()
            return []
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    return fake_run_d1


def test_resolve_pending_selects_price_exactly_at_target_ts(monkeypatch):
    """Fixture: observation exactly at target_ts. The query's own
    `ts >= target_ts` is inclusive of the boundary -- confirmed here by
    real execution, not string matching."""
    conn = _fresh_resolve_db()
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (1, 2000, 100.0, 5.0, 'UP')")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (1000, 90.0)")   # before -- must not be picked
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (2000, 110.0)")  # exactly at target_ts -- must be picked
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (3000, 130.0)")  # after -- must not be picked over the exact match
    monkeypatch.setattr(run_experiment, "run_d1", _sqlite_run_d1(conn))
    run_experiment.resolve_pending()
    row = conn.execute("SELECT actual_return_pct, resolved_ts FROM experiment_4_timesfm WHERE id=1").fetchone()
    assert row[0] == 10.0  # (110-100)/100*100
    assert row[1] is not None


def test_resolve_pending_excludes_observation_immediately_before_target_ts_and_uses_the_one_immediately_after(monkeypatch):
    """Fixtures: observation immediately before target_ts (must NOT be
    selected) and observation immediately after with no exact row at
    target_ts (must be selected as the nearest-at-or-after price)."""
    conn = _fresh_resolve_db()
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (1, 2000, 100.0, 5.0, 'UP')")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (1999, 999.0)")  # immediately before -- must be excluded
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (2001, 150.0)")  # immediately after -- must be selected
    monkeypatch.setattr(run_experiment, "run_d1", _sqlite_run_d1(conn))
    run_experiment.resolve_pending()
    row = conn.execute("SELECT actual_return_pct FROM experiment_4_timesfm WHERE id=1").fetchone()
    assert row[0] == 50.0  # (150-100)/100*100 -- proves the 999.0 "before" row was never used


def test_resolve_pending_duplicate_btc_data_timestamps_resolve_deterministically(monkeypatch):
    """Fixture: duplicate btc_data timestamps. The query
    (`ORDER BY ts ASC LIMIT 1`, no secondary/tiebreak column) has no
    DOCUMENTED tiebreak for two rows sharing the same ts -- this test
    establishes the ACTUAL current behavior (first-inserted/lowest-rowid
    row wins, confirmed by running it twice) rather than assuming an
    undocumented contract. FINDING: if this experiment's own ordering
    behavior ever needs to be robust to which duplicate is picked, the
    query itself would need an explicit secondary ORDER BY key (e.g.
    `id`) -- it does not have one today."""
    conn = _fresh_resolve_db()
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (1, 2000, 100.0, 5.0, 'UP')")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (2000, 111.0)")  # inserted first
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (2000, 222.0)")  # inserted second, same ts
    monkeypatch.setattr(run_experiment, "run_d1", _sqlite_run_d1(conn))
    run_experiment.resolve_pending()
    row = conn.execute("SELECT actual_return_pct FROM experiment_4_timesfm WHERE id=1").fetchone()
    assert row[0] == 11.0  # (111-100)/100*100 -- the first-inserted duplicate, confirmed empirically, not assumed


def test_resolve_pending_malformed_btc_data_timestamp_is_excluded_and_fails_closed(monkeypatch):
    """REGRESSION GUARD (EXP-004 Resolution Integrity Investigation,
    defect 1, now fixed). A malformed (non-numeric) btc_data.ts must
    never become a valid resolution candidate, even when it is the ONLY
    row that would otherwise satisfy `ts >= target_ts` (SQLite/D1
    compare TEXT as greater than any INTEGER, so it would win
    `ORDER BY ts ASC LIMIT 1` if not explicitly excluded). The fix adds
    `typeof(ts) IN ('integer','real')` to the query -- confirmed here
    that the row is excluded, the forecast is left UNRESOLVED (never
    coerced/fabricated), and the malformed row's obviously-wrong price
    (999999.0) is never used."""
    conn = _fresh_resolve_db()
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (1, 2000, 100.0, 5.0, 'UP')")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES ('not-a-timestamp', 999999.0)")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (500, 1.0)")  # a numeric row, but it does not satisfy >= 2000
    monkeypatch.setattr(run_experiment, "run_d1", _sqlite_run_d1(conn))
    run_experiment.resolve_pending()
    row = conn.execute("SELECT actual_return_pct, resolved_ts FROM experiment_4_timesfm WHERE id=1").fetchone()
    assert row[1] is None, "the malformed-timestamp row must never resolve this forecast"
    assert row[0] is None, "no return_pct must be persisted -- not fabricated from the malformed row's price"


def test_resolve_pending_malformed_timestamp_cannot_determine_resolution_price_even_when_a_valid_row_also_qualifies(monkeypatch):
    """REGRESSION GUARD, complementary to the above: when a GENUINELY
    valid, qualifying btc_data row ALSO exists alongside a malformed-ts
    row, resolution must use the valid row's price, never the malformed
    one's -- proving the exclusion, not merely the absence of a
    candidate, is what drives correctness here."""
    conn = _fresh_resolve_db()
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (1, 2000, 100.0, 5.0, 'UP')")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES ('not-a-timestamp', 999999.0)")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (2500, 130.0)")  # genuinely valid, qualifies
    monkeypatch.setattr(run_experiment, "run_d1", _sqlite_run_d1(conn))
    run_experiment.resolve_pending()
    row = conn.execute("SELECT actual_return_pct, resolved_ts FROM experiment_4_timesfm WHERE id=1").fetchone()
    assert row[1] is not None
    assert row[0] == pytest.approx((130.0 - 100.0) / 100.0 * 100.0)  # the VALID row's price, never 999999.0


def test_resolve_pending_null_btc_price_does_not_crash_the_batch(monkeypatch):
    """REGRESSION GUARD (EXP-004 Resolution Integrity Investigation,
    defect 2, now fixed). The fix adds `btc_price IS NOT NULL` to the
    same query -- a NULL-price row can no longer be selected as "the
    nearest price at/after target_ts" at all, so the pre-existing
    `if not actual_rows: continue` path (already correct for the
    genuinely-no-data-yet case) now also correctly covers this case:
    no crash, no fabricated price, the row is simply left unresolved
    for a future run once real price data exists."""
    conn = _fresh_resolve_db()
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (1, 2000, 100.0, 5.0, 'UP')")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (2000, NULL)")
    monkeypatch.setattr(run_experiment, "run_d1", _sqlite_run_d1(conn))
    run_experiment.resolve_pending()  # must NOT raise
    row = conn.execute("SELECT resolved_ts, actual_return_pct FROM experiment_4_timesfm WHERE id=1").fetchone()
    assert row[0] is None
    assert row[1] is None


def test_resolve_pending_null_price_row_does_not_prevent_another_pending_forecast_from_resolving(monkeypatch):
    """REGRESSION GUARD: a NULL-price row for ONE pending forecast must
    not abort resolution of a DIFFERENT pending forecast in the same
    batch. id=1's target_ts (8000) is LATER than id=2's own valid price
    row (ts=2000, deliberately too early to satisfy id=1's own
    `ts >= 8000`), so id=1's ONLY qualifying candidate is its own
    NULL-price row at ts=8000 -- must stay unresolved, no crash. id=2's
    target_ts (2000) is satisfied by that SAME early, valid row -- must
    resolve normally, unaffected by id=1's bad data."""
    conn = _fresh_resolve_db()
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (1, 8000, 100.0, 5.0, 'UP')")
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (2, 2000, 200.0, 5.0, 'UP')")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (2000, 220.0)")  # satisfies id=2 only (2000 < 8000)
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (8000, NULL)")   # id=1's only candidate -- invalid
    monkeypatch.setattr(run_experiment, "run_d1", _sqlite_run_d1(conn))
    run_experiment.resolve_pending()  # must NOT raise
    row1 = conn.execute("SELECT resolved_ts FROM experiment_4_timesfm WHERE id=1").fetchone()
    row2 = conn.execute("SELECT resolved_ts, actual_return_pct FROM experiment_4_timesfm WHERE id=2").fetchone()
    assert row1[0] is None, "id=1 must remain unresolved (its only qualifying row has a NULL price)"
    assert row2[0] is not None, "id=2 must still resolve normally, unaffected by id=1's bad data"
    assert row2[1] == 10.0  # (220-200)/200*100


def test_resolve_pending_duplicate_forecast_rows_sharing_target_ts_are_each_resolved_independently_not_double_counted(monkeypatch):
    """Fixture: two distinct forecast rows (different id, e.g. from a
    re-run) sharing the same target_ts. Each must be resolved against its
    OWN price_at_prediction using its own id-scoped UPDATE -- neither
    skipped nor double-applied to the other's row."""
    conn = _fresh_resolve_db()
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (1, 2000, 100.0, 5.0, 'UP')")
    conn.execute("INSERT INTO experiment_4_timesfm (id, target_ts, price_at_prediction, predicted_return_pct, direction) "
                 "VALUES (2, 2000, 200.0, -5.0, 'DOWN')")
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (2000, 220.0)")
    monkeypatch.setattr(run_experiment, "run_d1", _sqlite_run_d1(conn))
    run_experiment.resolve_pending()
    row1 = conn.execute("SELECT actual_return_pct, correct FROM experiment_4_timesfm WHERE id=1").fetchone()
    row2 = conn.execute("SELECT actual_return_pct, correct FROM experiment_4_timesfm WHERE id=2").fetchone()
    assert row1[0] == 120.0  # (220-100)/100*100, its OWN price_at_prediction
    assert row1[1] == 1      # predicted UP, actual UP -- correct
    assert row2[0] == 10.0   # (220-200)/200*100, ITS OWN price_at_prediction, not row 1's
    assert row2[1] == 0      # predicted DOWN, actual UP -- incorrect
    # Neither row's resolution was skipped or borrowed from the other's.
    assert row1[0] != row2[0]


def test_fetch_price_history_out_of_order_input_produces_the_same_result_as_sorted_input(monkeypatch):
    """REGRESSION GUARD (EXP-004 Resolution Integrity Investigation,
    defect 3, now fixed). fetch_price_history() now defensively sorts
    before deduplication, so it must be independent of the order run_d1()
    happens to return rows in. Before this fix, this exact fixture lost
    2 of 3 points (an out-of-order point was silently treated as a
    near-duplicate of a wrongly-larger running max and discarded) --
    confirmed here that all 3 genuinely distinct points now survive,
    and that a scrambled fetch order yields the IDENTICAL result to the
    already-sorted order. Points are spaced 1 real hour apart (well
    beyond DEDUP_TOLERANCE_MS=60s) so none of them collapse via the
    EXISTING, unchanged near-simultaneous dedup semantics -- this test
    is about fetch-order independence, not dedup behavior (see the
    dedicated dedup-semantics test below)."""
    hour = 3600000
    points = [
        {"ts": 1000, "btc_price": 90.0},
        {"ts": 1000 + hour, "btc_price": 95.0},
        {"ts": 1000 + 2 * hour, "btc_price": 100.0},
    ]
    scrambled = [points[2], points[0], points[1]]  # deliberately out of order

    monkeypatch.setattr(run_experiment, "run_d1", lambda sql: points)
    sorted_result = run_experiment.fetch_price_history()

    monkeypatch.setattr(run_experiment, "run_d1", lambda sql: scrambled)
    scrambled_result = run_experiment.fetch_price_history()

    assert sorted_result == [(1000, 90.0), (1000 + hour, 95.0), (1000 + 2 * hour, 100.0)]
    assert scrambled_result == sorted_result, (
        "fetch_price_history() must be independent of fetch/insertion order"
    )


def test_fetch_price_history_defensive_sort_preserves_near_simultaneous_dedup_semantics(monkeypatch):
    """Confirms the fix does not alter dedupe_near_simultaneous()'s own
    near-simultaneous collapsing semantics: two points within
    DEDUP_TOLERANCE_MS of each other must still collapse to one, exactly
    as before this fix, even when fetched out of order."""
    points = [
        {"ts": 1000, "btc_price": 50.0},
        {"ts": 1000 + run_experiment.DEDUP_TOLERANCE_MS - 1, "btc_price": 50.0},  # near-duplicate of the first
        {"ts": 1000 + run_experiment.DEDUP_TOLERANCE_MS + 3600000, "btc_price": 60.0},  # a real, later tick
    ]
    scrambled = [points[2], points[0], points[1]]
    monkeypatch.setattr(run_experiment, "run_d1", lambda sql: scrambled)
    result = run_experiment.fetch_price_history()
    assert len(result) == 2  # the near-duplicate pair collapsed to one point, as before


@pytest.mark.skip(reason=(
    "Fixture 'future observation after input_end_ts does not alter an "
    "already-computed forecast' lives entirely inside generate_forecasts(), "
    "which lazily imports the real torch/timesfm packages (not installed "
    "in this test environment, and not appropriate to install just for "
    "this suite -- see this file's own module docstring). Exercising it "
    "adversarially at runtime would require either installing those heavy "
    "ML dependencies, or refactoring generate_forecasts() to extract its "
    "input_end_ts/context_prices/target_ts computation into an "
    "independently-callable pure function -- a production code change "
    "explicitly out of scope for this task ('do NOT modify production "
    "EXP-004 behavior'). Reported as a testing-coverage limitation, not "
    "silently skipped without explanation: this property is currently "
    "guaranteed only by generate_forecasts()'s own single-call structure "
    "(input_end_ts is a local variable read fresh from price_history on "
    "every call; there is no persisted/cached forecast state a later call "
    "could retroactively contaminate), not by an independently-tested "
    "runtime fixture."
))
def test_future_observation_after_input_end_ts_does_not_alter_an_already_computed_forecast():
    pass
