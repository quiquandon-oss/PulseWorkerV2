-- 0032 (2026-11-21): knn-core-v2 shadow log (OD-18; OPERATIONS.md "k-NN look-ahead"; PR #110 redone on current main).
-- Purely additive: one new table and one index. Production prediction tables are never modified; V1 tables are never
-- touched. One row per persisted production prediction while KNN_V2_SHADOW_ENABLED = "1" (default "0"): the served
-- knn-core-v1 p_up next to the fp-asof-v2 shadow p_up. Outcomes are not copied: they resolve by joining
-- served_prediction_id to predictions / eth_predictions / link_predictions (per coin).
--
-- Apply BEFORE setting KNN_V2_SHADOW_ENABLED = "1" (the Worker tolerates the missing table: the shadow logs an error and
-- the production cycle continues). Expected growth with the flag on: <= 3 coins x 8 ticks x horizons per day (~5 KB/day).
-- Rollback: .ai/rollback/0032_knn_v2_shadow.down.sql.
CREATE TABLE IF NOT EXISTS knn_v2_shadow (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coin TEXT NOT NULL,                       -- BTC | ETH | LINK
  horizon_hours INTEGER NOT NULL,
  served_prediction_id INTEGER NOT NULL,    -- id in the coin's production prediction table
  served_ts INTEGER NOT NULL,
  served_model_version TEXT NOT NULL,       -- knn-core-v1 / knn-core-v1-selfcontained
  served_p_up REAL,
  shadow_model_version TEXT NOT NULL,       -- knn-core-v2-asof / knn-core-v2-selfcontained
  feature_pipeline TEXT NOT NULL,           -- fp-asof-v2
  shadow_status TEXT NOT NULL,              -- ok | insufficient_data | insufficient_resolved_analogs | error
  shadow_p_up REAL,
  shadow_calibrated_p_up REAL,
  shadow_median_return_pct REAL,
  shadow_n_analogs INTEGER,
  elapsed_ms INTEGER,                       -- wall time of the shadow run (CPU is measured by account telemetry)
  created_ts INTEGER NOT NULL,
  UNIQUE (coin, horizon_hours, served_prediction_id, shadow_model_version)
);
CREATE INDEX IF NOT EXISTS idx_knn_v2_shadow_coin_ts ON knn_v2_shadow (coin, horizon_hours, served_ts);
