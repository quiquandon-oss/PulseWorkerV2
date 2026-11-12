-- Prediction snapshots: the response each scheduled tick produced, per coin and horizon.
--
-- Purely additive. One new table, at most 6 rows (3 coins x 2 horizons), overwritten in place each tick.
-- No existing table, column or row is touched.
--
-- Why: GET /predict, /link-predict and /eth-predict serve this row instead of re-running the prediction
-- path on every dashboard load (see worker.js "Served predictions"). Until this migration is applied the
-- Worker detects the missing table and keeps the previous behaviour, so deploy order does not matter.
--
-- Not a research record: predictions, selection_decisions and the challenger tables remain the source of
-- truth. body_json is a copy of what the tick returned, for serving only.
--
-- Rollback: DROP TABLE prediction_snapshots;  (or set PREDICT_SNAPSHOTS = "0" on the Worker to stop using it)

CREATE TABLE IF NOT EXISTS prediction_snapshots (
  coin TEXT NOT NULL,
  horizon_hours INTEGER NOT NULL,
  computed_ts INTEGER NOT NULL,      -- when the tick stored it
  prediction_ts INTEGER,             -- ts of the predictions row it describes
  body_json TEXT NOT NULL,
  PRIMARY KEY (coin, horizon_hours)
);
