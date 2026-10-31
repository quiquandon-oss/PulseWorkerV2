# Proposal: port PR #82's remaining Stage 7 safety fixes onto `main`, without the feature switch (H4)

**Status (updated 2026-10-31):** P1–P6 are implemented on branch `fix/stage7-registration-safety`. The PR is not
merged and not deployed; see "Implementation" at the end. Base: `main` at `0e61c5f`; PR #82 head `b3077dd`
(merge-base `280d2d5`, still open; CI green on that head from 2026-10-01).

## Verified facts (2026-10-31)

1. Production Stage 7 is live on migration **0016** only:
   - `GET /api/research-lab/stage7-overview` returns `activated: true` with 10 requests.
   - `GET /api/learning/market` returns `stage7_available: true`.
   - `main` has no migration 0017 or 0018 file, and none of the 13 functions #82 adds exists on `main`.
2. #82 is not a fix list: it is +5,605 / −523 lines over 27 files, including about 1,800 lines of `worker.js`. It adds
   a candidate → prompt → paste-back → review → recalculation workflow on the new tables in **0017 and 0018**.
3. `main`'s production Stage 7 surface is two routes:
   - `GET /api/research-lab/stage7-overview` (`getResearchLabStage7Overview`, worker.js:6756);
   - token-gated `POST /api/research-lab/stage7-register-response` (`registerStage7ResearchResponse`, worker.js:6902).

   The learning loop (`/api/learning/*`) reads the same 0016 tables.
4. Defects on `main` that #82 fixes, each reproduced by reading `main`'s code:
   - **D1:** `getResearchLabStage7Overview` returns "migration 0016 is not yet applied" for **any** error from its
     first query, so a transient D1 failure is reported as a missing migration.
   - **D2:** the register route parses the body inside a `try` and returns `500 String(err)` on malformed JSON. The
     V8 parser message quotes part of the input. Run locally in Node, a body starting `{"token": secretvalue` produced
     `Unexpected token 's', ..."{"token": secretvalu"... is not valid JSON`. Malformed input should be a 400 with no
     echo.
   - **D3:** the same catch returns raw D1 error text (`String(err)`) to the caller. #82 maps it to a generic
     `STAGE7_INTERNAL_ERROR`, or to `STAGE7_SCHEMA_NOT_READY` for "no such table/column".
   - **D4:** registration is check-then-insert. Two concurrent submissions both pass the existence check, and the
     loser's `INSERT` hits the `response_id` PRIMARY KEY and surfaces as a 500 with SQL error text. #82 returns 409.
   - **D5:** an identical retry after a successful registration returns 409, not an idempotent 200. A client retry
     after a dropped response looks like a failure.
   - **D6:** the response `INSERT` and the request status `UPDATE` are separate statements. A failure between them
     leaves a response whose request still shows `PENDING`.
   - **D7 (corrected):** `stage7-staging-dispatcher.yml` on `main` still *declares* a daily cron (`0 11 * * *`) that
     dispatches the pipeline on the old branch `claude/stage7-research-pipeline`. **But the workflow is
     `disabled_manually` (since 2026-10-01)**, as are `stage7-research-pipeline.yml` and `deploy-stage7-staging.yml`
     (since 2026-10-04). Its last run was 2026-09-30, so the cron is dormant. The earlier "still fires daily" claim was
     wrong.
5. Merging #82 into `main` as is fails 8 learning-loop tests (500 instead of 200), because `stage7RoutePreflight`
   refuses everything unless `STAGE7_ENABLED === "true"`, and production never sets it.

## Assumptions (not verified)

- **A1:** the Workers runtime produces the same JSON parse message as Node's V8. Both use V8, but this was not
  reproduced on workerd.
- **A2:** nobody depends on the current 409 for an identical retry.
- **A3:** withdrawn. There are no recent D7 dispatches, because the workflows are disabled (verified through the
  GitHub API).
- **A4:** whether the owner wants #82's full candidate workflow (0017/0018) is undecided. It is out of scope here.

## Proposed change set (option 1 of the finish-line note)

One PR against `main`, `worker.js` and tests only. No migration, no new switch, no change to `/api/learning/*`.

