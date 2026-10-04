#!/usr/bin/env python3
"""
STAGING-ONLY collector: real public data -> the staging D1 database's existing btc_data and history tables.

    public data --> btc-tick ------> staging btc_data   (+ staging_ingest_ledger entry)
    public data --> history_harness.py (pinned CryptoPulse page, captured composite, nothing sent)
                --> history-ingest -> staging history    (+ staging_ingest_ledger entry)

There is no path from this module to production: no production database, Worker, Apps Script endpoint or
credential is reachable, by construction rather than by convention.

* Target: validate_target() accepts exactly one (account, name, id) triple -- pulseworker-v2-staging -- and
  refuses production's name or id under any spelling. verify_remote_identity() then asks Cloudflare to confirm
  the database before any SQL. There is no default and no fallback credential: only
  STAGING_COLLECTOR_CLOUDFLARE_API_TOKEN is read, and a value equal to CLOUDFLARE_API_TOKEN is refused.
* Network: every request goes through check_request_allowed(): Hyperliquid's public info endpoint, Coinbase's
  public spot price, and the staging database's own D1 API path. Everything else -- any *.workers.dev host,
  script.google.com, another D1 database -- raises before a socket is opened.
* SQL: the only statements this module can send are the fixed templates in SQL. Writes are four INSERT ... SELECT
  / INSERT ... VALUES templates into btc_data, history, staging_ingest_ledger and staging_collection_periods.
  There is no UPDATE, DELETE or schema statement, and no entry point that accepts caller-supplied SQL.
* Idempotency: each data INSERT is guarded by NOT EXISTS on its own table for the current fixed slot, computed
  from the DATABASE clock (30 min for BTC, 60 min for history), so a replayed or concurrent tick writes nothing.
* Provenance: every attempt -- WRITTEN, SKIPPED_DUPLICATE, NO_OBSERVATION, REJECTED -- is appended to
  staging_ingest_ledger (migration 0020). A btc_data/history row with no WRITTEN entry was not written here;
  that is how the two synthetic E9 fixture rows (btc_data ids 7 and 8) stay distinguishable.
* Ordering: a history row is only written once a collected BTC row exists at or before it (checked in Python
  and again inside the INSERT), so the first history observation can never be priced off the E9 fixture.

D1 binds JSON numbers as REAL (verified on staging), so every integer parameter is CAST(? AS INTEGER) in SQL.
"""
import argparse
import hashlib
import json
import math
import os
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

COLLECTOR_VERSION = "staging-collector-v1"

EXPECTED_STAGING_ACCOUNT_ID = "f58e761fbc8e62dc404d8684290af264"
EXPECTED_STAGING_DATABASE_NAME = "pulseworker-v2-staging"
EXPECTED_STAGING_DATABASE_ID = "5458d504-2778-49ae-bd25-7751f1c49d50"
# Named only so they can be refused explicitly.
PRODUCTION_DATABASE_NAME = "sentiment-history"
PRODUCTION_DATABASE_ID = "f91ca980-b886-423a-bd6f-f3baea46d181"
PRODUCTION_HOSTS = (
    "sentiment-ff75.quiquandon.workers.dev",   # V1 production Worker (writes production history)
    "pulseworker-v2.quiquandon.workers.dev",   # V2 production Worker
    "script.google.com",                       # CryptoPulse ALERTCONFIG_WRITE_URL (Apps Script)
)

ENV_ACCOUNT_ID = "STAGING_COLLECTOR_ACCOUNT_ID"
ENV_DATABASE_NAME = "STAGING_COLLECTOR_DATABASE_NAME"
ENV_DATABASE_ID = "STAGING_COLLECTOR_DATABASE_ID"
ENV_API_TOKEN = "STAGING_COLLECTOR_CLOUDFLARE_API_TOKEN"
ENV_GIT_SHA = "STAGING_COLLECTOR_GIT_SHA"
ENV_RUN_ID = "STAGING_COLLECTOR_RUN_ID"
PRODUCTION_TOKEN_ENV_VAR = "CLOUDFLARE_API_TOKEN"
REQUIRED_ENV_VARS = (ENV_ACCOUNT_ID, ENV_DATABASE_NAME, ENV_DATABASE_ID, ENV_API_TOKEN)

CLOUDFLARE_HOST = "api.cloudflare.com"
STAGING_DB_PATH = (f"/client/v4/accounts/{EXPECTED_STAGING_ACCOUNT_ID}"
                   f"/d1/database/{EXPECTED_STAGING_DATABASE_ID}")
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
# (method, host, exact path). Cloudflare is only ever the staging database's own metadata and /query paths.
ALLOWED_REQUESTS = frozenset({
    ("POST", "api.hyperliquid.xyz", "/info"),
    ("GET", "api.coinbase.com", "/v2/prices/BTC-USD/spot"),
    ("GET", CLOUDFLARE_HOST, STAGING_DB_PATH),
    ("POST", CLOUDFLARE_HOST, STAGING_DB_PATH + "/query"),
})
HTTP_TIMEOUT_S = 30
MAX_ERROR_CHARS = 1000

BTC_SLOT_MS = 30 * 60 * 1000
HISTORY_SLOT_MS = 60 * 60 * 1000
CROSS_CHECK_MAX_DIVERGENCE = 0.01          # Hyperliquid mark vs Coinbase spot, relative
BTC_PRICE_SANE_RANGE = (1_000.0, 10_000_000.0)
MAX_OBSERVATION_AGE_MS = 15 * 60 * 1000    # an observation older than this is stale, not collected
MAX_CLOCK_SKEW_MS = 60 * 1000              # an observation further in the future than this is invalid

# CryptoPulse commit whose unmodified index.html computes the composite (see history_harness.py).
PINNED_CRYPTOPULSE_COMMIT = "0a1dfb8ce88883336ee2e712a84e49be857b1724"
PINNED_CRYPTOPULSE_INDEX_SHA256 = "06e97b2dd8c0623ff7a87640720512a19914e3762f2aef13ff9514d803c9fe4a"
# COMPOSITE_SOURCES_DEFAULTS ids in that commit. A sources key outside this set is rejected.
KNOWN_SOURCE_IDS = frozenset({
    "fng", "funding", "longshort", "global", "cryptonews", "macrogeo", "geopolitics", "regulatory",
    "sosovalue", "onchain", "oil", "yield10y", "usd", "nasdaq", "sp500", "ninemag", "foufi", "etfflows",
    "hypefunding", "gold", "strc",
})
# The page's /history POST body keys (index.html, computeAndSyncCompositeScore) -> staging history columns.
HISTORY_PAYLOAD_KEYS = ("score", "technicalScore", "btcPrice", "sources", "goldRegime", "regimeMag",
                        "bottomScore", "globalMcap")

