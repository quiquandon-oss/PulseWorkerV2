-- Experiment 4 (Google TimesFM research challenger) schema -- 2026-09-06
--
-- NOT YET APPLIED to production D1 as of this commit -- the D1 MCP tool
-- was unavailable in the session that built this PR. This file is both
-- the audit-trail record (matching this repo's established out-of-band
-- schema-application precedent -- see .ai/migrations/0001_hot_path_
-- indexes.sql) AND, this time, the literal pending action: apply this
-- CREATE TABLE statement to production D1 (database sentiment-history,
-- f91ca980-b886-423a-bd6f-f3baea46d181) before merging/relying on
-- .github/workflows/exp004-timesfm.yml, which assumes this table
-- already exists.
--
-- Purely additive -- no existing table touched, no existing column
-- changed. Research-only: this table has no relationship to
-- selection_decisions, predictions, link_predictions, eth_predictions,
-- or challenger_predictions other than reading from them (price history,
-- production_chosen_variant) at prediction time.
CREATE TABLE experiment_4_timesfm (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coin TEXT NOT NULL,
  horizon_hours INTEGER NOT NULL,
  ts INTEGER NOT NULL,                    -- when this forecast was made
  target_ts INTEGER NOT NULL,             -- ts + horizon_hours*3600000, same convention as predictions.target_ts
  input_end_ts INTEGER NOT NULL,          -- timestamp of the last price point actually fed to TimesFM (no-look-ahead proof)
  price_at_prediction REAL NOT NULL,
  forecast_price REAL,                    -- TimesFM's raw point forecast, in price terms
  predicted_return_pct REAL,              -- (forecast_price - price_at_prediction) / price_at_prediction * 100 -- SAME formula as PulseWorkerV2's own return_pct (worker.js line ~530), not invented
  direction TEXT,                         -- 'UP' if predicted_return_pct > 0, else 'DOWN' -- documented threshold, not a probability
  model_version TEXT NOT NULL,            -- e.g. 'timesfm-2.5-200m'
  checkpoint TEXT NOT NULL,               -- e.g. 'google/timesfm-2.5-200m-pytorch'
  inference_backend TEXT NOT NULL,        -- e.g. 'torch-cpu'
  context_length INTEGER NOT NULL,
  features_used TEXT NOT NULL,            -- e.g. 'univariate: btc_price only, no covariates'
  production_chosen_variant TEXT,         -- selection_decisions.chosen_variant at prediction time, for comparison only -- never written back to
  actual_return_pct REAL,                 -- filled in at resolution
  actual_direction TEXT,
  correct INTEGER,                        -- 1/0/NULL (NULL = not yet resolved)
  absolute_error REAL,                    -- |predicted_return_pct - actual_return_pct|
  signed_error REAL,                      -- predicted_return_pct - actual_return_pct (bias direction)
  resolved_ts INTEGER
);

-- Indexes matching the established hot-path pattern (PR #33): one for
-- the resolution backfill's own lookup (unresolved rows), one for
-- read/comparison queries by coin+horizon+time.
CREATE INDEX idx_experiment_4_timesfm_unresolved ON experiment_4_timesfm(target_ts) WHERE resolved_ts IS NULL;
CREATE INDEX idx_experiment_4_timesfm_coin_horizon_ts ON experiment_4_timesfm(coin, horizon_hours, ts);
