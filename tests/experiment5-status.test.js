import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

// CI runs Node 20 (no node:sqlite), so D1 is faked in plain JS. The fake implements exactly the statement
// shapes the Worker issues and REJECTS any other SQL, so a changed/extra query fails loudly instead of
// being silently answered. A separate test cross-checks the Worker's column usage against the real
// migration 0019 text, and the Python suite runs the writer's INSERT against that same migration in real sqlite.
const MIGRATION_0019 = readFileSync(new URL('../.ai/migrations/0019_experiment5_pipeline_runs.sql', import.meta.url), 'utf8');
const HOUR = 3600000;
const RUN_COLUMNS = ['run_id', 'run_ts', 'status', 'error_text', 'pipeline_version', 'constants_json', 'history_rows_read',
  'btc_rows_read', 'newly_archived', 'archive_rows_observed', 'observations_without_sources', 'observations_rejected_malformed',
  'rejected_observation_ts_json', 'sources_observed', 'candidate_new_sources_json', 'agent_status', 'decisions_replayed',
  'decisions_created', 'decisions_evaluated', 'evaluated_passed', 'evaluated_failed', 'evaluated_inconclusive'];

function newState({ withRunLog }) {
  return { withRunLog, runs: [], hyps: [], nextRunId: 1, log: [] };
}

function d1Over(state) {
  const sortRuns = (rows) => [...rows].sort((a, b) => b.run_ts - a.run_ts || b.run_id - a.run_id);
  const answer = (sql) => {
    const q = sql.replace(/\s+/g, ' ').trim();
    let m;
    if ((m = q.match(/^SELECT \* FROM experiment5_pipeline_runs ORDER BY run_ts DESC, run_id DESC LIMIT (\d+)$/))) {
      if (!state.withRunLog) throw new Error('no such table: experiment5_pipeline_runs');
      return { all: sortRuns(state.runs).slice(0, Number(m[1])) };
    }
    if (q === 'SELECT COUNT(*) AS n FROM experiment5_pipeline_runs') {
      if (!state.withRunLog) throw new Error('no such table: experiment5_pipeline_runs');
      return { first: { n: state.runs.length } };
    }
    if ((m = q.match(/^SELECT \* FROM experiment5_pipeline_runs WHERE status = '(OK|FAILED)' ORDER BY run_ts DESC, run_id DESC LIMIT 1$/))) {
      if (!state.withRunLog) throw new Error('no such table: experiment5_pipeline_runs');
      return { first: sortRuns(state.runs.filter((r) => r.status === m[1]))[0] || null };
    }
    if (q === "SELECT evidence_summary_json FROM research_hypotheses WHERE subject LIKE 'experiment5:%' AND out_of_sample_status IS NOT NULL") {
      return { all: state.hyps.filter((h) => h.out_of_sample_status !== null).map((h) => ({ evidence_summary_json: h.evidence_summary_json })) };
    }
    throw new Error('UNEXPECTED SQL (the fake only answers the shapes the Worker is meant to issue): ' + q);
  };
  return {
    prepare(sql) {
      state.log.push(sql);
      return { first: async () => answer(sql).first, all: async () => ({ results: answer(sql).all }) };
    },
  };
}

function freshState({ withRunLog }) { return newState({ withRunLog }); }

function addRun(state, runTs, status, extra = {}) {
  const row = Object.fromEntries(RUN_COLUMNS.map((c) => [c, null]));
  Object.assign(row, {
    run_id: state.nextRunId++, run_ts: runTs, status,
    error_text: status === 'FAILED' ? (extra.error || 'RuntimeError: boom') : null,
    pipeline_version: 'experiment5-pipeline-v2',
    constants_json: '{"target_horizon_hours":24,"horizon_tolerance_ms":21600000}',
    history_rows_read: extra.history ?? 10, newly_archived: extra.archived ?? 0, archive_rows_observed: extra.observed ?? 10,
    observations_without_sources: extra.nosrc ?? 0, observations_rejected_malformed: extra.rejected ?? 0,
    rejected_observation_ts_json: '[]', decisions_replayed: extra.replayed ?? 0, decisions_created: extra.created ?? 0,
    decisions_evaluated: extra.evaluated ?? 0,
  });
  state.runs.push(row);
}

function addDecision(state, outcomeStatus, outcome) {
  state.hyps.push({ out_of_sample_status: outcomeStatus, evidence_summary_json: JSON.stringify({ decision: { eligible_ts: 1 }, outcome }) });
}

