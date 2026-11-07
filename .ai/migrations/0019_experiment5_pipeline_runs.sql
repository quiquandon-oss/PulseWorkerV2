-- Experiment 5 (agentic market evidence): one row per pipeline execution.
--
-- WHY THIS TABLE EXISTS. Until now nothing recorded that a run happened. A healthy run that
-- found nothing new (0 archived, 0 decisions) leaves no trace in research_sentiment_archive or
-- research_hypotheses, so "ran fine, nothing new" was indistinguishable from "did not run" or
-- "blocked". That made it impossible to answer, from the app, whether the experiment is
-- progressing or stuck. This table is OPERATIONAL telemetry only -- it never stores a
-- predictive result (decision outcomes stay in research_hypotheses.evidence_summary_json's
-- append-only "outcome" key, exactly as before).
--
-- OWNERSHIP. Written ONLY by research/experiment5_pipeline.py (via
-- scripts/experiment5-agent/run.py). Read ONLY by worker.js's Experiment 5 status endpoints.
-- Stage 7 never reads or writes it.
--
-- Purely additive: one new table, no existing table or column touched. NOT APPLIED to production
-- by this file. The pipeline checks for the table and, if it is absent, runs exactly as before and
-- reports run_record = SKIPPED_TABLE_MISSING (it does not fail and does not pretend to have
-- recorded anything). Rollback: stop writing and DROP TABLE experiment5_pipeline_runs -- nothing
-- else depends on it.
--
-- Independent of migration 0018 (Stage 7): either may be applied first.

CREATE TABLE experiment5_pipeline_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_ts INTEGER NOT NULL,              -- the pipeline's supplied now_ts: the run's own clock
                                         -- (read once by the runner; no module reads the wall clock)
  status TEXT NOT NULL CHECK (status IN ('OK', 'FAILED')),  -- a run record is either a success or a failure
  error_text TEXT,                      -- truncated "<ExceptionType>: <message>" when FAILED
  pipeline_version TEXT NOT NULL,       -- experiment5_pipeline.PIPELINE_VERSION
  constants_json TEXT NOT NULL,         -- the windows / horizon / tolerance in force for this run
  history_rows_read INTEGER,
  btc_rows_read INTEGER,
  newly_archived INTEGER,
  archive_rows_observed INTEGER,        -- observations inside the AGENT'S observe window (what it could see)
  observations_without_sources INTEGER, -- (same window) present but empty sources_json: nothing to classify
  observations_rejected_malformed INTEGER, -- (same window) sources_json not a valid JSON object: excluded, sampled below
  observations_rejected_outside_window INTEGER, -- malformed rows older than the observe window (archive-only population)
  rejected_observation_ts_json TEXT,    -- first rejected observation_ts values inside the window (bounded sample)
  sources_observed INTEGER,
  candidate_new_sources_json TEXT,
  agent_status TEXT,                    -- OK | INSUFFICIENT_ARCHIVE_DATA
  decisions_replayed INTEGER,           -- pending decisions loaded back from D1 under their real ids
  decisions_created INTEGER,            -- new decisions persisted this run
  decisions_skipped_duplicate INTEGER,  -- new decisions NOT persisted: same (subject, anchor_ts) already exists
  decisions_evaluated INTEGER,          -- replayed decisions whose outcome resolved this run
  evaluated_passed INTEGER,
  evaluated_failed INTEGER,
  evaluated_inconclusive INTEGER
);
CREATE INDEX idx_exp5_runs_run_ts ON experiment5_pipeline_runs(run_ts);
CREATE INDEX idx_exp5_runs_status_run_ts ON experiment5_pipeline_runs(status, run_ts);