EXPECTED_BTC_DATA_COLUMNS = ("id", "ts", "btc_price")
EXPECTED_HISTORY_COLUMNS = ("id", "ts", "score", "technical_score", "bottom_score", "regime_mag",
                            "gold_regime", "sources_json", "btc_price")
REQUIRED_TABLES = ("btc_data", "history", "staging_ingest_ledger", "staging_collection_periods")
# Present on production, absent on staging. Their absence keeps the staging Worker's public /predict
# fallback and /btc-backfill routes unable to write btc_data; the collector refuses to run if either appears.
FORBIDDEN_TABLES = ("stale_refresh_claim",)

# The E9 synthetic fixture (Stage 7 acceptance). Never written by this module; verify checks it is unchanged.
E9_FIXTURE_ROWS = {7: (1790640300000, 60000.0), 8: (1790726700000, 63000.0)}

# Readiness (operational, not predictive): what "enough input for Experiment 5" means.
READY_MIN_HISTORY_SPAN_MS = 14 * 24 * 3600 * 1000      # experiment5_agent.DEFAULT_OBSERVE_WINDOW_MS
READY_MIN_HISTORY_ROWS = 269                           # 80% of hourly slots over 14 days
READY_MAX_BTC_AGE_MS = 2 * 3600 * 1000
CONCLUSION_MIN_EVALUABLE = 20                          # worker.js EXPERIMENT5_MIN_SAMPLE_FOR_CONCLUSION
PREREGISTERED_SUCCESS_CRITERION = None                 # none exists (research/README.md): CONCLUSION is blocked

SERVER_NOW = "CAST(ROUND((julianday('now') - 2440587.5) * 86400000) AS INTEGER)"