describe('getResearchLabExperiment5Status', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(
      extractConstants('EXPERIMENT5_MIN_SAMPLE_FOR_CONCLUSION', 'EXPERIMENT5_EXPECTED_RUN_INTERVAL_MS', 'EXPERIMENT5_RECENT_RUNS_LIMIT') + '\n' +
      extractFunctions('getResearchLabExperiment5Status', 'getResearchLabExperiment5Results')
    );
  });

  it('run log table absent (migration 0019 not applied): says so, does not guess, never throws', async () => {
    const result = await scope.getResearchLabExperiment5Status({ DB: d1Over(freshState({ withRunLog: false })) }, 100 * HOUR);
    expect(result.ok).toBe(true);
    expect(result.operational.run_log_available).toBe(false);
    expect(result.operational.last_run).toBeNull();
    expect(result.operational.overdue).toBeNull();
    expect(result.operational.reason).toMatch(/0019/);
    expect(result.operational.reason).toMatch(/does not mean the pipeline is not running/);
  });

  it('run log present but empty: available, no last run, not overdue-judged', async () => {
    const result = await scope.getResearchLabExperiment5Status({ DB: d1Over(freshState({ withRunLog: true })) }, 100 * HOUR);
    expect(result.operational.run_log_available).toBe(true);
    expect(result.operational.runs_total).toBe(0);
    expect(result.operational.last_run).toBeNull();
    expect(result.operational.overdue).toBeNull();
  });

  it('last run / last success / last failure and consecutive failures come from the persisted rows', async () => {
    const st = freshState({ withRunLog: true });
    addRun(st, 1 * HOUR, 'OK', { archived: 3 });
    addRun(st, 2 * HOUR, 'FAILED', { error: 'OperationalError: no such table: btc_data' });
    addRun(st, 3 * HOUR, 'FAILED');
    const result = await scope.getResearchLabExperiment5Status({ DB: d1Over(st) }, 4 * HOUR);
    const op = result.operational;
    expect(op.runs_total).toBe(3);
    expect(op.last_run.run_ts).toBe(3 * HOUR);
    expect(op.last_run.status).toBe('FAILED');
    expect(op.last_success.run_ts).toBe(1 * HOUR);
    expect(op.last_success.newly_archived).toBe(3);
    expect(op.last_failure.run_ts).toBe(3 * HOUR);
    expect(op.consecutive_failures).toBe(2);
    expect(op.recent_runs.map((r) => r.run_ts)).toEqual([3 * HOUR, 2 * HOUR, 1 * HOUR]);
    expect(op.last_run.constants.target_horizon_hours).toBe(24);
  });

  it('a success after failures resets the consecutive-failure count to zero', async () => {
    const st = freshState({ withRunLog: true });
    addRun(st, 1 * HOUR, 'FAILED');
    addRun(st, 2 * HOUR, 'OK');
    const result = await scope.getResearchLabExperiment5Status({ DB: d1Over(st) }, 3 * HOUR);
    expect(result.operational.consecutive_failures).toBe(0);
  });

  it('overdue only after two missed intervals (12h), judged against the supplied clock', async () => {
    const st = freshState({ withRunLog: true });
    addRun(st, 0, 'OK');
    const at = async (t) => (await scope.getResearchLabExperiment5Status({ DB: d1Over(st) }, t)).operational.overdue;
    expect(await at(12 * HOUR)).toBe(false);
    expect(await at(12 * HOUR + 1)).toBe(true);
  });

  it('a malformed JSON column in one run row does not break the status', async () => {
    const st = freshState({ withRunLog: true });
    addRun(st, 1 * HOUR, 'OK');
    st.runs.forEach((r) => { r.constants_json = '{bad'; r.rejected_observation_ts_json = '{bad'; });
    const result = await scope.getResearchLabExperiment5Status({ DB: d1Over(st) }, 2 * HOUR);
    expect(result.operational.last_run.constants).toBeNull();
    expect(result.operational.last_run.rejected_observation_ts).toEqual([]);
  });

  it('predictive block never states a verdict, even with a large sample where the challenger beats V1', async () => {
    const st = freshState({ withRunLog: true });
    for (let i = 0; i < 30; i++) {
      addDecision(st, 'PASSED_HOLDOUT', { agent_correct: true, v1_baseline_correct: i % 2 === 0 });
    }
    const result = await scope.getResearchLabExperiment5Status({ DB: d1Over(st) }, HOUR);
    const p = result.predictive;
    expect(p.n_resolved).toBe(30);
    expect(p.sufficient_sample).toBe(true);
    expect(p.challenger_accuracy).toBe(1);
    expect(p.v1_baseline_accuracy).toBe(0.5);
    expect(p.success_criterion_defined).toBe(false);
    expect(p.verdict).toBeNull();
    expect(p.note).toMatch(/No pre-registered success criterion/);
  });

  it('operational success (healthy runs) and predictive evidence are independent blocks', async () => {
    const st = freshState({ withRunLog: true });
    addRun(st, 1 * HOUR, 'OK');
    const result = await scope.getResearchLabExperiment5Status({ DB: d1Over(st) }, 2 * HOUR);
    expect(result.operational.last_run.status).toBe('OK');
    expect(result.predictive.n_resolved).toBe(0);
    expect(result.predictive.verdict).toBeNull();
    expect(result.predictive.sufficient_sample).toBe(false);
  });

  it('issues only SELECT statements', async () => {
    const st = freshState({ withRunLog: true });
    addRun(st, 1 * HOUR, 'OK');
    addDecision(st, 'PASSED_HOLDOUT', { agent_correct: true, v1_baseline_correct: false });
    await scope.getResearchLabExperiment5Status({ DB: d1Over(st) }, 2 * HOUR);
    expect(st.log.length).toBeGreaterThan(0);
    for (const sql of st.log) expect(sql.trim()).toMatch(/^SELECT /i);
  });

  it('every column the Worker reads from the run log exists in migration 0019', () => {
    const body = MIGRATION_0019.slice(MIGRATION_0019.indexOf('CREATE TABLE experiment5_pipeline_runs'));
    const declared = [...body.slice(0, body.indexOf(');')).matchAll(/^\s{2}([a-z_0-9]+) /gm)].map((m) => m[1]);
    expect(declared.sort()).toEqual([...RUN_COLUMNS].sort());
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const fn = src.slice(src.indexOf('async function getResearchLabExperiment5Status'), src.indexOf('async function getResearchLabExperiment5Decisions'));
    for (const col of fn.matchAll(/r\.([a-z_0-9]+)/g)) {
      if (col[1] === 'status' || col[1] === 'n') continue;
      expect(declared).toContain(col[1]);
    }
  });

  it('the route is registered as GET-only and read-only', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toMatch(/url\.pathname === '\/api\/research-lab\/experiment5-status' && request\.method === 'GET'/);
  });
});

