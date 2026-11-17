# Incident 2026-11-17: direct D1 query read 28.7M rows (account daily free limit 5M)

Severity: high (likely D1 unavailability for the account until 00:00 UTC 2026-11-18). Cause: an operator query run by
Claude Code through the Cloudflare D1 connector, not by any Worker or workflow. Status: no further D1 reads by owner
order until 00:00 UTC 2026-11-18. No data written or changed by the query.

## Timeline (UTC, 2026-11-17)
- 05:28:56 Preparing to apply migration 0030 (owner-approved). Read-only pre-checks on `sentiment-history`
  (`f91ca980-b886-423a-bd6f-f3baea46d181`):
  - `d1_databases_list` confirmed the database;
  - `sqlite_master` showed neither index;
  - the table counts were `calibration_curve` 5,350 rows and `challenger_calibration_curve` 4,319 rows. That count
    query read 9,669 rows.
- About 05:30 `EXPLAIN QUERY PLAN` of the challenger lookup: 0 rows read. It showed `SCAN challenger_calibration_curve`
  plus a scalar subquery.
- About 05:30 **the incident query**, intended as a baseline fingerprint of the 12 latest curve batches to compare
  after the index:

  ```sql
  WITH k(coin, h) AS (VALUES ('BTC',12),('BTC',24),('ETH',12),('ETH',24),('LINK',12),('LINK',24))
  SELECT 'cal' t, k.coin, k.h, COUNT(c.id) n, SUM(c.decile*1000003 + CAST(c.empirical_up_rate*1e9 AS INTEGER)
         + CAST(c.predicted_p_up_mid*1e6 AS INTEGER) + c.n_samples) chk, MAX(c.computed_ts) ts
  FROM k LEFT JOIN calibration_curve c ON c.coin = k.coin AND c.horizon_hours = k.h
    AND c.computed_ts = (SELECT MAX(computed_ts) FROM calibration_curve WHERE coin = k.coin AND horizon_hours = k.h)
  GROUP BY k.coin, k.h
  UNION ALL
  SELECT 'chal', ... same shape on challenger_calibration_curve ...
  ```

  D1 meta: `rows_read: 28,685,030`, `sql_duration_ms: 2026.7`, `rows_written: 0`.
- Immediately after, `SELECT MAX(ts) FROM btc_data`: 1 row read, success. The limit was not yet enforced at that
  moment.
- Then all D1 activity stopped, the incident was reported, and the owner ordered no further D1 reads until the reset.

## Why it cost 28.7M rows
With no index on `(coin, horizon_hours, computed_ts)`, every evaluation of the correlated
`MAX(computed_ts)` subquery scans the whole table. Because the subquery sits in the `JOIN ... ON` clause, SQLite
evaluates it for every candidate row of the join: 6 `(coin, horizon)` keys × 5,350 rows. Each evaluation is itself a
5,350-row scan, plus the outer scans.

```
6 × 5,350 × 5,350 + 6 × 4,319 × 4,319 ≈ 171.7M + 111.9M (upper bound)
```

SQLite's equality filter on `coin` and `horizon_hours` cut this to the measured 28.7M (about 1/6 per key). This is
the quadratic pattern behind the production lookup (#160), multiplied by a join over every key.

## Why no estimate was made first
- The `EXPLAIN QUERY PLAN` result (SCAN plus a correlated scalar subquery) and the table sizes were both already in
  hand. Multiplying them would have given a cost in the tens of millions before execution. That multiplication was
  not done.
- The query was treated as a read-only and therefore harmless check. On Workers Free, reads are the scarce resource,
  so a read-only query is not cost-free.
- The query was written for the post-index state, where it costs about 120 rows, and run in the pre-index state.

## Why the 2.5M collector stop did not protect it
- The stop (`sv2CollectGuarded`) reads `d1_usage_daily`, which only the Worker's own instrumented D1 wrapper writes.
  It protects the Worker's discretionary work (the evidence collector) and nothing else.
- Queries sent through the Cloudflare REST API or the D1 connector bypass the Worker entirely. They are not recorded
  in `d1_usage_daily`, and nothing checks them against any budget. Cloudflare's own enforcement is account-wide and
  only acts after the limit is exceeded.

## Rules for every future direct D1 operation (connector or wrangler)
1. **Estimate before executing.** Run `EXPLAIN QUERY PLAN` first; it reads 0 rows. For each `SCAN t`, take the row
   count of `t` from known figures or a separately budgeted, indexed `COUNT`. Multiply nested and correlated scans.
   Record the estimate.
2. **Budget per query: at most 50,000 rows read.** Above that, do not run it. Rewrite it or ask the owner.
3. **Budget per day: at most 250,000 direct rows read** in total, and only while the Worker's recorded daily usage
   plus this budget stays under 4M. The meta `rows_read` of every direct query is logged in the session's evidence.
4. **Forbidden shapes on unindexed columns:** correlated subqueries in `JOIN` or `WHERE`, `MAX`/`MIN` per key, and
   joins against `VALUES` lists over full tables. Use single-key lookups instead, or wait for an index.
5. **Verification queries use the plan, not data.** After an index change, verify with `EXPLAIN QUERY PLAN` (0 rows)
   and at most one bounded lookup per key, with `LIMIT`.
6. **No retries or polling loops** against D1 from an operator session. A health check is one single-row read.

## Recovery (owner order, after 00:00 UTC 2026-11-18)
- **A.** One single-row health check.
- **B.** A bounded `sqlite_master` lookup of the two index names.
- **C.** Apply migration 0030: only the two `CREATE INDEX IF NOT EXISTS` statements.
- **D.** Verify with `EXPLAIN QUERY PLAN`.
- **E.** Measure the first prediction tick after that through `d1_usage_daily` `kind='stmt'`.
- **F.** Confirm prediction outputs are unchanged and report reads and writes.
- If D1 is unavailable at any step: stop and report.

## Not affected
- Stored data. The query wrote 0 rows; Cloudflare's enforcement blocks queries and does not touch data.
- V1 and V2 code, schedules, schema and AI configuration were not changed in response to the incident.
