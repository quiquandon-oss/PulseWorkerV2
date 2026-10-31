"""EXP-008 Chronos-2 runner and evaluator: anchoring to EXP-004 origins, no look-ahead, immutable
store, duplicate prevention, resolution, failure handling, and the real Chronos-2 pipeline code with a
tiny randomly initialised model (no Hub download)."""
import ast
import csv
import json
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    """Loads a module of this folder under a unique name: exp004-timesfm also has a run_experiment.py,
    and CI runs both folders in one pytest session."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rx = _load("exp008_run_experiment", "run_experiment.py")
ev = _load("exp008_evaluate", "evaluate.py")

H = 3600000
T0 = 1792000000000 - (1792000000000 % (3 * H))     # a 3h grid point after the seal end (2026-10-14)


class FakeD1:
    """SQLite stand-in for production D1 with the two tables the runner reads. Records every query."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE btc_data (ts INTEGER, btc_price REAL)")
        self.db.execute("CREATE TABLE experiment_4_timesfm (coin TEXT, horizon_hours INTEGER, ts INTEGER, "
                        "input_end_ts INTEGER, target_ts INTEGER, price_at_prediction REAL)")
        self.queries = []

    def __call__(self, sql):
        self.queries.append(sql)
        assert sql.lstrip().upper().startswith("SELECT"), "the runner must only read D1"
        return [dict(r) for r in self.db.execute(sql).fetchall()]

    def prices(self, points):
        self.db.executemany("INSERT INTO btc_data VALUES (?, ?)", points)

    def anchor(self, h, input_end_ts, price, ts=None):
        self.db.execute("INSERT INTO experiment_4_timesfm VALUES ('BTC', ?, ?, ?, ?, ?)",
                        (h, ts or input_end_ts + 2 * H, input_end_ts, input_end_ts + h * H, price))


def series(n=200, end=T0, start_price=100000.0, step=10.0):
    """n points on a 3h grid ending at `end`, each with a duplicate write 200 ms later."""
    pts = []
    for i in range(n):
        ts = end - (n - 1 - i) * 3 * H
        p = start_price + i * step
        pts += [(ts, p), (ts + 200, p + 0.5)]
    return pts


class FakePipeline:
    """Returns quantiles whose median is `factor` x the last context value at every step."""
    def __init__(self, factor=1.01):
        self.factor, self.calls = factor, []

    def predict_quantiles(self, inputs, prediction_length, quantile_levels):
        import numpy as np
        ctx = np.asarray(inputs[0])
        self.calls.append((len(ctx), prediction_length, list(quantile_levels)))
        last = float(ctx[-1]) * self.factor
        q = np.tile(np.array([last * 0.99, last, last * 1.01], dtype="float32"), (1, prediction_length, 1))
        return [q], [q[..., 1]]


def run(store, d1, pipe, now):
    return rx.main(["--store", str(store)], d1=d1, pipeline_loader=lambda: pipe, now_ms=now)


def files(store, kind):
    return sorted((Path(store) / kind).rglob("*.json"))


# ------------------------------------------------------------------ input preparation

def _exp004_functions():
    src = (HERE.parent / "exp004-timesfm" / "run_experiment.py").read_text()
    tree = ast.parse(src)
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("dedupe_near_simultaneous", "median_delta_ms")]
    consts = [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("DEDUP_TOLERANCE_MS", "RECENT_CADENCE_WINDOW") for t in n.targets)]
    ns = {}
    exec(compile(ast.Module(body=consts + keep, type_ignores=[]), "exp004", "exec"), ns)
    return ns


def test_input_preparation_is_identical_to_exp004():
    ns = _exp004_functions()
    assert ns["DEDUP_TOLERANCE_MS"] == rx.DEDUP_TOLERANCE_MS and ns["RECENT_CADENCE_WINDOW"] == rx.RECENT_CADENCE_WINDOW
    import random
    rng = random.Random(1)
    for _ in range(50):
        pts, ts = [], 0
        for _ in range(rng.randint(2, 150)):
            ts += rng.choice([150, 30000, 61000, 3 * H, H])
            pts.append((ts, rng.random()))
        assert ns["dedupe_near_simultaneous"](pts) == rx.dedupe_near_simultaneous(pts)
        d = rx.dedupe_near_simultaneous(pts)
        if len(d) > 1:
            assert ns["median_delta_ms"](d) == rx.median_delta_ms(d)


# ------------------------------------------------------------------ forecasting