SQL = {
    # ---- the four write templates ----
    "insert_btc": (
        "INSERT INTO btc_data (ts, btc_price) "
        f"SELECT n.server_ts, ? FROM (SELECT {SERVER_NOW} AS server_ts) AS n "
        "WHERE NOT EXISTS (SELECT 1 FROM btc_data AS b "
        "WHERE b.ts >= (n.server_ts / CAST(? AS INTEGER)) * CAST(? AS INTEGER) "
        "AND b.ts < (n.server_ts / CAST(? AS INTEGER)) * CAST(? AS INTEGER) + CAST(? AS INTEGER)) "
        "RETURNING id, ts"
    ),
    "insert_history": (
        "INSERT INTO history (ts, score, technical_score, bottom_score, regime_mag, gold_regime, sources_json, "
        "btc_price) "
        f"SELECT n.server_ts, ?, ?, ?, ?, ?, ?, ? FROM (SELECT {SERVER_NOW} AS server_ts) AS n "
        "WHERE NOT EXISTS (SELECT 1 FROM history AS h "
        "WHERE h.ts >= (n.server_ts / CAST(? AS INTEGER)) * CAST(? AS INTEGER) "
        "AND h.ts < (n.server_ts / CAST(? AS INTEGER)) * CAST(? AS INTEGER) + CAST(? AS INTEGER)) "
        "AND EXISTS (SELECT 1 FROM staging_ingest_ledger AS l WHERE l.kind = 'BTC_PRICE' "
        "AND l.status = 'WRITTEN' AND l.target_ts <= n.server_ts) "
        "RETURNING id, ts"
    ),
    "insert_ledger": (
        "INSERT INTO staging_ingest_ledger (ingest_id, kind, status, slot_ms, target_table, target_row_id, "
        "target_ts, server_ts, source_name, source_observed_ts, payload_json, payload_sha256, detail_json, "
        "collector_version, git_sha, run_id) "
        "VALUES (?, ?, ?, CAST(? AS INTEGER), ?, CAST(? AS INTEGER), CAST(? AS INTEGER), "
        f"{SERVER_NOW}, ?, CAST(? AS INTEGER), ?, ?, ?, ?, ?, ?)"
    ),
    "insert_period_event": (
        "INSERT INTO staging_collection_periods (period_id, event, event_ts, note, snapshot_json, git_sha, run_id) "
        f"VALUES (?, ?, {SERVER_NOW}, ?, ?, ?, ?)"
    ),
    # ---- read-only templates ----
    "table_names": "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
    "btc_data_columns": "SELECT name FROM pragma_table_info('btc_data')",
    "history_columns": "SELECT name FROM pragma_table_info('history')",
    "btc_in_current_slot": (
        f"SELECT b.id, b.ts FROM btc_data AS b, (SELECT {SERVER_NOW} AS server_ts) AS n "
        "WHERE b.ts >= (n.server_ts / CAST(? AS INTEGER)) * CAST(? AS INTEGER) "
        "AND b.ts < (n.server_ts / CAST(? AS INTEGER)) * CAST(? AS INTEGER) + CAST(? AS INTEGER) "
        "ORDER BY b.id LIMIT 1"
    ),
    "history_in_current_slot": (
        f"SELECT h.id, h.ts FROM history AS h, (SELECT {SERVER_NOW} AS server_ts) AS n "
        "WHERE h.ts >= (n.server_ts / CAST(? AS INTEGER)) * CAST(? AS INTEGER) "
        "AND h.ts < (n.server_ts / CAST(? AS INTEGER)) * CAST(? AS INTEGER) + CAST(? AS INTEGER) "
        "ORDER BY h.id LIMIT 1"
    ),
    "first_collected_btc": (
        "SELECT MIN(target_ts) AS first_ts, COUNT(*) AS n FROM staging_ingest_ledger "
        "WHERE kind = 'BTC_PRICE' AND status = 'WRITTEN'"
    ),
    "latest_period_event": (
        "SELECT period_id, event, event_ts FROM staging_collection_periods ORDER BY event_id DESC LIMIT 1"
    ),
    "snapshot": (
        "SELECT (SELECT COUNT(*) FROM history) AS history, (SELECT COUNT(*) FROM btc_data) AS btc_data, "
        "(SELECT COUNT(*) FROM predictions) AS predictions, "
        "(SELECT COUNT(*) FROM research_sentiment_archive) AS archive, "
        "(SELECT COUNT(*) FROM research_hypotheses) AS hypotheses, "
        "(SELECT COUNT(*) FROM experiment5_pipeline_runs) AS exp5_runs, "
        "(SELECT COUNT(*) FROM research_events) AS events, "
        "(SELECT COUNT(*) FROM research_event_evidence) AS evidence, "
        "(SELECT COUNT(*) FROM stage7_research_requests) AS s7_requests, "
        "(SELECT MAX(updated_ts) FROM stage7_research_requests) AS s7_requests_max_updated_ts, "
        "(SELECT COUNT(*) FROM stage7_research_responses) AS s7_responses, "
        "(SELECT COUNT(*) FROM stage7_research_candidates) AS s7_candidates, "
        "(SELECT MAX(updated_ts) FROM stage7_research_candidates) AS s7_candidates_max_updated_ts, "
        "(SELECT COUNT(*) FROM stage7_event_sentiment) AS s7_sentiment, "
        "(SELECT MAX(id) FROM stage7_event_sentiment) AS s7_sentiment_max_id, "
        "(SELECT COUNT(*) FROM staging_ingest_ledger) AS ledger, "
        "(SELECT COUNT(*) FROM staging_collection_periods) AS period_events, "
        f"{SERVER_NOW} AS server_ts"
    ),
    "e9_fixture_rows": "SELECT id, ts, btc_price FROM btc_data WHERE id IN (7, 8) ORDER BY id",
    "unattributed_btc_rows": (
        "SELECT b.id, b.ts, b.btc_price FROM btc_data AS b WHERE NOT EXISTS (SELECT 1 FROM staging_ingest_ledger "
        "AS l WHERE l.target_table = 'btc_data' AND l.target_row_id = b.id AND l.status = 'WRITTEN') ORDER BY b.id"
    ),
    "unattributed_history_rows": (
        "SELECT h.id, h.ts FROM history AS h WHERE NOT EXISTS (SELECT 1 FROM staging_ingest_ledger AS l "
        "WHERE l.target_table = 'history' AND l.target_row_id = h.id AND l.status = 'WRITTEN') ORDER BY h.id"
    ),
    "duplicate_written_slots": (
        "SELECT kind, target_ts / slot_ms AS slot_index, COUNT(*) AS n FROM staging_ingest_ledger "
        "WHERE status = 'WRITTEN' GROUP BY kind, slot_index HAVING COUNT(*) > 1"
    ),
    "ledger_summary": "SELECT kind, status, COUNT(*) AS n FROM staging_ingest_ledger GROUP BY kind, status ORDER BY kind, status",
    "readiness_facts": (
        "SELECT (SELECT COUNT(*) FROM staging_ingest_ledger WHERE kind = 'V1_COMPOSITE' AND status = 'WRITTEN') "
        "AS history_written, "
        "(SELECT MIN(target_ts) FROM staging_ingest_ledger WHERE kind = 'V1_COMPOSITE' AND status = 'WRITTEN') "
        "AS history_first_ts, "
        "(SELECT MAX(target_ts) FROM staging_ingest_ledger WHERE kind = 'V1_COMPOSITE' AND status = 'WRITTEN') "
        "AS history_last_ts, "
        "(SELECT COUNT(*) FROM staging_ingest_ledger WHERE kind = 'BTC_PRICE' AND status = 'WRITTEN') "
        "AS btc_written, "
        "(SELECT MAX(target_ts) FROM staging_ingest_ledger WHERE kind = 'BTC_PRICE' AND status = 'WRITTEN') "
        "AS btc_last_ts, "
        "(SELECT COUNT(*) FROM experiment5_pipeline_runs) AS exp5_runs, "
        "(SELECT COUNT(*) FROM research_hypotheses WHERE subject LIKE 'experiment5:%' "
        "AND out_of_sample_status IN ('PASSED_HOLDOUT', 'FAILED_HOLDOUT')) AS exp5_evaluable, "
        f"{SERVER_NOW} AS server_ts"
    ),
}
WRITE_TEMPLATES = frozenset({"insert_btc", "insert_history", "insert_ledger", "insert_period_event"})
WRITABLE_TABLES = frozenset({"btc_data", "history", "staging_ingest_ledger", "staging_collection_periods"})

EXIT_WRITTEN = 0
EXIT_TARGET_REFUSED = 2
EXIT_FAILED = 3
EXIT_NOT_READY = 4          # schema or collection period not in the required state; nothing written
EXIT_SKIPPED_DUPLICATE = 5
EXIT_REJECTED = 6
EXIT_NO_OBSERVATION = 7
EXIT_VERIFY_FAILED = 8


class TargetError(RuntimeError):
    """The configured target is not provably the staging database. Raised before any network access."""


class NetworkPolicyError(RuntimeError):
    """A request outside ALLOWED_REQUESTS was attempted. Raised before any socket is opened."""


class D1Error(RuntimeError):
    """A D1 call failed. The message never contains the token."""


class NotReadyError(RuntimeError):
    """The staging schema or collection period is not in the state the collector requires."""


# ---------------------------------------------------------------------------------------------------------
# Target and network policy
# ---------------------------------------------------------------------------------------------------------

class StagingTarget:
    __slots__ = ("account_id", "database_name", "database_id", "_api_token")

    def __init__(self, account_id, database_name, database_id, api_token):
        self.account_id = account_id
        self.database_name = database_name
        self.database_id = database_id
        self._api_token = api_token

    @property
    def api_token(self):
        return self._api_token

    def __repr__(self):
        return (f"StagingTarget(account_id={self.account_id!r}, database_name={self.database_name!r}, "
                f"database_id={self.database_id!r}, api_token=<redacted>)")

    __str__ = __repr__


def _norm(value):
    return (value or "").strip()


