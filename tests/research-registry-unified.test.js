// Unified Research Lab registry (2026-10-11): D1 rows plus code-defined
// entries for experiments that run outside research_experiment_registry,
// with display labels, data freshness kept apart from registry edits, an
// ETA that is only given when it is estimable, and a suggested status.
import { describe, it, expect } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const NOW = Date.UTC(2026, 9, 11, 9, 0);
const DAY = 86400000;

function load(fetchImpl) {
  return evalInScope(
    extractFunctions('getUnifiedResearchRegistry', 'getResearchLabRegistry', 'computeSnapshotTableLiveFields', 'fetchResearchDataJson', 'computeLoggedTableLiveFields', 'computeExp023LiveFields',
      'computeRiskRegimeForwardLiveFields', 'computeExperiment5RegistryLiveFields', 'registryProgressCount', 'registryEta',
      'decorateRegistryEntry') + '\n' +
    extractConstants('RESEARCH_DATA_RAW_BASE', 'CODE_LIVE_METRIC_PROVIDERS', 'CODE_REGISTRY_ENTRIES', 'REGISTRY_DISPLAY_LABELS', 'DAY_MS'),
    {
      fetch: fetchImpl,
      LIVE_METRIC_PROVIDERS: { fake_table: async () => ({ current_sample_size: 3, current_measured_result: 'x', oos_result: 'NOT_AVAILABLE', confidence_evidence_maturity: 'INSUFFICIENT_SAMPLE', last_updated: NOW - DAY }) },
      getResearchLabExperiment5Overview: async () => ({ ok: true, activated: false, reason: 'migrations not applied' }),
    }
  );
}

const githubFiles = {
  'research-data/exp23-evaluation/exp23_evaluation/state.json': { last_report: 'reports/abc', last_run_at: '2026-10-11T12:10:00Z', last_status: 'EVALUATED' },
  'research-data/exp23-evaluation/exp23_evaluation/reports/abc/report.json': {
    status: 'INSUFFICIENT_SAMPLE', checkpoint: { evaluable_events: 0, up: 0, down: 0, met: false },
    labels: { labelled_hours: 0, burn_in_complete: false }, decision_hours: { coverage: { F1: { evaluable_hours: 3 }, F3: { evaluable_hours: 0 } } },
    blockers: ['funding stale'],
  },
  'research-data/risk-regime-forward/risk_regime_forward/index.json': { sources: {
    binance_oi_archive: { complete: 2, observations: 1152, first: '2026-10-08', last: '2026-10-09', latest_complete: '2026-10-09', stale: false },
  } },
};
const okFetch = async (url) => {
  const key = url.replace('https://raw.githubusercontent.com/quiquandon-oss/PulseWorkerV2/', '');
  return githubFiles[key] ? { ok: true, json: async () => githubFiles[key] } : { ok: false, status: 404 };
};

function db(d1Rows, logged = {}) {
  return { prepare(sql) {
    const q = { bind() { return q; },
      async all() { return { results: d1Rows }; },
      async first() { const t = sql.match(/FROM (\w+)/)[1]; return logged[t] || { n: 0, first_ts: null, last_ts: null }; } };
    return q;
  } };
}

const d1Row = (id, extra = {}) => ({ experiment_id: id, title: 't' + id, status: 'ACCUMULATING', required_sample: 10,
  start_date: '2026-10-01', data_source_table: 'fake_table', updated_ts: NOW - 30 * DAY, ...extra });