describe('Experiment 5 status card (frontend helper)', () => {
  let ui;
  beforeAll(() => {
    ui = evalInScope(
      `const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
       const fmtTs = (t) => 'T' + t;
       const badge = (text, cls) => '[' + cls + ':' + text + ']';
       const emptyState = (h, d) => '{EMPTY ' + h + ' | ' + d + '}';\n` +
      extractFunctions('exp5Num', 'exp5RunRowHtml', 'exp5StatusHtml')
    );
  });
  const predictive = { n_resolved: 0, challenger_evaluable_n: 0, baseline_evaluable_n: 0, min_sample_for_conclusion: 20, sufficient_sample: false, success_criterion_defined: false, note: 'No pre-registered success criterion exists.' };

  it('unavailable run log is shown as unavailable, not as a healthy or failed run', () => {
    const html = ui.exp5StatusHtml({ ok: true, operational: { run_log_available: false, reason: 'Run log not available: migration 0019' }, predictive });
    expect(html).toContain('Run log not available');
    expect(html).not.toContain('[b-verified:OK]');
    expect(html).not.toContain('FAILED');
  });

  it('a failed last run is flagged with its error and failure count; verdict is always NO CONCLUSION', () => {
    const run = { run_id: 2, run_ts: 5, status: 'FAILED', error_text: 'OperationalError: x', pipeline_version: 'v2', constants: null, newly_archived: null, decisions_created: null, decisions_evaluated: null };
    const html = ui.exp5StatusHtml({ ok: true, operational: { run_log_available: true, runs_total: 1, last_run: run, last_success: null, last_failure: run, consecutive_failures: 1, overdue: false, recent_runs: [run] }, predictive });
    expect(html).toContain('[b-blocked:FAILED]');
    expect(html).toContain('OperationalError: x');
    expect(html).toContain('never');
    expect(html).toContain('[b-unknown:NO CONCLUSION]');
    expect(html).toContain('none defined');
  });

  it('escapes error text (no HTML injection from persisted run rows)', () => {
    const run = { run_id: 1, run_ts: 5, status: 'FAILED', error_text: '<img src=x onerror=alert(1)>', pipeline_version: 'v2', constants: null };
    const html = ui.exp5StatusHtml({ ok: true, operational: { run_log_available: true, runs_total: 1, last_run: run, last_success: null, last_failure: run, consecutive_failures: 1, overdue: false, recent_runs: [run] }, predictive });
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });

  it('failed status fetch renders an unavailable state', () => {
    expect(ui.exp5StatusHtml(null)).toContain('Status unavailable');
  });
});
