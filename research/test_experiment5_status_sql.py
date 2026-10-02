"""
Executes the Experiment 5 status endpoint's REAL SQL (taken verbatim out of worker.js) in real sqlite against the
real migrations (0008 research_hypotheses, 0019 experiment5_pipeline_runs).

The JS tests fake D1 (CI runs Node 20, which has no sqlite); this file is where the aggregate's counting semantics
are proven: identical to the established getResearchLabExperiment5Results rules (a decision counts toward a side
only when its recorded outcome is exactly true/false for that side), malformed JSON rows are counted and cannot fail
the query, unresolved and non-Experiment-5 rows are never counted.

Run with: python3 -m pytest research/test_experiment5_status_sql.py -v
"""
import json
import os
import re
import sqlite3

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
with open(os.path.join(REPO, "worker.js"), encoding="utf-8") as f:
    WORKER = f.read()


def _js_string_constant(name):
    m = re.search(rf"const {name} =\n(.*?);\n", WORKER, re.S)
    assert m, name
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))


def _migration(num):
    d = os.path.join(REPO, ".ai", "migrations")
    name = next(n for n in os.listdir(d) if n.startswith(num + "_"))
    with open(os.path.join(d, name), encoding="utf-8") as f:
        return f.read()


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_migration("0008"))
    conn.executescript(_migration("0019"))
    return conn


def _add(conn, subject, payload, status):
    conn.execute(
        "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, source_analysis_ids, status, "
        "evidence_summary_json, out_of_sample_status) VALUES (0, 0, ?, 'x', '[]', 'OBSERVATION', ?, ?)",
        (subject, payload if isinstance(payload, str) else json.dumps(payload), status),
    )


def _reference(rows):
    """The established semantics (getResearchLabExperiment5Results), applied in Python."""
    out = dict(n_resolved=0, n_malformed=0, agent_correct=0, agent_incorrect=0, v1_correct=0, v1_incorrect=0)
    for subject, payload, status in rows:
        if not subject.startswith("experiment5:") or status is None:
            continue
        out["n_resolved"] += 1
        try:
            parsed = json.loads(payload) if isinstance(payload, str) else payload
        except ValueError:
            out["n_malformed"] += 1
            continue
        o = (parsed or {}).get("outcome") if isinstance(parsed, dict) else None
        if not o:
            continue
        if o.get("agent_correct") is True: out["agent_correct"] += 1
        elif o.get("agent_correct") is False: out["agent_incorrect"] += 1
        if o.get("v1_baseline_correct") is True: out["v1_correct"] += 1
        elif o.get("v1_baseline_correct") is False: out["v1_incorrect"] += 1
    return out


ROWS = [
    ("experiment5:reversal:fng", {"outcome": {"agent_correct": True, "v1_baseline_correct": False}}, "PASSED_HOLDOUT"),
    ("experiment5:reversal:fng", {"outcome": {"agent_correct": False, "v1_baseline_correct": False}}, "FAILED_HOLDOUT"),
    ("experiment5:cross_source_confirmation:a,b", {"outcome": {"agent_correct": True, "v1_baseline_correct": True}}, "PASSED_HOLDOUT"),
    ("experiment5:reversal:x", {"outcome": {"agent_correct": None, "v1_baseline_correct": None}}, "INSUFFICIENT_DATA_FOR_HOLDOUT"),
    ("experiment5:reversal:x", {"outcome": {"agent_correct": True}}, "PASSED_HOLDOUT"),   # v1 side absent
    ("experiment5:reversal:x", {"decision": {}}, "PASSED_HOLDOUT"),                          # resolved but no outcome key
    ("experiment5:reversal:x", "{not valid json", "FAILED_HOLDOUT"),                         # malformed
    ("experiment5:reversal:x", "[]", "FAILED_HOLDOUT"),                                      # valid JSON, not an object
    ("experiment5:reversal:x", "null", "FAILED_HOLDOUT"),
    ("experiment5:reversal:y", {"outcome": {"agent_correct": True, "v1_baseline_correct": False}}, None),   # UNRESOLVED: never counted
    ("something:else", {"outcome": {"agent_correct": True, "v1_baseline_correct": True}}, "PASSED_HOLDOUT"),  # not Experiment 5
    ("experiment5:reversal:z", {"outcome": {"agent_correct": 1, "v1_baseline_correct": "yes"}}, "PASSED_HOLDOUT"),  # not real booleans
]


def test_the_aggregate_matches_the_established_counting_rules_on_a_mixed_dataset():
    conn = _db()
    for subject, payload, status in ROWS:
        _add(conn, subject, payload, status)
    got = dict(conn.execute(_js_string_constant("EXPERIMENT5_PREDICTIVE_COUNTS_SQL")).fetchone())
    ref = _reference(ROWS)
    # The integer 1 is indistinguishable from `true` to SQLite's json_extract. That is the single divergence from the
    # JS rule; it is asserted explicitly rather than hidden. Real outcomes are written by experiment5_agent as Python
    # bool/None, never integers (see the next test, which excludes the synthetic integer row).
    assert got["n_resolved"] == ref["n_resolved"]
    assert got["n_malformed"] == ref["n_malformed"]
    assert got["agent_incorrect"] == ref["agent_incorrect"]
    assert got["v1_incorrect"] == ref["v1_incorrect"]
    assert got["agent_correct"] == ref["agent_correct"] + 1
    assert got["v1_correct"] == ref["v1_correct"]  # the text value "yes" is neither 1 nor 0, so it is not counted


