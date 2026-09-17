-- Research ledger, PR1: minimal persistent foundation only.
--
-- Scope, exactly as authorized: schema + a bounded observation view over
-- EXISTING data + indexes + tests + documentation. No event detector, no
-- internet requests, no LLM calls, no workflow changes beyond wiring the
-- new tests into the existing Test job, no V1/V2 changes, no production
-- deployment changes. Zero paid dependencies.
--
-- Corrections applied from review, each noted at the point they matter:

-- ============================================================
-- research_observations_v1 -- CORRECTION 1 (avoid the correlated-
-- subquery trap)
-- ============================================================
-- The originally proposed view joined against selection_decisions via
--   sd.prediction_ts = (SELECT MAX(p.ts) FROM predictions p WHERE p.ts <= h.ts)
-- -- a correlated subquery re-evaluated per row of `history`, exactly the
-- shape that caused a real 16.7M-row-read incident earlier in this
-- project's history (an unrelated forensic analysis, but the same
-- underlying mistake). Rejected here before it could repeat.
--
-- PR1's view covers ONLY the `history` table -- a single-table SELECT,
-- no join at all, so there is no query-plan risk to evaluate: it can only
-- ever use `idx_ts` (already existing on history(ts), confirmed present
-- before writing this migration) or a full scan of a table currently
-- under 5KB. V2 linkage (selection_decisions, predictions) is explicitly
-- deferred to PR2, to be implemented only after a non-correlated join
-- strategy (an indexed range join or an explicitly-stored foreign key
-- captured at insert time, not computed after the fact) is proven safe
-- via EXPLAIN QUERY PLAN -- not before.
CREATE VIEW research_observations_v1 AS
SELECT
  ts,
  btc_price,
  score AS v1_composite,
  technical_score,
  bottom_score,
  regime_mag,
  gold_regime,
  sources_json
FROM history;

-- ============================================================
-- research_events -- CORRECTION 2 (idempotency) and CORRECTION 3
-- (events separated from evidence)
-- ============================================================
-- CORRECTION 2: `fingerprint` is a required, explicitly UNIQUE column --
-- not relying on AUTOINCREMENT id for identity. The future event
-- detector (PR3) must compute this deterministically from the event's
-- own defining properties (e.g. category + event_ts, or a hash of
-- category+event_ts+direction) so that re-running the same detection
-- logic against the same underlying data always produces the same
-- fingerprint, and a second insert attempt for an already-recorded event
-- is rejected by the UNIQUE constraint rather than creating a duplicate
-- row. "Same input -> same event identity -> no duplicate event" is
-- enforced by the schema itself, not by application-level care alone.
--
-- CORRECTION 3: this table holds ONLY the event's own properties.
-- Evidence (URLs, publication timestamps, extracted facts -- the
-- internet-evidence layer) deliberately has NO columns here. That
-- layer is out of scope for PR1 entirely (no internet requests happen
-- in this PR) and will be modeled later as its own table with a
-- one-to-many foreign key back to event_id (one event can have zero,
-- one, or several independent evidence records) -- not bolted onto
-- this table as if the relationship were one-to-one.
CREATE TABLE research_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL,
  event_ts INTEGER NOT NULL,
  detection_ts INTEGER NOT NULL,
  category TEXT NOT NULL,
  direction TEXT,
  intensity REAL,
  available_before_prediction INTEGER NOT NULL,
  is_post_event_analysis INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX idx_research_events_fingerprint ON research_events(fingerprint);
CREATE INDEX idx_research_events_event_ts ON research_events(event_ts);

-- ============================================================
-- research_analyses -- unchanged in shape from Phase 0, included here
-- since it carries no join risk (it is only ever written to, and read
-- back by its own primary key or analysis_ts -- no correlated pattern
-- possible against a table that references nothing else).
-- ============================================================
CREATE TABLE research_analyses (
  analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_ts INTEGER NOT NULL,
  window_start_ts INTEGER NOT NULL,
  window_end_ts INTEGER NOT NULL,
  sample_size INTEGER NOT NULL,
  subject TEXT NOT NULL,
  metric_json TEXT NOT NULL,
  multiple_testing_correction TEXT,
  validation_status TEXT NOT NULL DEFAULT 'observation'
);
CREATE INDEX idx_research_analyses_ts ON research_analyses(analysis_ts);

-- ============================================================
-- Deferred, documented, NOT built here -- CORRECTION 5
-- ============================================================
-- A future `research_runs` table (introduced alongside whichever PR
-- adds the first scheduled loop -- PR6 in the phased plan) must record,
-- per run: run_ts, workflow_run_id (the GitHub Actions run id, for
-- direct cross-reference), rows_processed, d1_rows_read/d1_rows_written
-- where measurable (D1's own query metadata already exposes this, as
-- used throughout this project's manual audits), external_requests_made,
-- external_requests_skipped (the cost-governor's own skip counter),
-- and execution_status. This is the mechanism that will let anyone
-- continuously verify the $0 constraint holds as the loop starts
-- actually running on a schedule, rather than trusting it by
-- inspection alone as PR1 still requires. Not created now because
-- nothing yet exists to run and account for -- PR1 has no scheduled
-- component at all.
