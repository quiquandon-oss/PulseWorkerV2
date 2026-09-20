-- Experiment Registry: the shared data contract for the CryptoPulse
-- V1-V4 research program ("Program Foundation" build authorization).
--
-- Purely additive -- a single new table, no existing table/column/view
-- touched, no V1/V2 prediction/selection/coefficient logic referenced.
-- This is administrative metadata ONLY: what an experiment IS, why it
-- exists, and what its next step is. It deliberately does NOT store
-- live metrics (sample size, measured result, OOS result) as columns,
-- because those change on every scheduled run and would silently go
-- stale the moment they were written here. Instead, `data_source_table`
-- names the existing D1 table (if any) that already holds an
-- experiment's live data; the read API (getResearchLabRegistry in
-- worker.js) joins against that table AT REQUEST TIME to compute the
-- live fields, so "current sample size" etc. are always real and
-- current, never a stored snapshot that drifts. `data_source_table`
-- being NULL is itself meaningful: it means the experiment has no
-- live-accumulating data source yet (PROPOSED/NOT_STARTED).
--
-- `status` is a free TEXT field (not an ENUM -- D1/SQLite has none) but
-- application code (and this migration's own seed rows) only ever uses
-- the fixed vocabulary defined in the Program Foundation build
-- authorization: PROPOSED, BUILDING, RUNNING, ACCUMULATING,
-- READY_FOR_ANALYSIS, ANALYZED, OOS_VALIDATION, CHATGPT_REVIEW,
-- HUMAN_APPROVAL, DEPLOYED, REJECTED, CONTINUE.
--
-- `experiment_type` is similarly free TEXT, restricted by convention to
-- TYPE_1 (coefficient/source research), TYPE_2 (prediction challenger),
-- TYPE_3 (incremental-information experiment), TYPE_4
-- (diagnostic/control) -- the same four-type framework already
-- documented in research/README.md's "Research Hierarchy and
-- Experiment Governance" section.
--
-- Applied directly to production D1 (sentiment-history) via the
-- Cloudflare D1 MCP tool in this same build session -- same established
-- out-of-band-application convention as migration 0002 (see that file's
-- own comment), since this table must exist before the read API can be
-- exercised against real data. This file is the permanent audit-trail
-- record of that action.
CREATE TABLE research_experiment_registry (
  experiment_id TEXT PRIMARY KEY,       -- e.g. 'EXP-004'; IDs are permanent, never reused
  title TEXT NOT NULL,
  research_question TEXT NOT NULL,
  purpose TEXT NOT NULL,
  experiment_type TEXT NOT NULL,        -- TYPE_1 .. TYPE_4
  expected_result TEXT NOT NULL,        -- stated BEFORE any result exists -- never edited after the fact
  success_criterion TEXT NOT NULL,
  start_date TEXT,                      -- ISO date (YYYY-MM-DD), NULL if not started
  target_date TEXT,                     -- ISO date (YYYY-MM-DD), NULL if no milestone date is defined
  status TEXT NOT NULL,                 -- see fixed vocabulary above
  baseline TEXT NOT NULL,               -- what this experiment is compared against
  required_sample INTEGER,              -- NULL if not yet defined for this experiment
  conclusion TEXT,                      -- NULL until the experiment reaches a real conclusion
  next_action TEXT NOT NULL,
  github_refs TEXT,                     -- free text: PR/commit/workflow references for THIS experiment's own execution
  data_source_table TEXT,               -- name of the D1 table backing live metrics, or NULL
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL           -- when this registry ROW (the administrative metadata) was last edited --
                                         -- distinct from a live experiment's own last data-write time, which the
                                         -- read API computes separately from data_source_table when present
);

-- Seed rows. EXP-004 reflects the ALREADY-VERIFIED real state of the
-- TimesFM challenger (this session's own direct D1 audit: 19/19
-- workflow runs successful, 30 forecasts, 28 resolved, 12/28 correct,
-- zero leakage, target_ts methodology verified). EXP-005 through
-- EXP-008 are registered PROPOSED/NOT_STARTED: no implementation exists
-- for any of them AS A TRACKED REGISTRY ENTRY under this new lifecycle
-- (EXP-005's topic has prior, separate, already-documented analysis in
-- research/README.md's PR5a-PR5g chain, and EXP-007's topic has a
-- related but organizationally separate system in CryptoPulseV4 -- both
-- disclosed in their own `purpose` text below as context, not folded
-- into this new experiment's own status/results, since neither ran
-- under this registry/lifecycle).
INSERT INTO research_experiment_registry
  (experiment_id, title, research_question, purpose, experiment_type,
   expected_result, success_criterion, start_date, target_date, status,
   baseline, required_sample, conclusion, next_action, github_refs,
   data_source_table, created_ts, updated_ts)
VALUES
  ('EXP-004',
   'TimesFM BTC Challenger',
   'Can a pretrained, univariate time-series foundation model (Google TimesFM 2.5) forecast BTC direction at 12h/24h horizons, using price history alone and no sentiment/coefficient signal?',
   'Establish a non-sentiment prediction-challenger baseline for later comparison against V1/V2, per the CryptoPulse research hierarchy (V1 composite -> {V2, TimesFM, Chronos} as parallel challengers/probes).',
   'TYPE_2',
   'No specific accuracy figure is asserted in advance. Once enough resolved observations exist, directional accuracy will be compared against V2 production and against descriptive always-UP/persistence baselines computed on the identical resolved rows.',
   'A pre-registered, out-of-sample-validated improvement over both the always-UP and persistence baselines, replicated rather than a single lucky window, before any claim of skill is made.',
   '2026-09-06', NULL, 'ACCUMULATING',
   'Always-UP and last-tick persistence, computed descriptively on the same resolved rows (see research/README.md Research Hierarchy section).',
   30,
   NULL,
   'Continue accumulating daily resolved observations via the existing exp004-timesfm.yml schedule; re-assess once >=30 resolved observations per horizon exist (a practical heuristic floor, not a proven statistical threshold -- see this session''s own Challenger Evaluation Protocol audit).',
   '.github/workflows/exp004-timesfm.yml; exp004-timesfm/run_experiment.py',
   'experiment_4_timesfm',
   1789900000000, 1789900000000),

  ('EXP-005',
   'V1 Source Incremental Value',
   'Do individual V1 sentiment sources carry information beyond the V1 composite that is useful for predicting BTC outcomes?',
   'Directly evaluates V1 sentiment sources for the coefficient rework. Note: a separate, already-documented research chain (PR5a-PR5g, see research/README.md) previously analyzed this exact question via chronological discovery/validation OLS gating; that prior work is context for this registry entry, not itself run under this registry/lifecycle.',
   'TYPE_1',
   'NOT_AVAILABLE', 'NOT_AVAILABLE',
   NULL, NULL, 'PROPOSED',
   'V1 composite-only baseline.',
   NULL, NULL,
   'Define this experiment''s own scope and success criterion as a registry-tracked unit before building; consider reusing PR5''s existing gate methodology rather than re-deriving one.',
   NULL, NULL,
   1789900000000, 1789900000000),

  ('EXP-006',
   'V1 Coefficient Hypothesis / OOS Validation',
   'For a specific candidate V1 coefficient change (a hypothesis, not yet chosen), does it produce a genuine, replicated out-of-sample improvement over current production?',
   'The controlled-change/OOS-validation stage of the production feedback loop (Evidence -> hypothesis -> controlled change -> OOS validation -> human approval -> production) documented in research/README.md''s governance section.',
   'TYPE_1',
   'NOT_AVAILABLE', 'NOT_AVAILABLE',
   NULL, NULL, 'PROPOSED',
   'Current production V1 coefficients.',
   NULL, NULL,
   'Awaits a specific candidate hypothesis (e.g. one of PR5g''s own BUILD_REQUEST-stage source candidates) to be selected as this experiment''s actual scope before it can move to BUILDING.',
   NULL, NULL,
   1789900000000, 1789900000000),

  ('EXP-007',
   'Sentiment vs No-Sentiment Control',
   'Does a purely technical, no-sentiment signal perform differently from V1/V2''s sentiment-informed predictions -- i.e., what is sentiment''s own marginal contribution?',
   'A diagnostic control arm for isolating sentiment''s marginal value, directly relevant to the V1 coefficient rework. Note: CryptoPulseV4 already operates an independent, no-sentiment technical-analysis signal engine (RSI/MACD/Bollinger/Ichimoku/regime) with its own accumulating outcome-resolution data, in a separate repository and D1 database from this registry -- disclosed here as context; this registry entry does not yet federate that data.',
   'TYPE_4',
   'NOT_AVAILABLE', 'NOT_AVAILABLE',
   NULL, NULL, 'PROPOSED',
   'V1/V2 sentiment-informed production predictions.',
   NULL, NULL,
   'Define a concrete comparison protocol against CryptoPulseV4''s existing signal data (cross-repo/cross-database federation is a future integration step, not built in this phase) before this can move to BUILDING.',
   NULL, NULL,
   1789900000000, 1789900000000),

  ('EXP-008',
   'Chronos Challenger',
   'Does Amazon Chronos-2 (Apache-2.0, native multivariate/covariate support) provide a useful second prediction-challenger reference point alongside TimesFM, and does covariate input (e.g. V1 composite, ETH/LINK) change its accuracy?',
   'A second, architecturally distinct prediction-challenger probe, per this session''s own Foundation Model Research Study and Challenger Evaluation Protocol Audit.',
   'TYPE_2',
   'NOT_AVAILABLE', 'NOT_AVAILABLE',
   NULL, NULL, 'PROPOSED',
   'TimesFM (EXP-004) and descriptive always-UP/persistence baselines.',
   NULL, NULL,
   'Not yet built -- explicitly out of scope for this Program Foundation PR (do not add Chronos). Remains PROPOSED until a dedicated, separately-authorized experiment PR builds it, following the same isolation pattern as EXP-004 (own table, own workflow, same forecast-origin/outcome-resolution protocol already specified for a future Chronos experiment).',
   NULL, NULL,
   1789900000000, 1789900000000);
