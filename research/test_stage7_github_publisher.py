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


def _successful_run_recorder(branch=pub.ALLOWED_PUBLISH_BRANCH):
    """Fakes `git rev-parse --abbrev-ref HEAD` as returning `branch`
    (the allowed publish branch by default) and every other git command
    as succeeding with empty output -- the shape publish_request_file's
    own branch guard now expects as its very first call."""
    calls = []

    def run(args, cwd):
        calls.append((args, cwd))
        if args[:2] == ["git", "rev-parse"]:
            return FakeResult(0, branch + "\n", "")
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
    assert len(calls) == 4  # rev-parse (branch guard), add, commit, push
    assert calls[0][0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    assert calls[1][0][:2] == ["git", "add"]
    assert calls[3][0] == ["git", "push"]


def test_publish_request_file_is_idempotent_on_identical_content(tmp_path):
    req = _request()
    run, calls = _successful_run_recorder()
    first = pub.publish_request_file(req, str(tmp_path), run=run)
    assert first["published"] is True
    second = pub.publish_request_file(req, str(tmp_path), run=run)
    assert second["published"] is True
    assert second["skipped_unchanged"] is True
    # first call: rev-parse + add + commit + push (4); second call: only
    # the branch-guard rev-parse (1) -- no additional add/commit/push on
    # the second, unchanged publish.
    assert len(calls) == 5


def test_publish_request_file_never_overwrites_different_existing_content(tmp_path):
    req = _request()
    path = tmp_path / "research" / "stage7_requests"
    path.mkdir(parents=True)
    (path / "stage7-req-1-1.json").write_text('{"different": "content"}\n')
    run, calls = _successful_run_recorder()
    result = pub.publish_request_file(req, str(tmp_path), run=run)
    assert result["published"] is False
    assert "refusing to overwrite" in result["error"]
    # only the branch-guard rev-parse call happens -- no add/commit/push
    # attempted when refusing to overwrite.
    assert len(calls) == 1
    assert calls[0][0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    # the file on disk must be untouched
    assert (path / "stage7-req-1-1.json").read_text() == '{"different": "content"}\n'


def test_publish_request_file_git_failure_is_retryable_not_a_crash(tmp_path):
    req = _request()

    def failing_run(args, cwd):
        if args[:2] == ["git", "rev-parse"]:
            return FakeResult(0, pub.ALLOWED_PUBLISH_BRANCH + "\n", "")
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
        if args[:2] == ["git", "rev-parse"]:
            return FakeResult(0, pub.ALLOWED_PUBLISH_BRANCH + "\n", "")
        if args[1] == "commit":
            return FakeResult(1, "", "nothing to commit")
        return FakeResult(0, "", "")

    result = pub.publish_request_file(req, str(tmp_path), run=run)
    assert result["published"] is False
    assert len(calls) == 3  # rev-parse, add, commit -- push never attempted


# =====================================================================
# Copilot-audit remediation: this publisher must never commit or push to
# main. The branch is resolved and checked BEFORE the request file is
# written to disk or any git mutation (add/commit/push) is attempted.
# =====================================================================

def test_publish_request_file_rejects_main_branch_before_any_git_mutation(tmp_path):
    req = _request()
    run, calls = _successful_run_recorder(branch="main")
    result = pub.publish_request_file(req, str(tmp_path), run=run)
    assert result["published"] is False
    assert "main" in result["error"]
    assert "forbidden" in result["error"]
    # only the read-only rev-parse call happened -- no add/commit/push,
    # i.e. zero git mutation.
    assert len(calls) == 1
    assert calls[0][0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    # no file was written either.
    written = tmp_path / "research" / "stage7_requests" / "stage7-req-1-1.json"
    assert not written.exists()


def test_publish_request_file_rejects_master_branch_before_any_git_mutation(tmp_path):
    req = _request()
    run, calls = _successful_run_recorder(branch="master")
    result = pub.publish_request_file(req, str(tmp_path), run=run)
    assert result["published"] is False
    assert "master" in result["error"]
    assert len(calls) == 1  # rev-parse only


def test_publish_request_file_rejects_arbitrary_other_branch(tmp_path):
    req = _request()
    run, calls = _successful_run_recorder(branch="some-other-feature-branch")
    result = pub.publish_request_file(req, str(tmp_path), run=run)
    assert result["published"] is False
    assert "some-other-feature-branch" in result["error"]
    assert len(calls) == 1  # rev-parse only, no git mutation
    written = tmp_path / "research" / "stage7_requests" / "stage7-req-1-1.json"
    assert not written.exists()


def test_publish_request_file_rejects_when_current_branch_cannot_be_resolved(tmp_path):
    req = _request()

    def failing_rev_parse(args, cwd):
        return FakeResult(128, "", "fatal: not a git repository")

    result = pub.publish_request_file(req, str(tmp_path), run=failing_rev_parse)
    assert result["published"] is False
    assert "not the allowed publish branch" in result["error"]
    written = tmp_path / "research" / "stage7_requests" / "stage7-req-1-1.json"
    assert not written.exists()


def test_publish_request_file_succeeds_on_the_allowed_staging_branch(tmp_path):
    req = _request()
    run, calls = _successful_run_recorder(branch=pub.ALLOWED_PUBLISH_BRANCH)
    result = pub.publish_request_file(req, str(tmp_path), run=run)
    assert result["published"] is True


def test_resolve_current_branch_returns_stripped_branch_name(tmp_path):
    def run(args, cwd):
        return FakeResult(0, "claude/stage7-research-pipeline\n", "")
    assert pub.resolve_current_branch(str(tmp_path), run) == "claude/stage7-research-pipeline"


def test_resolve_current_branch_returns_none_on_failure(tmp_path):
    def run(args, cwd):
        return FakeResult(128, "", "fatal: not a git repository")
    assert pub.resolve_current_branch(str(tmp_path), run) is None
