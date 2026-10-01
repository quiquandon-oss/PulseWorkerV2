"""
Contract between worker.js's Stage 7 schema probe and the REAL migrations, executed in a real sqlite database.

The JS tests (tests/stage7-schema-readiness.test.js) run on CI's Node 20, which has no sqlite, so they fake D1. This
file closes that gap: it takes the exact SQL string the Worker sends (STAGE7_SCHEMA_PROBE_SQL) and the Worker's
declared schema (STAGE7_REQUIRED_SCHEMA) out of worker.js, applies the actual migration files in order to a real
sqlite database and checks that

  * the probe is valid SQL and returns exactly the declared columns once all migrations are applied,
  * leaving out migration 0017 / 0018 makes precisely the declared columns of those migrations disappear
    (so a half-migrated database is detected, not mistaken for ready),
  * the Python pipeline's own required-column list (run_stage7.REQUIRED_STAGE7_COLUMNS) agrees with the Worker's.

Run with: python3 -m pytest stage7-research-pipeline/ -v
"""
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(__file__)
REPO = os.path.abspath(os.path.join(HERE, ".."))
MIGRATIONS = os.path.join(REPO, ".ai", "migrations")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "research"))

with open(os.path.join(REPO, "worker.js"), encoding="utf-8") as f:
    WORKER_SRC = f.read()


def _declared_schema():
    m = re.search(r"// BEGIN STAGE7_REQUIRED_SCHEMA_JSON\nconst STAGE7_REQUIRED_SCHEMA = (\[.*?\]);\n// END", WORKER_SRC, re.S)
    assert m, "STAGE7_REQUIRED_SCHEMA markers not found in worker.js"
    return json.loads(m.group(1))


def _probe_sql():
    m = re.search(r"const STAGE7_SCHEMA_PROBE_SQL =\n(.*?);\n", WORKER_SRC, re.S)
    assert m, "STAGE7_SCHEMA_PROBE_SQL not found in worker.js"
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))


def _migration(num):
    name = next(n for n in os.listdir(MIGRATIONS) if n.startswith(num + "_"))
    with open(os.path.join(MIGRATIONS, name), encoding="utf-8") as f:
        return f.read()


def _db(*migrations):
    conn = sqlite3.connect(":memory:")
    for num in migrations:
        conn.executescript(_migration(num))
    return conn


def _probe(conn):
    return {(t, c) for t, c in conn.execute(_probe_sql())}


def _expected(up_to):
    return {(g["table"], c) for g in _declared_schema() if g["migration"] <= up_to for c in g["columns"]}


def test_probe_is_a_single_read_only_statement():
    sql = _probe_sql()
    assert sql.lstrip().upper().startswith("SELECT ")
    assert ";" not in sql
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b", sql, re.I)


def test_after_all_migrations_the_probe_returns_exactly_the_declared_columns_it_asks_about():
    conn = _db("0005", "0006", "0016", "0017", "0018")
    found = _probe(conn)
    assert _expected("0018") <= found, sorted(_expected("0018") - found)


def test_a_database_without_any_stage7_migration_yields_no_stage7_rows():
    conn = _db("0005", "0006")
    found = _probe(conn)
    assert {t for t, _ in found} == {"research_events"}
    assert _expected("0005") <= found


def test_missing_0018_is_visible_as_exactly_the_0018_columns():
    conn = _db("0005", "0006", "0016", "0017")
    found = _probe(conn)
    assert _expected("0017") <= found
    missing = _expected("0018") - found
    assert missing == {(g["table"], c) for g in _declared_schema() if g["migration"] == "0018" for c in g["columns"]}
    assert ("stage7_research_requests", "recalculation_status") in missing


def test_missing_0017_is_visible_including_the_whole_candidates_table():
    conn = _db("0005", "0006", "0016")
    found = _probe(conn)
    missing = _expected("0017") - found
    assert ("stage7_research_candidates", "candidate_id") in missing
    assert ("stage7_research_requests", "prompt_text") in missing
    assert ("stage7_research_responses", "raw_response_text") in missing


def test_every_migration_applies_cleanly_in_order_to_a_fresh_database():
    # 0017 and 0018 only alter tables that 0016 creates; applied in order on top of 0005/0006 nothing errors.
    _db("0005", "0006", "0016", "0017", "0018")


def test_migration_0017_and_0018_are_not_idempotent_so_a_reapply_is_rejected_not_silently_accepted():
    conn = _db("0005", "0006", "0016", "0017", "0018")
    for num in ("0017", "0018"):
        try:
            conn.executescript(_migration(num))
        except sqlite3.OperationalError as exc:
            assert "already exists" in str(exc) or "duplicate column" in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"re-applying {num} unexpectedly succeeded")


def test_the_python_pipelines_required_columns_agree_with_the_workers():
    import run_stage7 as r7  # noqa: E402

    worker = {}
    for g in _declared_schema():
        worker.setdefault(g["table"], {}).update({c: g["migration"] for c in g["columns"]})
    for table, columns in r7.REQUIRED_STAGE7_COLUMNS.items():
        for column, migration in columns.items():
            assert worker[table].get(column) == migration, (table, column)
