-- 0031 (2026-11-19): GLM evidence-intelligence persistence (docs/GLM_PHASE_B_CONTRACTS.md §10).
-- Purely additive: three new tables and their indexes. No existing table, column or row is read-modified; V1 tables are
-- never touched. Original evidence, forecasts and predictions stay authoritative: these tables hold references,
-- hashes, statuses and the bounded adjustment ledger only (no raw source text).
--
-- Apply BEFORE (or after: the worker tolerates missing tables, reporting inputs_degraded and writing nothing) the
-- Worker that sets GLM_SIGNAL_ENABLED = "1". Expected growth: <= 2 runs + 2 signals + 8 adjustments per day (~4 KB/day).
-- Rollback: .ai/rollback/0031_glm_signal.down.sql (drops the three tables; nothing else references them).

-- One row per 07:00 run. package_hash is the idempotency key: an identical package is never sent to the model twice.
CREATE TABLE IF NOT EXISTS glm_runs (
  run_id TEXT PRIMARY KEY,                 -- 'glm_' + first 24 hex of package_hash + '_' + cutoff
  asset TEXT NOT NULL,
  decision_cutoff_ts INTEGER NOT NULL,     -- information cutoff (scheduled 07:00:00 UTC)
  started_ts INTEGER NOT NULL,
  finished_ts INTEGER,
  package_hash TEXT NOT NULL UNIQUE,
  input_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  model TEXT,                              -- NULL when inference is disabled
  status TEXT NOT NULL,                    -- see GLM_RUN_STATUSES in glm/contract.js
  reason TEXT,
  n_items INTEGER NOT NULL,
  n_excluded INTEGER NOT NULL,
  package_bytes INTEGER NOT NULL,
  input_refs_json TEXT NOT NULL,           -- lineage: ref, class, family, table, id, independence key, first-available time
  missing_json TEXT NOT NULL DEFAULT '[]',
  output_json TEXT,                        -- validated model output only (never an invalid one)
  validation_errors_json TEXT,
  usage_json TEXT,                         -- tokens, reserved/actual Neurons, D1 rows/calls, timings
  rule_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_glm_runs_asset_cutoff ON glm_runs (asset, decision_cutoff_ts);

-- One row per run and horizon. NO_SIGNAL rows are stored too (with reasons), so absence is never ambiguous.
CREATE TABLE IF NOT EXISTS glm_signals (
  signal_id TEXT PRIMARY KEY,              -- run_id + ':' + horizon
  run_id TEXT NOT NULL,
  asset TEXT NOT NULL,
  horizon_hours INTEGER NOT NULL,
  decision_cutoff_ts INTEGER NOT NULL,
  valid_from_ts INTEGER NOT NULL,
  valid_until_ts INTEGER NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('UP', 'DOWN', 'NO_SIGNAL')),
  strength TEXT CHECK (strength IS NULL OR strength IN ('weak', 'moderate', 'strong')),
  eligibility TEXT NOT NULL CHECK (eligibility IN ('NO_SIGNAL', 'CANDIDATE', 'ELIGIBLE')),
  proposed_direction TEXT,
  proposed_strength TEXT,
  reasons_json TEXT NOT NULL,
  supporting_families_json TEXT NOT NULL DEFAULT '[]',
  contradicting_families_json TEXT NOT NULL DEFAULT '[]',
  supporting_refs_json TEXT NOT NULL DEFAULT '[]',
  contradicting_refs_json TEXT NOT NULL DEFAULT '[]',
  timesfm_ref TEXT,
  chronos_ref TEXT,
  models_live INTEGER NOT NULL DEFAULT 0,
  would_apply REAL NOT NULL DEFAULT 0,     -- dir * k(strength) * reference cap (measurement scale, not validated)
  reference_cap REAL NOT NULL,
  rule_version TEXT NOT NULL,
  created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_glm_signals_asset_h_cutoff ON glm_signals (asset, horizon_hours, decision_cutoff_ts);

-- One row per served prediction the tick hook saw (idempotent). Baseline is the displayed headline value.
CREATE TABLE IF NOT EXISTS glm_adjustments (
  coin TEXT NOT NULL,
  horizon_hours INTEGER NOT NULL,
  prediction_ts INTEGER NOT NULL,
  baseline_p_up REAL NOT NULL,
  baseline_source TEXT NOT NULL,           -- 'p_up' | 'cycle_p_up' | 'chosen_p_up'
  chosen_variant TEXT,
  signal_id TEXT,
  signal_status TEXT NOT NULL,
  would_apply REAL NOT NULL,
  applied REAL NOT NULL,
  cap REAL NOT NULL,
  final_p_up REAL NOT NULL,
  mode TEXT NOT NULL,                      -- 'shadow' | 'active'
  reason TEXT,
  rule_version TEXT NOT NULL,
  created_ts INTEGER NOT NULL,
  PRIMARY KEY (coin, horizon_hours, prediction_ts)
);
