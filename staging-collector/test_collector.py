"""
Safety and behaviour tests for collector.py. No network, no credentials: Cloudflare, Hyperliquid and Coinbase are
replaced by a fake urlopen that serves D1 /query from a local SQLite replica of the staging schema (binding numbers
as REAL, as D1's HTTP API does). Run: python3 -m pytest staging-collector/ -q
"""
import io
import json
import os
import re
import sqlite3
import time
import urllib.parse

import pytest

import collector as c

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_FILES = ["collector.py", "history_harness.py", "harness_policy.py", "relay_shims.py"]
GOOD_ENV = {
    c.ENV_ACCOUNT_ID: c.EXPECTED_STAGING_ACCOUNT_ID,
    c.ENV_DATABASE_NAME: c.EXPECTED_STAGING_DATABASE_NAME,
    c.ENV_DATABASE_ID: c.EXPECTED_STAGING_DATABASE_ID,
    c.ENV_API_TOKEN: "staging-token-for-tests",
    c.ENV_GIT_SHA: "0" * 40,
    c.ENV_RUN_ID: "test-run",
}
PROV = c.Provenance("0" * 40, "test-run")


def now_ms():
    return int(time.time() * 1000)


def obs(price=64000.0, check=64010.0, observed_ts=None):
    return {"price": price, "cross_check_price": check, "observed_ts": observed_ts or now_ms(),
            "source": "hyperliquid:metaAndAssetCtxs:BTC.markPx", "cross_check_source": "coinbase:BTC-USD:spot"}


def capture(payload=None, **over):
    base = {"fixture_mode": False, "cryptopulse_commit": c.PINNED_CRYPTOPULSE_COMMIT,
            "index_sha256": c.PINNED_CRYPTOPULSE_INDEX_SHA256, "captured_at_ms": now_ms(), "payload": payload,
            "excluded_sources": ["foufi", "ninemag"], "blocked_request_count": 20}
    base.update(over)
    return base


def good_payload():
    return {"score": 55, "technicalScore": 60, "btcPrice": 64000.5,
            "sources": {"fng": 54, "funding": 50, "global": 38, "etfflows": 95},
            "goldRegime": "competing-haven", "regimeMag": None, "bottomScore": 17, "globalMcap": 3.1e12}


def add_other_staging_tables(conn):
    for ddl in (
        "CREATE TABLE predictions (id INTEGER PRIMARY KEY)",
        "CREATE TABLE research_sentiment_archive (archive_id INTEGER PRIMARY KEY)",
        "CREATE TABLE research_hypotheses (hypothesis_id INTEGER PRIMARY KEY, subject TEXT, out_of_sample_status TEXT)",
        "CREATE TABLE experiment5_pipeline_runs (run_id INTEGER PRIMARY KEY)",
        "CREATE TABLE research_events (event_id INTEGER PRIMARY KEY)",
        "CREATE TABLE research_event_evidence (evidence_id INTEGER PRIMARY KEY)",
        "CREATE TABLE stage7_research_requests (request_id TEXT PRIMARY KEY, updated_ts INTEGER)",
        "CREATE TABLE stage7_research_responses (response_id TEXT PRIMARY KEY)",
        "CREATE TABLE stage7_research_candidates (candidate_id TEXT PRIMARY KEY, updated_ts INTEGER)",
        "CREATE TABLE stage7_event_sentiment (id INTEGER PRIMARY KEY)",
    ):
        conn.execute(ddl)
    conn.commit()


@pytest.fixture
def db():
    replica = c.make_local_staging_replica()
    add_other_staging_tables(replica.conn)
    return replica


