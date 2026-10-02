"""Runs the REAL research/experiment5_pipeline.py against a local SQLite file (never a remote database).
Usage: pipeline_driver.py <db> <now_ts_ms> [ok|fail]   -- `fail` hides btc_data so the run really fails.
Prints one JSON line: the summary (ok) or {"raised": ...} (fail)."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "research"))
import experiment5_pipeline as ep  # noqa: E402

db, now_ts, mode = sys.argv[1], int(sys.argv[2]), (sys.argv[3] if len(sys.argv) > 3 else "ok")
conn = sqlite3.connect(db)


def query(sql):
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def execute(sql):
    conn.execute(sql)
    conn.commit()


if mode == "fail":
    conn.execute("ALTER TABLE btc_data RENAME TO btc_data_hidden")
    conn.commit()
try:
    summary = ep.run_pipeline_recorded(query, execute, now_ts)
    print(json.dumps(summary, default=str))
except Exception as exc:  # the real entry point re-raises; the driver only reports it
    print(json.dumps({"raised": f"{type(exc).__name__}: {exc}"}))
finally:
    if mode == "fail":
        conn.execute("ALTER TABLE btc_data_hidden RENAME TO btc_data")
        conn.commit()
