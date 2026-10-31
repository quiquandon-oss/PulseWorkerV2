# Proposal: port PR #82's remaining Stage 7 safety fixes onto `main`, without the feature switch (H4)

**Status:** proposal only. Nothing here is implemented, merged or deployed. Base: `main` at `0e61c5f`; PR #82 head
`b3077dd` (merge-base `280d2d5`).

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
   - **D7:** `stage7-staging-dispatcher.yml` on `main` still fires the pipeline daily (cron `0 11 * * *`) on the old
     branch `claude/stage7-research-pipeline`, which #82 identified as unpublishable.
5. Merging #82 into `main` as is fails 8 learning-loop tests (500 instead of 200), because `stage7RoutePreflight`
   refuses everything unless `STAGE7_ENABLED === "true"`, and production never sets it.

## Assumptions (not verified)

- **A1:** the Workers runtime produces the same JSON parse message as Node's V8. Both use V8, but this was not
  reproduced on workerd.
- **A2:** nobody depends on the current 409 for an identical retry.
- **A3:** the daily D7 dispatch writes nothing harmful. #82 observed `events_considered: 0`; today's runs were not
  inspected.
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
