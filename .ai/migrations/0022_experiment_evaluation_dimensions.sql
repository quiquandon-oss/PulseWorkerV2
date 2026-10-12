-- 0022: Experiment evaluation dimensions -- standalone outcome and pool contribution, kept apart.
--
-- NOT APPLIED. Applying it to production needs explicit owner authorization. Additive only: two new append-only
-- tables; research_experiment_registry and every existing table are untouched, and nothing here is read by V1,
-- V2 predictions, selection or any cron. No seed rows: the current truthful records (EXP-004/005/009 standalone
-- BLOCKED, pool CANDIDATE / NOT_TESTED) live in worker.js CODE_EVALUATION_RECORDS until a real evaluation is
-- recorded here, and no result is ever written to demonstrate the UI.
--
-- Why two tables, not columns on research_experiment_registry:
--   * The registry row is one mutable row per experiment; evaluations need full history (several versions, never
--     overwritten), so each evaluation is its own immutable row.
--   * A standalone evaluation answers the experiment's own frozen hypothesis. A pool evaluation answers whether its
--     component adds value to a defined pool. Separate tables mean a pool row can never rewrite a standalone row,
--     and neither is derived from the other.
--
-- Corrections are new rows (supersedes_id points at the row they replace); rows are never UPDATEd or DELETEd.
-- Re-ingesting the same evidence is idempotent through the UNIQUE indexes (INSERT ... ON CONFLICT DO NOTHING -- not
-- INSERT OR IGNORE, which would also silently drop a row that fails a CHECK); a correction differs
-- by supersedes_id, so it is never swallowed as a duplicate.
--
-- Conclusive pool states (VALIDATED_INCREMENTAL_VALUE, REDUNDANT, DEGRADING) must cite a protocol, its sha256,
-- the pool version, an out-of-sample window, eligible/overlap counts, evidence sha256 and the evaluator version
-- (CHECK below). Whether that protocol is APPROVED, and whether the recorded state follows from its rule, is
-- re-checked by every reader (CryptoPulseV3 api/pool.js); no pool protocol is approved as of this migration.

CREATE TABLE IF NOT EXISTS research_standalone_evaluations (
  record_id INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL REFERENCES research_experiment_registry(experiment_id),
  evaluation_version TEXT NOT NULL,          -- e.g. 'exp004-eval-v1'; one experiment can have several
  spec_ref TEXT NOT NULL,                    -- where the frozen hypothesis/plan lives
  spec_sha256 TEXT,                          -- sha256 of that frozen file, when one exists
  plan_sha256 TEXT,                          -- sha256 of the frozen evaluation plan, when one exists
  evaluation_status TEXT NOT NULL CHECK (evaluation_status IN ('NOT_RUN','BLOCKED','INCOMPLETE','EVALUATED')),
  scientific_outcome TEXT CHECK (scientific_outcome IS NULL OR scientific_outcome IN ('SUPPORTED','NOT_SUPPORTED','INCONCLUSIVE','NOT_APPLICABLE')),
  reason TEXT NOT NULL,
  sample_json TEXT,                          -- {count, required, meaning}
  window_start_ts INTEGER,
  window_end_ts INTEGER,
  out_of_sample INTEGER CHECK (out_of_sample IS NULL OR out_of_sample IN (0,1)),
  baseline TEXT,
  metrics_json TEXT,
  uncertainty_json TEXT,
  checks_json TEXT,                          -- data-quality / leakage / protocol checks performed
  limitations_json TEXT,
  evaluator_version TEXT,
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  evidence_sha256 TEXT,
  recorded_ts INTEGER NOT NULL,
  recorded_by TEXT NOT NULL,
  supersedes_id INTEGER REFERENCES research_standalone_evaluations(record_id),
  CHECK (evaluation_status <> 'EVALUATED' OR (scientific_outcome IS NOT NULL AND evidence_refs_json <> '[]')),
  CHECK (scientific_outcome IS NULL OR scientific_outcome = 'NOT_APPLICABLE' OR evaluation_status = 'EVALUATED'),
  CHECK (window_start_ts IS NULL OR window_end_ts IS NULL OR window_start_ts < window_end_ts)
);
CREATE INDEX IF NOT EXISTS idx_standalone_eval_experiment ON research_standalone_evaluations(experiment_id, recorded_ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_standalone_eval_idem
  ON research_standalone_evaluations(experiment_id, evaluation_version, evaluation_status, IFNULL(evidence_sha256, ''), IFNULL(scientific_outcome, ''), IFNULL(supersedes_id, 0));

CREATE TABLE IF NOT EXISTS research_pool_evaluations (
  record_id INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL REFERENCES research_experiment_registry(experiment_id),
  component_id TEXT NOT NULL,                -- what would enter the pool (e.g. 'timesfm_btc_direction')
  component_provenance TEXT NOT NULL,        -- where the component's values come from
  pool_id TEXT,                              -- NULL while only a candidate
  pool_version TEXT,
  pool_state TEXT NOT NULL CHECK (pool_state IN ('NOT_TESTED','CANDIDATE','VALIDATED_INCREMENTAL_VALUE','REDUNDANT','DEGRADING','INCONCLUSIVE','BLOCKED')),
  reason TEXT NOT NULL,
  protocol_id TEXT,
  protocol_sha256 TEXT,
  evaluator_version TEXT,
  window_start_ts INTEGER,
  window_end_ts INTEGER,
  out_of_sample INTEGER CHECK (out_of_sample IS NULL OR out_of_sample IN (0,1)),
  baseline_n INTEGER CHECK (baseline_n IS NULL OR baseline_n >= 0),
  candidate_n INTEGER CHECK (candidate_n IS NULL OR candidate_n >= 0),
  overlap_n INTEGER CHECK (overlap_n IS NULL OR overlap_n >= 0),
  composition_change TEXT,                   -- required when the eligible sets differ
  baseline_metrics_json TEXT,
  candidate_metrics_json TEXT,
  incremental_json TEXT,
  uncertainty_json TEXT,                     -- {ci:{lo,hi}, level, method, multiple_testing}
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  evidence_sha256 TEXT,
  recorded_ts INTEGER NOT NULL,
  recorded_by TEXT NOT NULL,
  supersedes_id INTEGER REFERENCES research_pool_evaluations(record_id),
  CHECK (pool_state NOT IN ('VALIDATED_INCREMENTAL_VALUE','REDUNDANT','DEGRADING') OR (
    protocol_id IS NOT NULL AND protocol_sha256 IS NOT NULL AND pool_id IS NOT NULL AND pool_version IS NOT NULL
    AND evaluator_version IS NOT NULL AND evidence_sha256 IS NOT NULL AND evidence_refs_json <> '[]'
    AND out_of_sample = 1 AND window_start_ts IS NOT NULL AND window_end_ts IS NOT NULL
    AND baseline_n IS NOT NULL AND candidate_n IS NOT NULL AND overlap_n IS NOT NULL
    AND baseline_metrics_json IS NOT NULL AND candidate_metrics_json IS NOT NULL AND uncertainty_json IS NOT NULL)),
  CHECK (window_start_ts IS NULL OR window_end_ts IS NULL OR window_start_ts < window_end_ts)
);
CREATE INDEX IF NOT EXISTS idx_pool_eval_experiment ON research_pool_evaluations(experiment_id, recorded_ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_eval_idem
  ON research_pool_evaluations(experiment_id, component_id, pool_state, IFNULL(pool_id, ''), IFNULL(pool_version, ''),
                               IFNULL(protocol_id, ''), IFNULL(evidence_sha256, ''), IFNULL(supersedes_id, 0));
