# Stage 7 (EXP-012) finish line under owner decision H4, and a production finding (2026-10-31)

**H4 (approved 2026-10-31):** finish Stage 7 in staging against explicit acceptance criteria; production migrations need
separate explicit authorization. EXP-011 PRs #83/#84 stay blocked.

## Finding: Stage 7 is already active in production (VERIFIED 2026-10-31)

- `GET /api/research-lab/stage7-overview` on the production Worker returns `activated: true` with 10 requests (all
  `RESEARCH_COMPLETED`).
- `GET /api/learning/market` returns `stage7_available: true` with 15 events. The Research Lab learning loop on `main`
  reads the Stage 7 tables in production.
- So the registry text for EXP-012 ("STAGING_ONLY; production migration 0016 not applied") is stale. It is now listed
  as a registry conflict on EXP-012. When and by whom the production tables were created is not established here.
  No owner decision is inferred.

## Consequence for PRs #82 and #85

- **#82** (head `b3077dd`) is 89 commits behind `main`. Merging `main` into it conflicts only in
  `.github/workflows/test.yml`, which resolves mechanically. But the result fails 8 learning-loop tests (500 instead of
  200): #82 makes every Stage 7 route refuse unless `STAGE7_ENABLED` is `"true"`, and production never sets it. That
  would switch off the production learning loop. The merge was not pushed.
- **#85** (staging collector workflow, inactive by default) conflicts only in `test.yml`.

**#82 cannot be finished "in staging" as written without breaking production behaviour that already exists.** Two
ways forward:
1. **Rebase the intent, not the branch:** port #82's remaining safety fixes onto `main` without the production kill
   switch. Candidates are idempotent registration, atomic claims, the expected-SHA binding for staging deploys and the
   schema probe. Production keeps the learning loop that is live today.
2. **Close #82 as superseded** by the learning loop on `main`, after confirming which of its fixes `main` already
   contains.

Either one is a code change to production routes and needs the normal review and merge. Neither is done in this
change.

## Acceptance criteria (finish line)

| # | Criterion | Evidence required | Status (2026-10-31) |
|---|---|---|---|
| 1 | Staging validation | The staging Worker runs the reviewed SHA; the staging walkthrough (`job=staging-session-walkthrough`) passes; rejected writes create no rows | Not run for #82's head |
| 2 | Migration readiness | Every Stage 7 table and column the code reads exists in production, checked by the route-level schema probe; any further migration (0018) has a reviewed file and a rollback note | 0016/0017 evidently present in production (routes active); 0018 not applied anywhere |
| 3 | Human approval controls | Writes need the admin token or a signed device session; approval is a separate human step; no automatic recalculation | Present on `main` (learning-loop tests) |
| 4 | Documented rollback | Disabling: delete the `STAGE7_ADMIN_TOKEN` secret (writes refused). Code: revert the merge commit. Data: Stage 7 tables are append-only and never read by V1 scoring | Documented here; not yet in the Stage 7 README |
| 5 | Production authorization | Any production migration or behaviour change: separate explicit owner authorization (H4) | Required for option 1 or 2 above |

**EXP-011 (PRs #83/#84):** unchanged and blocked (H4). They can be reviewed for collection and evidence integrity only.