def validate_target(env=None):
    """Pure, fail-closed. Returns a StagingTarget or raises TargetError; never touches the network."""
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV_VARS if not _norm(env.get(name))]
    if missing:
        raise TargetError(f"missing or empty staging configuration: {', '.join(missing)}. There is no default "
                          f"and no fallback to {PRODUCTION_TOKEN_ENV_VAR} or any other credential.")
    account_id = _norm(env[ENV_ACCOUNT_ID])
    database_name = _norm(env[ENV_DATABASE_NAME])
    database_id = _norm(env[ENV_DATABASE_ID])
    if database_name.lower() == PRODUCTION_DATABASE_NAME or database_id.lower() == PRODUCTION_DATABASE_ID:
        raise TargetError(f"the configured database is PRODUCTION (name={database_name!r}, id={database_id!r}).")
    expected = (EXPECTED_STAGING_ACCOUNT_ID, EXPECTED_STAGING_DATABASE_NAME, EXPECTED_STAGING_DATABASE_ID)
    if (account_id, database_name, database_id) != expected:
        raise TargetError(
            f"the configured target is not the staging database: got account_id={account_id!r}, "
            f"database_name={database_name!r}, database_id={database_id!r}; expected {expected!r}.")
    token = env[ENV_API_TOKEN].strip()
    if token and token == _norm(env.get(PRODUCTION_TOKEN_ENV_VAR)):
        raise TargetError(f"{ENV_API_TOKEN} equals {PRODUCTION_TOKEN_ENV_VAR} (the production credential's name).")
    return StagingTarget(account_id, database_name, database_id, token)


def check_request_allowed(method, url):
    """Raises NetworkPolicyError unless (method, host, path) is exactly one of ALLOWED_REQUESTS over https."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    key = (method.upper(), host, parsed.path)
    if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise NetworkPolicyError(f"refused {method} {url}: only plain https to an allowed host is permitted")
    if host in PRODUCTION_HOSTS or host.endswith(".workers.dev"):
        raise NetworkPolicyError(f"refused {method} {url}: production / workers.dev hosts are never contacted")
    if key not in ALLOWED_REQUESTS:
        raise NetworkPolicyError(f"refused {method} {url}: not in the collector's request allowlist")
    return key


def _redact(text, target):
    text = str(text)
    token = target.api_token if target is not None else ""
    if token:
        text = text.replace(token, "<redacted>")
    return text[:MAX_ERROR_CHARS]


def http_json(method, url, body=None, headers=None, opener=None, target=None):
    """The only HTTP entry point. Policy is checked before the request object is even built."""
    check_request_allowed(method, url)
    opener = opener or urllib.request.urlopen
    data = None if body is None else json.dumps(body).encode("utf-8")
    all_headers = {"Content-Type": "application/json", "User-Agent": COLLECTOR_VERSION}
    all_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method.upper(), headers=all_headers)
    try:
        with opener(request, timeout=HTTP_TIMEOUT_S) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
        raise D1Error(_redact(f"{method} {url} failed: HTTP {e.code} {detail}", target)) from None
    except urllib.error.URLError as e:
        raise D1Error(_redact(f"{method} {url} failed: {e.reason}", target)) from None
    try:
        return status, json.loads(raw)
    except (TypeError, ValueError):
        raise D1Error(f"{method} {url} returned a non-JSON response (HTTP {status})") from None


# ---------------------------------------------------------------------------------------------------------
# D1 access: only named templates, never caller SQL
# ---------------------------------------------------------------------------------------------------------

class StagingD1:
    """Remote staging database. run() only accepts a template name from SQL."""

    def __init__(self, target, opener=None):
        if not isinstance(target, StagingTarget):
            raise TargetError("StagingD1 requires a StagingTarget built by validate_target()")
        self.target = target
        self.opener = opener

    def _url(self, suffix=""):
        return f"https://{CLOUDFLARE_HOST}{STAGING_DB_PATH}{suffix}"

    def _auth(self):
        return {"Authorization": f"Bearer {self.target.api_token}"}

    def verify_remote_identity(self):
        status, parsed = http_json("GET", self._url(), headers=self._auth(), opener=self.opener, target=self.target)
        result = parsed.get("result") if isinstance(parsed, dict) else None
        if status != 200 or not isinstance(parsed, dict) or not parsed.get("success") or not isinstance(result, dict):
            raise TargetError("the D1 database lookup did not return the database description")
        uuid_, name = result.get("uuid"), result.get("name")
        if uuid_ != EXPECTED_STAGING_DATABASE_ID or name != EXPECTED_STAGING_DATABASE_NAME:
            raise TargetError(f"Cloudflare reports uuid={uuid_!r} name={name!r}; expected the staging database")
        return {"uuid": uuid_, "name": name}

    def run(self, name, params=()):
        sql = SQL[name]  # KeyError for anything that is not a template
        status, parsed = http_json("POST", self._url("/query"), body={"sql": sql, "params": list(params)},
                                   headers=self._auth(), opener=self.opener, target=self.target)
        if status != 200 or not isinstance(parsed, dict) or not parsed.get("success"):
            errors = parsed.get("errors") if isinstance(parsed, dict) else None
            raise D1Error(_redact(f"D1 {name} did not succeed: HTTP {status} errors={errors}", self.target))
        result = parsed.get("result")
        if not isinstance(result, list) or not result or not result[0].get("success", True):
            raise D1Error(f"D1 {name} returned no statement result")
        return result[0].get("results") or [], result[0].get("meta") or {}


class SqliteD1:
    """Local stand-in with the same run() contract, for tests and --dry-run. Binds every number as REAL,
    exactly as D1's HTTP API does, so the CAST(? AS INTEGER) requirement is exercised locally too."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def verify_remote_identity(self):
        return {"uuid": "local-sqlite", "name": "local-sqlite"}

    def run(self, name, params=()):
        sql = SQL[name]
        bound = [float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else p for p in params]
        cursor = self.conn.execute(sql, bound)
        rows = [dict(r) for r in cursor.fetchall()]
        meta = {"changes": cursor.rowcount if cursor.rowcount >= 0 else 0, "last_row_id": cursor.lastrowid}
        self.conn.commit()
        return rows, meta


