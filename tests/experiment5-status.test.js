import { describe, it, expect, beforeAll, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';
import worker from '../worker.js';

// Experiment 5 operational status vs predictive evidence, with truthful error classification.
//
// CI runs Node 20 (no node:sqlite), so D1 is faked in plain JS. The fake implements exactly the statement shapes
// the Worker issues and REJECTS any other SQL, so a changed/extra query fails loudly instead of being silently
// answered. Two further checks keep the fake honest: the Worker's column usage is cross-checked against the real
// migration 0019 text below, and research/test_experiment5_status_sql.py executes the Worker's actual aggregate SQL
// in real sqlite (incl. malformed JSON rows) -- that is where counting semantics are proven.
const MIGRATION_0019 = readFileSync(new URL('../.ai/migrations/0019_experiment5_pipeline_runs.sql', import.meta.url), 'utf8');
const WORKER_SRC = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
const HOUR = 3600000;
let RUN_COLUMNS; // the Worker's own explicit column list (EXPERIMENT5_RUN_COLUMNS), compared with migration 0019 below

let PREDICTIVE_SQL;
function newState({ withRunLog = true } = {}) {
  return { withRunLog, runs: [], hyps: [], nextRunId: 1, log: [], runLogError: null, predictiveError: null };
}

function d1Over(state) {
  const sortRuns = (rows) => [...rows].sort((a, b) => b.run_ts - a.run_ts || b.run_id - a.run_id);
  const guardRunLog = () => {
    if (state.runLogError) throw state.runLogError;
    if (!state.withRunLog) throw new Error('D1_ERROR: no such table: experiment5_pipeline_runs: SQLITE_ERROR');
  };
  const answer = (sql) => {
    const q = sql.replace(/\s+/g, ' ').trim();
    let m;
    const cols = RUN_COLUMNS.join(', ');
    if ((m = q.match(new RegExp(`^SELECT ${cols} FROM experiment5_pipeline_runs ORDER BY run_ts DESC, run_id DESC LIMIT (\\d+)$`)))) {
      guardRunLog();
      return { all: sortRuns(state.runs).slice(0, Number(m[1])) };
    }
    if (q === 'SELECT COUNT(*) AS n FROM experiment5_pipeline_runs') {
      guardRunLog();
      return { first: { n: state.runs.length } };
    }
    if ((m = q.match(new RegExp(`^SELECT ${cols} FROM experiment5_pipeline_runs WHERE status = '(OK|FAILED)' ORDER BY run_ts DESC, run_id DESC LIMIT 1$`)))) {
      guardRunLog();
      return { first: sortRuns(state.runs.filter((r) => r.status === m[1]))[0] || null };
    }
    if (q === PREDICTIVE_SQL.replace(/\s+/g, ' ').trim()) {
      if (state.predictiveError) throw state.predictiveError;
      // Mirrors the SQL's semantics for the fake only (the real SQL is executed in real sqlite by the Python test).
      let n_malformed = 0, agent_correct = 0, agent_incorrect = 0, v1_correct = 0, v1_incorrect = 0;
      for (const h of state.hyps) {
        let payload; try { payload = JSON.parse(h.evidence_summary_json); } catch (_e) { n_malformed++; continue; }
        const o = payload && payload.outcome;
        if (!o) continue;
        if (o.agent_correct === true) agent_correct++; else if (o.agent_correct === false) agent_incorrect++;
        if (o.v1_baseline_correct === true) v1_correct++; else if (o.v1_baseline_correct === false) v1_incorrect++;
      }
      return { first: { n_resolved: state.hyps.length, n_malformed, agent_correct, agent_incorrect, v1_correct, v1_incorrect } };
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
    decisions_skipped_duplicate: extra.dup ?? 0, decisions_evaluated: extra.evaluated ?? 0,
  });
  state.runs.push(row);
}

function addDecision(state, outcome, raw) {
  state.hyps.push({ evidence_summary_json: raw !== undefined ? raw : JSON.stringify({ decision: { eligible_ts: 1 }, outcome }) });
}

describe('getResearchLabExperiment5Status', () => {
  let scope;
  beforeAll(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    scope = evalInScope(
      extractConstants('EXPERIMENT5_MIN_SAMPLE_FOR_CONCLUSION', 'EXPERIMENT5_EXPECTED_RUN_INTERVAL_MS', 'EXPERIMENT5_RECENT_RUNS_LIMIT',
        'EXPERIMENT5_RUN_COLUMNS', 'EXPERIMENT5_RUN_SELECT', 'EXPERIMENT5_STATE_MESSAGES', 'EXPERIMENT5_PREDICTIVE_COUNTS_SQL') + '\n' +
      extractFunctions('getResearchLabExperiment5Status', 'classifyExperiment5Error')
    );
    PREDICTIVE_SQL = scope.EXPERIMENT5_PREDICTIVE_COUNTS_SQL;
    RUN_COLUMNS = scope.EXPERIMENT5_RUN_COLUMNS;
  });
  const status = (state, now = 100 * HOUR) => scope.getResearchLabExperiment5Status({ DB: d1Over(state) }, now);

  describe('error classification (pure)', () => {
    const k = (msg) => scope.classifyExperiment5Error(new Error(msg)).kind;
    it('a missing table is TABLE_MISSING', () => {
      expect(k('D1_ERROR: no such table: experiment5_pipeline_runs: SQLITE_ERROR')).toBe('TABLE_MISSING');
    });
    it('a missing column is SCHEMA_DRIFT and names only the column', () => {
      const c = scope.classifyExperiment5Error(new Error('D1_ERROR: no such column: decisions_skipped_duplicate at offset 31: SQLITE_ERROR'));
      expect(c).toEqual({ kind: 'SCHEMA_DRIFT', column: 'decisions_skipped_duplicate' });
    });
    it('unparseable stored data is MALFORMED_DATA', () => {
      expect(k('D1_ERROR: malformed JSON')).toBe('MALFORMED_DATA');
    });
    it.each(['D1_ERROR: Network connection lost', 'D1 DB is overloaded. Requests queued for too long.', 'Error: timeout of 30000ms exceeded',
      'D1_ERROR: internal error', 'too many requests', 'Worker exceeded CPU... connection reset'])('transient/database-side failure %j is QUERY_FAILED', (msg) => {
      expect(k(msg)).toBe('QUERY_FAILED');
    });
    it('a SQL syntax error is a bug, not a transient condition, even inside a D1_ERROR wrapper', () => {
      expect(k('D1_ERROR: near "FORM": syntax error at offset 9: SQLITE_ERROR')).toBe('UNEXPECTED');
    });
    it.each(['TypeError: cannot read properties of undefined', 'something odd', ''])('anything else (%j) is UNEXPECTED', (msg) => {
      expect(k(msg)).toBe('UNEXPECTED');
    });
    it('non-Error throwables are classified, not crashed on', () => {
      expect(scope.classifyExperiment5Error('no such table: x').kind).toBe('TABLE_MISSING');
      expect(scope.classifyExperiment5Error(undefined).kind).toBe('UNEXPECTED');
      expect(scope.classifyExperiment5Error(null).kind).toBe('UNEXPECTED');
    });
  });

  describe('run log (operational block)', () => {
    it('table missing: TABLE_MISSING, says execution health is UNKNOWN and migration 0019 is the reason', async () => {
      const r = await status(newState({ withRunLog: false }));
      expect(r.ok).toBe(true);
      expect(r.operational.run_log_available).toBe(false);
      expect(r.operational.run_log_state).toBe('TABLE_MISSING');
      expect(r.operational.last_run).toBeNull();
      expect(r.operational.overdue).toBeNull();
      expect(r.operational.reason).toMatch(/migration 0019 is not applied/);
      expect(r.operational.reason).toMatch(/neither healthy nor failed/);
      expect(r.operational.reason).toMatch(/does not mean the pipeline is not running/);
    });

    it('column missing: SCHEMA_DRIFT, names the column, does NOT say the migration is missing', async () => {
      const st = newState();
      st.runLogError = new Error('D1_ERROR: no such column: decisions_skipped_duplicate at offset 12: SQLITE_ERROR');
      const r = await status(st);
      expect(r.operational.run_log_state).toBe('SCHEMA_DRIFT');
      expect(r.operational.reason).toMatch(/missing column: decisions_skipped_duplicate/);
      expect(r.operational.reason).not.toMatch(/is not applied/);
      expect(r.operational.reason).not.toMatch(/offset 12|SQLITE_ERROR/);
    });

    it('transient D1 error: QUERY_FAILED, explicitly NOT evidence of a missing migration or a failing pipeline', async () => {
      const st = newState();
      st.runLogError = new Error('D1_ERROR: Network connection lost');
      const r = await status(st);
      expect(r.operational.run_log_state).toBe('QUERY_FAILED');
      expect(r.operational.reason).toMatch(/NOT evidence that migration 0019 is missing or that the pipeline is failing/);
      expect(r.operational.reason).not.toMatch(/Network connection lost/);
      expect(r.operational.last_run).toBeNull();
    });

    it('malformed data raised by the database: MALFORMED_DATA, rows untouched', async () => {
      const st = newState();
      st.runLogError = new Error('D1_ERROR: malformed JSON');
      expect((await status(st)).operational.run_log_state).toBe('MALFORMED_DATA');
    });

    it('unexpected failure: UNEXPECTED, logged server-side, no raw error text in the response', async () => {
      const st = newState();
      st.runLogError = new Error('secret-ish internal detail SELECT token FROM x');
      const r = await status(st);
      expect(r.operational.run_log_state).toBe('UNEXPECTED');
      expect(JSON.stringify(r)).not.toMatch(/secret-ish|SELECT token/);
      expect(console.error).toHaveBeenCalled();
    });

    it('a failing run log does not hide the predictive block (and vice versa)', async () => {
      const st = newState({ withRunLog: false });
      addDecision(st, { agent_correct: true, v1_baseline_correct: false });
      const r = await status(st);
      expect(r.operational.run_log_state).toBe('TABLE_MISSING');
      expect(r.predictive.available).toBe(true);
      expect(r.predictive.n_resolved).toBe(1);
    });

    it('present but empty: AVAILABLE, no last run, not overdue-judged', async () => {
      const r = await status(newState());
      expect(r.operational).toMatchObject({ run_log_available: true, run_log_state: 'AVAILABLE', runs_total: 0, last_run: null, overdue: null });
    });

    it('last run / last success / last failure and consecutive failures come from the persisted rows', async () => {
      const st = newState();
      addRun(st, 1 * HOUR, 'OK', { archived: 3, dup: 2 });
      addRun(st, 2 * HOUR, 'FAILED', { error: 'OperationalError: no such table: btc_data' });
      addRun(st, 3 * HOUR, 'FAILED');
      const op = (await status(st, 4 * HOUR)).operational;
      expect(op.runs_total).toBe(3);
      expect(op.last_run).toMatchObject({ run_ts: 3 * HOUR, status: 'FAILED' });
      expect(op.last_success).toMatchObject({ run_ts: 1 * HOUR, newly_archived: 3, decisions_skipped_duplicate: 2 });
      expect(op.last_failure.run_ts).toBe(3 * HOUR);
      expect(op.consecutive_failures).toBe(2);
      expect(op.recent_runs.map((r) => r.run_ts)).toEqual([3 * HOUR, 2 * HOUR, 1 * HOUR]);
      expect(op.last_run.constants.target_horizon_hours).toBe(24);
    });

    it('a success after failures resets the consecutive-failure count', async () => {
      const st = newState();
      addRun(st, 1 * HOUR, 'FAILED'); addRun(st, 2 * HOUR, 'OK');
      expect((await status(st, 3 * HOUR)).operational.consecutive_failures).toBe(0);
    });

    it('overdue only after two missed intervals (12h), judged against the supplied clock', async () => {
      const st = newState();
      addRun(st, 0, 'OK');
      expect((await status(st, 12 * HOUR)).operational.overdue).toBe(false);
      expect((await status(st, 12 * HOUR + 1)).operational.overdue).toBe(true);
    });

    it('a malformed JSON column in a run row is reported as a data warning, not silently swallowed and not fatal', async () => {
      const st = newState();
      addRun(st, 1 * HOUR, 'OK'); addRun(st, 2 * HOUR, 'OK');
      st.runs[0].constants_json = '{bad';
      const op = (await status(st, 3 * HOUR)).operational;
      expect(op.run_log_state).toBe('AVAILABLE');
      expect(op.runs_with_malformed_fields).toBe(1);
      const bad = op.recent_runs.find((r) => r.run_ts === 1 * HOUR);
      expect(bad.constants).toBeNull();
      expect(bad.data_warnings).toEqual(['constants_json is not valid JSON']);
      expect(op.recent_runs.find((r) => r.run_ts === 2 * HOUR).data_warnings).toEqual([]);
    });

    it('is bounded: only the most recent runs are returned however many exist', async () => {
      const st = newState();
      for (let i = 0; i < 60; i++) addRun(st, i * HOUR, 'OK');
      const op = (await status(st, 100 * HOUR)).operational;
      expect(op.recent_runs).toHaveLength(10);
      expect(op.runs_total).toBe(60);
      expect(op.recent_runs[0].run_ts).toBe(59 * HOUR);
    });
  });

  describe('decision counts (predictive block)', () => {
    it('empty history: AVAILABLE with zeros and null accuracies, never a fabricated rate', async () => {
      const p = (await status(newState())).predictive;
      expect(p).toMatchObject({ available: true, state: 'AVAILABLE', n_resolved: 0, challenger_evaluable_n: 0, baseline_evaluable_n: 0,
        challenger_accuracy: null, v1_baseline_accuracy: null, sufficient_sample: false, verdict: null });
    });

    it('research_hypotheses missing: TABLE_MISSING naming migration 0008, run log unaffected', async () => {
      const st = newState();
      st.predictiveError = new Error('D1_ERROR: no such table: research_hypotheses');
      const r = await status(st);
      expect(r.predictive).toMatchObject({ available: false, state: 'TABLE_MISSING', n_resolved: 0 });
      expect(r.predictive.reason).toMatch(/migration 0008/);
      expect(r.operational.run_log_state).toBe('AVAILABLE');
    });

    it('a column missing from research_hypotheses is SCHEMA_DRIFT, a transient error is QUERY_FAILED (never "no decisions")', async () => {
      const a = newState(); a.predictiveError = new Error('D1_ERROR: no such column: out_of_sample_status');
      expect((await status(a)).predictive).toMatchObject({ state: 'SCHEMA_DRIFT', available: false });
      const b = newState(); b.predictiveError = new Error('D1 DB is overloaded');
      const p = (await status(b)).predictive;
      expect(p).toMatchObject({ state: 'QUERY_FAILED', available: false });
      expect(p.reason).toMatch(/NOT evidence that there are no resolved decisions/);
      expect(p.n_resolved).toBe(0);
    });

    it('malformed stored outcome JSON is counted and excluded, not guessed at and not fatal', async () => {
      const st = newState();
      addDecision(st, { agent_correct: true, v1_baseline_correct: false });
      addDecision(st, null, '{not json');
      const p = (await status(st)).predictive;
      expect(p).toMatchObject({ available: true, n_resolved: 2, n_malformed_outcomes: 1, challenger_evaluable_n: 1, baseline_evaluable_n: 1 });
    });

    it('unexpected failure: UNEXPECTED with no raw text', async () => {
      const st = newState(); st.predictiveError = new Error('weird SELECT internals');
      const p = (await status(st)).predictive;
      expect(p.state).toBe('UNEXPECTED');
      expect(JSON.stringify(p)).not.toMatch(/weird|SELECT internals/);
    });

    it('never states a verdict, even with a large sample where the challenger beats V1', async () => {
      const st = newState();
      for (let i = 0; i < 30; i++) addDecision(st, { agent_correct: true, v1_baseline_correct: i % 2 === 0 });
      const p = (await status(st)).predictive;
      expect(p).toMatchObject({ n_resolved: 30, sufficient_sample: true, challenger_accuracy: 1, v1_baseline_accuracy: 0.5, success_criterion_defined: false, verdict: null });
      expect(p.note).toMatch(/No pre-registered success criterion/);
    });

    it('an inconclusive outcome (agent_correct null) is resolved but not evaluable for either side', async () => {
      const st = newState();
      addDecision(st, { agent_correct: null, v1_baseline_correct: null });
      const p = (await status(st)).predictive;
      expect(p).toMatchObject({ n_resolved: 1, challenger_evaluable_n: 0, baseline_evaluable_n: 0, challenger_accuracy: null });
    });

    it('operational success (healthy runs) and predictive evidence are independent', async () => {
      const st = newState(); addRun(st, 1 * HOUR, 'OK');
      const r = await status(st, 2 * HOUR);
      expect(r.operational.last_run.status).toBe('OK');
      expect(r.predictive).toMatchObject({ n_resolved: 0, verdict: null, sufficient_sample: false });
    });
  });

  describe('read-only and bounded', () => {
    it('issues only SELECT statements and a fixed, small number of them regardless of data volume', async () => {
      const st = newState();
      for (let i = 0; i < 100; i++) { addRun(st, i * HOUR, 'OK'); addDecision(st, { agent_correct: true, v1_baseline_correct: false }); }
      await status(st, 200 * HOUR);
      expect(st.log).toHaveLength(5); // recent runs, count, last OK, last FAILED, one predictive aggregate
      for (const sql of st.log) expect(sql.trim()).toMatch(/^SELECT /i);
      const bounded = st.log.filter((sql) => /FROM experiment5_pipeline_runs/.test(sql));
      for (const sql of bounded) expect(sql).toMatch(/LIMIT \d+|COUNT\(\*\)/);
    });

    it('the decision counts are one aggregate row (no per-decision row transfer)', () => {
      expect(PREDICTIVE_SQL).toMatch(/^SELECT COUNT\(\*\) AS n_resolved/);
      expect(PREDICTIVE_SQL).not.toMatch(/SELECT evidence_summary_json FROM/);
      expect(PREDICTIVE_SQL).toMatch(/json_valid\(evidence_summary_json\)/);
    });

    it('the Worker selects an EXPLICIT column list (never *) that equals migration 0019 exactly, so a behind schema is detected', () => {
      const body = MIGRATION_0019.slice(MIGRATION_0019.indexOf('CREATE TABLE experiment5_pipeline_runs'));
      const declared = [...body.slice(0, body.indexOf(');')).matchAll(/^\s{2}([a-z_0-9]+) /gm)].map((m) => m[1]);
      expect([...declared].sort()).toEqual([...RUN_COLUMNS].sort());
      expect(WORKER_SRC).not.toMatch(/SELECT \* FROM experiment5_pipeline_runs/);
      // Only publicRun maps stored columns (r.<column>); the code after it uses already-mapped fields.
      const fn = WORKER_SRC.slice(WORKER_SRC.indexOf('const publicRun = (r) => {'), WORKER_SRC.indexOf('operational.run_log_available = true;'));
      for (const col of fn.matchAll(/\br\.([a-z_0-9]+)/g)) {
        if (col[1] === 'data_warnings') continue;
        if (col[1] === 'status' || col[1] === 'n') continue;
        expect(declared).toContain(col[1]);
      }
    });
  });
});