def test_forecasts_at_the_exp004_origin_with_no_look_ahead(tmp_path):
    d1 = FakeD1()
    pts = series()
    d1.prices(pts + [(T0 + 3 * H, 999999.0)])                 # a later point that must not be used
    d1.anchor(12, T0, pts[-2][1]); d1.anchor(24, T0, pts[-2][1])
    pipe = FakePipeline(1.01)
    assert run(tmp_path, d1, pipe, T0 + 3 * H) == 0
    fcs = [json.loads(p.read_text()) for p in files(tmp_path, "forecasts")]
    assert sorted(f["horizon_hours"] for f in fcs) == [12, 24]
    for f in fcs:
        assert f["input_end_ts"] == T0 and f["target_ts"] == T0 + f["horizon_hours"] * H
        assert f["price_at_prediction"] == pts[-2][1]           # deduplicated: first write of the tick
        assert f["direction"] == "UP" and f["predicted_return_pct"] == pytest.approx(1.0, rel=1e-4)
        assert f["context_length"] == 128 and f["forecast_horizon_steps"] == f["horizon_hours"] // 3
        assert f["persistence_direction"] == "UP" and f["anchor"]["source"] == "experiment_4_timesfm"
    assert all(c[0] == 128 for c in pipe.calls)
    assert sum("FROM btc_data WHERE ts <=" in q for q in d1.queries) == 1   # one history read for both horizons
    assert not any("999999" in json.dumps(f) for f in fcs)


def test_rerun_never_duplicates_or_overwrites(tmp_path):
    d1 = FakeD1(); pts = series(); d1.prices(pts)
    d1.anchor(12, T0, pts[-2][1]); d1.anchor(24, T0, pts[-2][1])
    run(tmp_path, d1, FakePipeline(1.01), T0 + H)
    before = {p: p.read_bytes() for p in files(tmp_path, "forecasts")}
    pipe = FakePipeline(0.5)
    assert run(tmp_path, d1, pipe, T0 + 2 * H) == 0
    assert pipe.calls == []                                    # model not even loaded
    assert {p: p.read_bytes() for p in files(tmp_path, "forecasts")} == before


def test_refuses_an_unpaired_forecast_when_the_context_does_not_match_exp004(tmp_path):
    d1 = FakeD1(); pts = series(); d1.prices(pts)
    d1.anchor(12, T0, 123.0)                                   # TimesFM saw a different price
    assert run(tmp_path, d1, FakePipeline(), T0 + H) == 1
    assert files(tmp_path, "forecasts") == []
    rec = json.loads(files(tmp_path, "runs")[0].read_text())
    assert any("refusing an unpaired forecast" in e for e in rec["errors"])


def test_stale_or_missing_anchor_is_not_backfilled(tmp_path):
    d1 = FakeD1(); pts = series(); d1.prices(pts)
    d1.anchor(12, T0, pts[-2][1])
    assert run(tmp_path, d1, FakePipeline(), T0 + 49 * H) == 0
    rec = json.loads(files(tmp_path, "runs")[0].read_text())
    assert files(tmp_path, "forecasts") == []
    assert any("older than 48h" in n for n in rec["notes"]) and any("24h: no EXP-004" in n for n in rec["notes"])


def test_model_failure_writes_no_forecast_and_fails_the_run(tmp_path):
    d1 = FakeD1(); pts = series(); d1.prices(pts)
    d1.anchor(12, T0, pts[-2][1])

    def broken():
        raise RuntimeError("hub unreachable")
    assert rx.main(["--store", str(tmp_path)], d1=d1, pipeline_loader=broken, now_ms=T0 + H) == 1
    assert files(tmp_path, "forecasts") == []


def test_load_pipeline_retries_then_fails(monkeypatch):
    calls, sleeps = [], []

    class Boom:
        @staticmethod
        def from_pretrained(*a, **k):
            calls.append(k); raise OSError("503")
    monkeypatch.setitem(sys.modules, "chronos", type(sys)("chronos"))
    sys.modules["chronos"].Chronos2Pipeline = Boom
    with pytest.raises(RuntimeError):
        rx.load_pipeline(attempts=3, sleep=sleeps.append)
    assert len(calls) == 3 and sleeps == [2, 4] and calls[0]["device_map"] == "cpu"


# ------------------------------------------------------------------ resolution