def staging_schema_sql():
    """The staging database's own CREATE statements for the two data tables (read from staging, 2026-10-03),
    plus migration 0020. Used only to build a local SqliteD1."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", ".ai", "migrations", "0020_staging_ingest_ledger.sql")) as f:
        migration = f.read()
    return (
        "CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL);\n"
        "CREATE INDEX idx_btc_data_ts ON btc_data(ts);\n"
        "CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER, "
        "technical_score INTEGER, bottom_score INTEGER, regime_mag REAL, gold_regime TEXT, sources_json TEXT, "
        "btc_price REAL);\n"
        "CREATE INDEX idx_ts ON history(ts);\n" + migration
    )


def make_local_staging_replica(with_e9_fixture=True):
    conn = sqlite3.connect(":memory:")
    conn.executescript(staging_schema_sql())
    if with_e9_fixture:
        # Same ids as staging: 1..6 were used and deleted before the fixture, so AUTOINCREMENT continues at 9.
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('btc_data', 6)")
        for row_id, (ts, price) in E9_FIXTURE_ROWS.items():
            conn.execute("INSERT INTO btc_data (id, ts, btc_price) VALUES (?, ?, ?)", (row_id, ts, price))
    conn.execute("INSERT INTO staging_collection_periods (period_id, event, event_ts, git_sha, run_id) "
                 "VALUES ('local', 'OPENED', 0, 'local', 'local')")
    conn.commit()
    return SqliteD1(conn)


# ---------------------------------------------------------------------------------------------------------
# Validation (pure)
# ---------------------------------------------------------------------------------------------------------

def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_observation_ts(observed_ts, now_ms):
    if not isinstance(observed_ts, int) or isinstance(observed_ts, bool) or observed_ts <= 0:
        return "observation timestamp is not a positive integer (ms)"
    if observed_ts > now_ms + MAX_CLOCK_SKEW_MS:
        return "observation timestamp is in the future"
    if now_ms - observed_ts > MAX_OBSERVATION_AGE_MS:
        return f"observation is stale (older than {MAX_OBSERVATION_AGE_MS // 60000} min)"
    return None


def validate_btc_observation(obs, now_ms):
    """obs: {price, cross_check_price, observed_ts}. Returns a list of reasons; empty means valid."""
    reasons = []
    price, check = obs.get("price"), obs.get("cross_check_price")
    if not _finite_number(price) or price <= 0:
        reasons.append("price is not a positive finite number")
    elif not (BTC_PRICE_SANE_RANGE[0] <= price <= BTC_PRICE_SANE_RANGE[1]):
        reasons.append("price outside the sanity range")
    if not _finite_number(check) or check <= 0:
        reasons.append("cross-check price is not a positive finite number")
    if not reasons:
        divergence = abs(price - check) / check
        if divergence > CROSS_CHECK_MAX_DIVERGENCE:
            reasons.append(f"Hyperliquid and Coinbase differ by {divergence:.4%} (> {CROSS_CHECK_MAX_DIVERGENCE:.0%})")
    ts_reason = validate_observation_ts(obs.get("observed_ts"), now_ms)
    if ts_reason:
        reasons.append(ts_reason)
    return reasons


def _optional_0_100(payload, key, reasons):
    value = payload.get(key)
    if value is not None and (not _finite_number(value) or not 0 <= value <= 100):
        reasons.append(f"{key} must be null or a number in [0, 100]")


def validate_history_payload(payload, observed_ts, now_ms):
    """payload: the exact JSON body the pinned page would have POSTed to /history. Returns reasons."""
    reasons = []
    if not isinstance(payload, dict):
        return ["payload is not a JSON object"]
    missing = [k for k in HISTORY_PAYLOAD_KEYS if k not in payload]
    if missing:
        reasons.append(f"required field(s) missing: {', '.join(missing)}")
    score = payload.get("score")
    if not _finite_number(score) or not 0 <= score <= 100:
        reasons.append("score must be a number in [0, 100]")
    _optional_0_100(payload, "technicalScore", reasons)
    _optional_0_100(payload, "bottomScore", reasons)
    regime_mag = payload.get("regimeMag")
    if regime_mag is not None and not _finite_number(regime_mag):
        reasons.append("regimeMag must be null or a finite number")
    gold = payload.get("goldRegime")
    if gold is not None and (not isinstance(gold, str) or len(gold) > 64):
        reasons.append("goldRegime must be null or a short string")
    btc_price = payload.get("btcPrice")
    if btc_price is not None and (not _finite_number(btc_price) or btc_price <= 0):
        reasons.append("btcPrice must be null or a positive number")
    sources = payload.get("sources")
    if not isinstance(sources, dict) or not sources:
        reasons.append("sources must be a non-empty JSON object")
    else:
        unknown = sorted(set(sources) - KNOWN_SOURCE_IDS)
        if unknown:
            reasons.append(f"unknown source id(s): {', '.join(unknown)}")
        bad = sorted(k for k, v in sources.items() if not _finite_number(v) or not 0 <= v <= 100)
        if bad:
            reasons.append(f"source value(s) not a number in [0, 100]: {', '.join(bad)}")
    ts_reason = validate_observation_ts(observed_ts, now_ms)
    if ts_reason:
        reasons.append(ts_reason)
    return reasons


def history_row_params(payload):
    """Payload -> insert_history value params, in column order. globalMcap has no staging column (kept in the
    ledger payload). sources_json is serialised like the page's JSON.stringify (insertion order, compact)."""
    return [payload["score"], payload.get("technicalScore"), payload.get("bottomScore"), payload.get("regimeMag"),
            payload.get("goldRegime"), json.dumps(payload["sources"], separators=(",", ":")), payload.get("btcPrice")]


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------------
# Readiness (pure)
# ---------------------------------------------------------------------------------------------------------

