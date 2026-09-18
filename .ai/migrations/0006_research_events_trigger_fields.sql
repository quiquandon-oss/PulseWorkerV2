-- PR3: adds the three columns research_events needed for real trigger
-- data (trigger_metric, trigger_threshold) and reproducibility
-- (trigger_version), per the frozen PR3 specification. Purely additive
-- -- no existing column touched, no existing row affected (SQLite adds
-- new columns as NULL for pre-existing rows, which is correct here:
-- no research_events rows exist yet, since PR1/PR2 never wrote any).
ALTER TABLE research_events ADD COLUMN trigger_metric TEXT;
ALTER TABLE research_events ADD COLUMN trigger_threshold REAL;
ALTER TABLE research_events ADD COLUMN trigger_version TEXT;
