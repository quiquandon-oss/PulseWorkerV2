#!/usr/bin/env python3
"""
Experiment 5: thin I/O adapter for research/experiment5_pipeline.py.

This script contains NO archiving/classification/decision logic of its
own -- it only wires the pipeline's injected d1_query_fn/d1_execute_fn
to real `wrangler d1 execute --remote` calls, exactly mirroring
scripts/live-evidence-pipeline/run.py's own established pattern. All
decision logic lives in, and is unit-tested in,
research/experiment5_pipeline.py / research/experiment5_agent.py /
research/source_dynamics.py / research/source_intelligence.py /
research/sentiment_archive.py.

Read-only queries: SELECT only. Writes: explicit INSERT/UPDATE
statements only, both built by the pipeline module itself
(build_insert_archive_sql / build_insert_decision_sql /
build_update_decision_outcome_sql) -- this script never constructs SQL
by hand.

Requires CLOUDFLARE_API_TOKEN in the environment (same secret already
used by export-learning-data.yml / live-evidence-collection.yml). No
other credential, no paid service.

IMPORTANT: as of this change, neither research_sentiment_archive
(migration 0015) nor research_hypotheses (migration 0008) has been
applied to production D1 (confirmed directly, read-only, before writing
this script). Running this script against production today will fail
at its first query with "no such table" -- by design. Applying either
migration is a separate, later, explicitly-authorized deployment step;
this script and the tests behind it are built and proven correct now,
against a real in-memory sqlite mirror, per this project's established
process (see hypothesis_gate.py's own module docstring for the
identical situation with migration 0008, already true for over a
month at the time of this change).
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "research"))
import experiment5_pipeline as ep  # noqa: E402

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
    summary = ep.run_pipeline(d1_query, d1_execute, now_ts)

    print("=== Experiment 5 agent pipeline — execution summary ===")
    print(json.dumps(summary, indent=2, default=str))
    print()
    print(f"history rows read: {summary['history_rows_read']}")
    print(f"btc_data rows read: {summary['btc_rows_read']}")
    print(f"newly archived observations: {summary['newly_archived']}")
    print(f"decisions created this run: {summary['decisions_created']}")
    print(f"decisions evaluated this run: {summary['decisions_evaluated']}")

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        with open(step_summary_path, "a") as f:
            f.write("## Experiment 5 agent pipeline\n\n")
            f.write(f"- history rows read: {summary['history_rows_read']}\n")
            f.write(f"- btc_data rows read: {summary['btc_rows_read']}\n")
            f.write(f"- newly archived observations: {summary['newly_archived']}\n")
            f.write(f"- decisions created this run: {summary['decisions_created']}\n")
            f.write(f"- decisions evaluated this run: {summary['decisions_evaluated']}\n")
            f.write(f"- candidate new sources observed: {summary['agent_cycle'].get('candidate_new_sources')}\n")


if __name__ == "__main__":
    main()
