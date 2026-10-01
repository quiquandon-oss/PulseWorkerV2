"""
Stage 7 -- Step E: publish a research request as a versioned file in this
repository, using the SAME plain-git checkout/commit/push pattern already
proven in .github/workflows/ai-capital-rotation-research.yml (the GitHub
Actions ambient GITHUB_TOKEN, not a new PAT, not Octokit -- there is no
existing Octokit usage anywhere in this project, per audit, and no new
dependency is warranted for one write path).

File content and path are pure/deterministic (testable without touching
disk or git). The actual `git` invocations are isolated behind an
injectable `run` callable (defaulting to subprocess.run), matching this
project's established injectable-fetcher testing convention (see e.g.
data_sources.py in the CryptoPulse V1 research module) -- tests never
shell out to real git.
"""

import json
import os
import subprocess

REQUEST_DIR = "research/stage7_requests"
SCHEMA_VERSION = "stage7-request-v1"

# The ONLY branch this publisher will ever commit/push to. Named
# explicitly (never "the current branch" / "whatever HEAD is") so a
# misconfigured checkout -- a detached HEAD, a checkout of main, a
# workflow accidentally triggered on the wrong ref -- can never result in
# an automated push to main. See resolve_current_branch()/
# publish_request_file() below: the branch is checked BEFORE any file is
# staged, committed, or pushed, and "main" is rejected by name even if it
# were ever passed as the allowed target by mistake.
#
# This is the REVIEWED Stage 7 branch -- the same one the pipeline and staging-deploy workflows are bound to
# (stage7-research-pipeline/test_branch_binding.py asserts all of them agree). It used to name the older
# claude/stage7-research-pipeline branch, which made publishing impossible from the reviewed branch. Published
# request files land only under research/stage7_requests/, and both workflows accept those as the sole
# permitted change after the reviewed commit.
ALLOWED_PUBLISH_BRANCH = "claude/stage7-human-controlled-workflow"
FORBIDDEN_PUBLISH_BRANCHES = ("main", "master")

# A FAILED_RETRYABLE request is retried once per scheduled run (see
# run_stage7.py's own retry_stage7_request_publish()) until it either
# succeeds or this many total attempts (the original attempt at request-
# creation time, plus every retry) have been made. Kept small and
# explicit: a genuinely broken publish path (a revoked GITHUB_TOKEN, a
# newly-protected branch) should surface as FAILED_PERMANENT -- needing a
# human -- well before it could run up meaningful CI cost retrying forever.
MAX_PUBLISH_ATTEMPTS = 5


def decide_retry_outcome(current_attempts, published, max_attempts=MAX_PUBLISH_ATTEMPTS):
    """Pure. Decides the next status/attempt-count for a stage7_research_
    requests row after one publish attempt (the first, at request-creation
    time, or a later retry of a FAILED_RETRYABLE row).

    `current_attempts` is the row's publish_attempts value BEFORE this
    attempt. `published` is whether THIS attempt's publish_request_file()
    call reported success (including an idempotent "already published,
    unchanged" outcome -- both count as success here).

    Returns (new_status, new_attempts):
    - published=True -> ("RESEARCH_REQUEST_PUBLISHED", current_attempts + 1)
    - published=False and the incremented count has now reached
      max_attempts -> ("FAILED_PERMANENT", current_attempts + 1) -- never
      retried again automatically; needs manual investigation.
    - published=False otherwise -> ("FAILED_RETRYABLE", current_attempts + 1)
      -- eligible for another attempt on a subsequent scheduled run.
    """
    new_attempts = current_attempts + 1
    if published:
        return "RESEARCH_REQUEST_PUBLISHED", new_attempts
    if new_attempts >= max_attempts:
        return "FAILED_PERMANENT", new_attempts
    return "FAILED_RETRYABLE", new_attempts


def build_request_id(event_id, attempt=1):
    """Deterministic, not random -- repeated scheduled executions for the
    same event/attempt must compute the identical ID, which is what
    makes the whole publish step idempotent at the filesystem/git layer
    in addition to the DB partial-unique-index layer."""
    return f"stage7-req-{event_id}-{attempt}"


def request_file_path(request_id):
    return f"{REQUEST_DIR}/{request_id}.json"


def build_request_file_content(request):
    """`request` is a stage7_research_requests row (as a dict) plus the
    original event dict and evidence rows needed for a self-contained
    research prompt -- everything Step D lists, nothing more (no bulk
    historical market data, per the explicit instruction not to use
    GitHub as an archive for that).

    CANONICAL PROMPT SOURCE (confirmed human-controlled operating model):
    every request created via the app's candidate-review flow already has
    an exact `prompt_text` stored on it -- generated ONCE, server-side, by
    worker.js's buildStage7ResearchPromptText() at the moment the human's
    "Create research requests" action ran, and shown to that human
    verbatim in the UI. When present, that stored text is used here
    UNCHANGED -- never regenerated -- so the file this function publishes
    to GitHub is byte-identical to what the human actually saw and copied,
    and there is exactly ONE prompt implementation in the whole system,
    never two independently-maintained ones that could drift apart.
    build_research_prompt() below is kept only as a fallback for a legacy
    request that predates this field (request.get("prompt_text") is
    None/absent) -- no such request has ever been created against a real
    database as of this change."""
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request["request_id"],
        "event_id": request["event_id"],
        "created_ts": request["created_ts"],
        "historical_cutoff_ts": request["historical_cutoff_ts"],
        "sufficiency_status": request["sufficiency_status"],
        "reasons": request["reasons"],
        "questions": request["questions"],
        "missing_categories": request["missing_categories"],
        "event": request["event"],
        "evidence_snapshot": request["evidence_snapshot"],
        "research_prompt": request.get("prompt_text") or build_research_prompt(request),
    }