describe('GET /api/research-lab/experiment5-status (real route)', () => {
  const call = async (env, method = 'GET') => {
    const res = await worker.fetch(new Request('https://example.com/api/research-lab/experiment5-status', { method }), env);
    return { status: res.status, body: await res.json() };
  };

  it('non-GET is rejected and the database is never touched', async () => {
    const db = { prepare() { throw new Error('must not be touched'); } };
    for (const method of ['POST', 'PUT', 'DELETE']) {
      const r = await call({ DB: db }, method);
      expect(r.status).toBe(404);
    }
  });

  it('serves the status, including when both tables are missing (200 with explicit states, not a 500)', async () => {
    const db = { prepare() { throw new Error('D1_ERROR: no such table: nothing'); } };
    const r = await call({ DB: db });
    expect(r.status).toBe(200);
    expect(r.body.operational.run_log_state).toBe('TABLE_MISSING');
    expect(r.body.predictive.state).toBe('TABLE_MISSING');
  });

  it('an error that escapes the handler is a generic 500 with no internals', async () => {
    const spy = vi.spyOn(Date, 'now').mockImplementation(() => { throw new Error('boom SELECT secret'); });
    let r;
    try { r = await call({ DB: { prepare() { return { first: async () => null, all: async () => ({ results: [] }) }; } } }); } finally { spy.mockRestore(); }
    expect(r.status).toBe(500);
    expect(JSON.stringify(r.body)).not.toMatch(/boom|secret|SELECT/);
  });
});

