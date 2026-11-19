-- Rollback for 0031_glm_signal.sql. Run only on owner request, after exporting the tables (they hold the shadow
-- evaluation record). Nothing else references these tables. Disable first: GLM_SIGNAL_ENABLED = "0",
-- GLM_TICK_HOOK_ENABLED = "0", GLM_INFLUENCE_CAP = "0".
DROP TABLE IF EXISTS glm_adjustments;
DROP TABLE IF EXISTS glm_signals;
DROP INDEX IF EXISTS idx_glm_runs_asset_cutoff;
DROP TABLE IF EXISTS glm_runs;
