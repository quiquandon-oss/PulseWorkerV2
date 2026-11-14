-- Sentiment V2 results (docs/SENTIMENT_V2_BUILD_SPEC.md §5, §6).
--
-- Purely additive. Two new tables (results and AI call reservations); no existing table is touched, so V1's history, weights,
-- predictions and the research archive are never rewritten. Written only by worker.js runSentimentV2Cycle; read only
-- by the comparison API. One row per (asset, calc_version, decision_ts): a retried cycle cannot create a duplicate
-- (INSERT OR IGNORE). decision_ts is the observation_ts of the V1 archive row the cycle evaluated, so V1, V2 and BTC
-- share timestamps exactly.
--
-- Distinct outputs are kept in distinct columns: sentiment_score (0-100) is never mixed with confidence, evidence
-- coverage, event intensity or the predictive signal (predictive_json).
--
-- Rollback: set SENTIMENT_V2_ENABLED = "0" on the Worker; then, if wanted, DROP TABLE sentiment_v2_results;

CREATE TABLE IF NOT EXISTS sentiment_v2_results (
  result_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset TEXT NOT NULL,
  decision_ts INTEGER NOT NULL,
  calc_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('OK', 'DEGRADED', 'FAILED')),
  degraded_reason TEXT,
  sentiment_score REAL CHECK (sentiment_score IS NULL OR (sentiment_score >= 0 AND sentiment_score <= 100)),
  sentiment_class TEXT,
  confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  evidence_coverage REAL CHECK (evidence_coverage IS NULL OR (evidence_coverage >= 0 AND evidence_coverage <= 1)),
  market_regime TEXT,
  regime_json TEXT NOT NULL DEFAULT '{}',
  event_intensity REAL CHECK (event_intensity IS NULL OR (event_intensity >= 0 AND event_intensity <= 1)),
  event_json TEXT NOT NULL DEFAULT '{}',
  predictive_json TEXT NOT NULL DEFAULT '{}',
  family_json TEXT NOT NULL DEFAULT '{}',
  supporting_ids_json TEXT NOT NULL DEFAULT '[]',
  contradicting_ids_json TEXT NOT NULL DEFAULT '[]',
  missing_json TEXT NOT NULL DEFAULT '{}',
  ai_model TEXT,
  ai_status TEXT NOT NULL,
  ai_json TEXT NOT NULL DEFAULT '{}',
  usage_json TEXT NOT NULL DEFAULT '{}',
  created_ts INTEGER NOT NULL,
  UNIQUE (asset, calc_version, decision_ts)
);

-- The UNIQUE key (asset, calc_version, decision_ts) is the index every V2 query uses: series ranges, first/last
-- lookups and the latest row are all prefix + range or ordered LIMIT 1 on it, so their cost does not grow with other
-- versions' history.

-- Workers AI call reservations (spec section 3: hard daily ceiling, never exceeded). A cycle reserves the next slot of
-- the UTC day atomically BEFORE calling the model; the primary key makes a duplicate slot impossible, so overlapping or
-- retried cycles cannot exceed the ceiling, and a reserved slot counts as used even if the call later fails.
CREATE TABLE IF NOT EXISTS sentiment_v2_ai_calls (
  day TEXT NOT NULL,
  slot INTEGER NOT NULL,
  reserved_ts INTEGER NOT NULL,
  PRIMARY KEY (day, slot)
);
