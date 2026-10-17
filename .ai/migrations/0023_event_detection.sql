-- Migration 0023: Event Detection Layer (ledger, observations for baselines, source health, run log, processed
-- documents). Additive only: five NEW tables. No existing table, column, V1 weight, sentiment row or prediction is touched.
-- Idempotent (IF NOT EXISTS). Rollback: see EVENT_DETECTION.md section "Rollback" (DROP these five tables).

-- One row per detected event. dedup_key names what happened (type|asset|source|window or document), so
-- re-detecting the same window maps to the same row; INSERT ... ON CONFLICT(dedup_key) DO NOTHING keeps the
-- first detection (its detected_ts is the honest point-in-time availability).
CREATE TABLE IF NOT EXISTS event_ledger (
  event_id TEXT PRIMARY KEY,
  dedup_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  asset TEXT,
  market TEXT,
  scope TEXT,
  source TEXT NOT NULL,
  source_url TEXT,
  source_ts INTEGER NOT NULL,
  detected_ts INTEGER NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('UP', 'DOWN', 'MIXED', 'UNKNOWN')),
  severity TEXT NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
  confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  observed_value REAL,
  baseline_value REAL,
  threshold_value REAL,
  unit TEXT,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  explanation TEXT NOT NULL DEFAULT '',
  freshness_ms INTEGER,
  source_health TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'EXPIRED')),
  expires_ts INTEGER,
  correlation_key TEXT,
  created_ts INTEGER NOT NULL,
  CHECK (source_ts <= detected_ts)
);
CREATE INDEX IF NOT EXISTS idx_event_ledger_detected ON event_ledger (detected_ts);
CREATE INDEX IF NOT EXISTS idx_event_ledger_type ON event_ledger (event_type, detected_ts);
CREATE INDEX IF NOT EXISTS idx_event_ledger_corr ON event_ledger (correlation_key);

-- Normalised observations that rules need history of (liquidation buckets, DVOL, options OI, yields).
-- observed_ts = when this Worker first had the value (point-in-time availability); the first write wins.
CREATE TABLE IF NOT EXISTS event_observations (
  source TEXT NOT NULL,
  series TEXT NOT NULL,
  bucket_ts INTEGER NOT NULL,
  value REAL NOT NULL,
  unit TEXT,
  coverage TEXT NOT NULL DEFAULT 'COMPLETE' CHECK (coverage IN ('COMPLETE', 'PARTIAL', 'SAMPLED')),
  observed_ts INTEGER NOT NULL,
  PRIMARY KEY (source, series, bucket_ts)
);

-- Latest health of each detector source (one row per source, overwritten by each run).
CREATE TABLE IF NOT EXISTS event_source_health (
  source TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  last_attempt_ts INTEGER,
  last_success_ts INTEGER,
  last_observation_ts INTEGER,
  last_error TEXT,
  coverage TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  updated_ts INTEGER NOT NULL
);

-- One row per detection run (audit trail; never read by V1 scoring).
CREATE TABLE IF NOT EXISTS event_runs (
  run_id TEXT PRIMARY KEY,
  started_ts INTEGER NOT NULL,
  finished_ts INTEGER,
  trigger TEXT NOT NULL,
  summary_json TEXT NOT NULL DEFAULT '{}'
);

-- Documents a detector has already read and classified (SEC accession numbers), so they are fetched once.
CREATE TABLE IF NOT EXISTS event_documents (
  source TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  processed_ts INTEGER NOT NULL,
  findings INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (source, doc_id)
);
