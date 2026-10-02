"""
Tests for experiment5-staging-runner/run_staging.py.

No network anywhere: every remote call goes through an injected `opener`. FakeCloudflare emulates the two D1
HTTP API endpoints the runner uses (database GET, /query POST) over a REAL in-memory sqlite database built from
the repository's own migrations (0008, 0015, 0019), so the pipeline's real SQL runs unchanged.

Run with: python3 -m pytest experiment5-staging-runner/ -v
"""
import ast
import io
import json
import os
import sqlite3
import sys
import urllib.error

import pytest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "research"))
import run_staging as rs  # noqa: E402

HOUR = 3600000
STAGING_TOKEN = "staging-token-value-for-tests"

VALID_ENV = {
    rs.ENV_ACCOUNT_ID: rs.EXPECTED_STAGING_ACCOUNT_ID,
    rs.ENV_DATABASE_NAME: rs.EXPECTED_STAGING_DATABASE_NAME,
    rs.ENV_DATABASE_ID: rs.EXPECTED_STAGING_DATABASE_ID,
    rs.ENV_API_TOKEN: STAGING_TOKEN,
}


def env_with(**overrides):
    env = dict(VALID_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


class ExplodingOpener:
    """Fails the test if anything at all is sent over the network."""

    def __init__(self):
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        raise AssertionError(f"network access attempted: {request.get_method()} {request.full_url}")


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _migration(name):
    with open(os.path.join(ROOT, ".ai", "migrations", name)) as f:
        return f.read()


class FakeCloudflare:
    """Emulates GET /accounts/{a}/d1/database/{id} and POST .../query over real sqlite.
    Records every request so tests can assert what was (and was not) sent."""

    def __init__(self, report_uuid=rs.EXPECTED_STAGING_DATABASE_ID, report_name=rs.EXPECTED_STAGING_DATABASE_NAME,
                 with_run_table=True):
        self.report_uuid = report_uuid
        self.report_name = report_name
        self.requests = []
        self.conn = sqlite3.connect(":memory:")
        # The base tables as they exist on staging D1 (history.score is nullable there).
        self.conn.executescript("""
            CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER,
              technical_score INTEGER, bottom_score INTEGER, regime_mag REAL, gold_regime TEXT,
              sources_json TEXT, btc_price REAL);
            CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL);
        """)
        self.conn.executescript(_migration("0008_research_hypotheses.sql"))
        self.conn.executescript(_migration("0015_experiment5_sentiment_archive.sql"))
        if with_run_table:
            self.conn.executescript(_migration("0019_experiment5_pipeline_runs.sql"))
        self.conn.commit()

    def __call__(self, request, timeout=None):
        url = request.full_url
        auth = request.get_header("Authorization")
        sql = json.loads(request.data)["sql"] if request.data else None
        self.requests.append({"method": request.get_method(), "url": url, "auth": auth, "sql": sql})
        expected_prefix = (f"{rs.API_BASE}/accounts/{rs.EXPECTED_STAGING_ACCOUNT_ID}"
                           f"/d1/database/{rs.EXPECTED_STAGING_DATABASE_ID}")
        assert url.startswith(expected_prefix), f"request left the staging target: {url}"
        if request.get_method() == "GET":
            return _Response(200, {"success": True, "result": {"uuid": self.report_uuid, "name": self.report_name}})
        try:
            cursor = self.conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = [dict(zip(columns, r)) for r in cursor.fetchall()]
            self.conn.commit()
        except sqlite3.Error as e:
            return _Response(400, {"success": False, "errors": [{"message": str(e)}], "result": []})
        return _Response(200, {"success": True, "result": [{"success": True, "results": rows, "meta": {}}]})

    def sql_sent(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def add_history(self, ts, score, sources):
        self.conn.execute("INSERT INTO history (ts, score, sources_json) VALUES (?, ?, ?)", (ts, score, json.dumps(sources)))
        self.conn.commit()

    def add_btc(self, ts, price):
        self.conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))
        self.conn.commit()


def run_main(env, opener, now_ms):
    out = io.StringIO()
    code = rs.main(env=env, opener=opener, now_ms=now_ms, out=out)
    return code, out.getvalue()