def classify_readiness(facts):
    """facts: readiness_facts row. Operational states only -- never a predictive verdict."""
    now = facts.get("server_ts") or 0
    hist_n = facts.get("history_written") or 0
    span = (facts["history_last_ts"] - facts["history_first_ts"]) if hist_n else 0
    btc_last = facts.get("btc_last_ts")
    btc_fresh = btc_last is not None and now - btc_last <= READY_MAX_BTC_AGE_MS
    evaluable = facts.get("exp5_evaluable") or 0
    needs = []
    if hist_n < READY_MIN_HISTORY_ROWS:
        needs.append(f"history observations {hist_n}/{READY_MIN_HISTORY_ROWS}")
    if span < READY_MIN_HISTORY_SPAN_MS:
        needs.append(f"history span {span / 86400000:.2f}/{READY_MIN_HISTORY_SPAN_MS / 86400000:.0f} days")
    if not btc_fresh:
        needs.append("no collected BTC price in the last 2h")
    if needs:
        state = "COLLECTING"
        detail = ("no usable input yet" if hist_n == 0 else "insufficient data") + ": " + "; ".join(needs)
    elif (facts.get("exp5_runs") or 0) == 0:
        state, detail = "READY", "sufficient valid observations exist; Experiment 5 has not run on them"
    elif evaluable == 0:
        state, detail = "RUNNING", "Experiment 5 has run; no decision has reached its evaluated outcome yet"
    else:
        state = "EVALUATING"
        detail = f"{evaluable} evaluated decision(s)"
        if evaluable >= CONCLUSION_MIN_EVALUABLE and PREREGISTERED_SUCCESS_CRITERION is None:
            detail += ("; CONCLUSION is blocked: no success criterion was pre-registered (a human decision, "
                       "research/README.md)")
    return {"state": state, "detail": detail, "facts": facts}


# ---------------------------------------------------------------------------------------------------------
# Collector operations
# ---------------------------------------------------------------------------------------------------------

def check_schema_and_period(db, require_open_period=True):
    tables = {r["name"] for r in db.run("table_names")[0]}
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        raise NotReadyError(f"required table(s) missing: {', '.join(missing)} (apply migration 0020 to staging)")
    present_forbidden = [t for t in FORBIDDEN_TABLES if t in tables]
    if present_forbidden:
        raise NotReadyError(f"{', '.join(present_forbidden)} exists on staging: the staging Worker's /predict "
                            "fallback could now write btc_data. Refusing to collect.")
    btc_cols = tuple(r["name"] for r in db.run("btc_data_columns")[0])
    hist_cols = tuple(r["name"] for r in db.run("history_columns")[0])
    if btc_cols != EXPECTED_BTC_DATA_COLUMNS:
        raise NotReadyError(f"btc_data columns changed: {btc_cols} (expected {EXPECTED_BTC_DATA_COLUMNS}); a "
                            "technical_score column would re-enable the staging Worker's public /btc-backfill")
    if hist_cols != EXPECTED_HISTORY_COLUMNS:
        raise NotReadyError(f"history columns changed: {hist_cols} (expected {EXPECTED_HISTORY_COLUMNS})")
    if require_open_period:
        latest = db.run("latest_period_event")[0]
        if not latest or latest[0]["event"] != "OPENED":
            raise NotReadyError("no open collection period (run open-period first)")
        return latest[0]["period_id"]
    return None


class Provenance:
    def __init__(self, git_sha, run_id):
        self.git_sha = git_sha
        self.run_id = run_id


def append_ledger(db, prov, kind, status, slot_ms, source_name, observed_ts, payload, detail,
                  target_table=None, target_row_id=None, target_ts=None):
    payload_json = _canonical(payload) if payload is not None else None
    db.run("insert_ledger", [
        uuid.uuid4().hex, kind, status, slot_ms, target_table, target_row_id, target_ts, source_name, observed_ts,
        payload_json, _sha256(payload_json) if payload_json is not None else None, _canonical(detail),
        COLLECTOR_VERSION, prov.git_sha, prov.run_id,
    ])


def _slot_params(slot_ms):
    return [slot_ms] * 5


def record_insert_outcome(db, prov, kind, table, slot_ms, lookup_name, insert_result, source_name, observed_ts,
                          payload, detail, skip_reason_if_empty):
    """Turns the guarded INSERT's result into exactly one ledger entry and an exit code. The written row is
    taken from RETURNING; if a D1 response ever omits it while reporting one change, the slot lookup supplies
    the row (it is the only row the guard allows in the slot)."""
    rows, meta = insert_result
    row_id, row_ts = (rows[0]["id"], rows[0]["ts"]) if rows else (None, None)
    if row_id is None and meta.get("changes") == 1:
        found = db.run(lookup_name, _slot_params(slot_ms))[0]
        if found:
            row_id, row_ts = found[0]["id"], found[0]["ts"]
    if row_id is not None:
        append_ledger(db, prov, kind, "WRITTEN", slot_ms, source_name, observed_ts, payload, detail,
                      table, row_id, row_ts)
        return "WRITTEN", row_id, row_ts, EXIT_WRITTEN
    existing = db.run(lookup_name, _slot_params(slot_ms))[0]
    if existing:
        detail = dict(detail, reason="slot already holds a row; nothing inserted")
        append_ledger(db, prov, kind, "SKIPPED_DUPLICATE", slot_ms, source_name, observed_ts, payload, detail,
                      table, existing[0]["id"], existing[0]["ts"])
        return "SKIPPED_DUPLICATE", existing[0]["id"], existing[0]["ts"], EXIT_SKIPPED_DUPLICATE
    detail = dict(detail, reason=skip_reason_if_empty)
    append_ledger(db, prov, kind, "REJECTED", slot_ms, source_name, observed_ts, payload, detail)
    return "REJECTED", None, None, EXIT_REJECTED


def fetch_btc_observation(opener=None, clock=time.time):
    """V2 logBtcData's exact source (fetchHyperliquidPrice: metaAndAssetCtxs, universe name 'BTC', markPx)
    plus an independent Coinbase spot cross-check. Raises on transport errors."""
    _, info = http_json("POST", HYPERLIQUID_INFO_URL, body={"type": "metaAndAssetCtxs"}, opener=opener)
    observed_ts = int(clock() * 1000)
    meta, ctxs = info
    idx = next((i for i, u in enumerate(meta.get("universe") or []) if u.get("name") == "BTC"), -1)
    if idx < 0:
        raise ValueError("BTC not found in Hyperliquid universe")
    price = float(ctxs[idx]["markPx"])
    _, spot = http_json("GET", COINBASE_SPOT_URL, opener=opener)
    cross = float(spot["data"]["amount"])
    return {"price": price, "cross_check_price": cross, "observed_ts": observed_ts,
            "source": "hyperliquid:metaAndAssetCtxs:BTC.markPx", "cross_check_source": "coinbase:BTC-USD:spot"}


