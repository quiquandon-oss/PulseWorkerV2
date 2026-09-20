-- Registers EXP-009 ("Event x Source x Real-World Evidence") in the
-- Experiment Registry seeded by migration 0010. Purely an INSERT of one
-- new row -- no schema change, no existing row touched. EXP-006 already
-- existed in the registry for a different, unrelated question (V1
-- Coefficient Hypothesis / OOS Validation, still PROPOSED,
-- data_source_table NULL); per explicit user direction this new
-- experiment is registered under the next free ID, EXP-009, and
-- EXP-006 is left completely untouched (verified unchanged immediately
-- before this migration was written: still PROPOSED, still NULL
-- data_source_table, still its own original title).
--
-- EXP-009 is complementary to EXP-005: EXP-005 asks whether a V1
-- source's REPRESENTATION carries information about subsequent BTC
-- movement; EXP-009 asks what real-world event that representation
-- corresponds to (if any) and whether that correspondence relates to
-- the event's own control-adjusted BTC reaction. It reuses, unmodified,
-- research_events / research_event_evidence (migrations 0005-0007),
-- controlled_event_reaction.py (PR57+PR58) and
-- event_source_relevance.py (PR59) -- the only genuinely new code is
-- research/event_source_evidence_join.py, which joins those two
-- independently-computed result sets by (event_ts, source_key) and
-- classifies six purely deterministic research descriptors from
-- already-computed categorical labels. No new statistical threshold.
--
-- required_sample=4 mirrors the user's own explicit milestone framing
-- ("a minimum of 4 independent weekly/event batches") -- this is an
-- evidence-accumulation/schema-validation phase, not a hypothesis test
-- with a significance threshold.
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
  ('EXP-009',
   'Event x Source x Real-World Evidence',
   'Around a detected market event, what real-world evidence (if any) was publicly available, what did a given V1 sentiment source appear to be representing at that time, and how does that representation relate to the event''s own control-adjusted subsequent BTC reaction?',
   'Complementary to EXP-005 (which asks whether a V1 source''s representation carries measurable information about subsequent BTC movement in the aggregate). This experiment instead examines individual detected events: it never treats an RSS article as the event itself, never treats the market-derived event detector as proof of real-world causality, and keeps EVENT / EVIDENCE / SOURCE REPRESENTATION conceptually separate throughout. Reuses research_events / research_event_evidence (already-collected real-world evidence), controlled_event_reaction.py (PR57 raw event-source-reaction + PR58 control-adjusted reaction) and event_source_relevance.py (PR59 topic-relevance classification) completely unchanged -- these two modules are deliberately built with zero import dependency on each other, so research/event_source_evidence_join.py is the first place their independently-computed results are joined, by (event_ts, source_key). Never modifies V1 coefficients, V2 prediction/selection logic, production sentiment weights, TimesFM, V4, or EXP-005''s own methodology; never automatically promotes any finding to production.',
   'TYPE_1',
   'Across accumulated weekly/event batches, some (event, source) pairs will classify as EVENT_SOURCE_ALIGNED or EVENT_SOURCE_REDUNDANT_POSSIBLE (source representation directionally consistent with real-world evidence AND with a genuine control-adjusted BTC reaction); others will classify as EVENT_SOURCE_NOT_ALIGNED, EVENT_SOURCE_INFORMATIONAL_ONLY, or EVENT_SOURCE_MISLEADING_POSSIBLE. A large share of (event, source) pairs are expected to classify as INSUFFICIENT_EVIDENCE, particularly for sources with no direct topic affinity and for internal-model-detected events, which are excluded from this expectation entirely (they are unconditionally INSUFFICIENT_EVIDENCE by construction, per event_source_evidence_join.py''s own rule, since no real-world evidence collection is attempted for them). No specific proportions are asserted in advance.',
   'This is an evidence-accumulation and schema-validation phase, not a hypothesis test: no statistical significance threshold is defined yet. The milestone is 4 independent weekly/event batches of accumulated (event, source) classifications with stable, well-formed output (correct temporal integrity, no duplicate event/source rows, every declared classification label reachable) -- only after that milestone is reached would a specific statistical criterion for a later, separately-authorized experiment be considered. These are purely descriptive research labels; none of EVENT_SOURCE_ALIGNED / NOT_ALIGNED / REDUNDANT_POSSIBLE / INFORMATIONAL_ONLY / MISLEADING_POSSIBLE / INSUFFICIENT_EVIDENCE implies or supports a causal claim from correlation alone.',
   '2026-09-20', NULL, 'PROPOSED',
   'No production baseline applies -- this experiment does not predict or score anything; PR57''s own raw (uncontrolled) event-source reaction and PR59''s own evidence-absence rate serve as internal descriptive reference points already computed by the reused, unmodified modules.',
   4,
   NULL,
   'Wire this registry entry to a live data_source_table once the first real scheduled run has produced at least one research_analyses row (mirroring EXP-005''s own PROPOSED -> real-first-run -> ACCUMULATING sequencing); do not claim accumulation before real data exists.',
   'research/event_source_evidence_join.py; research/test_event_source_evidence_join.py; exp009-event-source-evidence/run_experiment.py; exp009-event-source-evidence/test_exp009_run_experiment.py; controlled_event_reaction.py (PR57+PR58, unchanged, reused); event_source_relevance.py (PR59, unchanged, reused)',
   'research_analyses_exp009_event_source_evidence',
   1789920000000, 1789920000000);

-- status remains PROPOSED (not ACCUMULATING) as committed here -- it is
-- only updated to ACCUMULATING, in a SEPARATE, later step, after a real
-- workflow_dispatch/scheduled run has actually produced at least one
-- real research_analyses row for subject 'EXP-009:event_source_evidence',
-- so the registry never claims accumulation before any data genuinely
-- exists. EXP-006's own pre-existing row is not referenced or modified
-- anywhere in this file.
