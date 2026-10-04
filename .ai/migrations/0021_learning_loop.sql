-- 0021: Learning loop -- learning candidates and V1 methodology versions.
--
-- STAGING / LOCAL ONLY until production is explicitly authorized. Additive: two new tables, nothing existing is
-- altered, and nothing here is read by V1, V2 predictions or any cron.
--
-- Why new tables (not existing ones):
--   * learning_candidates: a candidate has its own mutable lifecycle (DRAFT -> PENDING_REVIEW -> ACCEPTED / REJECTED /
--     NEEDS_MORE_RESEARCH) and an editable source-adjustment proposal. Stage 7's response rows are immutable research
--     records (never overwritten), so they cannot hold this state.
--   * v1_methodology_versions: V1's weights are not persisted anywhere today (they live in the V1 web app's code plus
--     browser-local overrides). A reproducible, approvable methodology needs its configuration stored as data.
-- The v1.0 baseline row is written by the API on first use from learning/learning-method.js baselineConfig(), so
-- there is exactly one definition of it.

CREATE TABLE IF NOT EXISTS learning_candidates (
  candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL,
  event_id INTEGER NOT NULL REFERENCES research_events(event_id),
  request_id TEXT NOT NULL REFERENCES stage7_research_requests(request_id),
  response_id TEXT NOT NULL REFERENCES stage7_research_responses(response_id), -- the confirmed finding
  candidate_type TEXT NOT NULL,        -- NEW_SOURCE | NEW_TREND | NEW_SIGNAL | SOURCE_RECLASSIFICATION |
                                       -- SOURCE_WEIGHT_ADJUSTMENT | REGIME_SPECIFIC_SIGNAL
  title TEXT NOT NULL,
  reason TEXT NOT NULL,
  expected_effect TEXT,
  confidence TEXT,                     -- LOW | MEDIUM | HIGH (human-set)
  evidence_json TEXT NOT NULL,
  status TEXT NOT NULL,                -- DRAFT | PENDING_REVIEW | ACCEPTED | REJECTED | NEEDS_MORE_RESEARCH
  base_version_id TEXT NOT NULL,       -- the V1 methodology version the adjustment is proposed against
  adjustment_json TEXT,                -- the source adjustment proposal (learning-method.js normalizeAdjustment)
  analysis_json TEXT,                  -- latest deterministic impact + validation summary (recomputable)
  decision TEXT,                       -- APPROVE | REJECT | NEEDS_MORE_RESEARCH
  decided_ts INTEGER,
  decided_by TEXT,
  decision_note TEXT,
  produced_version_id TEXT             -- set when approval created a methodology version
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_candidates_response ON learning_candidates(response_id);
CREATE INDEX IF NOT EXISTS idx_learning_candidates_event ON learning_candidates(event_id);

CREATE TABLE IF NOT EXISTS v1_methodology_versions (
  version_id TEXT PRIMARY KEY,         -- v1.0 (baseline), v1.1, ...
  parent_version_id TEXT,
  formula_id TEXT NOT NULL,            -- e.g. v1-weighted-mean@1
  config_json TEXT NOT NULL,           -- sources (weight, confidence, group, invert, regime) + signals
  created_ts INTEGER NOT NULL,
  reason TEXT NOT NULL,
  candidate_id INTEGER REFERENCES learning_candidates(candidate_id),
  status TEXT NOT NULL,                -- BASELINE | APPROVED (ready, not active) | ACTIVE | SUPERSEDED
  approved_by TEXT,
  approved_ts INTEGER,
  effective_ts INTEGER,                -- set only by a separate, explicitly authorized activation
  validation_json TEXT                 -- validation result at approval time
);