def build_research_prompt(request):
    """The actual text Olivier copies into Claude/ChatGPT/Gemini/Grok
    (Step F.2/G). Deterministic template, filled only with real request
    fields -- never invented content, never a claim that research has
    already happened."""
    event = request["event"]
    lines = [
        f"Research this market event for {event.get('coin', 'BTC')} using your actual internet-search "
        f"capability. Event category: {event.get('category')}. Event timestamp (ms): {event.get('event_ts')}.",
        f"Respect this historical cutoff: do not use information published after {request['historical_cutoff_ts']} "
        "(ms since epoch) -- reconstruct only what was knowable at or before that time.",
        "",
        "Why existing evidence is insufficient:",
        *[f"- {r}" for r in request["reasons"]],
        "",
        "Questions to answer:",
        *[f"- {q}" for q in request["questions"]],
        "",
        "For your findings, explicitly state: (1) what existing sources failed to explain, (2) relevant "
        "primary sources and independent reporting with verifiable URLs/publishers/dates, (3) the event's "
        "relevance to the affected asset and broader market, (4) a plausible transmission mechanism without "
        "presenting speculation as fact, (5) whether the implications are POSITIVE, NEGATIVE, MIXED, or "
        "INDETERMINATE, (6) contradictory evidence and unresolved questions, and (7) your confidence and "
        "limitations. Distinguish sourced facts, third-party claims, analysis, and uncertainty. Do not "
        "fabricate citations or claim a source was checked if it was not.",
    ]
    return "\n".join(lines)


def resolve_current_branch(repo_dir, run):
    """Returns the current branch name via `git rev-parse --abbrev-ref
    HEAD`, using the SAME injectable `run` callable publish_request_file's
    own git steps use -- tests fake this exactly like every other git call
    here, never a real subprocess. Returns None (never raises, never
    guesses a branch name) if the command fails, e.g. a repo_dir that
    isn't actually a git checkout -- the caller treats None the same as
    any other not-the-allowed-branch value: refuse to publish."""
    result = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def publish_request_file(request, repo_dir, run=None):
    """Writes the request file and commits+pushes it via plain git.

    `run(args, cwd)` defaults to a thin subprocess.run wrapper; tests
    inject a fake that records calls and returns canned
    CompletedProcess-like results, so this function is fully testable
    without a real git repo or network access.

    BRANCH GUARD (Copilot-audit remediation): the current branch is
    resolved and checked against ALLOWED_PUBLISH_BRANCH FIRST, before the
    request file is even written to disk -- never mind staged, committed,
    or pushed. Any branch other than the one explicit allowed target
    (main/master included, and rejected by name in the error message) is
    refused with zero git mutation and zero filesystem write.

    Idempotency / never-overwrite: if the target file already exists on
    disk with IDENTICAL content, this is a no-op success (a repeated
    scheduled run must not fail or re-commit). If it exists with
    DIFFERENT content, this refuses to overwrite it and returns an
    explicit conflict -- per the task's explicit "avoid overwriting an
    existing request or completed response" instruction.

    Returns {"published": bool, "path": str, "error": str|None,
    "skipped_unchanged": bool}. A git failure (non-zero exit at any
    step) never raises -- it returns published=False with `error` set,
    so the caller can record FAILED_RETRYABLE and retry on the next
    scheduled run, per Step E's explicit requirement. The branch-guard
    refusal uses this same never-raises, published=False contract.
    """
    run = run or _default_run
    path = request_file_path(request["request_id"])

    current_branch = resolve_current_branch(repo_dir, run)
    if current_branch != ALLOWED_PUBLISH_BRANCH:
        branch_desc = "could not be determined" if current_branch is None else repr(current_branch)
        forbidden_note = " (main/master are explicitly forbidden publish targets)" \
            if current_branch in FORBIDDEN_PUBLISH_BRANCHES else ""
        return {
            "published": False, "path": path,
            "error": f"refusing to publish: current branch {branch_desc} is not the allowed publish "
                     f"branch {ALLOWED_PUBLISH_BRANCH!r}{forbidden_note}. No file was written and no git "
                     "command (add/commit/push) was run.",
            "skipped_unchanged": False,
        }

    abs_path = os.path.join(repo_dir, path)
    content = json.dumps(build_request_file_content(request), indent=2, sort_keys=True) + "\n"

    if os.path.exists(abs_path):
        with open(abs_path, "r") as f:
            existing = f.read()
        if existing == content:
            return {"published": True, "path": path, "error": None, "skipped_unchanged": True}
        return {"published": False, "path": path,
                "error": f"refusing to overwrite existing, different content at {path}",
                "skipped_unchanged": False}

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as f:
        f.write(content)

    steps = [
        ["git", "add", path],
        ["git", "commit", "-m", f"stage7: publish research request {request['request_id']} [skip ci]"],
        ["git", "push"],
    ]
    for step in steps:
        result = run(step, cwd=repo_dir)
        if result.returncode != 0:
            return {"published": False, "path": path,
                    "error": f"`{' '.join(step)}` failed (exit {result.returncode}): {result.stderr}",
                    "skipped_unchanged": False}

    return {"published": True, "path": path, "error": None, "skipped_unchanged": False}


def _default_run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)
