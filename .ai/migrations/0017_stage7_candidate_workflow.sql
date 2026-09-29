-- Stage 7.1: human-controlled candidate workflow. Confirmed operating
-- model (see PR description / task history): a human reviews a BATCH of
-- proposed candidates, explicitly selects which to research, copies an
-- app-generated prompt to an external AI, pastes the answer back, and
-- explicitly triggers recalculation -- never an automatic create-and-
-- publish-and-recalculate loop.
--
-- This migration separates "assessed as needing research" (a candidate,
-- new table below) from "a human decided to research it" (a request,
-- 0016's own stage7_research_requests, extended below only with new
-- nullable columns -- never a destructive change to an existing column).
-- See stage7-research-pipeline/run_stage7.py's own run_propose_candidates()
-- (writes candidates only, never a request) vs. worker.js's
-- createStage7ResearchRequests() (the only thing that turns a SELECTED
-- candidate into a real request row, human-triggered, D1-only).
--
-- NOT APPLIED to production D1 by this migration -- staging only, per
-- the same convention 0016's own header established. This file assumes
-- 0016-AS-CURRENTLY-WRITTEN on `main` (which already includes
-- publish_attempts, added by the M1 fix, PR #81) is the schema state
-- immediately before it. Staging's own D1 database was originally
-- migrated from an EARLIER copy of 0016 that predates that fix and is
-- therefore missing publish_attempts -- that one-off drift is reconciled
-- by a separate, explicit ALTER TABLE run directly against staging
-- outside this file (see the delivery report), never folded into this
-- migration's own body, so this file stays correct for a fresh
-- 0016-then-0017 apply against any environment that starts clean.

-- ---------------------------------------------------------------------
-- 1. The candidate table -- one row per event the pipeline has assessed
--    as needing research, BEFORE any human decision to research it.
-- ---------------------------------------------------------------------
CREATE TABLE stage7_research_candidates (
  candidate_id TEXT PRIMARY KEY,        -- deterministic: stage7-cand-<event_id>
  event_id INTEGER NOT NULL REFERENCES research_events(event_id),
  proposed_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL,
  status TEXT NOT NULL,                 -- PROPOSED | SELECTED | CONVERTED | DISMISSED | STALE
                                         -- PROPOSED: assessed, shown in the review batch.
                                         -- SELECTED: a human checked it in the batch UI but
                                         --   has not yet clicked "Create research requests"
                                         --   (a soft, reversible client-side-confirmed state;
                                         --   see stage7-select-candidates below).
                                         -- CONVERTED: a request row now exists for it
                                         --   (request_id set below) -- terminal, never
                                         --   re-proposed for the same event while the
                                         --   request stays non-terminal (enforced by the
                                         --   SAME event_id the request's own unique index
                                         --   already guards).
                                         -- DISMISSED: a human explicitly declined to
                                         --   research this candidate -- terminal, but the
                                         --   event may be re-proposed on a LATER run if its
                                         --   own input_fingerprint has since changed (e.g.
                                         --   new evidence arrived), never merely because
                                         --   time passed.
                                         -- STALE: a later propose run found this event no
                                         --   longer eligible (aged out past
                                         --   MAX_EVENT_AGE_FOR_STAGE7_MS, or evidence has
                                         --   since become SUFFICIENT) -- terminal, informational.
  sufficiency_status TEXT NOT NULL,     -- INSUFFICIENT | CONFLICTING | INSUFFICIENT_EVIDENCE
  reasons_json TEXT NOT NULL,
  questions_json TEXT NOT NULL,
  missing_categories_json TEXT NOT NULL,
  historical_cutoff_ts INTEGER NOT NULL,
  evidence_snapshot_json TEXT NOT NULL,
  input_fingerprint TEXT NOT NULL,      -- identical shape/purpose to the requests table's
                                         -- own column -- a re-propose run that finds this
                                         -- event's fingerprint unchanged is a no-op, never a
                                         -- duplicate PROPOSED row (see the partial unique
                                         -- index below, which is on event_id, not
                                         -- fingerprint, so an unchanged re-propose simply
                                         -- has nothing new to insert against an existing
                                         -- open row -- run_propose_candidates() checks this
                                         -- explicitly before ever attempting the insert).
  request_id TEXT REFERENCES stage7_research_requests(request_id) -- set only once CONVERTED
);
-- Only one OPEN (PROPOSED or SELECTED) candidate may exist per event at a
-- time -- mirrors stage7_research_requests' own idx_stage7_requests_active_event
-- reasoning exactly. A CONVERTED candidate's event_id is simultaneously
-- guarded by the request table's own unique index, so the two tables can
-- never disagree about "is there an open ask for this event right now".
CREATE UNIQUE INDEX idx_stage7_candidates_active_event
  ON stage7_research_candidates(event_id)
  WHERE status IN ('PROPOSED', 'SELECTED');
CREATE INDEX idx_stage7_candidates_status ON stage7_research_candidates(status);
CREATE INDEX idx_stage7_candidates_event_id ON stage7_research_candidates(event_id);

-- ---------------------------------------------------------------------
-- 3. Request-table extensions -- all nullable, all additive, no existing
--    column's meaning changes.
-- ---------------------------------------------------------------------
ALTER TABLE stage7_research_requests ADD COLUMN candidate_id TEXT REFERENCES stage7_research_candidates(candidate_id);
                                       -- NULL for a request that predates this migration
                                       -- (none exist in practice: 0016 has never been
                                       -- exercised against a real event yet) or, in
                                       -- principle, any future request created outside the
                                       -- candidate flow. Every request created by
                                       -- worker.js's createStage7ResearchRequests() sets
                                       -- this to the candidate it was converted from.
ALTER TABLE stage7_research_requests ADD COLUMN prompt_text TEXT;
                                       -- The EXACT prompt text shown to the human and
                                       -- copied to the external AI, generated ONCE at
                                       -- request-creation time (worker.js's
                                       -- buildStage7ResearchPromptText(), the single
                                       -- canonical implementation) and stored verbatim --
                                       -- never regenerated from a possibly-changed template
                                       -- later, and never independently recomputed by
                                       -- research/stage7_github_publisher.py, which now
                                       -- reads this column instead of calling its own
                                       -- build_research_prompt() whenever it is present
                                       -- (see that module's build_request_file_content()).
ALTER TABLE stage7_research_requests ADD COLUMN recalculation_requested_ts INTEGER;
                                       -- NULL until a human explicitly clicks "Trigger
                                       -- recalculation" for this request's VALIDATED
                                       -- response (worker.js's
                                       -- triggerStage7Recalculation()). run_stage7.py's own
                                       -- recalculation step now considers a response
                                       -- eligible ONLY when this is non-null -- registering
                                       -- a validated response no longer implicitly queues a
                                       -- recalculation (Task 3.E's explicit requirement).
ALTER TABLE stage7_research_requests ADD COLUMN recalculation_requested_by TEXT;
                                       -- Free-text human identifier/note captured at trigger
                                       -- time, for the audit trail only -- NEVER an
                                       -- authorization mechanism (the STAGE7_ADMIN_TOKEN
                                       -- check on the endpoint is the only real gate, exactly
                                       -- as for response registration).

-- ---------------------------------------------------------------------
-- 4. Response-table extensions.
-- ---------------------------------------------------------------------
ALTER TABLE stage7_research_responses ADD COLUMN raw_response_text TEXT NOT NULL DEFAULT '';
                                       -- The AI's full answer, exactly as pasted by the
                                       -- human, before any parsing/editing -- preserved for
                                       -- auditability independent of the (possibly
                                       -- human-corrected) structured findings_json/
                                       -- sources_json below. DEFAULT '' only so this
                                       -- ADD COLUMN succeeds against a table that -- in
                                       -- staging today -- has zero existing rows; the
                                       -- registration endpoint always supplies a real value
                                       -- going forward and rejects an empty one.
ALTER TABLE stage7_research_responses ADD COLUMN source_validation_json TEXT;
                                       -- The SERVER's own source-validation verdict at
                                       -- registration time -- [{url, status: valid|excluded|
                                       -- questionable, reason}], computed by worker.js's
                                       -- validateStage7Sources() (mirroring research/
                                       -- stage7_sentiment_recalculation.py's own
                                       -- normalize_response_sources() rejection rules) and
                                       -- NEVER trusted from the client -- see that
                                       -- function's own header comment. Never itself a
                                       -- claim that a URL's content was independently
                                       -- fetched/verified, only that it is syntactically
                                       -- well-formed, dated, and not post-cutoff.