describe('Experiment 5 status cards (frontend helper)', () => {
  let ui;
  beforeAll(() => {
    ui = evalInScope(
      `const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
       const fmtTs = (t) => 'T' + t;
       const badge = (text, cls) => '[' + cls + ':' + text + ']';
       const emptyState = (h, d) => '{EMPTY ' + h + ' | ' + d + '}';\n` +
      WORKER_SRC.slice(WORKER_SRC.indexOf('  var EXP5_RUN_LOG_LABELS = {'), WORKER_SRC.indexOf('  function exp5StatusHtml')).replace('var EXP5_RUN_LOG_LABELS', 'const EXP5_RUN_LOG_LABELS') + '\n' +
      extractFunctions('exp5Num', 'exp5RunRowHtml', 'exp5StatusHtml')
    );
  });
  const predictive = { available: true, state: 'AVAILABLE', n_resolved: 0, n_malformed_outcomes: 0, challenger_evaluable_n: 0, baseline_evaluable_n: 0,
    min_sample_for_conclusion: 20, sufficient_sample: false, success_criterion_defined: false, note: 'No pre-registered success criterion exists.' };
  const opUnavailable = (state, reason) => ({ run_log_available: false, run_log_state: state, reason });

  it.each([
    ['TABLE_MISSING', 'TELEMETRY NOT RECORDED -- MIGRATION 0019 NOT APPLIED'],
    ['SCHEMA_DRIFT', 'RUN LOG SCHEMA BEHIND THIS PAGE'],
    ['MALFORMED_DATA', 'RUN LOG HAS UNREADABLE DATA'],
    ['QUERY_FAILED', 'RUN LOG TEMPORARILY UNREADABLE'],
    ['UNEXPECTED', 'RUN LOG ERROR'],
  ])('%s is shown as execution health UNKNOWN with its own label, never as OK or FAILED', (state, label) => {
    const html = ui.exp5StatusHtml({ ok: true, operational: opUnavailable(state, 'why: ' + state), predictive });
    expect(html).toContain('[b-unknown:UNKNOWN]');
    expect(html).toContain(label);
    expect(html).toContain('why: ' + state);
    expect(html).not.toContain('[b-verified:OK]');
    expect(html).not.toContain('[b-blocked:FAILED]');
  });

  it('a failed last run is flagged with its error and failure count; verdict is always NO CONCLUSION', () => {
    const run = { run_id: 2, run_ts: 5, status: 'FAILED', error_text: 'OperationalError: x', pipeline_version: 'v2', constants: null };
    const html = ui.exp5StatusHtml({ ok: true, operational: { run_log_available: true, run_log_state: 'AVAILABLE', runs_total: 1, last_run: run, last_success: null, last_failure: run, consecutive_failures: 1, overdue: false, recent_runs: [run], runs_with_malformed_fields: 0 }, predictive });
    expect(html).toContain('[b-blocked:FAILED]');
    expect(html).toContain('OperationalError: x');
    expect(html).toContain('never');
    expect(html).toContain('[b-unknown:NO CONCLUSION]');
    expect(html).toContain('none defined');
  });

  it('shows skipped duplicates and flags unreadable run fields', () => {
    const run = { run_id: 2, run_ts: 5, status: 'OK', pipeline_version: 'v2', constants: null, decisions_replayed: 1, decisions_created: 0, decisions_skipped_duplicate: 3, decisions_evaluated: 1 };
    const html = ui.exp5StatusHtml({ ok: true, operational: { run_log_available: true, run_log_state: 'AVAILABLE', runs_total: 1, last_run: run, last_success: run, last_failure: null, consecutive_failures: 0, overdue: false, recent_runs: [run], runs_with_malformed_fields: 1 }, predictive });
    expect(html).toContain('decisions replayed / created / skipped as duplicates / evaluated');
    expect(html).toContain('1 / 0 / 3 / 1');
    expect(html).toContain('1 recent run record(s) have an unreadable JSON field');
  });

  it('rejected rows older than the agent window are shown separately from the in-window counts', () => {
    const run = { run_id: 2, run_ts: 5, status: 'OK', pipeline_version: 'v2', constants: null, archive_rows_observed: 4, observations_without_sources: 1,
      observations_rejected_malformed: 1, observations_rejected_outside_window: 7 };
    const html = ui.exp5StatusHtml({ ok: true, operational: { run_log_available: true, run_log_state: 'AVAILABLE', runs_total: 1, last_run: run, last_success: run, last_failure: null, consecutive_failures: 0, overdue: false, recent_runs: [run], runs_with_malformed_fields: 0 }, predictive });
    expect(html).toContain('Last run, agent window: observations used / without sources / rejected');
    expect(html).toContain('4 / 1 / 1');
    expect(html).toContain('Rejected rows older than the agent window (archive only)');
  });

  it('predictive counts unavailable are shown as UNAVAILABLE with the reason, not as zero resolved decisions', () => {
    const html = ui.exp5StatusHtml({ ok: true, operational: opUnavailable('TABLE_MISSING', 'x'), predictive: { ...predictive, available: false, state: 'QUERY_FAILED', reason: 'counts could not be read right now' } });
    expect(html).toContain('[b-unknown:UNAVAILABLE]');
    expect(html).toContain('counts could not be read right now');
    expect(html).not.toContain('Resolved decisions');
  });

  it('the two cards are separately titled: execution vs predictive evidence', () => {
    const html = ui.exp5StatusHtml({ ok: true, operational: opUnavailable('TABLE_MISSING', 'x'), predictive });
    expect(html).toContain('Pipeline execution (is it running?)');
    expect(html).toContain('Predictive evidence (does it predict better than V1?)');
    expect(html.indexOf('data-testid="exp5-operational"')).toBeLessThan(html.indexOf('data-testid="exp5-predictive"'));
  });

  it('shows unreadable-outcome rows as excluded when present', () => {
    const html = ui.exp5StatusHtml({ ok: true, operational: opUnavailable('TABLE_MISSING', 'x'), predictive: { ...predictive, n_resolved: 5, n_malformed_outcomes: 2 } });
    expect(html).toContain('unreadable outcome data (excluded)');
  });

  it('escapes error text (no HTML injection from persisted run rows)', () => {
    const run = { run_id: 1, run_ts: 5, status: 'FAILED', error_text: '<img src=x onerror=alert(1)>', pipeline_version: 'v2', constants: null };
    const html = ui.exp5StatusHtml({ ok: true, operational: { run_log_available: true, run_log_state: 'AVAILABLE', runs_total: 1, last_run: run, last_success: null, last_failure: run, consecutive_failures: 1, overdue: false, recent_runs: [run], runs_with_malformed_fields: 0 }, predictive });
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });

  it('failed status fetch renders an unavailable state', () => {
    expect(ui.exp5StatusHtml(null)).toContain('Status unavailable');
  });
});
