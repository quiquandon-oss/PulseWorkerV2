-- PR5a: persistent hypothesis lifecycle table.
--
-- Purely additive. No existing table touched, no existing column changed,
-- no existing row affected.
--
-- research_hypotheses tracks a hypothesis ACROSS repeated analysis runs --
-- something research_analyses (one row per single statistical run) cannot
-- represent on its own. status is free text here (not a SQL CHECK
-- constraint) so the OBSERVATION -> MONITOR -> RESEARCH_HYPOTHESIS ->
-- VALIDATION_READY -> BUILD_REQUEST -> AWAITING_APPROVAL -> IMPLEMENTED ->
-- VALIDATED -> REJECTED -> ROLLED_BACK lifecycle defined in the approved
-- PR5 specification can evolve without a schema migration each time a
-- state name changes -- the same convention research_analyses.
-- validation_status already uses.
--
-- evidence_summary_json is deliberately never collapsed to a single
-- significant/not-significant flag. Per the PR5 specification review, it
-- must retain, at minimum: effect size, confidence interval, sample size,
-- p-value (post multiple-testing correction), out-of-sample stability,
-- regime stability, and economic/practical magnitude. out_of_sample_status
-- holds one of the four labels the specification defines from that record:
-- STATISTICALLY_SIGNIFICANT, STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE,
-- INCONCLUSIVE, NOT_REPLICATED, or CONTRADICTED (CONTRADICTED reserved for
-- an adequate independent sample showing a materially incompatible
-- effect/sign -- not mere non-replication with insufficient data).
--
-- This PR (5a) creates the table only. No PR5 analysis code writes to it
-- yet -- that begins in PR5b/c and is not implemented here.
CREATE TABLE research_hypotheses (
  hypothesis_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ts INTEGER NOT NULL,
  last_updated_ts INTEGER NOT NULL,
  subject TEXT NOT NULL,
  statement TEXT NOT NULL,
  source_analysis_ids TEXT NOT NULL,   -- JSON array of research_analyses.analysis_id
  status TEXT NOT NULL DEFAULT 'OBSERVATION',
  evidence_summary_json TEXT,
  out_of_sample_status TEXT
);
CREATE INDEX idx_research_hypotheses_status ON research_hypotheses(status);
CREATE INDEX idx_research_hypotheses_subject ON research_hypotheses(subject);