def btc_tick(db, prov, obs, now_ms):
    """Validates one BTC observation and writes it (or records why not). Returns (status, row_id, row_ts, code)."""
    check_schema_and_period(db)
    detail = {"cross_check_price": obs.get("cross_check_price"), "cross_check_source": obs.get("cross_check_source")}
    payload = {"btc_price": obs.get("price")}
    reasons = validate_btc_observation(obs, now_ms)
    if reasons:
        append_ledger(db, prov, "BTC_PRICE", "REJECTED", BTC_SLOT_MS, obs.get("source") or "unknown",
                      obs.get("observed_ts"), payload, dict(detail, reasons=reasons))
        return "REJECTED", None, None, EXIT_REJECTED
    result = db.run("insert_btc", [obs["price"]] + _slot_params(BTC_SLOT_MS))
    return record_insert_outcome(db, prov, "BTC_PRICE", "btc_data", BTC_SLOT_MS, "btc_in_current_slot", result,
                                 obs["source"], obs["observed_ts"], payload, detail,
                                 "insert skipped but the slot is empty on lookup")


def history_ingest(db, prov, capture, now_ms):
    """capture: history_harness.py's output. Returns (status, row_id, row_ts, code)."""
    check_schema_and_period(db)
    source_name = f"cryptopulse@{capture.get('cryptopulse_commit')}"
    observed_ts = capture.get("captured_at_ms")
    payload = capture.get("payload")
    detail = {"excluded_sources": capture.get("excluded_sources"), "index_sha256": capture.get("index_sha256"),
              "blocked_requests": capture.get("blocked_request_count"),
              "dropped_fields": {"globalMcap": (payload or {}).get("globalMcap")} if isinstance(payload, dict) else None}
    if capture.get("cryptopulse_commit") != PINNED_CRYPTOPULSE_COMMIT or \
            capture.get("index_sha256") != PINNED_CRYPTOPULSE_INDEX_SHA256:
        append_ledger(db, prov, "V1_COMPOSITE", "REJECTED", HISTORY_SLOT_MS, source_name, observed_ts, payload,
                      dict(detail, reasons=["capture is not from the pinned CryptoPulse commit/index.html"]))
        return "REJECTED", None, None, EXIT_REJECTED
    if capture.get("fixture_mode") is not False:
        append_ledger(db, prov, "V1_COMPOSITE", "REJECTED", HISTORY_SLOT_MS, source_name, observed_ts, payload,
                      dict(detail, reasons=["capture was produced from test fixtures, not live public data"]))
        return "REJECTED", None, None, EXIT_REJECTED
    if payload is None:
        append_ledger(db, prov, "V1_COMPOSITE", "NO_OBSERVATION", HISTORY_SLOT_MS, source_name, observed_ts, None,
                      dict(detail, reason=capture.get("no_observation_reason") or "the page produced no composite"))
        return "NO_OBSERVATION", None, None, EXIT_NO_OBSERVATION
    reasons = validate_history_payload(payload, observed_ts, now_ms)
    first_btc = db.run("first_collected_btc")[0][0]
    if not first_btc["n"]:
        reasons.append("no collected BTC observation exists yet: history must not precede the first real BTC row")
    if reasons:
        append_ledger(db, prov, "V1_COMPOSITE", "REJECTED", HISTORY_SLOT_MS, source_name, observed_ts, payload,
                      dict(detail, reasons=reasons))
        return "REJECTED", None, None, EXIT_REJECTED
    result = db.run("insert_history", history_row_params(payload) + _slot_params(HISTORY_SLOT_MS))
    return record_insert_outcome(db, prov, "V1_COMPOSITE", "history", HISTORY_SLOT_MS, "history_in_current_slot",
                                 result, source_name, observed_ts, payload, detail,
                                 "insert skipped: no collected BTC row at or before the database clock")


def snapshot(db):
    return db.run("snapshot")[0][0]


def verify(db):
    """Read-only checks for the controlled test and for monitoring. Returns (ok, report)."""
    problems = []
    e9 = {r["id"]: (r["ts"], r["btc_price"]) for r in db.run("e9_fixture_rows")[0]}
    if e9 != E9_FIXTURE_ROWS:
        problems.append(f"E9 fixture rows changed: {e9}")
    unattributed_btc = db.run("unattributed_btc_rows")[0]
    extra_btc = [r for r in unattributed_btc if r["id"] not in E9_FIXTURE_ROWS]
    if extra_btc:
        problems.append(f"btc_data rows without a WRITTEN ledger entry (not the E9 fixture): {extra_btc}")
    unattributed_history = db.run("unattributed_history_rows")[0]
    if unattributed_history:
        problems.append(f"history rows without a WRITTEN ledger entry: {unattributed_history}")
    duplicates = db.run("duplicate_written_slots")[0]
    if duplicates:
        problems.append(f"more than one WRITTEN row in a slot: {duplicates}")
    report = {"e9_fixture": e9, "unattributed_btc_rows": unattributed_btc,
              "unattributed_history_rows": unattributed_history, "duplicate_written_slots": duplicates,
              "ledger_summary": db.run("ledger_summary")[0], "problems": problems}
    return not problems, report


# Single-tick validation: exactly these snapshot counters may change, by exactly these amounts.
SINGLE_TICK_EXPECTED_DELTA = {"btc_data": 1, "history": 1, "ledger": 4, "period_events": 1}
SNAPSHOT_IGNORED_KEYS = ("server_ts",)


def check_single_tick_delta(before, after, expected=None):
    """Pure. Compares two snapshot() results: every counter must be unchanged except the expected deltas
    (one BTC row, one history row, two WRITTEN + two SKIPPED_DUPLICATE ledger entries, one OPENED event).
    Returns a list of problems; empty means the controlled test changed nothing else (Stage 7, Experiment 5,
    predictions, events, evidence and the E9 rows included)."""
    expected = SINGLE_TICK_EXPECTED_DELTA if expected is None else expected
    problems = []
    for key in sorted(set(before) | set(after)):
        if key in SNAPSHOT_IGNORED_KEYS:
            continue
        b, a = before.get(key), after.get(key)
        want = expected.get(key, 0)
        if want == 0 and a != b:
            problems.append(f"{key} changed: {b} -> {a}")
        elif want and (not isinstance(a, int) or not isinstance(b, int) or a - b != want):
            problems.append(f"{key} expected +{want}: {b} -> {a}")
    return problems