def test_resolution_uses_the_first_price_at_or_after_target(tmp_path):
    d1 = FakeD1(); pts = series(); d1.prices(pts)
    d1.anchor(12, T0, pts[-2][1])
    run(tmp_path, d1, FakePipeline(1.01), T0 + H)
    price0 = pts[-2][1]
    d1.prices([(T0 + 12 * H - 1, price0 * 2), (T0 + 12 * H + 5000, price0 * 0.99), (T0 + 12 * H + 9000, price0 * 3)])
    assert run(tmp_path, d1, FakePipeline(), T0 + 13 * H) == 0
    res = json.loads(files(tmp_path, "resolutions")[0].read_text())
    assert res["actual_direction"] == "DOWN" and res["correct"] == 0 and res["persistence_correct"] == 0
    assert res["resolution_price_ts"] == T0 + 12 * H + 5000
    assert res["actual_return_pct"] == pytest.approx(-1.0)


def test_unresolvable_forecast_stays_pending(tmp_path):
    d1 = FakeD1(); pts = series(); d1.prices(pts)
    d1.anchor(12, T0, pts[-2][1])
    run(tmp_path, d1, FakePipeline(), T0 + H)
    run(tmp_path, d1, FakePipeline(), T0 + 13 * H)            # no price after target yet
    assert files(tmp_path, "resolutions") == []


# ------------------------------------------------------------------ real Chronos-2 code, tiny random model

def test_real_chronos2_pipeline_shapes_with_a_tiny_random_model():
    torch = pytest.importorskip("torch")
    chronos = pytest.importorskip("chronos")
    from chronos.chronos2.config import Chronos2CoreConfig
    from chronos.chronos2.model import Chronos2Model
    torch.manual_seed(0)
    qs = [round(0.05 * i, 2) for i in range(1, 20)]
    cfg = Chronos2CoreConfig(d_model=32, d_ff=64, num_layers=1, num_heads=2, d_kv=16, dropout_rate=0.0,
                             chronos_config={"context_length": 256, "input_patch_size": 16, "output_patch_size": 16,
                                             "input_patch_stride": 16, "quantiles": qs, "max_output_patches": 4,
                                             "use_reg_token": True})
    pipe = chronos.Chronos2Pipeline(Chronos2Model(cfg).eval())
    median, q10, q90 = rx.forecast_point(pipe, [100000.0 + i for i in range(128)], 4)
    # random weights: only the plumbing is checked (finite floats from the final step), not quantile order
    import math
    assert all(isinstance(v, float) and math.isfinite(v) for v in (median, q10, q90))


# ------------------------------------------------------------------ evaluation

def test_evaluate_pairs_on_origin_and_excludes_mismatches(tmp_path):
    d1 = FakeD1(); pts = series(); d1.prices(pts)
    d1.anchor(12, T0, pts[-2][1])
    run(tmp_path, d1, FakePipeline(1.01), T0 + H)
    d1.prices([(T0 + 12 * H + 1000, pts[-2][1] * 1.02)])
    run(tmp_path, d1, FakePipeline(), T0 + 13 * H)
    tfm = tmp_path / "tfm12.json"
    tfm.write_text(json.dumps({"forecasts": [{"horizon_hours": 12, "input_end_ts": T0, "correct": 0, "actual_direction": "UP"}]}))
    knn = tmp_path / "knn.csv"
    with open(knn, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["ts", "horizon_hours", "p_up"])
        w.writerow([T0 + 60000, 12, 0.7]); w.writerow([1700000000000, 12, 0.1])   # second row: sealed window, dropped
    rep = ev.evaluate(tmp_path, [tfm], knn)["h12"]
    assert rep["n_shared"] == 1
    a = rep["approaches"]
    assert a["chronos"]["correct"] == 1 and a["timesfm"]["correct"] == 0 and a["knn"]["correct"] == 1
    assert a["always_up"]["correct"] == 1 and a["persistence"]["correct"] == 1
    assert rep["paired_vs_chronos"]["timesfm"] == {"n": 1, "chronos_only_right": 1, "other_only_right": 0,
                                                   "diff_pts": 100.0, "mcnemar_exact_p": 1.0}
    tfm.write_text(json.dumps({"forecasts": [{"horizon_hours": 12, "input_end_ts": T0, "correct": 1, "actual_direction": "DOWN"}]}))
    rep = ev.evaluate(tmp_path, [tfm], knn)["h12"]
    assert rep["n_shared"] == 0 and rep["excluded"][0][1] == "ground truth differs from TimesFM"