| # | Fix | Exact change | Ported from #82 |
|---|---|---|---|
| P1 | D1 | Add `STAGE7_REQUIRED_SCHEMA` (**0016 rows only**, plus `research_events` 0005), `STAGE7_SCHEMA_PROBE_SQL`, `classifyStage7Schema`, `getStage7SchemaStatus`. The overview returns `activated: false` only when the probe says NOT_APPLIED or PARTIAL; a probe failure returns `CHECK_FAILED` ("not evidence of a missing migration") | yes, schema list trimmed to 0016 |
| P2 | D2, D3 | Add `stage7SafeError`. In the register route: parse the JSON in its own `try` and return 400 `"request body must be valid JSON"`; send the outer catch through `stage7SafeError`. Same for the overview route's catch | yes |
| P3 | D4 | Wrap the response `INSERT`; a UNIQUE/constraint error returns 409 "registered concurrently", anything else is rethrown to `stage7SafeError` | yes |
| P4 | D5 | Identical retry returns 200 `already_registered: true`. **Adapted:** #82 compares `raw_response_text` (a 0017 column); on 0016 the comparison is `findings_json` + `sources_json` + `confidence` + `provider`, all exact. Different content stays 409 | adapted |
| P5 | D6 | Write the `INSERT` and the status `UPDATE` in one `env.DB.batch([...])` (atomic in D1) | #82's atomic-batch pattern |
| P6 | write preflight without the switch | A `stage7WritePreflight(env, providedToken)`: token check (503 if not configured, 401 if wrong; constant-time, as today), then the schema probe (503 `STAGE7_SCHEMA_NOT_READY` or `CHECK_FAILED`). **No `STAGE7_ENABLED`**, and no read route is gated | yes, minus `stage7Enabled` |

**Not ported (needs migrations 0017/0018, so separate production authorization under H4):** the candidate workflow,
prompt text, response review endpoint, recalculation lifecycle and claim, `human_confirmed` gate, storing
`raw_response_text` / `source_validation_json`, and the local acceptance harness. `validateStage7Sources` could be
ported as response-only feedback (no stored column) in a later PR.

**Workflows (D7) are a schedule change, so a separate decision:** remove the `0 11 * * *` cron from
`stage7-staging-dispatcher.yml` (manual only), and port #82's `expected_sha` binding for the staging deploy. Proposed
as a second, independent PR, not included above.

## Tests required

New `tests/stage7-routes.test.js`, adapted from #82, through the real `worker.js` fetch handler against real SQLite
with migration 0016:

1. Overview: tables present → `activated: true`. Tables missing → `activated: false` and `NOT_APPLIED`. A failing
   D1 (throwing stub) → `CHECK_FAILED`, never "not applied".
2. Register:
   - malformed JSON → 400, and the response body contains none of the input bytes;
   - token unset → 503; wrong token → 401 (before any D1 call: assert zero queries);
   - schema missing → 503 `STAGE7_SCHEMA_NOT_READY`.
3. Register success writes exactly one response row and moves the request status, in one batch. A forced failure of
   the second statement leaves **no** response row.
4. Identical retry → 200 `already_registered`, still one row. Different content → 409, row unchanged. Simulated
   concurrent insert (UNIQUE error) → 409 with no SQL text.
