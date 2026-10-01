import { describe, it, expect, beforeAll, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractConstants, extractFunctions, evalInScope } from './helpers/extract.js';

// Lifecycle invariants for the human-controlled Stage 7 workflow, audited end to end:
//   - registering a response never recalculates (and never sets any recalculation_* column),
//   - a retried registration is idempotent when identical and can never overwrite different content,
//   - two concurrent recalculation requests cannot both stamp the request,
//   - per-event Stage 7 sentiment cannot touch V1's composite or any prediction path.
const WORKER_SRC = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
const TOKEN = 'lifecycle-audit-token';

describe('Stage 7 lifecycle invariants', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(
      extractConstants('STAGE7_SENTIMENT_ASSESSMENTS') + '\n' +
      extractFunctions(
        'parseStage7JsonField', 'stage7ConstantTimeEqual', 'registerStage7ResearchResponse', 'validateStage7Sources',
        'stage7IsHttpUrl', 'stage7ParseSourcePublicationTs', 'triggerStage7Recalculation'
      )
    );
  });

  // Scripted fake D1: statements are matched by SQL substring, every statement is recorded.
  function db(script) {
    const calls = [];
    const make = (sql, args) => {
      const answer = (kind) => {
        calls.push({ sql, args, kind });
        for (const [needle, fn] of script) if (sql.includes(needle)) return fn(args, kind);
        throw new Error(`unscripted SQL: ${sql}`);
      };
      return { first: async () => answer('first'), all: async () => answer('all'), run: async () => answer('run') };
    };
    return { calls, prepare: (sql) => ({ ...make(sql, []), bind: (...args) => make(sql, args) }) };
  }
  const env = (d) => ({ DB: d, STAGE7_ADMIN_TOKEN: TOKEN });
  const findings = { summary: 's', sentiment_assessment: 'POSITIVE' };
  const RAW = 'verbatim AI answer {"summary":"s"}';
  const writes = (d) => d.calls.filter((c) => /^\s*(INSERT|UPDATE|DELETE)/i.test(c.sql));

  describe('registering a response never recalculates', () => {
    it('a VALIDATED, human-confirmed registration touches no recalculation_* column and no sentiment table', async () => {
      const d = db([
        ['FROM stage7_research_requests WHERE request_id', () => ({ request_id: 'r1', status: 'RESEARCH_REQUEST_PUBLISHED', historical_cutoff_ts: 2e12 })],
        ['FROM stage7_research_responses WHERE request_id', () => null],
        ['INSERT INTO stage7_research_responses', () => ({ success: true })],
        ['UPDATE stage7_research_requests SET status', () => ({ success: true })],
      ]);
      const result = await scope.registerStage7ResearchResponse(env(d), {
        requestId: 'r1', findings, rawResponseText: RAW, validated: true, humanConfirmed: true, providedToken: TOKEN,
        sources: [{ url: 'https://example.com/a', publisher: 'p', publication_date: '2020-01-01', claim: 'c' }],
      });
      expect(result.ok).toBe(true);
      const all = writes(d).map((c) => c.sql).join('\n');
      expect(all).not.toMatch(/recalculation_/);
      expect(all).not.toMatch(/stage7_event_sentiment/);
      expect(writes(d).map((c) => c.sql.match(/(INSERT INTO|UPDATE)\s+(\w+)/)[2])).toEqual(['stage7_research_responses', 'stage7_research_requests']);
    });

    it('registering WITHOUT validation also leaves recalculation untouched and the response PENDING', async () => {
      const d = db([
        ['FROM stage7_research_requests WHERE request_id', () => ({ request_id: 'r1', status: 'PENDING_RESEARCH', historical_cutoff_ts: 2e12 })],
        ['FROM stage7_research_responses WHERE request_id', () => null],
        ['INSERT INTO stage7_research_responses', () => ({ success: true })],
        ['UPDATE stage7_research_requests SET status', () => ({ success: true })],
      ]);
      const result = await scope.registerStage7ResearchResponse(env(d), {
        requestId: 'r1', findings, rawResponseText: RAW, validated: false, providedToken: TOKEN,
      });
      expect(result.validation_status).toBe('PENDING');
      expect(writes(d).map((c) => c.sql).join('\n')).not.toMatch(/recalculation_/);
    });
  });

  describe('retried registration', () => {
    const existing = (raw) => [
      ['FROM stage7_research_requests WHERE request_id', () => ({ request_id: 'r1', status: 'RESEARCH_COMPLETED', historical_cutoff_ts: 2e12 })],
      ['FROM stage7_research_responses WHERE request_id', () => ({ response_id: 'stage7-resp-r1', raw_response_text: raw, validation_status: 'VALIDATED' })],
    ];

    it('the SAME submission again is idempotent: success, already_registered, and nothing is written', async () => {
      const d = db(existing(RAW));
      const result = await scope.registerStage7ResearchResponse(env(d), { requestId: 'r1', findings, rawResponseText: RAW, providedToken: TOKEN });
      expect(result).toMatchObject({ ok: true, status: 200, already_registered: true, response_id: 'stage7-resp-r1', validation_status: 'VALIDATED' });
      expect(writes(d)).toEqual([]);
    });

    it('DIFFERENT content for an already-registered request is refused (409) and nothing is written', async () => {
      const d = db(existing(RAW));
      const result = await scope.registerStage7ResearchResponse(env(d), { requestId: 'r1', findings, rawResponseText: RAW + ' edited', providedToken: TOKEN });
      expect(result).toMatchObject({ ok: false, status: 409 });
      expect(result.error).toMatch(/different content/);
      expect(writes(d)).toEqual([]);
    });

    it('a concurrent loser (unique-index violation on insert) gets 409, not a database error', async () => {
      const d = db([
        ['FROM stage7_research_requests WHERE request_id', () => ({ request_id: 'r1', status: 'PENDING_RESEARCH', historical_cutoff_ts: 2e12 })],
        ['FROM stage7_research_responses WHERE request_id', () => null],
        ['INSERT INTO stage7_research_responses', () => { throw new Error('UNIQUE constraint failed: stage7_research_responses.request_id'); }],
      ]);
      const result = await scope.registerStage7ResearchResponse(env(d), { requestId: 'r1', findings, rawResponseText: RAW, providedToken: TOKEN });
      expect(result).toMatchObject({ ok: false, status: 409 });
      expect(JSON.stringify(result)).not.toMatch(/UNIQUE constraint/);
      expect(writes(d).map((c) => c.sql.split(/\s/)[0])).toEqual(['INSERT']); // the failed insert only; no status UPDATE followed
    });

    it('a non-uniqueness database error is NOT swallowed as a conflict', async () => {
      const d = db([
        ['FROM stage7_research_requests WHERE request_id', () => ({ request_id: 'r1', status: 'PENDING_RESEARCH', historical_cutoff_ts: 2e12 })],
        ['FROM stage7_research_responses WHERE request_id', () => null],
        ['INSERT INTO stage7_research_responses', () => { throw new Error('D1_ERROR: disk I/O error'); }],
      ]);
      await expect(scope.registerStage7ResearchResponse(env(d), { requestId: 'r1', findings, rawResponseText: RAW, providedToken: TOKEN }))
        .rejects.toThrow(/disk I\/O/);
    });
  });

  describe('recalculation request is claimed by exactly one caller', () => {
    const base = (updateResult) => [
      ['FROM stage7_research_requests WHERE request_id', () => ({ request_id: 'r1', status: 'RESEARCH_COMPLETED', recalculation_requested_ts: null, recalculation_status: null })],
      ['FROM stage7_research_responses WHERE request_id', () => ({ response_id: 'stage7-resp-r1', validation_status: 'VALIDATED' })],
      ['UPDATE stage7_research_requests', () => updateResult],
    ];

    it('the UPDATE repeats the eligibility condition, so a second concurrent click matches no row', async () => {
      const d = db(base({ success: true, meta: { changes: 1 } }));
      await scope.triggerStage7Recalculation(env(d), { requestId: 'r1', providedToken: TOKEN });
      const update = d.calls.find((c) => c.sql.includes('UPDATE stage7_research_requests'));
      expect(update.sql).toMatch(/recalculation_status = 'FAILED' OR \(recalculation_status IS NULL AND recalculation_requested_ts IS NULL\)/);
    });

    it('winner: REQUESTED; loser (0 rows changed): ok + already_requested, never a second stamp', async () => {
      const winner = await scope.triggerStage7Recalculation(env(db(base({ success: true, meta: { changes: 1 } }))), { requestId: 'r1', providedToken: TOKEN });
      expect(winner).toMatchObject({ ok: true, recalculation_status: 'REQUESTED' });
      expect(winner.already_requested).toBeUndefined();
      const loser = await scope.triggerStage7Recalculation(env(db(base({ success: true, meta: { changes: 0 } }))), { requestId: 'r1', providedToken: TOKEN });
      expect(loser).toMatchObject({ ok: true, already_requested: true, recalculation_status: 'REQUESTED' });
      expect(loser.recalculation_requested_ts).toBeUndefined();
    });

    it('the Worker never performs a recalculation or writes stage7_event_sentiment anywhere', () => {
      const writesToSentiment = WORKER_SRC.match(/(INSERT INTO|UPDATE|DELETE FROM)\s+stage7_event_sentiment/g);
      expect(writesToSentiment).toBeNull();
    });
  });

  describe('per-event Stage 7 sentiment cannot influence V1 or predictions', () => {
    it('the Worker only ever WRITES to Stage 7 tables from Stage 7 functions (no other table is written by Stage 7 code)', () => {
      const start = WORKER_SRC.indexOf('async function registerStage7ResearchResponse');
      const end = WORKER_SRC.indexOf('// Pure. Maps a stage7_research_requests row');
      // SQL only: drop comment lines so prose like "UPDATE changes ..." is not mistaken for a statement.
      const region = WORKER_SRC.slice(start, end).split('\n').filter((l) => !l.trim().startsWith('//')).join('\n');
      const targets = new Set([...region.matchAll(/(?:INSERT INTO|UPDATE|DELETE FROM)\s+(\w+)/g)].map((m) => m[1]));
      expect([...targets].sort()).toEqual(['stage7_research_candidates', 'stage7_research_requests', 'stage7_research_responses']);
    });

    it('no prediction, selection, composite or learning function references a Stage 7 table or helper', () => {
      const names = [
        'runPrediction', 'runLinkPrediction', 'runEthPrediction', 'runChallengerPrediction', 'selectBestVariant',
        'decideSelection', 'computeSentiment',
      ];
      let checked = 0;
      for (const name of names) {
        const m = WORKER_SRC.match(new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`));
        if (!m) continue;
        const body = extractFunctions(name);
        expect(body, `${name} must not mention Stage 7`).not.toMatch(/stage7/i);
        checked++;
      }
      expect(checked).toBeGreaterThanOrEqual(3);
    });

    it('stage7_event_sentiment is read only by the Stage 7 overview (and written only by the staging pipeline)', () => {
      const reads = [...WORKER_SRC.matchAll(/FROM stage7_event_sentiment|JOIN stage7_event_sentiment/g)];
      expect(reads.length).toBeGreaterThan(0);
      for (const m of reads) {
        const before = WORKER_SRC.slice(0, m.index);
        const owner = [...before.matchAll(/(?:async\s+)?function\s+(\w+)\s*\(/g)].pop()[1];
        expect(owner).toBe('getResearchLabStage7Overview');
      }
    });
  });
});

describe('source validation is presented as a format check, never as verification of truth', () => {
  let ui;
  beforeAll(() => {
    ui = evalInScope(
      `const esc = (s) => String(s);
       const badge = (text, cls) => '[' + cls + ':' + text + ']';\n` +
      extractFunctions('stage7SourceValidationHtml')
    );
  });
  const results = [
    { index: 0, status: 'valid', reason: null },
    { index: 1, status: 'questionable', reason: 'same claim text' },
    { index: 2, status: 'excluded', reason: 'published after the historical cutoff' },
  ];
  const sources = [{ url: 'https://a.example/x' }, { url: 'https://b.example/y' }, { url: 'https://c.example/z' }];

  it('a "valid" API status is labelled FORMAT OK (never VALID/VERIFIED) and is not styled as verified', () => {
    const html = ui.stage7SourceValidationHtml(results, sources);
    expect(html).toContain('[b-plausible:FORMAT OK]');
    expect(html).not.toMatch(/\[b-verified:/);
    expect(html).not.toMatch(/VERIFIED|>VALID</);
  });

  it('the table always carries the disclaimer: URL not opened, claims not compared with sources', () => {
    const html = ui.stage7SourceValidationHtml(results, sources);
    expect(html).toMatch(/Format check only: URL syntax, publication date, historical cutoff and duplicate URLs/);
    expect(html).toMatch(/URL was not opened/);
    expect(html).toMatch(/does not mean the source is real/);
  });

  it('a no-citations finding is stated as such, not as a validated source set', () => {
    expect(ui.stage7SourceValidationHtml([], [])).toMatch(/No sources were submitted/);
  });

  it('a registered VALIDATED response is explained as human acceptance after a format-only check', () => {
    expect(WORKER_SRC).toMatch(/VALIDATED means a person accepted this response after a format-only source check\. The sources were not independently verified\./);
  });

  it('the server-side validator documents that it never fetches a URL or verifies a claim', () => {
    const start = WORKER_SRC.indexOf('function validateStage7Sources');
    const header = WORKER_SRC.slice(start - 1500, start);
    expect(header).toMatch(/NO claim that a URL's content or the truth of any\s*\/\/ claim was independently fetched\/verified/);
  });
});
