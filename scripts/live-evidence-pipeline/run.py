#!/usr/bin/env python3
"""
PR-6: thin I/O adapter for research/live_evidence_pipeline.py.

This script contains NO detection/evidence/relevance logic of its own --
it only wires the pipeline's injected d1_query_fn/d1_execute_fn to real
`wrangler d1 execute --remote` calls (same tool/pattern already used by
.github/workflows/export-learning-data.yml and the earlier one-off
verify_pr4_live.py verification script), then prints an execution
summary. All decision logic lives in, and is unit-tested in,
research/live_evidence_pipeline.py -- mirroring this project's existing
"pure lib.js / I/O *-checks.js" split (scripts/canary-audit/,
scripts/production-chain-audit/).

Read-only queries: SELECT only. Writes: two explicit, narrow INSERT
statements only, both built by the pipeline module itself
(build_insert_event_sql / build_insert_evidence_sql) -- this script
never constructs SQL by hand.

Requires CLOUDFLARE_API_TOKEN in the environment (same secret already
used by export-learning-data.yml). No other credential, no paid
service.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "research"))
import live_evidence_pipeline as lep  # noqa: E402

D1_DATABASE = "sentiment-history"


def d1_query(sql):
    result = subprocess.run(
        ["wrangler", "d1", "execute", D1_DATABASE, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)[0]["results"]


def d1_execute(sql):
    subprocess.run(
        ["wrangler", "d1", "execute", D1_DATABASE, "--remote", "--command", sql],
        capture_output=True, text=True, check=True,
    )


def main():
    now_ts = int(time.time() * 1000)
    summary = lep.run_pipeline(d1_query, d1_execute, now_ts)

    print("=== PR-6 live event/evidence pipeline — execution summary ===")
    print(json.dumps(summary, indent=2, default=str))
    print()
    print(f"status: {summary['status']}")
    print(f"candidate events (last {lep.DETECTION_WINDOW_MS // 86400000}d window): {summary['candidate_events']}")
    print(f"already known (skipped entirely): {summary['already_known_events']}")
    print(f"newly persisted to research_events: {summary['newly_persisted_events']}")
    print(f"eligible for evidence collection (<= {lep.MAX_EVENT_AGE_FOR_EVIDENCE_MS // 86400000}d old): "
          f"{summary['evidence_eligible_events']}")
    print(f"skipped evidence collection (too old): {summary['evidence_skipped_too_old_events']}")

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        with open(step_summary_path, "a") as f:
            f.write("## PR-6 live event/evidence pipeline\n\n")
            f.write(f"- status: `{summary['status']}`\n")
            f.write(f"- candidate events: {summary['candidate_events']}\n")
            f.write(f"- already known (skipped): {summary['already_known_events']}\n")
            f.write(f"- newly persisted events: {summary['newly_persisted_events']}\n")
            f.write(f"- evidence-eligible events: {summary['evidence_eligible_events']}\n")
            f.write(f"- evidence skipped (too old): {summary['evidence_skipped_too_old_events']}\n")
            for e in summary["events"]:
                f.write(f"  - `{e['category']}` @ {e['event_ts']}: evidence={e['evidence']}\n")


if __name__ == "__main__":
    main()