# ---------------------------------------------------------------------------------------------------------------
# 1. Target validation: production and anything not exactly staging is refused BEFORE any network access.
# ---------------------------------------------------------------------------------------------------------------
class TestProductionRejectedBeforeAnyNetworkCall:
    @pytest.mark.parametrize("overrides", [
        {rs.ENV_DATABASE_NAME: "sentiment-history"},
        {rs.ENV_DATABASE_NAME: "  Sentiment-History  "},
        {rs.ENV_DATABASE_ID: "f91ca980-b886-423a-bd6f-f3baea46d181"},
        {rs.ENV_DATABASE_ID: " F91CA980-B886-423A-BD6F-F3BAEA46D181 "},
        {rs.ENV_DATABASE_NAME: "sentiment-history", rs.ENV_DATABASE_ID: "f91ca980-b886-423a-bd6f-f3baea46d181"},
    ])
    def test_production_identifiers_are_refused_without_network(self, overrides):
        opener = ExplodingOpener()
        with pytest.raises(rs.StagingTargetError, match="PRODUCTION"):
            rs.validate_staging_target(env_with(**overrides))
        code, out = run_main(env_with(**overrides), opener, now_ms=10 * HOUR)
        assert code == rs.EXIT_TARGET_REFUSED
        assert opener.calls == 0
        assert "PRODUCTION" in out

    @pytest.mark.parametrize("overrides", [
        {rs.ENV_ACCOUNT_ID: "0000000000000000000000000000000a"},
        {rs.ENV_DATABASE_NAME: "pulseworker-v2-staging-copy"},
        {rs.ENV_DATABASE_ID: "00000000-0000-0000-0000-000000000000"},
    ])
    def test_any_mismatch_with_the_exact_staging_triple_is_refused(self, overrides):
        opener = ExplodingOpener()
        with pytest.raises(rs.StagingTargetError, match="not the expected staging database"):
            rs.validate_staging_target(env_with(**overrides))
        assert run_main(env_with(**overrides), opener, 10 * HOUR)[0] == rs.EXIT_TARGET_REFUSED
        assert opener.calls == 0

    @pytest.mark.parametrize("var", rs.REQUIRED_ENV_VARS)
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing_or_empty_configuration_fails_closed(self, var, value):
        opener = ExplodingOpener()
        with pytest.raises(rs.StagingTargetError, match=var):
            rs.validate_staging_target(env_with(**{var: value}))
        assert run_main(env_with(**{var: value}), opener, 10 * HOUR)[0] == rs.EXIT_TARGET_REFUSED
        assert opener.calls == 0

    def test_never_falls_back_to_the_production_token_variable(self):
        env = env_with(**{rs.ENV_API_TOKEN: None, "CLOUDFLARE_API_TOKEN": "production-token"})
        with pytest.raises(rs.StagingTargetError, match="no fallback"):
            rs.validate_staging_target(env)

    def test_staging_token_equal_to_the_production_token_is_refused(self):
        env = env_with(CLOUDFLARE_API_TOKEN=STAGING_TOKEN)
        opener = ExplodingOpener()
        with pytest.raises(rs.StagingTargetError, match="same value"):
            rs.validate_staging_target(env)
        assert run_main(env, opener, 10 * HOUR)[0] == rs.EXIT_TARGET_REFUSED
        assert opener.calls == 0

    def test_an_unrelated_production_token_in_the_environment_is_simply_ignored(self):
        target = rs.validate_staging_target(env_with(CLOUDFLARE_API_TOKEN="a-different-production-token"))
        assert target.api_token == STAGING_TOKEN

    def test_no_default_exists_for_any_identifier(self):
        with pytest.raises(rs.StagingTargetError):
            rs.validate_staging_target({})


