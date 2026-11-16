"""Derived EXP-008 ledger: verbatim copy of forecast and resolution fields, pending vs resolved, seal exclusion,
population label, unreadable files reported (never fatal), immutable source files untouched."""
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("exp008_build_ledger", HERE / "build_ledger.py")
bl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bl)

H = 3600000
NOV2 = int(datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp() * 1000)
OCT20 = int(datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp() * 1000)
SEALED = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _fc(origin, h=12, **over):
    d = {"id": f"BTC_{h}h_{origin}", "coin": "BTC", "horizon_hours": h, "issued_ts": origin + 60000, "input_end_ts": origin,
         "target_ts": origin + h * H, "price_at_prediction": 64471.0, "forecast_median": 64176.84765625,
         "forecast_q10": 62056.078125, "forecast_q90": 66853.3828125, "quantile_levels": [0.1, 0.5, 0.9],
         "predicted_return_pct": -0.4562552833832266, "direction": "DOWN", "persistence_direction": "DOWN",
         "last_tick_return_pct": -0.42, "model_id": "amazon/chronos-2", "model_revision": "29ec376",
         "anchor": {"source": "experiment_4_timesfm", "timesfm_ts": origin + 5000, "timesfm_target_ts": origin + h * H}}
    d.update(over)
    return d


def _write(store, rel, obj):
    p = store / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return p


def test_merges_forecasts_with_resolutions_verbatim_and_marks_pending(tmp_path):
    s = tmp_path / "exp008_chronos"
    _write(s, "forecasts/BTC/12h/a.json", _fc(NOV2))
    _write(s, "resolutions/BTC/12h/a.json", {"actual_price": 65324.0, "actual_return_pct": 0.14871142318365094,
                                             "actual_direction": "UP", "correct": 0, "persistence_correct": 0,
                                             "absolute_error": 0.6, "signed_error": -0.6, "resolved_ts": NOV2 + 13 * H})
    _write(s, "forecasts/BTC/24h/b.json", _fc(NOV2, h=24))
    led = bl.build(s, now_ms=NOV2 + 20 * H)
    assert led["format"] == "exp008-chronos-ledger-v1" and led["derived"] is True
    r12, r24 = led["forecasts"]
    assert r12["status"] == "resolved" and r24["status"] == "pending"
    assert r12["forecast_median"] == 64176.84765625 and r12["actual_return_pct"] == 0.14871142318365094   # no rounding
    assert r12["forecast_file"] == "forecasts/BTC/12h/a.json" and r12["resolution_file"] == "resolutions/BTC/12h/a.json"
    assert r12["timesfm_ts"] == NOV2 + 5000 and r12["population"] == "prospective"
    assert "actual_price" not in r24 and "resolution_file" not in r24


def test_sealed_origins_and_their_outcomes_are_never_read_into_the_ledger(tmp_path):
    s = tmp_path / "exp008_chronos"
    _write(s, "forecasts/BTC/12h/old.json", _fc(SEALED))
    _write(s, "resolutions/BTC/12h/old.json", {"actual_price": 1.0, "correct": 1})
    _write(s, "forecasts/BTC/12h/oct.json", _fc(OCT20))
    led = bl.build(s, now_ms=NOV2)
    assert led["excluded_sealed"] == 1
    assert [r["input_end_ts"] for r in led["forecasts"]] == [OCT20]
    assert led["forecasts"][0]["population"] == "post_seal_descriptive"
    assert all("actual_price" not in r and "correct" not in r for r in led["forecasts"])   # the sealed outcome is not copied


def test_unreadable_files_are_reported_not_fatal_and_sources_are_not_modified(tmp_path):
    s = tmp_path / "exp008_chronos"
    good = _write(s, "forecasts/BTC/12h/a.json", _fc(NOV2))
    _write(s, "forecasts/BTC/12h/bad.json", "{not json")
    _write(s, "forecasts/BTC/24h/b.json", _fc(NOV2, h=24))
    _write(s, "resolutions/BTC/24h/b.json", "{broken")
    _write(s, "runs/2026-11-02T120000Z.json", {"run_ts": NOV2, "run_id": "1", "forecasts": [{"id": "x"}], "resolved": 0,
                                              "pending": 1, "errors": [], "code_sha": "abc"})
    before = hashlib.sha256(good.read_bytes()).hexdigest()
    assert bl.main(["--store", str(s)]) == 0
    led = json.loads((s / "derived" / "ledger.json").read_text())
    assert {x["file"] for x in led["skipped"]} == {"forecasts/BTC/12h/bad.json", "resolutions/BTC/24h/b.json"}
    assert [r["status"] for r in led["forecasts"]] == ["pending", "resolution_unreadable"]
    assert led["recent_runs"] == [{"file": "runs/2026-11-02T120000Z.json", "run_ts": NOV2, "run_id": "1", "forecasts_made": 1,
                                   "resolved": 0, "pending": 1, "errors": 0, "code_sha": "abc"}]
    assert hashlib.sha256(good.read_bytes()).hexdigest() == before
    assert not (s / "derived" / "ledger.tmp").exists()


def test_empty_store_builds_an_empty_ledger(tmp_path):
    led = bl.build(tmp_path / "exp008_chronos", now_ms=NOV2)
    assert led["forecasts"] == [] and led["skipped"] == [] and led["recent_runs"] == []