# ---------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------

def _provenance(env):
    git_sha = _norm(env.get(ENV_GIT_SHA)) or "unknown"
    run_id = _norm(env.get(ENV_RUN_ID)) or f"local:{socket.gethostname()}:{int(time.time())}"
    return Provenance(git_sha, run_id)


def _open_remote(env, out):
    target = validate_target(env)
    print(f"Validated staging target: account={target.account_id} database={target.database_name} "
          f"id={target.database_id}", file=out)
    db = StagingD1(target)
    identity = db.verify_remote_identity()
    print(f"Cloudflare confirms database uuid={identity['uuid']} name={identity['name']}", file=out)
    return db


def main(argv=None, env=None, out=None):
    out = out or sys.stdout
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="STAGING-ONLY collector (never production).")
    sub = parser.add_subparsers(dest="command", required=True)
    btc = sub.add_parser("btc-tick", help="collect one real BTC price into staging btc_data")
    btc.add_argument("--dry-run", action="store_true",
                     help="fetch and validate, then write only to a local in-memory replica (no staging access)")
    hist = sub.add_parser("history-ingest", help="ingest one history_harness.py capture into staging history")
    hist.add_argument("capture_file")
    hist.add_argument("--dry-run", action="store_true",
                      help="validate and write only to a local in-memory replica (no staging access)")
    period = sub.add_parser("open-period", help="append an OPENED collection-period event (with a snapshot)")
    period.add_argument("--period-id", required=True)
    period.add_argument("--note", default="")
    close = sub.add_parser("close-period", help="append a CLOSED collection-period event (with a snapshot)")
    close.add_argument("--period-id", required=True)
    close.add_argument("--note", default="")
    ver = sub.add_parser("verify", help="read-only checks: E9 fixture, provenance coverage, duplicate slots, counts")
    ver.add_argument("--json-out", help="also write the report (with snapshot) to this file")
    delta = sub.add_parser("check-delta", help="local only: compare two verify --json-out reports for a single tick")
    delta.add_argument("before")
    delta.add_argument("after")
    sub.add_parser("status", help="read-only readiness state for Experiment 5")
    args = parser.parse_args(argv)
    prov = _provenance(env)
    if args.command == "check-delta":  # pure local comparison: no target, no network
        with open(args.before) as f1, open(args.after) as f2:
            problems = check_single_tick_delta(json.load(f1)["snapshot"], json.load(f2)["snapshot"])
        print(json.dumps({"problems": problems}, indent=2), file=out)
        print(f"RESULT: {'DELTA OK' if not problems else 'DELTA FAILED'}", file=out)
        return EXIT_WRITTEN if not problems else EXIT_VERIFY_FAILED

    try:
        if getattr(args, "dry_run", False):
            db = make_local_staging_replica()
            print("DRY RUN: local in-memory replica of the staging schema (+ E9 fixture). Staging is not contacted.",
                  file=out)
            if args.command == "history-ingest":
                db.run("insert_btc", [1.0] + _slot_params(BTC_SLOT_MS))  # placeholder so ordering can pass
                rid = db.conn.execute("SELECT MAX(id) FROM btc_data").fetchone()[0]
                append_ledger(db, prov, "BTC_PRICE", "WRITTEN", BTC_SLOT_MS, "dry-run-placeholder", None, None, {},
                              "btc_data", rid, 0)
        else:
            db = _open_remote(env, out)
    except TargetError as e:
        print(f"STAGING TARGET REFUSED: {e}", file=out)
        return EXIT_TARGET_REFUSED
    except (D1Error, NetworkPolicyError) as e:
        print(f"FAILED before any write: {e}", file=out)
        return EXIT_FAILED

    try:
        if args.command == "btc-tick":
            obs = fetch_btc_observation()
            print(f"Observation: {json.dumps(obs)}", file=out)
            status, row_id, row_ts, code = btc_tick(db, prov, obs, int(time.time() * 1000))
            row = {"table": "btc_data", "id": row_id, "ts": row_ts, "btc_price": obs["price"]}
            print(f"RESULT: {status} {json.dumps(row)}", file=out)
            return code
        if args.command == "history-ingest":
            with open(args.capture_file) as f:
                capture = json.load(f)
            status, row_id, row_ts, code = history_ingest(db, prov, capture, int(time.time() * 1000))
            print(f"RESULT: {status} {json.dumps({'table': 'history', 'id': row_id, 'ts': row_ts})}", file=out)
            return code
        if args.command in ("open-period", "close-period"):
            check_schema_and_period(db, require_open_period=False)
            snap = snapshot(db)
            event = "OPENED" if args.command == "open-period" else "CLOSED"
            db.run("insert_period_event", [args.period_id, event, args.note, _canonical(snap), prov.git_sha,
                                           prov.run_id])
            print(f"RESULT: period {args.period_id} {event}; snapshot={json.dumps(snap)}", file=out)
            return EXIT_WRITTEN
        if args.command == "verify":
            ok, report = verify(db)
            report["snapshot"] = snapshot(db)
            print(json.dumps(report, indent=2, default=str), file=out)
            if args.json_out:
                with open(args.json_out, "w") as f:
                    json.dump(report, f, indent=2, default=str)
            print(f"RESULT: {'VERIFIED' if ok else 'VERIFY FAILED'}", file=out)
            return EXIT_WRITTEN if ok else EXIT_VERIFY_FAILED
        if args.command == "status":
            readiness = classify_readiness(db.run("readiness_facts")[0][0])
            print(json.dumps(readiness, indent=2), file=out)
            print(f"RESULT: {readiness['state']} -- {readiness['detail']}", file=out)
            return EXIT_WRITTEN
    except NotReadyError as e:
        print(f"NOT READY (nothing written): {e}", file=out)
        return EXIT_NOT_READY
    except (D1Error, NetworkPolicyError, ValueError, KeyError) as e:
        print(f"FAILED: {type(e).__name__}: {e}", file=out)
        return EXIT_FAILED
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
