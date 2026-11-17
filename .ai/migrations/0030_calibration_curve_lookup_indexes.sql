-- 0030 (2026-11-16): indexes for the calibration-curve lookups of every prediction tick. Index only: no table, row,
-- column or query changes; every query returns exactly the same rows (tests/calibration-curve-index.test.js replays
-- the real worker with and without these indexes and compares every table byte for byte).
--
-- Measured on production 2026-11-16 21:00 UTC (d1_usage_daily kind='stmt', deploy #128 attribution):
--   getLatestCalibrationCurve           6 calls/tick, 64,200 rows read (10,700 per call = 2 scans of 5,350 rows)
--   getLatestChallengerCalibrationCurve 6 calls/tick, 51,828 rows read ( 8,638 per call = 2 scans of 4,319 rows)
-- together ~116k of the ~180k rows one tick reads. EXPLAIN QUERY PLAN on production: SCAN calibration_curve (outer)
-- plus the MAX(computed_ts) subquery. Both tables are append-only (one 10-decile batch per coin/horizon per daily
-- refresh), so the cost grows every day. With (coin, horizon_hours, computed_ts) the MAX subquery is one index
-- seek and the outer query reads only the latest batch (~10 rows).
--
-- Rollback: DROP INDEX IF EXISTS idx_calibration_curve_coin_h_ts; DROP INDEX IF EXISTS idx_challenger_calibration_curve_coin_h_ts;

CREATE INDEX IF NOT EXISTS idx_calibration_curve_coin_h_ts ON calibration_curve (coin, horizon_hours, computed_ts);
CREATE INDEX IF NOT EXISTS idx_challenger_calibration_curve_coin_h_ts ON challenger_calibration_curve (coin, horizon_hours, computed_ts);
