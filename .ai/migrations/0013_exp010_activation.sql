-- Registers EXP-010 ("Source Dialogue Validation") in the Experiment
-- Registry seeded by migration 0010. Purely an INSERT of one new row --
-- no schema change, no existing row touched.
--
-- EXP-010 validates the merged, independently-audited Source Dialogue
-- Engine (research/source_dialogue.py, PR #68) against real V1/V2
-- historical data ONLY -- no EIA, no GDELT (both remain PRIMARY
-- VERIFICATION BLOCKED and are never referenced here). Research
-- question: "When two existing V1 sources observe information around
-- the same market event, do they support, contradict, or remain
-- insufficiently comparable to each other after applying strict
-- temporal as-of rules?"
--
-- Reuses, unmodified: event_source_relevance.py (PR59, event detection
-- + V1 source snapshots), event_source_reaction.py (PR57, source-median
-- direction reference), source_analysis.py (PR5, source enumeration +
-- pairwise redundancy), and source_dialogue.py (the locked engine,
-- PR #68). The only genuinely new code is
-- research/exp010_source_dialogue_validation.py, which builds the
-- normalized observation contract the locked engine requires from real
-- V1 data and calls it -- it recomputes no classification itself.
--
-- required_sample=4 mirrors the same milestone framing already
-- established for EXP-005/EXP-009 ("a minimum of independent weekly
-- batches") -- this is a validation/diagnostic run, not a hypothesis
-- test with a statistical threshold.
--
-- Applied directly to production D1 (sentiment-history) via the
-- Cloudflare D1 MCP tool in this same build session, using this file's
-- own exact text -- per the migration-governance rule documented in
-- research/README.md.
INSERT INTO research_experiment_registry
  (experiment_id, title, research_question, purpose, experiment_type,
   expected_result, success_criterion, start_date, target_date, status,
   baseline, required_sample, conclusion, next_action, github_refs,
   data_source_table, created_ts, updated_ts)
VALUES
  ('EXP-010',
   'Source Dialogue Validation',
   'When two existing V1 sources observe information around the same market event, do they support, contradict, or remain insufficiently comparable to each other after applying strict temporal as-of rules?',
   'A validation/diagnostic experiment for the merged Source Dialogue Engine (research/source_dialogue.py, PR #68), exercised against real V1/V2 historical data ONLY -- no new external source (EIA, GDELT -- both PRIMARY VERIFICATION BLOCKED) is introduced here. Confirms the engine behaves correctly on data this project already has before any future EIA/GDELT integration is attempted.',
   'TYPE_1',
   'Across accumulated runs, some V1 source pairs around real (non-internal-model) market events will classify as SUPPORTING or CONTRADICTING; others will be INSUFFICIENT_EVIDENCE (missing/stale/out-of-window snapshots) or, rarely under current V1 snapshot semantics, DIFFERENT_TIMING (see this experiment''s own disclosed methodological finding: two present V1 sources compared via event_source_relevance.snapshot_v1_source always share the same observation timestamp, so DIFFERENT_TIMING is expected to be rare-to-absent for pure V1-vs-V1 pairs and becomes meaningful once a source with an independent timestamp axis, e.g. EIA or GDELT, is compared against a V1 source). Separately, the whole-window pairwise redundancy leg (reusing source_analysis.pairwise_source_redundancy() and STRONG_REDUNDANCY_THRESHOLD=0.7 unchanged) will flag some source pairs as REDUNDANCY_UNRESOLVED and most as NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED -- never a claim of independence or complementarity. No specific proportions are asserted in advance.',
   'This is a validation/diagnostic phase, not a hypothesis test: no statistical significance threshold is defined. The milestone is 4 independent weekly runs with stable, well-formed output (correct temporal as-of/window enforcement, no duplicate source-pair rows, canonical A/B ordering preserved, every declared relationship/redundancy label reachable) -- confirming the locked engine remains correct against real, evolving V1/V2 data over time.',
   '2026-09-21', NULL, 'PROPOSED',
   'No production baseline applies -- this experiment does not predict or score anything; it validates a research tool''s own correctness against real data.',
   4,
   NULL,
   'Wire this registry entry to a live data_source_table once the first real scheduled run has produced at least one research_analyses row; do not claim accumulation before real data exists.',
   'research/exp010_source_dialogue_validation.py; research/test_exp010_source_dialogue_validation.py; exp010-source-dialogue-validation/run_experiment.py; exp010-source-dialogue-validation/test_exp010_run_experiment.py; research/source_dialogue.py (PR #68, locked, unchanged, reused); event_source_relevance.py / event_source_reaction.py / source_analysis.py (unchanged, reused)',
   'research_analyses_exp010_source_dialogue_validation',
   1789970000000, 1789970000000);

-- status remains PROPOSED (not ACCUMULATING) as committed here -- it is
-- only updated to ACCUMULATING, in a SEPARATE, later step, after a real
-- workflow_dispatch/scheduled run has actually produced at least one
-- real research_analyses row for subject
-- 'EXP-010:source_dialogue_validation', so the registry never claims
-- accumulation before any data genuinely exists. EXP-005/EXP-006/
-- EXP-009's own pre-existing rows are not referenced or modified
-- anywhere in this file.
