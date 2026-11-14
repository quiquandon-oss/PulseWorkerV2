-- Sentiment V2 independent evidence (sentiment-v2/collector.js) and the GLM Neuron budget (worker.js sv2RunAi).
-- Purely additive: new tables and indexes only. No existing table, column or row is touched; V1 tables are never read
-- or written by these features except `history`/archive reads that already exist.
--
-- Apply BEFORE deploying the Worker that sets SV2_COLLECTOR_ENABLED = "1". Until then the collector reports
-- "state read failed: no such table" and V2 runs without independent evidence (it never invents any).
-- Rollback: dropping is not needed for correctness (set SV2_COLLECTOR_ENABLED = "0"); to remove the tables run
-- .ai/rollback/0029_sv2_evidence_and_ai_budget.down.sql after exporting them.

-- One row per evidence item. evidence_id = 'ev_' + first 32 hex of sha256(canonical URL): the same story URL is
-- stored once (INSERT OR IGNORE). title_hash = sha256(normalised title): an item whose title was already stored from
-- another source within 7 days is kept with dup_of set, and is excluded from scoring. Append-only by convention:
-- the collector issues INSERT OR IGNORE only.
CREATE TABLE IF NOT EXISTS sv2_evidence (
  evidence_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  family TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT,
  published_ts INTEGER,                 -- publisher time; NULL when absent or in the future
  retrieved_ts INTEGER NOT NULL,        -- when this Worker stored it; the point-in-time bound for V2
  title_hash TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  dup_of TEXT,
  collector_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sv2_evidence_retrieved ON sv2_evidence (retrieved_ts);
CREATE INDEX IF NOT EXISTS idx_sv2_evidence_source_retrieved ON sv2_evidence (source_id, retrieved_ts);
CREATE INDEX IF NOT EXISTS idx_sv2_evidence_title_hash ON sv2_evidence (title_hash, retrieved_ts);

-- Per-source reliability and freshness (one row per registry source, upserted by the collector).
CREATE TABLE IF NOT EXISTS sv2_source_state (
  source_id TEXT PRIMARY KEY,
  last_attempt_ts INTEGER,
  last_success_ts INTEGER,
  last_status TEXT,
  last_error TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  ok_count INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0,
  last_new_items INTEGER NOT NULL DEFAULT 0,
  etag TEXT,
  last_modified TEXT
);

-- One row per collection run: what was attempted and the budget it used.
CREATE TABLE IF NOT EXISTS sv2_collection_runs (
  run_ts INTEGER PRIMARY KEY,
  collector_version TEXT NOT NULL,
  sources_json TEXT NOT NULL,
  items_seen INTEGER NOT NULL,
  items_new INTEGER NOT NULL,
  items_duplicate INTEGER NOT NULL,
  subrequests INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  errors_json TEXT NOT NULL
);

-- GLM Neuron ledger. A call may run only after a reservation row is inserted atomically by a single conditional
-- INSERT ... SELECT that checks today's reserved total plus this reservation against the daily budget. The reserved
-- amount is an upper bound (input-token upper bound + max_tokens at the model's published Neuron rates), so the
-- budget cannot be exceeded. actual_neurons is filled from the model's reported token usage (one UPDATE of the row's
-- own measurement columns; the reservation itself never changes).
CREATE TABLE IF NOT EXISTS sv2_ai_neurons (
  day TEXT NOT NULL,
  slot INTEGER NOT NULL,
  reserved_ts INTEGER NOT NULL,
  model TEXT NOT NULL,
  reserved_neurons REAL NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  actual_neurons REAL,
  status TEXT,
  PRIMARY KEY (day, slot)
);