5. Any internal error → 500 `STAGE7_INTERNAL_ERROR` with no `err.message` in the body. "no such table" → 503.
6. **Production-behaviour guard:** the full existing `tests/learning-loop.test.js` (the 8 tests that fail with #82)
   and `tests/stage7-research.test.js` pass unchanged, and `STAGE7_ENABLED` appears nowhere in `worker.js` or
   `wrangler.toml`.
7. Python: port `stage7-research-pipeline/test_worker_schema_contract.py` (checks the Worker's Stage 7 SQL against
   real SQLite built from the migrations), restricted to 0016.

**Acceptance:** `npm test` and `pytest` green in CI. After the owner-approved merge, live checks:
- `stage7-overview` still `activated: true` with the same request count;
- `/api/learning/market` still `stage7_available: true`;
- an unauthenticated malformed POST returns 401, because the token check runs before body parsing (P6), so it never
  echoes. That probe writes nothing.

**Rollback:** revert the single merge commit. No schema or data change is involved.

## What happens to PR #82 afterwards

Once P1–P6 are on `main`, the only unique value left in #82 is the 0017/0018 workflow. The owner then chooses:
- close #82 as superseded for production safety; or
- rebuild the candidate workflow on top of `main` as its own migration-gated proposal.

## Implementation (branch `fix/stage7-registration-safety`)

**Files changed:** `worker.js`, `tests/stage7-routes.test.js` (new), `tests/stage7-research.test.js` and
`tests/learning-loop.test.js` (harness only: a D1 `batch` method and the new helpers in the extraction list; no
assertion changed). No migration, workflow, schedule, model, experiment or plan change.

| Item | Implemented as |
|---|---|
| P1 | `classifyStage7DbError` and `stage7OverviewSchemaResult`. The existing overview queries are kept unchanged (no extra probe query). Results: a "no such table/column" error on the first query means `NOT_APPLIED`; on the later queries it means `PARTIAL`, with the missing object named. Any other D1 error returns 503 `STAGE7_SCHEMA_CHECK_FAILED` ("not evidence that a migration is missing"). |
| P2 | The register route parses JSON in its own `try` and returns 400 `INVALID_JSON` without echoing the input. The overview, register and learning routes map unexpected errors to generic codes (`STAGE7_INTERNAL_ERROR`, `STAGE7_SCHEMA_NOT_READY`, `LEARNING_INTERNAL_ERROR`), with the original error only in the Worker log (`console.error`). Error messages no longer echo the caller's `request_id`. |
| P3 | Concurrency safety comes from migration 0016's own constraints (`response_id` PRIMARY KEY, `UNIQUE(request_id)`), not from the pre-check. Only a **uniqueness** violation is treated as a competing registration: the stored row is re-read and the result is 200 (identical) or 409 (different). Other constraint failures (FOREIGN KEY, NOT NULL, CHECK), and a uniqueness violation with no stored row to explain it, return a generic 500 with nothing written. |
| P4 | `sameStage7Registration` compares `provider`, `submitted_ts`, `confidence` and `validation_status` exactly, and `findings_json` / `sources_json` as canonical JSON (object key order ignored; array order, values and types exact; malformed stored JSON never equal). These are all 0016 columns. An identical payload returns 200 `already_registered`; any difference returns 409 and nothing is written. The stored text is the first registration's, verbatim. |
| P5 | `INSERT` and status `UPDATE` run in one `env.DB.batch`. Cloudflare documents a D1 batch as one SQL transaction that is rolled back if a statement fails (developers.cloudflare.com/d1/worker-api/d1-database/#batch). |
| P6 | `stage7AuthorizeWrite`: 503 when the token is not configured, 401 when it is missing or wrong. The register route checks it **before** parsing the body or touching D1, and `registerStage7ResearchResponse` still checks it for the learning-loop caller. No `STAGE7_ENABLED`; read routes are not gated. |

**Additional corrections (same route surface):**
1. **Terminal race:** the `INSERT` is now conditional on the request being non-terminal (`INSERT … SELECT … WHERE
   EXISTS (… status NOT IN ('INTEGRATED','REJECTED'))`), the same predicate as the `UPDATE`. A request that became
   terminal between the check and the write previously still received a response row; now it gets 409 and nothing is
   written.
2. **Learning routes:** the `/api/learning/*` catch returned `String(err)`. `/api/learning/findings` writes Stage 7
   responses through the same function, so its D1 errors could reach callers. It now returns a generic
   `LEARNING_INTERNAL_ERROR`.

**Tests:**
- `tests/stage7-routes.test.js`: 19 tests through the real `worker.js` fetch handler on real SQLite built from
  migrations 0005 and 0016. They cover schema present, missing and partial; transient failure; malformed JSON;
  secret-string leakage; token unset, missing and wrong with zero D1 access; auth before body parsing; success and
  status transition; identical retry; conflicting retry; a concurrent UNIQUE race (both outcomes); batch rollback when
  the UPDATE fails; the terminal race; SELECT-only reads; 0016-only columns; no `STAGE7_ENABLED`; generic learning
  errors.
- Against unpatched `main`, 13 of the 19 fail, reproducing the defects. The 6 that pass are behaviour `main` already
  had.
- **Limit:** SQLite stands in for D1. The suite proves the Worker's logic under documented batch semantics, not D1's
  production behaviour. The D1 error-message formats ("no such table: …", "UNIQUE constraint failed") are assumed
  from SQLite and D1 conventions and were not observed on production.

**Dispatcher recommendation (separate PR, owner decision):** the workflows are disabled, so nothing runs today.
Before anyone re-enables them, change the dispatcher to manual-only (remove the cron) and point it at a reviewed ref,
because `claude/stage7-research-pipeline` (`08da540`) predates the learning loop.
- Writes: the pipeline writes to the **staging** D1 only, guarded by `validate_staging_target`, and git-pushes
  request files.
- Dependencies: no other workflow or code path depends on the dispatcher.
- No action is needed while the workflows stay disabled.

**Adversarial review (2026-10-31), two corrections:**
1. The constraint classifier matched any constraint failure, so a FOREIGN KEY or NOT NULL error would have been
   reported as "registered concurrently" (409). It is now limited to UNIQUE/PRIMARY KEY. A uniqueness violation that
   no stored row explains now fails (500) instead of claiming a registration. Neither path is reachable with current
   inputs, because the request exists and every NOT NULL column is always set.
2. An identical retry with reordered object keys returned 409. It is now compared as canonical JSON.

Regression tests: 6 added (25 in `tests/stage7-routes.test.js`). 3 of them fail on the previous head `5280ebb`; the
other 3 guard behaviour that was already correct.
