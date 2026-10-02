#!/usr/bin/env python3
"""
Experiment 5: STAGING-ONLY runner for research/experiment5_pipeline.py.

A separate entry point, not a mode of scripts/experiment5-agent/run.py. That production adapter (hardcoded
D1_DATABASE = "sentiment-history", called by the scheduled live-evidence-collection.yml with the production
CLOUDFLARE_API_TOKEN) is left exactly as it is; nothing here imports it, wraps it, or changes its defaults.

What this runner guarantees, in order, before the pipeline touches anything:

1. validate_staging_target() -- pure, no network. Reads ONLY the EXP5_STAGING_* variables below. Fails closed
   when any is missing or empty, when the database name or id is production's (under any spelling), when the
   (account, name, id) triple is not exactly the expected staging triple, or when the staging token is the same
   value as a CLOUDFLARE_API_TOKEN present in the environment (the production secret's name). There is no
   default value for any identifier and no fallback to CLOUDFLARE_API_TOKEN or any other credential.
2. verify_remote_identity() -- one read-only metadata GET for exactly that account/database id; the database
   Cloudflare returns must report the same uuid AND the staging name, or the run aborts before any SQL.
3. Only then experiment5_pipeline.run_pipeline_recorded() runs -- unchanged, so its idempotency (archive by
   observation_ts, decisions by (subject, anchor_ts)), append-only outcome resolution, deferred same-run
   decisions and observe-window bounds are exactly the reviewed behaviour. Its run record is written to
   migration 0019's experiment5_pipeline_runs table.

Transport: the Cloudflare D1 HTTP API (/query) with the validated account id, database id and token -- never
wrangler, which resolves a database through wrangler.toml (production's config). Errors name the HTTP status and
Cloudflare's error messages; the token is never printed (and is redacted from any message as a safeguard).

Stricter than the production adapter by design: a run whose operational record was not WRITTEN (migration 0019
missing on staging, or the record write failed) exits non-zero, because recording the run is part of what a
staging run is for.

This file contains no GitHub API access and no ability to dispatch any workflow.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "research"))
import experiment5_pipeline as ep  # noqa: E402

EXPECTED_STAGING_ACCOUNT_ID = "f58e761fbc8e62dc404d8684290af264"
EXPECTED_STAGING_DATABASE_NAME = "pulseworker-v2-staging"
EXPECTED_STAGING_DATABASE_ID = "5458d504-2778-49ae-bd25-7751f1c49d50"

# Named only so they can be refused explicitly, with a clear message.
PRODUCTION_DATABASE_NAME = "sentiment-history"
PRODUCTION_DATABASE_ID = "f91ca980-b886-423a-bd6f-f3baea46d181"

ENV_ACCOUNT_ID = "EXP5_STAGING_ACCOUNT_ID"
ENV_DATABASE_NAME = "EXP5_STAGING_DATABASE_NAME"
ENV_DATABASE_ID = "EXP5_STAGING_DATABASE_ID"
ENV_API_TOKEN = "EXP5_STAGING_CLOUDFLARE_API_TOKEN"
REQUIRED_ENV_VARS = (ENV_ACCOUNT_ID, ENV_DATABASE_NAME, ENV_DATABASE_ID, ENV_API_TOKEN)
PRODUCTION_TOKEN_ENV_VAR = "CLOUDFLARE_API_TOKEN"

API_BASE = "https://api.cloudflare.com/client/v4"
HTTP_TIMEOUT_S = 60
MAX_ERROR_CHARS = 1000

EXIT_OK = 0
EXIT_TARGET_REFUSED = 2
EXIT_PIPELINE_FAILED = 3
EXIT_RUN_NOT_RECORDED = 4


class StagingTargetError(RuntimeError):
    """The configured target is not, provably, the staging database. Raised before any network access."""


class D1RequestError(RuntimeError):
    """A D1 HTTP API call failed. The message never contains the token."""


class StagingTarget:
    """The single validated target every remote call reads from. Only built by validate_staging_target()."""

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


def validate_staging_target(env=None):
    """Pure and fail-closed. Returns a StagingTarget or raises StagingTargetError. Never touches the network."""
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV_VARS if not _norm(env.get(name))]
    if missing:
        hint = ""
        if ENV_API_TOKEN in missing and _norm(env.get(PRODUCTION_TOKEN_ENV_VAR)):
            hint = (f" {PRODUCTION_TOKEN_ENV_VAR} is set but is never used by this runner: it is the production "
                    f"credential's name, and there is no fallback to it.")
        raise StagingTargetError(
            f"Refusing to run: missing or empty staging configuration: {', '.join(missing)}.{hint} "
            "Aborted before any remote access."
        )

    account_id = _norm(env[ENV_ACCOUNT_ID])
    database_name = _norm(env[ENV_DATABASE_NAME])
    database_id = _norm(env[ENV_DATABASE_ID])
    api_token = env[ENV_API_TOKEN].strip()

    if database_name.lower() == PRODUCTION_DATABASE_NAME or database_id.lower() == PRODUCTION_DATABASE_ID:
        raise StagingTargetError(
            f"Refusing to run: the configured database is PRODUCTION (name={database_name!r}, id={database_id!r}). "
            "This runner never touches production. Aborted before any remote access."
        )

    expected = (EXPECTED_STAGING_ACCOUNT_ID, EXPECTED_STAGING_DATABASE_NAME, EXPECTED_STAGING_DATABASE_ID)
    if (account_id, database_name, database_id) != expected:
        raise StagingTargetError(
            "Refusing to run: the configured target is not the expected staging database. "
            f"Got account_id={account_id!r}, database_name={database_name!r}, database_id={database_id!r}; "
            f"expected account_id={expected[0]!r}, database_name={expected[1]!r}, database_id={expected[2]!r}. "
            "Aborted before any remote access."
        )

    production_token = _norm(env.get(PRODUCTION_TOKEN_ENV_VAR))
    if production_token and production_token == api_token:
        raise StagingTargetError(
            f"Refusing to run: {ENV_API_TOKEN} has the same value as {PRODUCTION_TOKEN_ENV_VAR} (the production "
            "credential's name). Use the dedicated staging token. Aborted before any remote access."
        )

    return StagingTarget(account_id, database_name, database_id, api_token)


def _redact(text, target):
    text = str(text)
    token = target.api_token if target is not None else ""
    if token:
        text = text.replace(token, "<redacted>")
    return text[:MAX_ERROR_CHARS]


def _call(target, method, path, payload=None, opener=None):
    """One D1 HTTP API call. `opener` is urllib.request.urlopen unless a test injects a fake."""
    opener = opener or urllib.request.urlopen
    url = f"{API_BASE}/accounts/{target.account_id}/d1/database/{target.database_id}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {target.api_token}", "Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=HTTP_TIMEOUT_S) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
        raise D1RequestError(_redact(f"D1 API {method} {path or '/'} failed: HTTP {e.code} {detail}", target)) from None
    except urllib.error.URLError as e:
        raise D1RequestError(_redact(f"D1 API {method} {path or '/'} failed: {e.reason}", target)) from None
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        raise D1RequestError(f"D1 API {method} {path or '/'} returned a non-JSON response (HTTP {status})") from None
    if status != 200 or not isinstance(parsed, dict) or not parsed.get("success"):
        errors = parsed.get("errors") if isinstance(parsed, dict) else None
        raise D1RequestError(_redact(f"D1 API {method} {path or '/'} did not succeed: HTTP {status} errors={errors}", target))
    return parsed.get("result")


def verify_remote_identity(target, opener=None):
    """Read-only. Confirms Cloudflare itself reports the configured id as the staging database before any SQL."""
    result = _call(target, "GET", "", opener=opener)
    if not isinstance(result, dict):
        raise StagingTargetError("Refusing to run: the D1 database lookup returned no database description.")
    uuid, name = result.get("uuid"), result.get("name")
    if uuid != target.database_id or name != target.database_name:
        raise StagingTargetError(
            f"Refusing to run: Cloudflare reports database uuid={uuid!r} name={name!r} for the configured id, "
            f"expected uuid={target.database_id!r} name={target.database_name!r}. No SQL was sent."
        )
    if name == PRODUCTION_DATABASE_NAME or uuid == PRODUCTION_DATABASE_ID:
        raise StagingTargetError("Refusing to run: Cloudflare identifies the database as PRODUCTION. No SQL was sent.")
    return {"uuid": uuid, "name": name}


def make_d1_functions(target, opener=None):
    """The (query, execute) pair experiment5_pipeline expects, bound to the validated staging target only."""

    def d1_query(sql):
        result = _call(target, "POST", "/query", {"sql": sql}, opener=opener)
        if not isinstance(result, list) or not result:
            raise D1RequestError("D1 API response is missing the statement result list")
        statement = result[0]
        if not statement.get("success", True):
            raise D1RequestError(_redact(f"D1 statement did not succeed: {statement.get('error')}", target))
        return statement.get("results") or []

    def d1_execute(sql):
        d1_query(sql)

    return d1_query, d1_execute


def main(env=None, opener=None, now_ms=None, out=None):
    """Returns a process exit code. Every refusal happens before the pipeline is called."""
    out = out or sys.stdout
    try:
        target = validate_staging_target(env)
    except StagingTargetError as e:
        print(f"STAGING TARGET REFUSED: {e}", file=out)
        return EXIT_TARGET_REFUSED
    print(f"Validated staging target: account={target.account_id} database={target.database_name} "
          f"id={target.database_id}", file=out)

    try:
        identity = verify_remote_identity(target, opener=opener)
    except (StagingTargetError, D1RequestError) as e:
        print(f"STAGING TARGET REFUSED: {e}", file=out)
        return EXIT_TARGET_REFUSED
    print(f"Cloudflare confirms database uuid={identity['uuid']} name={identity['name']}", file=out)

    d1_query, d1_execute = make_d1_functions(target, opener=opener)
    now_ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    try:
        summary = ep.run_pipeline_recorded(d1_query, d1_execute, now_ts)
    except Exception as e:  # run_pipeline_recorded has already tried to record the FAILED run
        print(f"PIPELINE FAILED: {_redact(f'{type(e).__name__}: {e}', target)}", file=out)
        return EXIT_PIPELINE_FAILED

    print("=== Experiment 5 STAGING run -- summary ===", file=out)
    print(json.dumps(summary, indent=2, default=str), file=out)
    if summary.get("run_record") != "WRITTEN":
        reason = summary.get("run_record_error") or "migration 0019 (experiment5_pipeline_runs) is not applied"
        print(f"RUN NOT RECORDED: {summary.get('run_record')}: {_redact(reason, target)}", file=out)
        return EXIT_RUN_NOT_RECORDED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