describe('getResearchLabRegistry -- unified list', () => {
  it('keeps D1 rows, adds code entries, and a D1 row wins over a code entry with the same id', async () => {
    const { getUnifiedResearchRegistry: getResearchLabRegistry } = load(okFetch);
    const out = await getResearchLabRegistry({ DB: db([d1Row('EXP-005'), d1Row('EXP-003', { title: 'from d1' })]) }, NOW);
    const ids = out.experiments.map(e => e.experiment_id);
    expect(ids).toEqual(['EXP-001', 'EXP-002', 'EXP-003', 'EXP-005', 'EXP-011', 'EXP-012', 'EXP-023', 'EXP-024', 'EXP-025', 'EXP-026', 'EXP-027', 'EXP-028']);
    expect(out.experiments.find(e => e.experiment_id === 'EXP-003')).toMatchObject({ title: 'from d1', registry_source: 'd1' });
    expect(out.experiments.find(e => e.experiment_id === 'EXP-001').registry_source).toBe('code');
  });

  it('resolves the EXP-005 / Experiment 5 naming collision with distinct ids and labels', async () => {
    const { getUnifiedResearchRegistry: getResearchLabRegistry } = load(okFetch);
    const out = await getResearchLabRegistry({ DB: db([d1Row('EXP-005')]) }, NOW);
    const e5 = out.experiments.find(e => e.experiment_id === 'EXP-005');
    const e11 = out.experiments.find(e => e.experiment_id === 'EXP-011');
    expect(e5.display_label).toMatch(/V1 Source \/ Coefficient Research \(not Experiment 5\)/);
    expect(e11.display_label).toMatch(/Experiment 5 — Agentic Market Evidence/);
    expect(e11.aliases).toEqual(['Experiment 5']);
    expect(e11.current_measured_result).toEqual({ activated: false, reason: 'migrations not applied' });
    expect(e11.status).toBe('BUILT_NOT_ACTIVATED');
  });

  it('separates data freshness from registry edits; rows without a live source have no data_last_updated', async () => {
    const { getUnifiedResearchRegistry: getResearchLabRegistry } = load(okFetch);
    const out = await getResearchLabRegistry({ DB: db([d1Row('EXP-005'), d1Row('EXP-008', { data_source_table: null, status: 'PROPOSED' })]) }, NOW);
    const e5 = out.experiments.find(e => e.experiment_id === 'EXP-005');
    expect(e5.data_last_updated).toBe(NOW - DAY);
    expect(e5.registry_updated_ts).toBe(NOW - 30 * DAY);
    const e8 = out.experiments.find(e => e.experiment_id === 'EXP-008');
    expect(e8.data_last_updated).toBeNull();
    expect(e8.eta.estimable).toBe(false);
  });

  it('reads EXP-023 and EXP-024 from the public data branches', async () => {
    const { getUnifiedResearchRegistry: getResearchLabRegistry } = load(okFetch);
    const out = await getResearchLabRegistry({ DB: db([]) }, NOW);
    const e23 = out.experiments.find(e => e.experiment_id === 'EXP-023');
    expect(e23.current_sample_size).toBe(0);
    expect(e23.current_measured_result).toMatchObject({ evaluation_status: 'INSUFFICIENT_SAMPLE', evaluable_hours: { F1: 3, F3: 0 }, blockers: ['funding stale'] });
    expect(e23.oos_result).toBe('INSUFFICIENT_SAMPLE');
    expect(e23.data_last_updated).toBe(Date.parse('2026-10-11T12:10:00Z'));
    expect(e23.eta).toMatchObject({ estimable: false, reason: 'No progress yet, so no observed rate.' });
    const e24 = out.experiments.find(e => e.experiment_id === 'EXP-024');
    expect(e24.current_sample_size).toBe(2);
    expect(e24.current_measured_result.sources.binance_oi_archive.observations).toBe(1152);
  });

  it('a failing GitHub fetch degrades only that entry to NOT_AVAILABLE', async () => {
    const { getUnifiedResearchRegistry: getResearchLabRegistry } = load(async () => ({ ok: false, status: 503 }));
    const out = await getResearchLabRegistry({ DB: db([d1Row('EXP-005')]) }, NOW);
    const e23 = out.experiments.find(e => e.experiment_id === 'EXP-023');
    expect(e23.current_sample_size).toBe('NOT_AVAILABLE');
    expect(e23.live_error).toMatch(/HTTP 503/);
    expect(out.experiments.find(e => e.experiment_id === 'EXP-005').current_sample_size).toBe(3);
  });

  it('logged-table experiments report real row counts and use the first row as observed start', async () => {
    const { getUnifiedResearchRegistry: getResearchLabRegistry } = load(okFetch);
    const out = await getResearchLabRegistry({ DB: db([], { selection_decisions_momentum: { n: 14, first_ts: NOW - 15 * DAY, last_ts: NOW - DAY } }) }, NOW);
    const e3 = out.experiments.find(e => e.experiment_id === 'EXP-003');
    expect(e3.current_sample_size).toBe(14);
    expect(e3.eta).toMatchObject({ estimable: true, reached: false, progress: 14, required: 50 });
    expect(e3.eta.eta_date).toBe(new Date(NOW + (36 / (14 / 15)) * DAY).toISOString().slice(0, 10));
  });
});

