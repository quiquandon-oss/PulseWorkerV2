import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow_health as wh  # noqa: E402

NOW = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.timezone.utc)


def _run(hours_ago, conclusion="success", status="completed"):
    return {"created_at": (NOW - dt.timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"), "status": status, "conclusion": conclusion}


def test_recent_successes_are_healthy():
    assert wh.assess("w", [_run(i) for i in range(1, 30)], NOW, 3, 20) == []


def test_old_last_success_is_reported():
    assert "last success 5.0 h ago" in wh.assess("w", [_run(5), _run(6)], NOW, 3, None)[0]


def test_no_success_is_reported():
    assert "no successful run" in wh.assess("w", [_run(1, "failure")], NOW, 3, None)[0]
    assert "no successful run" in wh.assess("w", [], NOW, 3, None)[0]


def test_repeated_failures_are_reported_even_with_an_older_success():
    p = wh.assess("w", [_run(1, "failure"), _run(2, "failure"), _run(3, "cancelled"), _run(4)], NOW, 40, None)
    assert p == ["w: the latest 3 completed runs all failed"]


def test_running_runs_are_ignored_and_one_failure_is_tolerated():
    assert wh.assess("w", [_run(0, None, "in_progress"), _run(1, "failure"), _run(2)], NOW, 3, None) == []


def test_gaps_in_an_hourly_job_are_reported():
    runs = [_run(i) for i in range(1, 25, 2)]      # every second hour: 12 in 24 h
    assert wh.assess("w", runs, NOW, 3, 20) == ["w: 12 successful runs in the last 24 h (minimum 20)"]


def test_every_expected_workflow_file_exists():
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    assert all((root / wf).exists() for wf in wh.EXPECTED)