def test_the_aggregate_matches_exactly_on_data_the_pipeline_actually_writes():
    conn = _db()
    real = [r for r in ROWS if not (isinstance(r[1], dict) and r[1].get("outcome", {}).get("agent_correct") == 1 and r[1]["outcome"].get("v1_baseline_correct") == "yes")]
    for subject, payload, status in real:
        _add(conn, subject, payload, status)
    got = dict(conn.execute(_js_string_constant("EXPERIMENT5_PREDICTIVE_COUNTS_SQL")).fetchone())
    assert got == _reference(real)


def test_one_malformed_row_cannot_fail_the_query_and_is_counted():
    conn = _db()
    _add(conn, "experiment5:reversal:fng", "{broken", "FAILED_HOLDOUT")
    _add(conn, "experiment5:reversal:fng", {"outcome": {"agent_correct": True, "v1_baseline_correct": False}}, "PASSED_HOLDOUT")
    got = dict(conn.execute(_js_string_constant("EXPERIMENT5_PREDICTIVE_COUNTS_SQL")).fetchone())
    assert got["n_malformed"] == 1 and got["agent_correct"] == 1 and got["n_resolved"] == 2


def test_an_empty_table_yields_zeros_not_nulls():
    conn = _db()
    got = dict(conn.execute(_js_string_constant("EXPERIMENT5_PREDICTIVE_COUNTS_SQL")).fetchone())
    assert got == dict(n_resolved=0, n_malformed=0, agent_correct=0, agent_incorrect=0, v1_correct=0, v1_incorrect=0)


def test_the_aggregate_is_read_only_and_one_row():
    sql = _js_string_constant("EXPERIMENT5_PREDICTIVE_COUNTS_SQL")
    assert sql.lstrip().upper().startswith("SELECT ")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b", sql, re.I)
    conn = _db()
    for subject, payload, status in ROWS:
        _add(conn, subject, payload, status)
    assert len(conn.execute(sql).fetchall()) == 1


def test_the_run_log_selects_work_against_the_real_migration_and_stay_bounded():
    conn = _db()
    for i in range(30):
        conn.execute("INSERT INTO experiment5_pipeline_runs (run_ts, status, pipeline_version, constants_json) VALUES (?, ?, 'v', '{}')",
                     (i, "FAILED" if i % 3 == 0 else "OK"))
    recent = conn.execute("SELECT * FROM experiment5_pipeline_runs ORDER BY run_ts DESC, run_id DESC LIMIT 10").fetchall()
    assert len(recent) == 10 and recent[0]["run_ts"] == 29
    assert conn.execute("SELECT COUNT(*) AS n FROM experiment5_pipeline_runs").fetchone()["n"] == 30
    assert conn.execute("SELECT * FROM experiment5_pipeline_runs WHERE status = 'OK' ORDER BY run_ts DESC, run_id DESC LIMIT 1").fetchone()["run_ts"] == 29
    assert conn.execute("SELECT * FROM experiment5_pipeline_runs WHERE status = 'FAILED' ORDER BY run_ts DESC, run_id DESC LIMIT 1").fetchone()["run_ts"] == 27
    # the Worker's literal statements are the ones above
    assert "ORDER BY run_ts DESC, run_id DESC LIMIT ' + EXPERIMENT5_RECENT_RUNS_LIMIT" in WORKER


def _run_columns():
    m = re.search(r"const EXPERIMENT5_RUN_COLUMNS = \[(.*?)\];", WORKER, re.S)
    assert m
    return re.findall(r"'([a-z_0-9]+)'", m.group(1))


def test_the_workers_explicit_run_log_columns_are_exactly_the_migrations_columns():
    conn = _db()
    in_table = [r["name"] for r in conn.execute("PRAGMA table_info(experiment5_pipeline_runs)")]
    assert sorted(_run_columns()) == sorted(in_table)
    assert "SELECT *" not in WORKER[WORKER.index("const EXPERIMENT5_RUN_SELECT"):WORKER.index("const EXPERIMENT5_RUN_SELECT") + 200]


def test_a_run_log_table_that_is_behind_makes_the_explicit_select_fail_with_no_such_column():
    """With SELECT * a table missing a column reads fine and yields undefined fields -- indistinguishable from 'nothing
    recorded'. The explicit list makes the database report it, and the Worker classifies that as SCHEMA_DRIFT."""
    select = "SELECT " + ", ".join(_run_columns()) + " FROM experiment5_pipeline_runs"
    full = _db()
    full.execute("INSERT INTO experiment5_pipeline_runs (run_ts, status, pipeline_version, constants_json) VALUES (1, 'OK', 'v', '{}')")
    assert len(full.execute(select).fetchall()) == 1

    behind = sqlite3.connect(":memory:")
    behind.execute("CREATE TABLE experiment5_pipeline_runs (run_id INTEGER PRIMARY KEY, run_ts INTEGER, status TEXT, pipeline_version TEXT, constants_json TEXT)")
    behind.execute("INSERT INTO experiment5_pipeline_runs VALUES (1, 1, 'OK', 'v', '{}')")
    assert behind.execute("SELECT * FROM experiment5_pipeline_runs").fetchall()  # what the old SELECT * would have happily returned
    try:
        behind.execute(select)
    except sqlite3.OperationalError as exc:
        assert re.search(r"no such column: \w+", str(exc))
    else:  # pragma: no cover
        raise AssertionError("the explicit select did not detect the missing columns")
