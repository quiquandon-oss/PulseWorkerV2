"""Runs the REAL stage7-research-pipeline/run_stage7.py main() against a local
SQLite file -- the stand-in for "a human dispatched the staging workflow" in the
local acceptance harness. The only seams replaced are the same ones the unit
tests replace (network D1 access, the staging-target guard, the GitHub publish
call, and the Stage 6 event-dataset builder); every decision, SQL statement and
state transition in run_stage7.py itself runs for real.

Usage: python3 pipeline_driver.py <db_path> <publish_log_path>
Prints the pipeline's JSON result as the LAST stdout line and exits with
run_stage7.exit_code_for(result), exactly like the workflow step would.
"""
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "research"))
sys.path.insert(0, os.path.join(ROOT, "stage7-research-pipeline"))
import run_stage7 as rs  # noqa: E402


def main(db_path, publish_log_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    def fake_run_d1(sql):
        return [dict(r) for r in conn.execute(sql).fetchall()]

    def fake_d1_api_query(sql, params):
        cursor = conn.execute(sql, params)
        rows = [dict(r) for r in cursor.fetchall()] if cursor.description else []
        conn.commit()
        return rows

    def fake_build_dataset(_mirror, _start_ts, _end_ts):
        events = [dict(r) for r in conn.execute("SELECT event_id, event_ts, category FROM research_events").fetchall()]
        return {
            "events": [{"event_id": e["event_id"], "event_ts": e["event_ts"], "category": e["category"],
                        "is_internal_model_event": False, "coin": "BTC"} for e in events],
            "results": [],
        }

    def fake_publish(request, repo_dir):
        with open(publish_log_path, "a") as f:
            f.write(json.dumps({"request_id": request["request_id"]}) + "\n")
        return {"published": True, "path": rs.pub.request_file_path(request["request_id"]),
                "error": None, "skipped_unchanged": False}

    rs.run_d1 = fake_run_d1
    rs.d1_api_query = fake_d1_api_query
    rs.validate_staging_target = lambda env=None: SimpleNamespace(
        account_id="local", database_name="local-sqlite", database_id="local", api_token="local")
    rs.join_module.build_event_source_evidence_dataset = fake_build_dataset
    rs.pub.publish_request_file = fake_publish

    result = rs.main()
    conn.close()
    return rs.exit_code_for(result)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
