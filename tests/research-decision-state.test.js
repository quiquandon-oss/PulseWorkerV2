// Decision state (decision-state-v1): one display model for every frontend. Rows below are test data only.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

function scope(overrides = '') {
  return evalInScope(
    extractConstants('DECISION_MODEL_VERSION', 'DECISION_STATE_LABELS', 'OWNER_DECISION_RECORDS', 'PORTFOLIO_REVIEW', 'rec',
      'PROPOSED_DECISION_RECORDS', 'PRODUCTION_USE_RECORDS', 'COLLECTION_EVIDENCE_RECORDS', 'REGISTRY_CONFLICTS') + '\n' + overrides + '\n' +
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

  it('a recommendation is attached but never changes the state', () => {
    const { decisionStateFor } = scope();
    const d = decisionStateFor({ experiment_id: 'EXP-003', status: 'ACCUMULATING', progress_count: 52, required_sample: 50 });
    expect(d.state).toBe('READY_FOR_EVALUATION');
    expect(d.proposal.proposal).toBe('CONTINUE');
    expect(d.proposal.basis).toMatch(/withdrawn/);
    expect(d.owner_decided).toBe(false);
  });

  it('every registered experiment has a recommendation from the fixed action set', () => {
    const { PROPOSED_DECISION_RECORDS } = scope();
    const ids = ['EXP-001','EXP-002','EXP-003','EXP-004','EXP-005','EXP-006','EXP-007','EXP-008','EXP-009','EXP-010','EXP-011','EXP-012','EXP-023','EXP-024','EXP-025','EXP-026','EXP-027','EXP-028'];
    expect(Object.keys(PROPOSED_DECISION_RECORDS).sort()).toEqual(ids);
    for (const r of Object.values(PROPOSED_DECISION_RECORDS)) {
      expect(['CONTINUE', 'EVALUATE_NOW', 'PAUSE', 'CLOSE_OR_REJECT_PROPOSED', 'DEFER', 'BLOCKED']).toContain(r.proposal);
      expect(r.basis && r.next_checkpoint && r.evidence_ref).toBeTruthy();
    }
  });

  it('a frozen prospective plan (latest NOT_RUN) reads COLLECTING, not ready or blocked', () => {
    const d = scope().decisionStateFor(entry({ progress_count: 55, standalone_evaluations: [
      { evaluation_status: 'BLOCKED', reason: 'old' }, { evaluation_status: 'NOT_RUN', reason: 'plan frozen; checkpoint 100' }] }));
    expect(d.state).toBe('COLLECTING');
    expect(d.basis).toMatch(/plan frozen/);
  });

  it('a stale PROPOSED registry row with verified collection reads COLLECTING and lists the contradiction', () => {
    const d = scope().decisionStateFor({ experiment_id: 'EXP-008', status: 'PROPOSED' });
    expect(d.state).toBe('COLLECTING');
    expect(d.collection_active).toBe(true);
    expect(d.conflicts.join(' ')).toMatch(/PROPOSED/);
  });

  it('production use carries its own owner decision and never becomes the experiment\'s decided state', () => {
    const d = scope().decisionStateFor({ experiment_id: 'EXP-027', status: 'PROPOSED' });
    expect(d.state).toBe('PROPOSED');
    expect(d.production_use.where).toMatch(/riskregime/);
    expect(d.production_use.decision_recorded).toBe(true);
    expect(d.production_use.decision_ref).toMatch(/COMPOSITE_WEIGHTS_CHANGELOG 2026-10-12-riskregime/);
    expect(d.owner_decided).toBe(false);
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
