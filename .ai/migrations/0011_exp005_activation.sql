-- Phase 2: activates EXP-005 in the Experiment Registry seeded by
-- migration 0010. Purely a content UPDATE on the existing
-- research_experiment_registry row for experiment_id='EXP-005' --
-- no schema change, no new table, no other row touched.
--
-- Replaces the PROPOSED-stage placeholder text (NOT_AVAILABLE for
-- expected_result/success_criterion, NULL dates, generic next_action)
-- with the explicit, measurable definition required before this
-- experiment starts accumulating real data. See
-- exp005-source-effectiveness/run_experiment.py for the automation
-- this activates, and research/source_analysis.py (UNCHANGED, reused)
-- for the statistical machinery it runs.
--
-- Applied directly to production D1 (sentiment-history) via the
-- Cloudflare D1 MCP tool in this same build session, using this file's
-- own exact text (not a separately re-typed equivalent) -- per the
-- migration-governance rule this project's own research/README.md now
-- documents, following the drift incident found and corrected during
-- Phase 1's adversarial re-audit.
UPDATE research_experiment_registry SET
  title = 'V1 Source / Coefficient Research',
  purpose = 'Determine, per V1 sentiment source, whether its representation carries measurable, potentially incremental information about subsequent BTC movement beyond the V1 composite, decomposed into seven separate evidence axes (directional alignment, predictive association, incremental information beyond composite, horizon dependence, regime/event dependence, redundancy with other sources, evidence strength) -- to determine whether the current V1 source contribution is justified by evidence, without implying any production change. Reuses research/source_analysis.py''s existing PR5 statistical machinery completely unchanged.',
  expected_result = 'Across >=4 independent weekly analysis runs (chronologically shifted 90-day windows), some V1 sources will show STATISTICALLY_SIGNIFICANT (Benjamini-Hochberg corrected) association with subsequent BTC return AND a measurable out-of-sample RMSE reduction beyond the V1 composite alone, in at least one of the tested horizons (1h/3h/6h/12h/24h); other sources will show no such incremental signal. The specific sources and horizons that qualify are not predicted in advance.',
  success_criterion = 'A candidate source/horizon pair is evidence-worthy only if it reaches STATISTICALLY_SIGNIFICANT (multiple-testing corrected) association AND a positive OOS RMSE-reduction (IMPROVED) beyond the V1 composite in EVERY one of at least 4 accumulated independent weekly runs, not merely one -- replication across time, not a single positive result, is required before this experiment''s evidence is used to propose a coefficient/weight hypothesis (EXP-006).',
  start_date = '2026-09-20',
  target_date = '2026-10-18',
  status = 'PROPOSED',
  baseline = 'V1 composite-only model (Level 3''s own nested-regression baseline); the project''s established naive always-UP/persistence descriptive BTC baselines (see EXP-004 / research/README.md''s Research Hierarchy section); and PR5a-PR5g''s own prior historical source-effectiveness snapshot (research/README.md) as the existing qualitative source-level result this experiment''s live-updating version extends.',
  required_sample = 4,
  next_action = 'Wait for >=4 independent weekly analysis runs to accumulate via the new exp005-source-effectiveness.yml GitHub Actions schedule; re-assess replication once the threshold is reached. Do not draw a coefficient conclusion before then.',
  github_refs = '.github/workflows/exp005-source-effectiveness.yml; exp005-source-effectiveness/run_experiment.py; research/source_analysis.py (unchanged, reused)',
  data_source_table = 'research_analyses_exp005_source_effectiveness',
  updated_ts = 1789910000000
WHERE experiment_id = 'EXP-005';

-- status remains PROPOSED (not ACCUMULATING) as committed here -- it is
-- only updated to ACCUMULATING, in a SEPARATE, later step, after a real
-- workflow_dispatch run has actually produced at least one real
-- research_analyses row, so the registry never claims accumulation
-- before any data genuinely exists.
