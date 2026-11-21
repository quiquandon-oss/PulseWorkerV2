-- Rollback of 0032: drops the shadow log only (nothing else references it). Set KNN_V2_SHADOW_ENABLED = "0" first.
DROP INDEX IF EXISTS idx_knn_v2_shadow_coin_ts;
DROP TABLE IF EXISTS knn_v2_shadow;
