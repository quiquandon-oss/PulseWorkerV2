// Decision state (decision-state-v1): one display model for every frontend. Rows below are test data only.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

function scope(overrides = '') {
  return evalInScope(
    extractConstants('DECISION_MODEL_VERSION', 'DECISION_STATE_LABELS', 'OWNER_DECISION_RECORDS',
      'PROPOSED_DECISION_RECORDS', 'PRODUCTION_USE_RECORDS') + '\n' + overrides + '\n' +
    extractFunctions('decisionStateFor', 'attachDecisionState'), {});
}

const entry = (o = {}) => ({ experiment_id: 'EXP-900', status: 'ACCUMULATING', progress_count: 5, required_sample: 30, ...o });

describe('decisionStateFor', () => {
  it('collecting below the required sample, ready at or above it (eligibility, not success)', () => {
    const { decisionStateFor } = scope();
    expect(decisionStateFor(entry()).state).toBe('COLLECTING');
    const ready = decisionStateFor(entry({ progress_count: 30 }));
    expect(ready.state).toBe('READY_FOR_EVALUATION');
    expect(ready.basis).toMatch(/not a result/);
    expect(ready.owner_decided).toBe(false);
  });

  it('no required sample stays COLLECTING and says so', () => {
    const d = scope().decisionStateFor(entry({ required_sample: null, progress_count: 900 }));
    expect(d.state).toBe('COLLECTING');
    expect(d.basis).toMatch(/no required sample/);
  });

  it('a BLOCKED evaluation wins over a met sample threshold', () => {
    const d = scope().decisionStateFor(entry({ progress_count: 55,
      standalone_evaluations: [{ evaluation_status: 'BLOCKED', scientific_outcome: null, reason: 'no pre-registered test' }] }));
    expect(d.state).toBe('BLOCKED');
    expect(d.basis).toBe('no pre-registered test');
  });

  it('an evaluated outcome without an owner decision is DECISION PENDING, never decided', () => {
    const d = scope().decisionStateFor(entry({ standalone_evaluations: [{ evaluation_status: 'COMPLETE', scientific_outcome: 'NO_EFFECT' }] }));
    expect(d.state).toBe('EVALUATED_DECISION_PENDING');
    expect(d.owner_decided).toBe(false);
  });

  it('registry PROPOSED / STAGING_ONLY / ON_DEMAND_AUDIT map to their own states', () => {
    const { decisionStateFor } = scope();
    expect(decisionStateFor(entry({ status: 'PROPOSED' })).state).toBe('PROPOSED');
    expect(decisionStateFor(entry({ status: 'STAGING_ONLY' })).state).toBe('STAGING_ONLY');
    expect(decisionStateFor(entry({ status: 'ON_DEMAND_AUDIT' })).state).toBe('ON_DEMAND');
  });

  it('only an owner record (or registry REJECTED/RETIRED) produces a decided state', () => {
    const owned = scope(`OWNER_DECISION_RECORDS['EXP-900'] = { decision: 'IMPLEMENTED_RESEARCH_ONLY', decided_ts: 1, decided_by: 'owner', rationale: 'r', evidence_ref: 'x' };`);
    const d = owned.decisionStateFor(entry({ standalone_evaluations: [{ evaluation_status: 'BLOCKED' }] }));
    expect(d.state).toBe('IMPLEMENTED_RESEARCH_ONLY');
    expect(d.owner_decided).toBe(true);
    expect(scope().decisionStateFor(entry({ status: 'REJECTED' })).state).toBe('REJECTED');
  });

  it('a proposal is attached but never changes the state', () => {
    const { decisionStateFor } = scope();
    const d = decisionStateFor({ experiment_id: 'EXP-003', status: 'ACCUMULATING', progress_count: 52, required_sample: 50 });
    expect(d.state).toBe('READY_FOR_EVALUATION');
    expect(d.proposal.proposal).toBe('REJECT');
    expect(d.owner_decided).toBe(false);
  });

  it('known production use is shown without turning research into a production decision', () => {
    const d = scope().decisionStateFor({ experiment_id: 'EXP-027', status: 'PROPOSED' });
    expect(d.state).toBe('PROPOSED');
    expect(d.production_use.where).toMatch(/riskregime/);
    expect(d.production_use.decision_recorded).toBe(false);
    expect(d.state).not.toMatch(/IMPLEMENTED/);
  });

  it('every state has a label and no owner decision is recorded in this change', () => {
    const { DECISION_STATE_LABELS, OWNER_DECISION_RECORDS } = scope();
    for (const s of ['PROPOSED', 'COLLECTING', 'READY_FOR_EVALUATION', 'BLOCKED', 'EVALUATED_DECISION_PENDING', 'REJECTED',
      'IMPLEMENTED_RESEARCH_ONLY', 'IMPLEMENTED_PRODUCTION', 'STAGING_ONLY', 'ON_DEMAND']) expect(DECISION_STATE_LABELS[s]).toBeTruthy();
    expect(Object.keys(OWNER_DECISION_RECORDS)).toEqual([]);
  });

  it('attachDecisionState decorates every entry in place', () => {
    const list = [entry(), entry({ experiment_id: 'EXP-901', status: 'PROPOSED' })];
    scope().attachDecisionState(list);
    expect(list.map(e => e.decision.state)).toEqual(['COLLECTING', 'PROPOSED']);
    expect(list.every(e => e.decision.model_version === 'decision-state-v1')).toBe(true);
  });

  it('the registry route attaches the decision model after the evaluation dimensions', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const body = extractFunctions('getUnifiedResearchRegistry');
    expect(body.indexOf('attachEvaluationDimensions(')).toBeLessThan(body.indexOf('attachDecisionState('));
    expect(src).toMatch(/decision_model: \{ version: DECISION_MODEL_VERSION/);
  });
});
