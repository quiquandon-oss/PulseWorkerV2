# Change record: served predictions and D1 usage accounting

**Status:** implemented and tested locally. **Not deployed, not committed.** Methodology-relevant from the moment it
is deployed; the deploy date and Worker version are to be added here then.

## What changes

1. **Dashboard requests no longer compute or write.** `GET /predict`, `/link-predict` and `/eth-predict` serve the
   response the last scheduled tick produced (table `prediction_snapshots`, migration `0024`) while it is at most 6
   hours old. They run no prediction, no outcome resolution and no selection, and write nothing.
2. **A selection row is written from a request only alongside a prediction that request persisted** (the stalled-cron
   self-healing path). Otherwise the stored decision is returned unchanged.
3. **Every invocation logs its D1 usage** (`evt: "d1_usage"`): statements, rows read, rows written, per route and per
   cron run.

`selectBestVariant`, the prediction models, calibration, the scheduled tick's sequence and every existing table are
unchanged.

## Effect on research data (from deploy onward)

- `selection_decisions` receives rows from the scheduled tick, plus one row for each prediction persisted by the
  stalled-cron path. Page loads stop adding rows. Existing rows are untouched.
- Outcomes are resolved by the scheduled tick only (every 3 hours), no longer also by page loads. Resolved values do
  not depend on when resolution runs; `resolved_ts` can be up to 3 hours later than before.
- Consequence for EXP-002: between a tick and the 07:00 UTC shadow job, page loads can no longer resolve predictions,
  so the production and shadow selections see the same resolved set unless the stalled-cron path ran. This changes
  the data-generating process inside the EXP-002 window and must be dated when deployed.

## Evidence (local replay on synthetic data; not production measurements)

| Check | Result |
|---|---|
| Scheduled tick, 73 ticks: every pre-existing table compared with the unmodified Worker | identical (11 tables, SHA-256 of contents) |
| `GET /predict` | 31 statements, 23 full scans, 1 selection row written → 1 statement, 0 scans, 0 writes |
| `GET /eth-predict` | 27 statements, 19 full scans, 1 selection row → 1, 0, 0 |
| `GET /link-predict` | 28 statements, 20 full scans, 1 selection row → 1, 0, 0 |
| Table not migrated yet | Worker falls back to the previous live path; still no selection row from page loads |
| Cron stalled (data older than 6 h) | live path self-heals: one prediction and exactly one selection |
| Test suites | 1,106 JavaScript tests and 992 research tests pass |

## Rollback

Set `PREDICT_SNAPSHOTS = "0"` (live path for every request) and `D1_USAGE_LOG = "0"` on the Worker, or redeploy the
previous version. The table can stay; it is used for serving only.

## Production steps required (owner approval each)

1. Apply `.ai/migrations/0024_prediction_snapshots.sql` (one additive table, at most 6 rows).
2. Deploy the Worker. Order does not matter: without the table the Worker keeps the previous behaviour.
