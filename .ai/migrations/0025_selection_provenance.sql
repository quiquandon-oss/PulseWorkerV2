-- Selection provenance: what each selection row was computed from and what triggered it.
--
-- Purely additive. One new table. No existing table, column or row is touched, and the existing
-- selection_decisions / selection_decisions_anomaly INSERT statements are unchanged.
--
-- Why: selection_decisions.chosen_p_up and selection_decisions_anomaly.chosen_p_up_anomaly hold the chosen
-- variant's p_up on its latest RESOLVED prediction at selection time, not the prediction of the cycle named by
-- prediction_ts (behaviour since 60729d8, documented in research/README.md, PR5a). From the deploy date on,
-- each new selection row gets one row here so both values are available without recalculation:
--   chosen_p_up_source_ts  ts of the resolved prediction chosen_p_up came from
--   cycle_p_up / _ts       the chosen variant's p_up on the row written for the cycle itself
--   selection_trigger      'cron' (scheduled tick), 'cron_daily' (shadow job), 'request_refresh' (stalled-cron path)
-- Rows written before the deploy have no provenance row; nothing is backfilled.
--
-- Rollback: DROP TABLE selection_provenance;  (or set SELECTION_PROVENANCE = "0" on the Worker)

CREATE TABLE IF NOT EXISTS selection_provenance (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  selection_table TEXT NOT NULL CHECK (selection_table IN ('selection_decisions', 'selection_decisions_anomaly')),
  selection_row_id INTEGER,
  recorded_ts INTEGER NOT NULL,
  coin TEXT NOT NULL,
  horizon_hours INTEGER NOT NULL,
  prediction_ts INTEGER,
  chosen_variant TEXT,
  chosen_p_up_source_ts INTEGER,
  cycle_p_up REAL,
  cycle_p_up_ts INTEGER,
  selection_trigger TEXT NOT NULL,
  semantics_version TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_selection_provenance_row ON selection_provenance (selection_table, selection_row_id);
CREATE INDEX IF NOT EXISTS idx_selection_provenance_cycle ON selection_provenance (coin, horizon_hours, prediction_ts);