class TestValidStagingConfigResolvesOnlyToStaging:
    def test_exact_staging_triple(self):
        target = rs.validate_staging_target(dict(VALID_ENV))
        assert (target.account_id, target.database_name, target.database_id) == (
            "f58e761fbc8e62dc404d8684290af264", "pulseworker-v2-staging", "5458d504-2778-49ae-bd25-7751f1c49d50")

    def test_surrounding_whitespace_is_tolerated_but_resolves_to_the_same_target(self):
        env = {k: f"  {v}  " for k, v in VALID_ENV.items()}
        target = rs.validate_staging_target(env)
        assert target.database_id == rs.EXPECTED_STAGING_DATABASE_ID and target.api_token == STAGING_TOKEN

    def test_every_request_url_is_the_staging_database(self):
        fake = FakeCloudflare()
        code, _ = run_main(dict(VALID_ENV), fake, 10 * HOUR)
        assert code == rs.EXIT_OK
        assert fake.requests and all(rs.EXPECTED_STAGING_DATABASE_ID in r["url"] for r in fake.requests)
        assert not any(rs.PRODUCTION_DATABASE_ID in r["url"] for r in fake.requests)
        assert all(r["auth"] == f"Bearer {STAGING_TOKEN}" for r in fake.requests)

    def test_token_never_appears_in_repr_or_output(self):
        target = rs.validate_staging_target(dict(VALID_ENV))
        assert STAGING_TOKEN not in repr(target) and STAGING_TOKEN not in str(target)
        fake = FakeCloudflare()
        _, out = run_main(dict(VALID_ENV), fake, 10 * HOUR)
        assert STAGING_TOKEN not in out


# ---------------------------------------------------------------------------------------------------------------
# 2. Remote identity check: Cloudflare must confirm the database before any SQL is sent.
# ---------------------------------------------------------------------------------------------------------------
class TestRemoteIdentity:
    @pytest.mark.parametrize("uuid,name", [
        (rs.EXPECTED_STAGING_DATABASE_ID, "sentiment-history"),
        (rs.EXPECTED_STAGING_DATABASE_ID, "some-other-db"),
        ("f91ca980-b886-423a-bd6f-f3baea46d181", rs.EXPECTED_STAGING_DATABASE_NAME),
    ])
    def test_identity_mismatch_aborts_before_any_sql(self, uuid, name):
        fake = FakeCloudflare(report_uuid=uuid, report_name=name)
        code, out = run_main(dict(VALID_ENV), fake, 10 * HOUR)
        assert code == rs.EXIT_TARGET_REFUSED
        assert [r["method"] for r in fake.requests] == ["GET"]
        assert fake.sql_sent() == []
        assert "No SQL was sent" in out

    def test_lookup_http_error_aborts_without_leaking_the_token(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                         io.BytesIO(f"denied for {STAGING_TOKEN}".encode()))
        code, out = run_main(dict(VALID_ENV), opener, 10 * HOUR)
        assert code == rs.EXIT_TARGET_REFUSED
        assert "HTTP 403" in out and STAGING_TOKEN not in out


# ---------------------------------------------------------------------------------------------------------------
# 3. Transport.
# ---------------------------------------------------------------------------------------------------------------
class TestTransport:
    def test_query_returns_rows(self):
        fake = FakeCloudflare()
        fake.add_btc(1, 100.0)
        query, _ = rs.make_d1_functions(rs.validate_staging_target(dict(VALID_ENV)), opener=fake)
        assert query("SELECT ts, btc_price FROM btc_data") == [{"ts": 1, "btc_price": 100.0}]

    def test_unsuccessful_statement_raises_and_never_returns_partial_data(self):
        fake = FakeCloudflare()
        query, _ = rs.make_d1_functions(rs.validate_staging_target(dict(VALID_ENV)), opener=fake)
        with pytest.raises(rs.D1RequestError, match="did not succeed"):
            query("SELECT * FROM no_such_table")

    def test_non_json_response_raises(self):
        class Bad(_Response):
            def read(self):
                return b"<html>"
        query, _ = rs.make_d1_functions(rs.validate_staging_target(dict(VALID_ENV)), opener=lambda r, timeout=None: Bad(200, {}))
        with pytest.raises(rs.D1RequestError, match="non-JSON"):
            query("SELECT 1")

    def test_network_error_raises_without_token(self):
        def opener(request, timeout=None):
            raise urllib.error.URLError(f"connection reset ({STAGING_TOKEN})")
        query, _ = rs.make_d1_functions(rs.validate_staging_target(dict(VALID_ENV)), opener=opener)
        with pytest.raises(rs.D1RequestError) as excinfo:
            query("SELECT 1")
        assert STAGING_TOKEN not in str(excinfo.value)