# ---------------------------------------------------------------------------------------------------------
# Fake network: records every request; serves staging D1 from a local replica
# ---------------------------------------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __init__(self, status, obj):
        super().__init__(json.dumps(obj).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    def __init__(self, replica, identity=None, hl_price="64000.0", cb_price="64050.00"):
        self.replica = replica
        self.identity = identity or {"uuid": c.EXPECTED_STAGING_DATABASE_ID, "name": c.EXPECTED_STAGING_DATABASE_NAME}
        self.hl_price, self.cb_price = hl_price, cb_price
        self.requests = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        body = json.loads(request.data) if request.data else None
        self.requests.append({"method": request.get_method(), "url": url, "body": body,
                              "auth": request.get_header("Authorization")})
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname == "api.hyperliquid.xyz":
            return FakeResponse(200, [{"universe": [{"name": "ETH"}, {"name": "BTC"}]},
                                      [{"markPx": "2500"}, {"markPx": self.hl_price}]])
        if parsed.hostname == "api.coinbase.com":
            return FakeResponse(200, {"data": {"amount": self.cb_price, "currency": "USD"}})
        if parsed.path == c.STAGING_DB_PATH:
            return FakeResponse(200, {"success": True, "result": self.identity})
        if parsed.path == c.STAGING_DB_PATH + "/query":
            sql = body["sql"]
            assert sql in c.SQL.values(), "only templates may be sent"
            params = [float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else p
                      for p in body.get("params", [])]
            cur = self.replica.conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            self.replica.conn.commit()
            return FakeResponse(200, {"success": True, "result": [{"success": True, "results": rows,
                                      "meta": {"changes": max(cur.rowcount, 0), "last_row_id": cur.lastrowid}}]})
        raise AssertionError(f"unexpected request {request.get_method()} {url}")


def remote(replica, **kw):
    net = FakeNet(replica, **kw)
    return c.StagingD1(c.validate_target(GOOD_ENV), opener=net), net


# ---------------------------------------------------------------------------------------------------------
# Safety requirements 1-12
# ---------------------------------------------------------------------------------------------------------

def _source(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


def test_01_production_d1_uuid_never_used_for_any_request_or_sql():
    for sql in c.SQL.values():
        assert c.PRODUCTION_DATABASE_ID not in sql and c.PRODUCTION_DATABASE_NAME not in sql
    assert c.PRODUCTION_DATABASE_ID not in c.STAGING_DB_PATH
    assert all(c.PRODUCTION_DATABASE_ID not in path for _, _, path in c.ALLOWED_REQUESTS)
    # The literal exists exactly once (the refusal constant) and never in the harness modules.
    assert _source("collector.py").count(c.PRODUCTION_DATABASE_ID) == 1
    for name in COLLECTOR_FILES[1:]:
        assert c.PRODUCTION_DATABASE_ID not in _source(name)


@pytest.mark.parametrize("host", list(c.PRODUCTION_HOSTS) + ["anything.workers.dev",
                                                            "pulseworker-v2-staging.quiquandon.workers.dev"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_02_production_worker_and_workers_dev_hosts_are_refused_before_any_socket(host, method):
    with pytest.raises(c.NetworkPolicyError):
        c.check_request_allowed(method, f"https://{host}/history")


def test_02_full_flows_contact_only_allowlisted_hosts(db):
    d1, net = remote(db)
    c.btc_tick(d1, PROV, c.fetch_btc_observation(opener=net), now_ms())
    c.history_ingest(d1, PROV, capture(good_payload()), now_ms())
    c.verify(d1)
    hosts = {urllib.parse.urlsplit(r["url"]).hostname for r in net.requests}
    assert hosts <= {"api.hyperliquid.xyz", "api.coinbase.com", "api.cloudflare.com"}
    for r in net.requests:
        c.check_request_allowed(r["method"], r["url"])
        if r["url"].startswith("https://api.cloudflare.com"):
            assert c.EXPECTED_STAGING_DATABASE_ID in r["url"] and r["auth"] == "Bearer staging-token-for-tests"
        else:
            assert r["auth"] is None, "the Cloudflare token is never sent to a data source"


def test_02_allowlist_itself_names_no_production_or_workers_dev_host():
    for method, host, path in c.ALLOWED_REQUESTS:
        assert host not in c.PRODUCTION_HOSTS and not host.endswith(".workers.dev") and "google" not in host
        assert host in {"api.hyperliquid.xyz", "api.coinbase.com", "api.cloudflare.com"}
        if host == "api.cloudflare.com":
            assert path.startswith(c.STAGING_DB_PATH)


@pytest.mark.parametrize("url", ["https://script.google.com/macros/s/x/exec", "https://api.cloudflare.com/client/v4/"
                                 "accounts/f58e761fbc8e62dc404d8684290af264/d1/database/"
                                 "f91ca980-b886-423a-bd6f-f3baea46d181/query", "http://api.hyperliquid.xyz/info",
                                 "https://api.hyperliquid.xyz/exchange", "https://evil.example/info"])
def test_04_apps_script_other_databases_and_unknown_requests_are_refused(url):
    with pytest.raises(c.NetworkPolicyError):
        c.check_request_allowed("POST", url)


_FORBIDDEN_SQL = re.compile(r"\b(DELETE|UPDATE|ALTER|DROP|CREATE|REPLACE|ATTACH|DETACH|VACUUM|REINDEX|PRAGMA)\b",
                            re.IGNORECASE)


def test_05_every_template_is_an_insert_or_a_select():
    for name, sql in c.SQL.items():
        head = sql.lstrip().split(None, 1)[0].upper()
        if name in c.WRITE_TEMPLATES:
            assert head == "INSERT", name
        else:
            assert head == "SELECT", name


@pytest.mark.parametrize("name", sorted(c.SQL))
def test_06_07_08_no_delete_update_or_schema_statement_in_any_template(name):
    sql = c.SQL[name].replace("pragma_table_info", "")  # the read-only table-valued function, not a PRAGMA
    assert not _FORBIDDEN_SQL.search(sql), name
    assert "ON CONFLICT" not in sql.upper() and "OR REPLACE" not in sql.upper()


def test_06_07_08_write_templates_only_target_the_four_collector_tables():
    for name in c.WRITE_TEMPLATES:
        table = re.match(r"\s*INSERT INTO (\w+)", c.SQL[name]).group(1)
        assert table in c.WRITABLE_TABLES
    assert c.WRITE_TEMPLATES == {n for n, s in c.SQL.items() if s.lstrip().upper().startswith("INSERT")}


def test_06_07_08_only_templates_can_be_sent(db):
    d1, _ = remote(db)
    with pytest.raises(KeyError):
        d1.run("DELETE FROM btc_data")
    with pytest.raises(KeyError):
        d1.run("UPDATE btc_data SET btc_price = 1")


def test_09_target_url_is_the_staging_database_only():
    assert c.STAGING_DB_PATH.endswith(f"/d1/database/{c.EXPECTED_STAGING_DATABASE_ID}")
    assert c.validate_target(GOOD_ENV).database_id == c.EXPECTED_STAGING_DATABASE_ID


@pytest.mark.parametrize("override", [
    {c.ENV_DATABASE_ID: "00000000-0000-0000-0000-000000000000"},
    {c.ENV_DATABASE_NAME: "pulseworker-v2-staging-2"},
    {c.ENV_ACCOUNT_ID: "0" * 32},
    {c.ENV_API_TOKEN: ""},
    {c.ENV_DATABASE_ID: ""},
])
def test_10_any_target_mismatch_fails_before_any_network_access(override):
    env = dict(GOOD_ENV, **override)
    with pytest.raises(c.TargetError):
        c.validate_target(env)
    out = io.StringIO()
    assert c.main(["verify"], env=env, out=out) == c.EXIT_TARGET_REFUSED
    assert "STAGING TARGET REFUSED" in out.getvalue()


def test_10_remote_identity_mismatch_aborts_before_any_sql(db):
    d1, net = remote(db, identity={"uuid": c.EXPECTED_STAGING_DATABASE_ID, "name": "something-else"})
    with pytest.raises(c.TargetError):
        d1.verify_remote_identity()
    assert all(not r["url"].endswith("/query") for r in net.requests)


@pytest.mark.parametrize("override", [
    {c.ENV_DATABASE_NAME: "sentiment-history"},
    {c.ENV_DATABASE_NAME: "SENTIMENT-HISTORY"},
    {c.ENV_DATABASE_ID: "f91ca980-b886-423a-bd6f-f3baea46d181"},
    {c.ENV_DATABASE_ID: "F91CA980-B886-423A-BD6F-F3BAEA46D181"},
])
def test_11_production_identifiers_are_rejected_as_production(override):
    # Matched on the explicit production refusal, not merely the later staging-triple mismatch.
    with pytest.raises(c.TargetError, match="PRODUCTION"):
        c.validate_target(dict(GOOD_ENV, **override))


def test_11_production_credential_value_is_rejected():
    with pytest.raises(c.TargetError, match="production credential"):
        c.validate_target(dict(GOOD_ENV, **{c.ENV_API_TOKEN: "same", "CLOUDFLARE_API_TOKEN": "same"}))


def test_11_production_token_is_never_read_as_a_fallback():
    env = {k: v for k, v in GOOD_ENV.items() if k != c.ENV_API_TOKEN}
    env["CLOUDFLARE_API_TOKEN"] = "production-token"
    with pytest.raises(c.TargetError):
        c.validate_target(env)


def test_12_repeated_btc_tick_in_one_slot_writes_one_row(db):
    first = c.btc_tick(db, PROV, obs(), now_ms())
    second = c.btc_tick(db, PROV, obs(price=64100.0, check=64100.0), now_ms())
    assert first[0] == "WRITTEN" and second[0] == "SKIPPED_DUPLICATE"
    assert second[1] == first[1]
    assert db.conn.execute("SELECT COUNT(*) FROM btc_data WHERE id NOT IN (7, 8)").fetchone()[0] == 1


def test_12_repeated_history_ingest_in_one_slot_writes_one_row(db):
    c.btc_tick(db, PROV, obs(), now_ms())
    first = c.history_ingest(db, PROV, capture(good_payload()), now_ms())
    second = c.history_ingest(db, PROV, capture(good_payload()), now_ms())
    assert (first[0], second[0]) == ("WRITTEN", "SKIPPED_DUPLICATE")
    assert db.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1


# ---------------------------------------------------------------------------------------------------------
# G1-C: write simulation through the REST code path
# ---------------------------------------------------------------------------------------------------------

def test_btc_row_through_rest_path_matches_staging_schema_and_server_clock(db):
    d1, net = remote(db)
    before = now_ms()
    status, row_id, row_ts, code = c.btc_tick(d1, PROV, c.fetch_btc_observation(opener=net), now_ms())
    after = now_ms()
    assert (status, code) == ("WRITTEN", c.EXIT_WRITTEN)
    row = db.conn.execute("SELECT id, ts, btc_price, typeof(ts) AS t FROM btc_data WHERE id = ?", (row_id,)).fetchone()
    assert row["t"] == "integer" and before - 1000 <= row["ts"] <= after + 1000 and row["ts"] == row_ts
    assert row["btc_price"] == 64000.0 and row_id == 9  # AUTOINCREMENT continues after the E9 fixture


def test_history_row_maps_every_payload_field_to_the_staging_columns(db):
    c.btc_tick(db, PROV, obs(), now_ms())
    status, row_id, _, _ = c.history_ingest(db, PROV, capture(good_payload()), now_ms())
    row = dict(db.conn.execute("SELECT * FROM history WHERE id = ?", (row_id,)).fetchone())
    assert status == "WRITTEN"
    assert row["score"] == 55 and row["technical_score"] == 60 and row["bottom_score"] == 17
    assert row["regime_mag"] is None and row["gold_regime"] == "competing-haven" and row["btc_price"] == 64000.5
    assert json.loads(row["sources_json"]) == good_payload()["sources"]
    assert row["sources_json"] == '{"fng":54,"funding":50,"global":38,"etfflows":95}'  # JSON.stringify form


def test_slot_arithmetic_floors_even_with_real_bound_parameters(db):
    rows, _ = db.run("btc_in_current_slot", [c.BTC_SLOT_MS] * 5)
    assert rows == []
    c.btc_tick(db, PROV, obs(), now_ms())
    rows, _ = db.run("btc_in_current_slot", [c.BTC_SLOT_MS] * 5)
    assert len(rows) == 1 and rows[0]["ts"] // c.BTC_SLOT_MS == now_ms() // c.BTC_SLOT_MS


def test_a_row_already_in_the_slot_from_any_writer_blocks_the_insert(db):
    db.conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, 1.0)", (now_ms(),))
    db.conn.commit()
    status, row_id, _, code = c.btc_tick(db, PROV, obs(), now_ms())
    assert status == "SKIPPED_DUPLICATE" and code == c.EXIT_SKIPPED_DUPLICATE


# ---------------------------------------------------------------------------------------------------------
# G1-D: provenance
# ---------------------------------------------------------------------------------------------------------

def ledger(db):
    return [dict(r) for r in db.conn.execute("SELECT * FROM staging_ingest_ledger ORDER BY rowid")]


def test_every_outcome_is_recorded_and_links_rows_unambiguously(db):
    c.btc_tick(db, PROV, obs(), now_ms())                                       # WRITTEN
    c.btc_tick(db, PROV, obs(), now_ms())                                       # SKIPPED_DUPLICATE
    c.btc_tick(db, PROV, obs(price=64000.0, check=70000.0), now_ms())           # REJECTED (cross-check)
    c.history_ingest(db, PROV, capture(None, no_observation_reason="only 30%"), now_ms())  # NO_OBSERVATION
    c.history_ingest(db, PROV, capture(dict(good_payload(), score=140)), now_ms())         # REJECTED
    c.history_ingest(db, PROV, capture(good_payload()), now_ms())               # WRITTEN
    entries = ledger(db)
    assert [(e["kind"], e["status"]) for e in entries] == [
        ("BTC_PRICE", "WRITTEN"), ("BTC_PRICE", "SKIPPED_DUPLICATE"), ("BTC_PRICE", "REJECTED"),
        ("V1_COMPOSITE", "NO_OBSERVATION"), ("V1_COMPOSITE", "REJECTED"), ("V1_COMPOSITE", "WRITTEN")]
    btc_written, btc_dup = entries[0], entries[1]
    assert btc_dup["target_row_id"] == btc_written["target_row_id"]
    for e in entries:
        assert e["git_sha"] == PROV.git_sha and e["run_id"] == PROV.run_id and e["server_ts"] > 0
        if e["status"] in ("WRITTEN", "SKIPPED_DUPLICATE"):
            row = db.conn.execute(f"SELECT ts FROM {e['target_table']} WHERE id = ?", (e["target_row_id"],)).fetchone()
            assert row["ts"] == e["target_ts"]
        else:
            assert e["target_row_id"] is None
    assert "differ" in json.loads(entries[2]["detail_json"])["reasons"][0]
    assert json.loads(entries[5]["payload_json"])["globalMcap"] == 3.1e12  # kept in provenance, no staging column
    ok, report = c.verify(db)
    assert ok, report["problems"]


def test_ledger_rejects_a_second_written_entry_for_the_same_row(db):
    c.btc_tick(db, PROV, obs(), now_ms())
    row_id = ledger(db)[0]["target_row_id"]
    with pytest.raises(sqlite3.IntegrityError):
        c.append_ledger(db, PROV, "BTC_PRICE", "WRITTEN", c.BTC_SLOT_MS, "x", None, None, {}, "btc_data", row_id, 1)


def test_verify_flags_a_row_written_by_anything_but_the_collector(db):
    db.conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (1, 2.0)")
    db.conn.commit()
    ok, report = c.verify(db)
    assert not ok and "without a WRITTEN ledger entry" in report["problems"][0]


@pytest.mark.parametrize("mutation,expect", [
    (lambda p: p.pop("sources"), "missing"),
    (lambda p: p.update(sources={}), "non-empty"),
    (lambda p: p.update(sources={"fng": "54"}), "not a number"),
    (lambda p: p.update(sources={"fng": 101}), "not a number"),
    (lambda p: p.update(sources={"made_up": 50}), "unknown source"),
    (lambda p: p.update(score=None), "score"),
    (lambda p: p.update(score=float("nan")), "score"),
    (lambda p: p.update(technicalScore=-1), "technicalScore"),
    (lambda p: p.update(btcPrice=0), "btcPrice"),
    (lambda p: p.update(goldRegime=7), "goldRegime"),
])
def test_invalid_history_payloads_are_rejected_not_inserted(db, mutation, expect):
    c.btc_tick(db, PROV, obs(), now_ms())
    payload = good_payload()
    mutation(payload)
    status, row_id, _, code = c.history_ingest(db, PROV, capture(payload), now_ms())
    assert (status, row_id, code) == ("REJECTED", None, c.EXIT_REJECTED)
    assert db.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    assert expect in ledger(db)[-1]["detail_json"]


@pytest.mark.parametrize("cap", [
    capture(good_payload(), fixture_mode=True),
    capture(good_payload(), cryptopulse_commit="f" * 40),
    capture(good_payload(), index_sha256="0" * 64),
    capture(good_payload(), captured_at_ms=now_ms() + 10 * 60 * 1000),   # future
    capture(good_payload(), captured_at_ms=now_ms() - 60 * 60 * 1000),   # stale
])
def test_untrusted_or_stale_captures_are_rejected(db, cap):
    c.btc_tick(db, PROV, obs(), now_ms())
    assert c.history_ingest(db, PROV, cap, now_ms())[0] == "REJECTED"
    assert db.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0


@pytest.mark.parametrize("o", [obs(price=0), obs(price=-5), obs(price=float("inf")), obs(price=500.0, check=500.0),
                               obs(check=None), obs(observed_ts=now_ms() + 600000),
                               obs(observed_ts=now_ms() - 3600000)])
def test_invalid_btc_observations_are_rejected_not_inserted(db, o):
    status, row_id, _, code = c.btc_tick(db, PROV, o, now_ms())
    assert (status, row_id, code) == ("REJECTED", None, c.EXIT_REJECTED)
    assert db.conn.execute("SELECT COUNT(*) FROM btc_data").fetchone()[0] == 2


def test_cross_check_threshold_is_one_percent(db):
    assert c.validate_btc_observation(obs(price=64000, check=64600), now_ms()) == []      # 0.93%
    assert c.validate_btc_observation(obs(price=64000, check=63300), now_ms()) != []      # 1.1%


# ---------------------------------------------------------------------------------------------------------
# G1-E: E9 fixture protection and ordering
# ---------------------------------------------------------------------------------------------------------

def test_e9_rows_untouched_and_never_attributed_to_the_collector(db):
    for _ in range(3):
        c.btc_tick(db, PROV, obs(), now_ms())
    c.history_ingest(db, PROV, capture(good_payload()), now_ms())
    rows = {r["id"]: (r["ts"], r["btc_price"]) for r in db.conn.execute("SELECT id, ts, btc_price FROM btc_data "
                                                                         "WHERE id IN (7, 8)")}
    assert rows == c.E9_FIXTURE_ROWS
    attributed = {r[0] for r in db.conn.execute("SELECT target_row_id FROM staging_ingest_ledger WHERE "
                                                 "target_table = 'btc_data' AND status = 'WRITTEN'")}
    assert attributed.isdisjoint(c.E9_FIXTURE_ROWS)
    first_btc = db.run("first_collected_btc")[0][0]
    collected_ts = db.conn.execute("SELECT MIN(ts) FROM btc_data WHERE id NOT IN (7, 8)").fetchone()[0]
    assert first_btc["first_ts"] == collected_ts > max(ts for ts, _ in c.E9_FIXTURE_ROWS.values())


def test_history_before_any_collected_btc_is_rejected_even_though_e9_rows_exist(db):
    status, row_id, _, code = c.history_ingest(db, PROV, capture(good_payload()), now_ms())
    assert (status, code) == ("REJECTED", c.EXIT_REJECTED)
    assert "precede the first real BTC row" in ledger(db)[-1]["detail_json"]
    assert db.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0


def test_history_insert_sql_itself_refuses_without_a_collected_btc_row(db):
    rows, _ = db.run("insert_history", c.history_row_params(good_payload()) + [c.HISTORY_SLOT_MS] * 5)
    assert rows == [] and db.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0


# ---------------------------------------------------------------------------------------------------------
# Schema / period gates, readiness, CLI
# ---------------------------------------------------------------------------------------------------------

def test_collection_refused_without_an_open_period(db):
    db.conn.execute("INSERT INTO staging_collection_periods (period_id, event, event_ts, git_sha, run_id) "
                    "VALUES ('local', 'CLOSED', 1, 'x', 'x')")
    db.conn.commit()
    with pytest.raises(c.NotReadyError):
        c.btc_tick(db, PROV, obs(), now_ms())


@pytest.mark.parametrize("ddl", ["CREATE TABLE stale_refresh_claim (coin TEXT)",
                                 "ALTER TABLE btc_data ADD COLUMN technical_score INTEGER",
                                 "ALTER TABLE history ADD COLUMN global_mcap REAL"])
def test_collection_refused_when_the_staging_schema_drifts(db, ddl):
    db.conn.execute(ddl)
    with pytest.raises(c.NotReadyError):
        c.btc_tick(db, PROV, obs(), now_ms())


def test_collection_refused_without_migration_0020():
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                       "btc_price REAL); CREATE TABLE history (id INTEGER PRIMARY KEY);")
    with pytest.raises(c.NotReadyError, match="migration 0020"):
        c.btc_tick(c.SqliteD1(conn), PROV, obs(), now_ms())


def _facts(**kw):
    base = {"history_written": 0, "history_first_ts": None, "history_last_ts": None, "btc_written": 0,
            "btc_last_ts": None, "exp5_runs": 0, "exp5_evaluable": 0, "server_ts": 10 ** 12}
    base.update(kw)
    return base


def test_readiness_states():
    now = 10 ** 12
    full = dict(history_written=300, history_first_ts=now - 15 * 86400000, history_last_ts=now, btc_written=700,
                btc_last_ts=now - 60000, server_ts=now)
    assert c.classify_readiness(_facts())["state"] == "COLLECTING"
    assert "no usable input" in c.classify_readiness(_facts())["detail"]
    assert c.classify_readiness(_facts(**dict(full, history_written=100)))["state"] == "COLLECTING"
    assert c.classify_readiness(_facts(**dict(full, btc_last_ts=now - 3 * 3600000)))["state"] == "COLLECTING"
    assert c.classify_readiness(_facts(**full))["state"] == "READY"
    assert c.classify_readiness(_facts(**dict(full, exp5_runs=3)))["state"] == "RUNNING"
    assert c.classify_readiness(_facts(**dict(full, exp5_runs=3, exp5_evaluable=5)))["state"] == "EVALUATING"
    concl = c.classify_readiness(_facts(**dict(full, exp5_runs=40, exp5_evaluable=25)))
    assert concl["state"] == "EVALUATING" and "CONCLUSION is blocked" in concl["detail"]


def test_readiness_and_snapshot_queries_run_against_the_staging_schema(db):
    c.btc_tick(db, PROV, obs(), now_ms())
    facts = db.run("readiness_facts")[0][0]
    assert facts["btc_written"] == 1 and c.classify_readiness(facts)["state"] == "COLLECTING"
    snap = c.snapshot(db)
    assert snap["btc_data"] == 3 and snap["ledger"] == 1 and snap["history"] == 0


def test_cli_dry_run_never_builds_a_remote_client(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("dry run must not open a remote client")
    monkeypatch.setattr(c, "_open_remote", refuse)
    monkeypatch.setattr(c, "fetch_btc_observation", lambda: obs())
    out = io.StringIO()
    assert c.main(["btc-tick", "--dry-run"], env={}, out=out) == c.EXIT_WRITTEN
    assert "RESULT: WRITTEN" in out.getvalue() and '"id": 9' in out.getvalue()


def test_token_never_printed_on_failure():
    target = c.validate_target(GOOD_ENV)
    assert "staging-token-for-tests" not in repr(target)
    assert "<redacted>" in c._redact("boom staging-token-for-tests", target)


def test_single_tick_delta_accepts_exactly_the_expected_changes(db):
    before = c.snapshot(db)
    db.conn.execute("INSERT INTO staging_collection_periods (period_id, event, event_ts, git_sha, run_id) "
                    "VALUES ('p1', 'OPENED', 1, 'x', 'x')")
    db.conn.commit()
    c.btc_tick(db, PROV, obs(), now_ms())
    c.history_ingest(db, PROV, capture(good_payload()), now_ms())
    c.btc_tick(db, PROV, obs(), now_ms())
    c.history_ingest(db, PROV, capture(good_payload()), now_ms())
    after = c.snapshot(db)
    assert c.check_single_tick_delta(before, after) == []
    db.conn.execute("INSERT INTO stage7_event_sentiment (id) VALUES (8)")
    db.conn.commit()
    assert c.check_single_tick_delta(before, c.snapshot(db)) == ["s7_sentiment changed: 0 -> 1",
                                                                  "s7_sentiment_max_id changed: None -> 8"]


def test_workflow_is_staging_only_manual_and_holds_only_the_collector_secret():
    yaml = pytest.importorskip("yaml")
    path = os.path.join(HERE, "..", ".github", "workflows", "staging-collector.yml")
    with open(path) as f:
        text = f.read()
    wf = yaml.safe_load(text)
    triggers = wf.get(True) or wf.get("on")
    assert set(triggers) == {"workflow_dispatch"}, "no schedule/push trigger until activation is authorized"
    assert wf["permissions"] == {"contents": "read"}
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", text)) == {"STAGING_COLLECTOR_CLOUDFLARE_API_TOKEN"}
    assert "CLOUDFLARE_API_TOKEN:" not in text.replace("STAGING_COLLECTOR_CLOUDFLARE_API_TOKEN:", "")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("wrangler", "workers.dev", "script.google", c.PRODUCTION_DATABASE_ID, c.PRODUCTION_DATABASE_NAME):
        assert forbidden not in code
    assert "vars.STAGING_COLLECTION_ENABLED == 'true'" in text
    assert c.PINNED_CRYPTOPULSE_COMMIT in text
