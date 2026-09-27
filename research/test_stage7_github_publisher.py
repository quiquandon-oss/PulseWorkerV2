"""
Tests for research/stage7_github_publisher.py. Uses a real temp
directory for file writes (fast, no cleanup needed beyond pytest's own
tmp_path fixture) and a fake `run` callable in place of subprocess.run
-- no real git invocation, no network.

Run with: python3 -m pytest research/test_stage7_github_publisher.py -v
"""
import json
import sys
import os
from collections import namedtuple

sys.path.insert(0, os.path.dirname(__file__))
import stage7_github_publisher as pub  # noqa: E402

FakeResult = namedtuple("FakeResult", ["returncode", "stdout", "stderr"])


def _request(request_id="stage7-req-1-1", event_id=1):
    return {
        "request_id": request_id,
        "event_id": event_id,
        "created_ts": 1000,
        "historical_cutoff_ts": 999,
        "sufficiency_status": "INSUFFICIENT_EVIDENCE",
        "reasons": ["no evidence rows exist"],
        "questions": ["what happened?"],
        "missing_categories": ["primary_reporting"],
        "event": {"event_id": event_id, "coin": "BTC", "category": "LARGE_MOVE", "event_ts": 950},
        "evidence_snapshot": [],
    }


def _successful_run_recorder():
    calls = []

    def run(args, cwd):
        calls.append((args, cwd))
        return FakeResult(0, "", "")
    return run, calls


def test_build_request_id_is_deterministic():
    assert pub.build_request_id(5, attempt=1) == pub.build_request_id(5, attempt=1)
    assert pub.build_request_id(5, attempt=1) != pub.build_request_id(5, attempt=2)
    assert pub.build_request_id(5, attempt=1) != pub.build_request_id(6, attempt=1)


def test_request_file_path_uses_request_id():
    assert pub.request_file_path("stage7-req-1-1") == "research/stage7_requests/stage7-req-1-1.json"


def test_build_request_file_content_includes_required_fields():
    req = _request()
    content = pub.build_request_file_content(req)
    assert content["request_id"] == "stage7-req-1-1"
    assert content["event_id"] == 1
    assert content["sufficiency_status"] == "INSUFFICIENT_EVIDENCE"
    assert content["questions"] == ["what happened?"]
    assert "research_prompt" in content and isinstance(content["research_prompt"], str)


def test_research_prompt_respects_historical_cutoff_and_asks_for_sentiment_classification():
    req = _request()
    prompt = pub.build_research_prompt(req)
    assert "999" in prompt  # the actual cutoff value, not a placeholder
    assert "POSITIVE" in prompt and "NEGATIVE" in prompt and "MIXED" in prompt and "INDETERMINATE" in prompt
    assert "no evidence rows exist" in prompt
    assert "what happened?" in prompt


def test_publish_request_file_writes_and_commits(tmp_path):
    req = _request()
    run, calls = _successful_run_recorder()
    result = pub.publish_request_file(req, str(tmp_path), run=run)
    assert result["published"] is True
    assert result["skipped_unchanged"] is False
    written = tmp_path / "research" / "stage7_requests" / "stage7-req-1-1.json"
    assert written.exists()
    saved = json.loads(written.read_text())
    assert saved["request_id"] == "stage7-req-1-1"
    assert len(calls) == 3  # add, commit, push
    assert calls[0][0][:2] == ["git", "add"]
    assert calls[2][0] == ["git", "push"]


def test_publish_request_file_is_idempotent_on_identical_content(tmp_path):
    req = _request()
    run, calls = _successful_run_recorder()
    first = pub.publish_request_file(req, str(tmp_path), run=run)
    assert first["published"] is True
    second = pub.publish_request_file(req, str(tmp_path), run=run)
    assert second["published"] is True
    assert second["skipped_unchanged"] is True
    assert len(calls) == 3  # no additional git calls on the second, unchanged publish


def test_publish_request_file_never_overwrites_different_existing_content(tmp_path):
    req = _request()
    path = tmp_path / "research" / "stage7_requests"
    path.mkdir(parents=True)
    (path / "stage7-req-1-1.json").write_text('{"different": "content"}\n')
    run, calls = _successful_run_recorder()
    result = pub.publish_request_file(req, str(tmp_path), run=run)
    assert result["published"] is False
    assert "refusing to overwrite" in result["error"]
    assert calls == []  # no git call attempted when refusing to overwrite
    # the file on disk must be untouched
    assert (path / "stage7-req-1-1.json").read_text() == '{"different": "content"}\n'


def test_publish_request_file_git_failure_is_retryable_not_a_crash(tmp_path):
    req = _request()

    def failing_run(args, cwd):
        return FakeResult(1, "", "push rejected: network error")

    result = pub.publish_request_file(req, str(tmp_path), run=failing_run)
    assert result["published"] is False
    assert "network error" in result["error"]
    # the file was still written locally even though the push failed --
    # a retry (which finds identical content) must succeed without
    # rewriting it.
    written = tmp_path / "research" / "stage7_requests" / "stage7-req-1-1.json"
    assert written.exists()


def test_publish_request_file_stops_at_first_failing_git_step(tmp_path):
    req = _request()
    calls = []

    def run(args, cwd):
        calls.append(args)
        if args[1] == "commit":
            return FakeResult(1, "", "nothing to commit")
        return FakeResult(0, "", "")

    result = pub.publish_request_file(req, str(tmp_path), run=run)
    assert result["published"] is False
    assert len(calls) == 2  # add, commit -- push never attempted