describe('snapshot-table providers (tables created by the Pulse Worker)', () => {
  it('a not-yet-created table is NOT_STARTED, an existing one reports its snapshot count', async () => {
    const { getUnifiedResearchRegistry } = load(okFetch);
    const dbWith = (hl) => ({ prepare(sql) { const q = { bind() { return q; }, async all() { return { results: [] }; },
      async first() {
        if (/FROM hl_oi_snapshots/.test(sql)) { if (!hl) throw new Error('D1_ERROR: no such table: hl_oi_snapshots'); return hl; }
        if (/FROM research_source_snapshots/.test(sql)) throw new Error('D1_ERROR: no such table: research_source_snapshots');
        return { n: 0, first_ts: null, last_ts: null }; } }; return q; } });
    let out = await getUnifiedResearchRegistry({ DB: dbWith(null) }, NOW);
    expect(out.experiments.find(e => e.experiment_id === 'EXP-025').current_sample_size).toBe('NOT_STARTED');
    expect(out.experiments.find(e => e.experiment_id === 'EXP-028').current_sample_size).toBe('NOT_STARTED');
    out = await getUnifiedResearchRegistry({ DB: dbWith({ n: 5, first_ts: NOW - 5 * 3600000, last_ts: NOW }) }, NOW);
    const e25 = out.experiments.find(e => e.experiment_id === 'EXP-025');
    expect(e25).toMatchObject({ current_sample_size: 5, data_last_updated: NOW, status: 'ACCUMULATING' });
  });
});

describe('registryEta / suggested_status', () => {
  const { registryEta, registryProgressCount, decorateRegistryEntry } = load(okFetch);
  it('EXP-004-style per-horizon samples use the minimum horizon', () => {
    expect(registryProgressCount({ by_horizon: [{ resolved: 40 }, { resolved: 31 }] })).toBe(31);
    expect(registryProgressCount('NOT_STARTED')).toBeNull();
  });
  it('flags READY_FOR_ANALYSIS only when ACCUMULATING and the requirement is met', () => {
    const met = decorateRegistryEntry({ experiment_id: 'EXP-004', title: 't', status: 'ACCUMULATING', required_sample: 30 },
      { current_sample_size: { by_horizon: [{ resolved: 30 }, { resolved: 33 }] }, last_updated: 1 }, true, NOW);
    expect(met.suggested_status).toBe('READY_FOR_ANALYSIS');
    expect(met.status).toBe('ACCUMULATING');
    const notYet = decorateRegistryEntry({ experiment_id: 'EXP-004', title: 't', status: 'ACCUMULATING', required_sample: 30 },
      { current_sample_size: { by_horizon: [{ resolved: 29 }] } }, true, NOW);
    expect(notYet.suggested_status).toBeNull();
  });
  it('never estimates without a requirement, a start, or enough history', () => {
    expect(registryEta({ required_sample: null }, 5, NOW).estimable).toBe(false);
    expect(registryEta({ required_sample: 10 }, 5, NOW).reason).toBe('No start date recorded.');
    expect(registryEta({ required_sample: 10, start_date: '2026-10-10' }, 5, NOW).reason).toBe('Fewer than 3 days of history.');
  });
});
