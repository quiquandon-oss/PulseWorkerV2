-- D1 usage per day, per route and per cron: how many rows each consumer read and wrote.
--
-- Purely additive. One new table, one row per (day, kind, key); a few dozen rows a day. No existing table is
-- touched. Written by the Worker's usage accounting (worker.js "D1 usage accounting") for scheduled runs and for
-- requests that read at least 50 rows. Operational data only, not a research record.
--
-- Why: the account exhausted D1's daily rows-read allowance on 2026-11-08 and nothing recorded which consumer
-- used it. Workers Logs carry the same figures but cannot be queried from the tooling used to operate this system.
--
-- Rollback: DROP TABLE d1_usage_daily;  (or set D1_USAGE_TABLE = "0" on the Worker)

CREATE TABLE IF NOT EXISTS d1_usage_daily (
  day TEXT NOT NULL,                 -- UTC date, YYYY-MM-DD
  kind TEXT NOT NULL,                -- 'fetch' or 'scheduled'
  key TEXT NOT NULL,                 -- 'GET /path' or the cron expression
  invocations INTEGER NOT NULL,
  statements INTEGER NOT NULL,
  rows_read INTEGER NOT NULL,        -- as reported by D1 (meta.rows_read); first() calls report none
  rows_written INTEGER NOT NULL,
  max_rows_read INTEGER NOT NULL,    -- largest single invocation
  last_ts INTEGER NOT NULL,
  PRIMARY KEY (day, kind, key)
);
