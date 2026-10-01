-- Stage 7.2: persisted recalculation lifecycle + explicit human confirmation.
--
-- Purely additive: nullable columns / columns with defaults on the two
-- Stage 7 tables 0016/0017 created. No existing column changes meaning,
-- no table is dropped or rewritten, no Stage 6 / V1 / V2 table is touched.
--
-- NOT APPLIED to production D1 by this migration -- staging only, same
-- convention as 0016/0017. Rollback: these columns are only ever READ by
-- worker.js's Stage 7 endpoints and WRITTEN by worker.js (REQUESTED) and
-- stage7-research-pipeline/run_stage7.py (RUNNING/COMPLETED/FAILED); leaving
-- them in place after reverting those two files is harmless (SQLite cannot
-- cheaply DROP COLUMN on older engines, and nothing depends on them).
--
-- WHY: until now "recalculation" was a single flag
-- (recalculation_requested_ts). The UI could not tell "requested, not run
-- yet" from "running", "completed" or "failed", so it could not honestly show
-- what the staging workflow actually did. These columns make that state
-- persisted data instead of an inference.

-- ---------------------------------------------------------------------
-- 1. Recalculation lifecycle on the request.
-- ---------------------------------------------------------------------
ALTER TABLE stage7_research_requests ADD COLUMN recalculation_status TEXT;
                                       -- NULL (never requested) | REQUESTED | RUNNING |
                                       -- COMPLETED | FAILED.
                                       -- REQUESTED is set ONLY by worker.js's
                                       -- triggerStage7Recalculation() (a human click).
                                       -- RUNNING / COMPLETED / FAILED are set ONLY by
                                       -- run_stage7.py, the human-dispatched staging
                                       -- workflow. Nothing else writes this column.
ALTER TABLE stage7_research_requests ADD COLUMN recalculation_started_ts INTEGER;
                                       -- When run_stage7.py last began working on it.
ALTER TABLE stage7_research_requests ADD COLUMN recalculation_completed_ts INTEGER;
                                       -- When it last reached COMPLETED or FAILED.
ALTER TABLE stage7_research_requests ADD COLUMN recalculation_error TEXT;
                                       -- Truncated error text when FAILED, else NULL.
ALTER TABLE stage7_research_requests ADD COLUMN recalculation_attempts INTEGER NOT NULL DEFAULT 0;
                                       -- Incremented each time run_stage7.py starts work on
                                       -- this request, so a retry is visible, never silent.
ALTER TABLE stage7_research_requests ADD COLUMN recalculation_sentiment_id INTEGER;
                                       -- stage7_event_sentiment.id of the result this request
                                       -- produced (or, on an idempotent repeat, the identical
                                       -- earlier result). NULL until COMPLETED.

-- ---------------------------------------------------------------------
-- 2. Explicit human confirmation on the response.
-- ---------------------------------------------------------------------
ALTER TABLE stage7_research_responses ADD COLUMN human_confirmed_ts INTEGER;
                                       -- Set only when a human explicitly confirmed, on the
                                       -- review screen, that they reviewed the response and its
                                       -- sources. Distinct from source_validation_json (the
                                       -- server's OBJECTIVE technical check): technical validity
                                       -- never implies human acceptance, and acceptance never
                                       -- overrides a failed technical check.
ALTER TABLE stage7_research_responses ADD COLUMN human_review_note TEXT;
                                       -- Optional free-text note from the review step.
