import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prediction_staleness as ps  # noqa: E402


def _payload(status="FRESH", cron_rows=8, age=0.3):
    path = {"status": status, "cron_rows_24h": cron_rows}
    return {"ok": True, "coins": {c: {"price_data": {"latest_age_hours": age},
                                      "horizons": {"12": {"core": dict(path), "challenger": dict(path)},
                                                   "24": {"core": dict(path), "challenger": dict(path)}}} for c in ("BTC", "ETH", "LINK")}}


def test_healthy_payload_has_no_problems():
    assert ps.problems(_payload()) == []


def test_stale_status_is_reported_for_every_path():
    assert len(ps.problems(_payload(status="STALE"))) == 12


def test_missed_scheduled_runs_are_reported():
    assert ps.problems(_payload(cron_rows=6)) == []
    assert len(ps.problems(_payload(cron_rows=5))) == 12


def test_old_price_data_is_reported():
    assert len(ps.problems(_payload(age=4.5))) == 3
    assert len(ps.problems(_payload(age=None))) == 3


def test_bad_or_empty_payload_is_a_problem():
    assert ps.problems({"ok": False}) and ps.problems({"ok": True, "coins": {}})


def test_main_exit_code(tmp_path):
    f = tmp_path / "f.json"
    f.write_text(json.dumps(_payload()))
    assert ps.main([str(f)]) == 0
    f.write_text(json.dumps(_payload(status="NONE")))
    assert ps.main([str(f)]) == 1