# ---------------------------------------------------------------------------------------------------------------
# 4. End to end through the REAL pipeline: run record (migration 0019), idempotency, failure handling.
# ---------------------------------------------------------------------------------------------------------------
def seed_market(fake, hours=4):
    for i in range(hours):
        fake.add_history(i * HOUR, 50, {"fng": 50 + i * 5, "etfflows": 40 + i * 5})
    for i in range(hours + 26):
        fake.add_btc(i * HOUR, 100.0 + i)


class TestEndToEnd:
    def test_first_run_archives_decides_and_writes_an_ok_run_record(self):
        fake = FakeCloudflare()
        seed_market(fake)
        code, out = run_main(dict(VALID_ENV), fake, now_ms=3 * HOUR)
        assert code == rs.EXIT_OK, out
        assert fake.count("research_sentiment_archive") == 4
        assert fake.count("research_hypotheses") >= 1
        run = fake.conn.execute(
            "SELECT status, error_text, pipeline_version, newly_archived, decisions_created FROM experiment5_pipeline_runs"
        ).fetchall()
        assert len(run) == 1 and run[0][0] == "OK" and run[0][1] is None
        assert run[0][2] == "experiment5-pipeline-v2" and run[0][3] == 4

    def test_rerun_on_the_same_data_is_idempotent(self):
        fake = FakeCloudflare()
        seed_market(fake)
        assert run_main(dict(VALID_ENV), fake, now_ms=3 * HOUR)[0] == rs.EXIT_OK
        archive_1, decisions_1 = fake.count("research_sentiment_archive"), fake.count("research_hypotheses")
        assert run_main(dict(VALID_ENV), fake, now_ms=3 * HOUR)[0] == rs.EXIT_OK
        assert fake.count("research_sentiment_archive") == archive_1
        assert fake.count("research_hypotheses") == decisions_1
        second = fake.conn.execute(
            "SELECT newly_archived, decisions_created FROM experiment5_pipeline_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        assert second == (0, 0)
        assert fake.count("experiment5_pipeline_runs") == 2  # one operational record per execution

    def test_pipeline_failure_is_recorded_as_failed_and_exits_non_zero(self):
        fake = FakeCloudflare()
        seed_market(fake)
        fake.conn.execute("ALTER TABLE btc_data RENAME TO btc_data_hidden")
        code, out = run_main(dict(VALID_ENV), fake, now_ms=3 * HOUR)
        assert code == rs.EXIT_PIPELINE_FAILED
        assert "PIPELINE FAILED" in out and STAGING_TOKEN not in out
        status, error = fake.conn.execute("SELECT status, error_text FROM experiment5_pipeline_runs").fetchone()
        assert status == "FAILED" and "btc_data" in error
        assert fake.count("research_sentiment_archive") == 0

    def test_missing_run_table_is_not_reported_as_recorded(self):
        fake = FakeCloudflare(with_run_table=False)
        seed_market(fake)
        code, out = run_main(dict(VALID_ENV), fake, now_ms=3 * HOUR)
        assert code == rs.EXIT_RUN_NOT_RECORDED
        assert "SKIPPED_TABLE_MISSING" in out

    def test_writes_touch_only_experiment5_tables(self):
        fake = FakeCloudflare()
        seed_market(fake)
        before = (fake.count("history"), fake.count("btc_data"))
        assert run_main(dict(VALID_ENV), fake, now_ms=3 * HOUR)[0] == rs.EXIT_OK
        assert (fake.count("history"), fake.count("btc_data")) == before
        writes = [r["sql"] for r in fake.sql_sent() if r["sql"].lstrip().split()[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE")]
        assert writes, "expected the run to write"
        allowed = ("research_sentiment_archive", "research_hypotheses", "experiment5_pipeline_runs")
        for sql in writes:
            assert not sql.lstrip().upper().startswith("DELETE")
            target_table = sql.split()[2] if sql.upper().startswith("INSERT") else sql.split()[1]
            assert target_table in allowed, sql


# ---------------------------------------------------------------------------------------------------------------
# 5. Static isolation guarantees.
# ---------------------------------------------------------------------------------------------------------------
RUNNER_PATH = os.path.join(HERE, "run_staging.py")
WORKFLOW_PATH = os.path.join(ROOT, ".github", "workflows", "exp005-staging-runner.yml")


def _imports(path):
    tree = ast.parse(open(path).read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


class TestRunnerIsolation:
    def test_runner_cannot_shell_out_or_reach_github(self):
        imports = _imports(RUNNER_PATH)
        assert not imports & {"subprocess", "requests", "http.client", "socket"}
        source = open(RUNNER_PATH).read()
        for forbidden in ("api.github.com", "/dispatches", "wrangler d1", "gh workflow", "GITHUB_TOKEN"):
            assert forbidden not in source

    def test_runner_does_not_import_the_production_adapter(self):
        imports = _imports(RUNNER_PATH)
        assert not any("experiment5-agent" in n or n == "run" for n in imports)

    def test_production_identifiers_appear_only_as_refusal_constants(self):
        tree = ast.parse(open(RUNNER_PATH).read())
        production_values = {"sentiment-history", "f91ca980-b886-423a-bd6f-f3baea46d181"}
        assigned = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value in production_values:
                assigned[node.targets[0].id] = node.value.value
        assert assigned == {"PRODUCTION_DATABASE_NAME": "sentiment-history",
                            "PRODUCTION_DATABASE_ID": "f91ca980-b886-423a-bd6f-f3baea46d181"}
        # ...and no other code-level string literal (the module docstring is prose, not code) carries them.
        docstring_node = tree.body[0].value  # the module docstring expression
        code_literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                         and n is not docstring_node and any(p in n.value for p in production_values)]
        assert sorted(code_literals) == sorted(production_values)

    def test_production_adapter_is_unchanged_in_behaviour(self):
        adapter = open(os.path.join(ROOT, "scripts", "experiment5-agent", "run.py")).read()
        assert 'D1_DATABASE = "sentiment-history"' in adapter
        assert "run_staging" not in adapter


@pytest.fixture(scope="module")
def workflow():
    with open(WORKFLOW_PATH) as f:
        return yaml.safe_load(f)


class TestWorkflowGuard:
    def test_manual_dispatch_only(self, workflow):
        triggers = workflow.get(True, workflow.get("on"))  # PyYAML parses the bare key `on` as True
        assert set(triggers) == {"workflow_dispatch"}
        assert triggers["workflow_dispatch"]["inputs"]["expected_sha"]["required"] is True

    def test_read_only_permissions(self, workflow):
        assert workflow["permissions"] == {"contents": "read"}

    def test_never_references_the_production_credential_or_another_workflow(self, workflow):
        # Only executable content counts -- comments may name the production path to explain the separation.
        code = "\n".join(line for line in open(WORKFLOW_PATH).read().splitlines() if not line.lstrip().startswith("#"))
        assert "secrets.CLOUDFLARE_API_TOKEN" not in code
        assert "schedule" not in workflow.get(True, workflow.get("on"))
        for forbidden in ("gh workflow", "/dispatches", "actions: write", "contents: write", "scripts/experiment5-agent"):
            assert forbidden not in code

    def test_pinned_target_matches_the_runner_constants(self, workflow):
        step_env = [s.get("env", {}) for s in workflow["jobs"]["run-staging"]["steps"] if "run_staging.py" in s.get("run", "")]
        assert len(step_env) == 1
        env = step_env[0]
        assert env["EXP5_STAGING_ACCOUNT_ID"] == rs.EXPECTED_STAGING_ACCOUNT_ID
        assert env["EXP5_STAGING_DATABASE_NAME"] == rs.EXPECTED_STAGING_DATABASE_NAME
        assert env["EXP5_STAGING_DATABASE_ID"] == rs.EXPECTED_STAGING_DATABASE_ID
        assert env["EXP5_STAGING_CLOUDFLARE_API_TOKEN"] == "${{ secrets.STAGE7_STAGING_CLOUDFLARE_API_TOKEN }}"

    def test_reviewed_sha_is_enforced_before_the_runner(self, workflow):
        steps = workflow["jobs"]["run-staging"]["steps"]
        names = [s.get("name", "") for s in steps]
        assert names.index("Verify the head is the reviewed commit") < next(
            i for i, s in enumerate(steps) if "run_staging.py" in s.get("run", ""))

    def test_production_workflow_still_calls_only_the_production_adapter(self):
        text = open(os.path.join(ROOT, ".github", "workflows", "live-evidence-collection.yml")).read()
        assert "run_staging" not in text and "EXP5_STAGING" not in text
