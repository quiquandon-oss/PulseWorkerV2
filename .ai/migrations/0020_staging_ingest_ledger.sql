-- STAGING ONLY: provenance and collection-period tracking for staging-collector/ (real public data
-- collected prospectively into the staging database's existing btc_data and history tables).
--
-- NEVER APPLY TO PRODUCTION. Production's btc_data/history are written by the production Worker and the
-- CryptoPulse browser; nothing in production reads or writes these two tables. The collector itself refuses
-- any target other than pulseworker-v2-staging / 5458d504-2778-49ae-bd25-7751f1c49d50.
--
-- WHY. btc_data and history carry no provenance column, so a real collected row is indistinguishable from the
-- two synthetic E9 fixture rows (btc_data ids 7 and 8) by its own columns. Instead of altering those shared
-- tables (read by the Worker, Stage 7 and Experiment 5), every collector attempt is recorded here:
--   * a collected row has exactly one WRITTEN ledger entry pointing at it (target_table, target_row_id);
--   * any btc_data/history row WITHOUT a WRITTEN entry was not written by the collector (ids 7 and 8 included);
--   * duplicate slots, missing composites and rejected payloads are recorded too, never silently dropped.
-- Idempotency itself is enforced by the collector's INSERT ... WHERE NOT EXISTS on the target table's own
-- slot, so it does not depend on this ledger being written in the same transaction.
--
-- Append-only by design: the collector only ever INSERTs into these tables (no UPDATE, no DELETE). A period is
-- closed by appending a CLOSED event, not by updating the OPENED one.
--
-- Purely additive: two new tables and their indexes; no existing table or column is touched.
-- Rollback: DROP TABLE staging_ingest_ledger; DROP TABLE staging_collection_periods; (nothing depends on them).

CREATE TABLE staging_ingest_ledger (
  ingest_id TEXT PRIMARY KEY,                     -- random per attempt (hex); provenance is in the other columns
  kind TEXT NOT NULL CHECK (kind IN ('BTC_PRICE', 'V1_COMPOSITE')),
  status TEXT NOT NULL CHECK (status IN ('WRITTEN', 'SKIPPED_DUPLICATE', 'NO_OBSERVATION', 'REJECTED')),
  slot_ms INTEGER NOT NULL,                       -- fixed observation-slot width used for idempotency
  target_table TEXT CHECK (target_table IN ('btc_data', 'history')),
  target_row_id INTEGER,                          -- the row written (WRITTEN) or already occupying the slot (SKIPPED_DUPLICATE)
  target_ts INTEGER,                              -- that row's ts (database clock)
  server_ts INTEGER NOT NULL,                     -- database clock when this ledger entry was written
  source_name TEXT NOT NULL,                      -- e.g. hyperliquid:metaAndAssetCtxs:BTC.markPx / cryptopulse@<sha>
  source_observed_ts INTEGER,                     -- collector clock when the source was read
  payload_json TEXT,                              -- the exact validated (or rejected) observation
  payload_sha256 TEXT,
  detail_json TEXT,                               -- cross-check, excluded sources, rejection reasons
  collector_version TEXT NOT NULL,
  git_sha TEXT NOT NULL,
  run_id TEXT NOT NULL,                           -- GitHub run id, or local:<host>:<time> for a local run
  CHECK ((status IN ('WRITTEN', 'SKIPPED_DUPLICATE')) = (target_row_id IS NOT NULL))
);

-- One WRITTEN entry per collected row: the row <-> provenance link is unambiguous.
CREATE UNIQUE INDEX idx_staging_ingest_written_row
  ON staging_ingest_ledger(target_table, target_row_id) WHERE status = 'WRITTEN';
CREATE INDEX idx_staging_ingest_kind_status_ts ON staging_ingest_ledger(kind, status, target_ts);

CREATE TABLE staging_collection_periods (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_id TEXT NOT NULL,
  event TEXT NOT NULL CHECK (event IN ('OPENED', 'CLOSED')),
  event_ts INTEGER NOT NULL,                      -- database clock
  note TEXT,
  snapshot_json TEXT,                             -- read-only baseline counts taken when the event was recorded
  git_sha TEXT NOT NULL,
  run_id TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_staging_collection_period_event ON staging_collection_periods(period_id, event);
