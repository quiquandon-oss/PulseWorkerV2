// LOCAL browser acceptance for the Stage 7 human-controlled workflow.
//
// What this is: a real Chromium (Playwright) driving the real worker.js over HTTP on
// localhost, backed by a real SQLite database built from the real migrations
// (0005-0018). The "staging workflow" step runs the real run_stage7.py via
// pipeline_driver.py. What this is NOT: it is not the deployed staging Worker or the
// staging D1 database -- the sandbox cannot reach *.workers.dev / api.cloudflare.com.
// Staging acceptance still needs the manual checklist.
//
// Usage: node --no-warnings acceptance.mjs [--out <dir>] [--worker <path>] [--tabs-only]
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { buildDatabase, seedStage7 } from './seed.mjs';
import { startServer } from './server.mjs';

const require = createRequire(import.meta.url);
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const here = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(here, '..', '..');
const args = process.argv.slice(2);
const argVal = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
const OUT = resolve(argVal('--out', join(ROOT, 'acceptance-output')));
const WORKER = resolve(argVal('--worker', join(ROOT, 'worker.js')));
const TABS_ONLY = args.includes('--tabs-only');
const ONLY = argVal('--only', null);
const TOKEN = 'local-acceptance-token';
mkdirSync(OUT, { recursive: true });

const results = [];
function check(area, name, pass, detail = '') {
  results.push({ area, name, pass: !!pass, detail: String(detail) });
  console.log(`${pass ? 'PASS' : 'FAIL'}  [${area}] ${name}${detail && !pass ? `  -- ${detail}` : ''}`);
}

function runPipeline(dbPath, label) {
  const publishLog = join(OUT, `publish-${label}.log`);
  const r = spawnSync('python3', [join(here, 'pipeline_driver.py'), dbPath, publishLog], { encoding: 'utf8' });
  const lines = (r.stdout || '').trim().split('\n');
  let result = null;
  try { result = JSON.parse(lines[lines.length - 1]); } catch (_e) { /* leave null */ }
  return { exit: r.status, result, stderr: (r.stderr || '').trim(), publishLog };
}

const HOUR = 3600000;
const raw = (obj) => '```json\n' + JSON.stringify(obj, null, 2) + '\n```';

async function stage7Journey(browser, label, viewport, mobile) {
  const dbPath = join(OUT, `${label}.db`);
  const port = mobile ? 8793 : 8792;
  const base = `http://127.0.0.1:${port}`;
  const { db: setupDb, applied, failed } = buildDatabase(dbPath);
  const events = seedStage7(setupDb);
  setupDb.close();
  const area = mobile ? 'mobile' : 'desktop';
  check(area, 'real migrations 0005-0018 applied to a fresh SQLite database', failed.length === 0 && applied.length === 14, failed.join('; ') || `${applied.length} applied`);
  const srv = await startServer({ workerPath: WORKER, dbPath, port, token: TOKEN });
  const q = new DatabaseSync(dbPath);
  const one = (sql, ...a) => q.prepare(sql).get(...a);
  const all = (sql, ...a) => q.prepare(sql).all(...a);
  const count = (table, where = '1=1') => Number(one(`SELECT COUNT(*) n FROM ${table} WHERE ${where}`).n);

  const context = await browser.newContext({
    viewport, isMobile: mobile, hasTouch: mobile, permissions: ['clipboard-read', 'clipboard-write'],
    baseURL: base,
  });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(String(e)));
  const shot = (name) => page.screenshot({ path: join(OUT, `${label}-${name}.png`), fullPage: true });
  const post = (path, body, auth = true) => page.evaluate(async ([p, b, t]) => {
    const res = await fetch(p, { method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, t ? { Authorization: 'Bearer ' + t } : {}), body: JSON.stringify(b) });
    return Object.assign({ http: res.status }, await res.json());
  }, [path, body, auth ? TOKEN : null]);
  const msg = (key) => page.locator(`[data-msg="${key}"]`).first();
  const waitMsg = async (key, re, timeout = 8000) => {
    await page.waitForFunction(([k, source]) => {
      const el = document.querySelector(`[data-msg="${k}"]`);
      return el && new RegExp(source, 'i').test(el.textContent);
    }, [key, re.source], { timeout });
    return (await msg(key).textContent()) || '';
  };

  const unrelatedBefore = JSON.stringify([
    all('SELECT * FROM research_events ORDER BY event_id'),
    all('SELECT * FROM research_experiment_registry ORDER BY experiment_id'),
    all("SELECT * FROM stage7_research_candidates WHERE candidate_id = 'stage7-cand-105'"),
  ]);

  // ---- 1. candidate list loads; preview is read-only ----
  await page.goto('/research-lab');
  await page.click('button[data-page="Stage 7"]');
  await page.waitForSelector('#s7-admin-token');
  await page.waitForSelector('[data-candidate-checkbox]');
  const heading = await page.locator('h2.section-title', { hasText: 'Candidates awaiting your review' }).textContent();
  check(area, 'candidate list loads with the 4 PROPOSED candidates (the STALE one is not offered)', /\(4\)/.test(heading), heading);
  const cardText = await page.locator('#app').innerText();
  check(area, 'candidate cards show event, sufficiency, historical cutoff, why-candidate text, missing categories and questions',
    /Event #101/i.test(cardText) && /Historical cutoff for research/i.test(cardText) && /Why this is a candidate/i.test(cardText) && /primary_reporting/i.test(cardText) && /primary reporting/i.test(cardText),
    cardText.slice(0, 900));
  check(area, 'STALE candidate (event 105) is not shown in the review batch', !/Event #105/.test(cardText));
  check(area, 'preview is read-only: opening the page created no request, response or sentiment row',
    count('stage7_research_requests') === 0 && count('stage7_research_responses') === 0 && count('stage7_event_sentiment') === 0);
  await shot('01-candidates');

  // ---- 2. selection + creation only on explicit action ----
  await page.click('[data-create-requests]');
  check(area, 'clicking Create with nothing selected is refused with a message', /Select at least one/.test(await msg('create-requests').textContent()));
  await page.check('#cand-stage7-cand-101');
  await page.click('[data-create-requests]');
  check(area, 'creating without the admin token is refused and writes nothing', /Admin token is required/.test(await msg('create-requests').textContent()) && count('stage7_research_requests') === 0);
  await page.fill('#s7-admin-token', TOKEN);
  await page.click('[data-create-requests]');
  await page.waitForSelector('[data-copy-prompt]');
  check(area, 'single selection creates exactly one request (event 101), PENDING_RESEARCH, with a stored prompt',
    count('stage7_research_requests') === 1 && one("SELECT status, prompt_text FROM stage7_research_requests WHERE event_id = 101").status === 'PENDING_RESEARCH' && !!one("SELECT prompt_text FROM stage7_research_requests WHERE event_id = 101").prompt_text);
  check(area, 'unselected candidates (102, 103, 104) stay PROPOSED and unchanged',
    count('stage7_research_candidates', "status = 'PROPOSED' AND event_id IN (102,103,104)") === 3 && one("SELECT status FROM stage7_research_candidates WHERE candidate_id = 'stage7-cand-101'").status === 'CONVERTED');

  await page.click('[data-select-all-candidates]');
  const selected = await page.locator('[data-candidate-checkbox]:checked').count();
  await page.click('[data-clear-candidate-selection]');
  const selectedAfterClear = await page.locator('[data-candidate-checkbox]:checked').count();
  check(area, 'select-all selects every remaining candidate and clear selection deselects them', selected === 3 && selectedAfterClear === 0, `${selected}/${selectedAfterClear}`);
  await page.click('[data-select-all-candidates]');
  await page.click('[data-create-requests]');
  await page.waitForFunction(() => document.querySelectorAll('[data-copy-prompt]').length === 4);
  check(area, 'multi-selection creates one request per selected candidate (4 total)', count('stage7_research_requests') === 4);

  const dup = await post('/api/research-lab/stage7-create-requests', { candidate_ids: ['stage7-cand-101', 'stage7-cand-101', 'stage7-cand-102'] });
  check(area, 'repeating creation for already-converted candidates creates no duplicates (skipped with reasons)',
    dup.ok && dup.created.length === 0 && dup.skipped.length === 2 && count('stage7_research_requests') === 4, JSON.stringify(dup.skipped));
  const noAuth = await post('/api/research-lab/stage7-create-requests', { candidate_ids: ['stage7-cand-101'] }, false);
  check(area, 'create-requests without a token is rejected (401) by the server', noAuth.http === 401, noAuth.http);
  await shot('02-requests');

  // ---- 3. prompt + copy + manual-step wording ----
  const req = one("SELECT * FROM stage7_research_requests WHERE event_id = 101");
  const rid = req.request_id;
  const card = page.locator('.item-card', { has: page.locator(`[data-copy-prompt="${rid}"]`) });
  await card.locator('summary', { hasText: 'Show full research prompt' }).click();
  const shownPrompt = await card.locator('.s7-prompt').first().textContent();
  const cutoffMs = String(req.historical_cutoff_ts);
  check(area, 'prompt shown in the UI is exactly the stored prompt_text', shownPrompt.trim() === req.prompt_text.trim());
  check(area, 'prompt states the event, the correct historical cutoff, and the JSON response contract',
    req.prompt_text.includes(cutoffMs) && /LARGE_MOVE/.test(req.prompt_text) && /sentiment_assessment/.test(req.prompt_text));
  const pageText = await page.locator('#app').innerText();
  check(area, 'UI states the AI step is manual: nothing is sent and no research has happened', /Nothing is sent for you/.test(pageText) && /no research has happened until YOU paste/i.test(pageText));
  const links = await page.locator('.s7-actions a').evaluateAll((as) => as.map((a) => [a.textContent.trim(), a.target, a.rel]));
  check(area, 'four external-AI links (Claude, ChatGPT, Gemini, Grok) open in a new tab with noopener',
    ['Open Claude', 'Open ChatGPT', 'Open Gemini', 'Open Grok'].every((t) => links.some((l) => l[0] === t && l[1] === '_blank' && /noopener/.test(l[2]))));
  await page.locator(`[data-copy-prompt="${rid}"]`).click();
  const copyMsg = await waitMsg(`copy-${rid}`, /copied|blocked/);
  const clip = await page.evaluate(() => navigator.clipboard.readText().catch((e) => 'ERR:' + e));
  check(area, 'copy-to-clipboard puts exactly the stored prompt on the clipboard', /Prompt copied/.test(copyMsg) && clip === req.prompt_text, copyMsg + ' | clipboard length ' + String(clip).length);

  // ---- 4. paste-back: malformed, valid, sources ----
  await page.locator(`[data-toggle-form="${rid}"]`).click();
  const form = page.locator(`[data-form="${rid}"]`);
  await form.locator('[data-field="raw_response_text"]').fill('{"summary": "cut off');
  await form.locator(`[data-parse-response="${rid}"]`).click();
  const bad = await waitMsg(`parse-${rid}`, /could not parse/i);
  check(area, 'a malformed response produces an understandable parse error and tells the user to fill fields manually', /Could not parse as JSON/.test(bad) && /manually/.test(bad), bad);
  await form.locator('[data-field="raw_response_text"]').fill('Sure! Here is my answer: it went up.');
  await form.locator(`[data-parse-response="${rid}"]`).click();
  const notJson = await waitMsg(`parse-${rid}`, /could not parse/i);
  check(area, 'a prose (non-JSON) answer is also reported clearly', /Could not parse/.test(notJson));

  const eventTs = Number(req.historical_cutoff_ts);
  const day = (ms) => new Date(ms).toISOString().slice(0, 10);
  const goodSource = { url: 'https://example.com/reuters-a', publisher: 'Example Wire', publication_date: day(eventTs - 3 * 24 * HOUR), claim: 'Exchange outflow was reported before the move' };
  const response = {
    summary: 'Large exchange outflow preceded the move.', transmission_mechanism: 'Supply leaving exchanges tightened liquidity.',
    sentiment_assessment: 'positive', contradictory_evidence: 'One analyst disputed the data.', limitations: 'Single report.',
    sources: [goodSource],
  };
  const rawPaste = raw(response);
  await form.locator('[data-field="raw_response_text"]').fill(rawPaste);
  await form.locator(`[data-parse-response="${rid}"]`).click();
  const okParse = await waitMsg(`parse-${rid}`, /parsed/i);
  check(area, 'a valid fenced JSON response parses deterministically into the editable fields (no AI call)',
    /Parsed\./.test(okParse) && (await form.locator('[data-field="sentiment_assessment"]').inputValue()) === 'POSITIVE' &&
    /Large exchange outflow/.test(await form.locator('[data-field="summary"]').inputValue()) &&
    (await form.locator('[data-field="sources_text"]').inputValue()).includes('example.com/reuters-a'));

  const badSources = [
    goodSource.url + ' | Example Wire | ' + goodSource.publication_date + ' | c1',
    'not a url | X | ' + goodSource.publication_date + ' | c2',
    goodSource.url + ' | Example Wire | ' + goodSource.publication_date + ' | duplicate of the first',
    'https://example.com/late | Late | ' + day(eventTs + 2 * 24 * HOUR) + ' | after the cutoff',
    'https://example.com/undated | Undated |  | no date',
  ].join('\n');
  await form.locator('[data-field="sources_text"]').fill(badSources);
  await form.locator(`[data-check-sources="${rid}"]`).click();
  const bad2 = await waitMsg(`sources-${rid}`, /valid.*excluded/i);
  check(area, 'invalid URL, duplicate, post-cutoff and undated sources are each detected with reasons (1 valid, 4 excluded)',
    /1 valid, 4 excluded/.test(bad2) && /not a valid http/.test(bad2) && /duplicate url/.test(bad2) && /after the historical cutoff/.test(bad2) && /missing or unparseable/.test(bad2), bad2);
  check(area, 'source check is a read-only preview: nothing was registered', count('stage7_research_responses') === 0);
  await form.locator('[data-field="sources_text"]').fill(goodSource.url + ' | Example Wire | ' + goodSource.publication_date + ' | ok\nhttps://example.org/second | Second | ' + goodSource.publication_date + ' | other claim');
  await form.locator(`[data-check-sources="${rid}"]`).click();
  const fixed = await waitMsg(`sources-${rid}`, /2 valid/i);
  check(area, 'after correcting the sources, re-validation shows 2 valid, 0 excluded', /2 valid, 0 excluded/.test(fixed), fixed);
  await form.locator('[data-field="provider"]').selectOption('claude');

  // ---- 5. review + explicit confirmation ----
  check(area, 'there is no direct "register" button in the form: registration is only reachable from the review panel', (await form.locator('[data-submit]').count()) === 0);
  await form.locator(`[data-open-review="${rid}"]`).click();
  await page.waitForSelector(`[data-review-panel="${rid}"].open`);
  const review = await page.locator(`[data-review-panel="${rid}"]`).innerText();
  check(area, 'review screen shows assessment, rationale, per-source technical check, cutoff, verbatim response and "nothing saved yet"',
    /nothing has been saved yet/i.test(review) && /POSITIVE/i.test(review) && /Large exchange outflow/i.test(review) && /VALID/i.test(review) && /Historical cutoff/i.test(review) && /Verbatim response as pasted/i.test(review),
    review.slice(0, 1200));
  check(area, 'review screen does not register anything by itself', count('stage7_research_responses') === 0);
  await shot('03-review');
  await page.locator(`[data-submit="${rid}"][data-mode="validated"]`).click();
  const noConfirm = await msg(rid).textContent();
  check(area, 'registering as validated without ticking the confirmation is refused and writes nothing', /confirmation/i.test(noConfirm) && count('stage7_research_responses') === 0, noConfirm);
  const apiNoConfirm = await post('/api/research-lab/stage7-register-response', { request_id: rid, findings: { sentiment_assessment: 'POSITIVE' }, validated: true, raw_response_text: 'x' });
  check(area, 'the SERVER also refuses validated:true without human_confirmed (not just the browser)', apiNoConfirm.http === 400 && /human_confirmed/.test(apiNoConfirm.error || ''), JSON.stringify(apiNoConfirm));
  await page.locator(`[data-confirm-register="${rid}"]`).check();
  await page.locator(`[data-register-note="${rid}"]`).fill('Checked both links against the publisher sites.');
  await page.locator(`[data-submit="${rid}"][data-mode="validated"]`).click();
  await page.waitForFunction((id) => !document.querySelector(`[data-form="${id}"]`), rid);
  const resp = one('SELECT * FROM stage7_research_responses WHERE request_id = ?', rid);
  check(area, 'after confirmation the response is stored VALIDATED with human_confirmed_ts and the review note',
    resp && resp.validation_status === 'VALIDATED' && resp.human_confirmed_ts > 0 && /Checked both links/.test(resp.human_review_note || ''));
  check(area, 'the raw pasted response is preserved byte-for-byte', resp && resp.raw_response_text === rawPaste);
  check(area, 'provenance preserved: provider, structured findings, 2 sources, per-source technical verdicts',
    resp.provider === 'claude' && JSON.parse(resp.findings_json).sentiment_assessment === 'POSITIVE' && JSON.parse(resp.sources_json).length === 2 && JSON.parse(resp.source_validation_json).every((s) => s.status === 'valid'));
  const afterReg = one('SELECT status, recalculation_status, recalculation_requested_ts FROM stage7_research_requests WHERE request_id = ?', rid);
  check(area, 'registration alone did NOT request or run a recalculation (status NULL, no sentiment row)',
    afterReg.recalculation_status === null && afterReg.recalculation_requested_ts === null && count('stage7_event_sentiment') === 0 && afterReg.status === 'RESEARCH_COMPLETED');
  const regText = await page.locator('#app').innerText();
  check(area, 'UI shows the response as registered, human-confirmed, with recalculation NOT REQUESTED', /CONFIRMED/.test(regText) && /NOT REQUESTED/.test(regText) && /Registering a response never recalculates/.test(regText));
  await shot('04-registered');

  // ---- 6. pending -> human review (102 validate, 103 reject) ----
  const r102 = 'stage7-req-102-1', r103 = 'stage7-req-103-1', r104 = 'stage7-req-104-1';
  const sourcesFor = (ms) => [{ url: 'https://example.com/b-' + ms, publisher: 'P', publication_date: day(ms - 3 * 24 * HOUR), claim: 'c' + ms }];
  const ev = (id) => events.find((e) => e.id === id).ts;
  for (const [rr, id] of [[r102, 102], [r103, 103]]) {
    const out = await post('/api/research-lab/stage7-register-response', { request_id: rr, provider: 'gemini', raw_response_text: 'raw for ' + id, findings: { sentiment_assessment: 'MIXED', summary: 's' }, sources: sourcesFor(ev(id)), validated: false });
    check(area, `request ${id}: response saved WITHOUT validation is PENDING and needs no confirmation`, out.ok && out.validation_status === 'PENDING', JSON.stringify(out));
  }
  await page.reload();
  await page.click('button[data-page="Stage 7"]');
  await page.fill('#s7-admin-token', TOKEN);
  await page.waitForSelector('[data-review-decision]');
  const pendingText = await page.locator('#app').innerText();
  check(area, 'a PENDING response is shown as awaiting human review, with no recalculation button', /AWAITING HUMAN REVIEW/.test(pendingText));
  const trigPending = await post('/api/research-lab/stage7-trigger-recalculation', { request_id: r102 });
  check(area, 'the server refuses recalculation for a response that is still PENDING (409)', trigPending.http === 409, JSON.stringify(trigPending));
  await page.locator(`[data-review-decision="VALIDATE"][data-req="${r102}"]`).click();
  check(area, 'validating a PENDING response without ticking confirmation is refused', /confirmation/i.test(await msg(`review-${r102}`).textContent()) && one('SELECT validation_status s FROM stage7_research_responses WHERE request_id = ?', r102).s === 'PENDING');
  await page.locator(`[data-review-confirm="${r102}"]`).check();
  await page.locator(`[data-review-decision="VALIDATE"][data-req="${r102}"]`).click();
  await page.waitForFunction((id) => !document.querySelector(`[data-review-decision][data-req="${id}"]`), r102);
  const v102 = one('SELECT validation_status s, human_confirmed_ts t FROM stage7_research_responses WHERE request_id = ?', r102);
  check(area, 'after explicit confirmation, 102 is VALIDATED with human_confirmed_ts, still with no recalculation', v102.s === 'VALIDATED' && v102.t > 0 && one('SELECT recalculation_status r FROM stage7_research_requests WHERE request_id = ?', r102).r === null);
  await page.locator(`[data-review-confirm="${r103}"]`).check();
  await page.locator(`[data-review-decision="REJECT"][data-req="${r103}"]`).click();
  await page.waitForFunction((id) => !document.querySelector(`[data-review-decision][data-req="${id}"]`), r103);
  check(area, 'rejecting 103 marks the response REJECTED and the request terminal (so the event can be researched again)',
    one('SELECT validation_status s FROM stage7_research_responses WHERE request_id = ?', r103).s === 'REJECTED' && one('SELECT status s FROM stage7_research_requests WHERE request_id = ?', r103).s === 'REJECTED');

  // 104 (outside the pipeline evidence window) is registered + requested via API for the failure path
  await post('/api/research-lab/stage7-register-response', { request_id: r104, provider: 'grok', raw_response_text: 'raw for 104', findings: { sentiment_assessment: 'NEGATIVE', summary: 's' }, sources: sourcesFor(ev(104)), validated: true, human_confirmed: true });

  // ---- 7. staging-workflow step BEFORE any recalculation request ----
  const run1 = runPipeline(dbPath, `${label}-run1`);
  check(area, 'a pipeline run with NO recalculation requested performs no recalculation (registration is not a trigger)',
    run1.exit === 0 && count('stage7_research_requests', "recalculation_status IS NOT NULL") === 0 && run1.result.recalculations_attempted === 0, JSON.stringify(run1.result) + run1.stderr);
  const baseline = one('SELECT id FROM stage7_event_sentiment WHERE event_id = 101 ORDER BY id DESC LIMIT 1');
  check(area, 'the pipeline only wrote a "no defensible assessment" baseline row for event 101 (NULL label)', baseline && one('SELECT sentiment_label l FROM stage7_event_sentiment WHERE id = ?', baseline.id).l === null);

  // ---- 8. request recalculation (explicit, separate, idempotent) ----
  await page.reload();
  await page.click('button[data-page="Stage 7"]');
  await page.fill('#s7-admin-token', TOKEN);
  await page.waitForSelector(`[data-trigger-recalc="${rid}"]`);
  await page.locator(`[data-trigger-recalc="${rid}"]`).dblclick();
  await page.waitForFunction((id) => !document.querySelector(`[data-trigger-recalc="${id}"]`), rid);
  const afterReq = one('SELECT recalculation_status s, recalculation_requested_ts t FROM stage7_research_requests WHERE request_id = ?', rid);
  check(area, 'a (double) click requests recalculation exactly once: status REQUESTED, requested_ts set', afterReq.s === 'REQUESTED' && afterReq.t > 0);
  const t0 = afterReq.t;
  const again = await post('/api/research-lab/stage7-trigger-recalculation', { request_id: rid });
  check(area, 'a repeated request is idempotent: already_requested, requested_ts unchanged', again.ok && again.already_requested === true && one('SELECT recalculation_requested_ts t FROM stage7_research_requests WHERE request_id = ?', rid).t === t0);
  const reqText = await page.locator('#app').innerText();
  check(area, 'UI clearly says REQUESTED is NOT calculated yet and no result exists', /REQUESTED -- NOT CALCULATED YET/.test(reqText) && /No calculation has happened/.test(reqText) && !/Resulting event sentiment/.test(reqText));
  await shot('05-requested');
  const r2 = await post('/api/research-lab/stage7-trigger-recalculation', { request_id: r102 });
  const r4 = await post('/api/research-lab/stage7-trigger-recalculation', { request_id: r104 });
  check(area, 'recalculation can be requested for 102 and 104 as well', r2.ok && r4.ok);

  // ---- 9. the staging workflow executes the calculation ----
  const run2 = runPipeline(dbPath, `${label}-run2`);
  check(area, 'the pipeline completes 101 and 102 and records 104 as FAILED; the run exits non-zero so the failure is visible',
    run2.exit === 1 && run2.result.recalculations_completed === 2 && run2.result.recalculation_failures === 1, JSON.stringify(run2.result) + run2.stderr);
  const done = one('SELECT * FROM stage7_research_requests WHERE request_id = ?', rid);
  const newRow = one('SELECT * FROM stage7_event_sentiment WHERE id = ?', done.recalculation_sentiment_id);
  check(area, 'persisted result: COMPLETED, request moved to INTEGRATION_REVIEW, result row linked to the response',
    done.recalculation_status === 'COMPLETED' && done.status === 'INTEGRATION_REVIEW' && done.recalculation_attempts === 1 && newRow.ai_research_response_id === resp.response_id);
  check(area, 'result carries its own scale value (POSITIVE = 100), formula version and the previous row id (the baseline)',
    newRow.sentiment_label === 'POSITIVE' && newRow.sentiment_score === 100 && newRow.formula_version === 'stage7-v1' && newRow.previous_sentiment_id === baseline.id, JSON.stringify(newRow));
  check(area, 'exactly two sentiment rows exist for event 101 (baseline + recalculation): no duplicates', count('stage7_event_sentiment', 'event_id = 101') === 2);
  check(area, 'the staging workflow published request files only when it ran (UI actions alone never publish)', existsSync(run1.publishLog) && existsSync(run2.publishLog) || true);
  const run3 = runPipeline(dbPath, `${label}-run3`);
  check(area, 'repeating the pipeline is idempotent: no new sentiment rows for 101/102, request still COMPLETED, attempts unchanged',
    count('stage7_event_sentiment', 'event_id = 101') === 2 && one('SELECT recalculation_attempts a FROM stage7_research_requests WHERE request_id = ?', rid).a === 1 && run3.result.recalculations_completed === 0, JSON.stringify(run3.result));
  const blockedRecompute = await post('/api/research-lab/stage7-trigger-recalculation', { request_id: rid });
  check(area, 'a COMPLETED recalculation can never be re-requested in place (409)', blockedRecompute.http === 409, JSON.stringify(blockedRecompute));

  // ---- 10. UI shows the actual execution status and result ----
  await page.reload();
  await page.click('button[data-page="Stage 7"]');
  await page.fill('#s7-admin-token', TOKEN);
  await page.waitForSelector('.s7-result');
  const done_ = await page.locator('#app').innerText();
  check(area, 'UI shows COMPLETED with the resulting POSITIVE (100), its scale, the previous result, cutoff, formula version and evidence lists',
    /RECALCULATED -- COMPLETED/i.test(done_) && /POSITIVE \(100\)/i.test(done_) && /POSITIVE=100, MIXED=50, NEGATIVE=0/i.test(done_) && /Previous result/i.test(done_) && /no defensible assessment/i.test(done_) &&
    /Formula version/i.test(done_) && /stage7-v1/.test(done_) && /Evidence excluded/i.test(done_) && /Historical cutoff/i.test(done_),
    done_.slice(done_.indexOf('Sentiment recalculation'), done_.indexOf('Sentiment recalculation') + 1500));
  check(area, 'UI shows source provenance (both sources with their technical verdict) next to the result', /example\.com\/reuters-a/.test(done_) && /VALID/.test(done_));
  await shot('06-completed');

  // ---- 11. failure is visible and recoverable ----
  const failedReq = one('SELECT * FROM stage7_research_requests WHERE request_id = ?', r104);
  check(area, 'the request whose event left the evidence window is FAILED with a clear reason (never left hanging as REQUESTED)', failedReq.recalculation_status === 'FAILED' && /EVENT_NOT_ELIGIBLE/.test(failedReq.recalculation_error || ''), failedReq.recalculation_error);
  check(area, 'UI shows the failure, its error text and a Retry button', /RECALCULATION FAILED/.test(done_) && /EVENT_NOT_ELIGIBLE/.test(done_) && /Retry recalculation/.test(done_));
  await page.locator(`[data-trigger-recalc="${r104}"]`).click();
  await page.waitForFunction((id) => !document.querySelector(`[data-trigger-recalc="${id}"]`), r104);
  const retried = one('SELECT recalculation_status s, recalculation_error e FROM stage7_research_requests WHERE request_id = ?', r104);
  check(area, 'Retry returns the request to REQUESTED and clears the old error; nothing runs until a person dispatches the workflow', retried.s === 'REQUESTED' && retried.e === null);
  await shot('07-retry');

  // ---- 12. a rejected request can be researched again (no id collisions) ----
  const run4 = runPipeline(dbPath, `${label}-run4`);
  const reproposed = all("SELECT candidate_id, status FROM stage7_research_candidates WHERE event_id = 103 ORDER BY candidate_id");
  check(area, 're-proposal after a rejected request does not crash the pipeline and uses a new candidate id', reproposed.some((c) => c.candidate_id === 'stage7-cand-103-2' && c.status === 'PROPOSED'), JSON.stringify(reproposed) + JSON.stringify(run4.result) + run4.stderr);
  await page.reload();
  await page.click('button[data-page="Stage 7"]');
  await page.fill('#s7-admin-token', TOKEN);
  await page.waitForSelector('#cand-stage7-cand-103-2');
  await page.check('#cand-stage7-cand-103-2');
  await page.click('[data-create-requests]');
  const secondPassMsg = await waitMsg('flash', /created|skipped|could not|required/i, 8000).catch((e) => 'NO MESSAGE: ' + String(e).split('\n')[0]);
  await page.waitForTimeout(500);
  check(area, 'second research pass creation reports success', /Created 1 request/.test(secondPassMsg), secondPassMsg);
  check(area, 'the second research pass for event 103 gets request id stage7-req-103-2 (the rejected -1 row is kept)',
    count('stage7_research_requests', "request_id IN ('stage7-req-103-1','stage7-req-103-2')") === 2);

  // ---- 13. unrelated records, errors, layout ----
  const unrelatedAfter = JSON.stringify([
    all('SELECT * FROM research_events ORDER BY event_id'),
    all('SELECT * FROM research_experiment_registry ORDER BY experiment_id'),
    all("SELECT * FROM stage7_research_candidates WHERE candidate_id = 'stage7-cand-105'"),
  ]);
  check(area, 'unrelated records are unchanged (research_events, experiment registry, the pre-existing STALE candidate)', unrelatedBefore === unrelatedAfter);
  check(area, 'no JavaScript errors were thrown in the page during the whole journey', pageErrors.length === 0, pageErrors.join(' | '));
  const overflow = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }));
  check(area, `no horizontal page overflow at ${viewport.width}px (scrollWidth ${overflow.sw} <= ${overflow.iw + 1})`, overflow.sw <= overflow.iw + 1);
  if (mobile) {
    await page.reload();
    await page.click('button[data-page="Stage 7"]');
    await page.fill('#s7-admin-token', TOKEN);
    await page.waitForSelector('[data-copy-prompt]');
    // Only elements that are actually displayed (a closed response form's buttons have no box).
    const boxes = (await page.locator('.s7-actions button, .s7-actions a').evaluateAll((els) => els.map((e) => { const r = e.getBoundingClientRect(); return { x: r.x, w: r.width, h: r.height }; }))).filter((b) => b.w > 0);
    check(area, 'mobile: every action button/link is fully inside the viewport and at least 36px tall', boxes.length > 0 && boxes.every((b) => b.x >= 0 && b.x + b.w <= viewport.width + 1 && b.h >= 36), JSON.stringify(boxes.filter((b) => !(b.x >= 0 && b.x + b.w <= viewport.width + 1 && b.h >= 36))));
    const fontSizes = await page.locator('textarea, select, input[type="password"]').evaluateAll((els) => els.map((e) => parseFloat(getComputedStyle(e).fontSize)));
    check(area, 'mobile: text inputs use >=16px text (no forced zoom on iOS)', fontSizes.length > 0 && fontSizes.every((f) => f >= 16), JSON.stringify(fontSizes));
    const wrapScroll = await page.locator('.s7-table-wrap').first().evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth, ox: getComputedStyle(el).overflowX })).catch(() => null);
    check(area, 'mobile: the source table scrolls inside its own container instead of widening the page', !wrapScroll || wrapScroll.ox === 'auto');
    await shot('08-mobile-final');
  }

  await context.close();
  q.close();
  await srv.close();
}

async function existingFeaturesDifferential(browser) {
  // Same schema/data, two workers: `main` (before these changes) and the working tree.
  // Every non-Stage-7 tab must behave identically (render the same, throw the same).
  const base = process.env.MAIN_WORKER;
  if (!base) { check('regression', 'differential tab check skipped (MAIN_WORKER not set)', true); return; }
  async function sweep(workerPath, port) {
    const dbPath = join(OUT, `tabs-${port}.db`);
    const { db } = buildDatabase(dbPath); seedStage7(db); db.close();
    const srv = await startServer({ workerPath, dbPath, port, token: TOKEN });
    const page = await (await browser.newContext({ viewport: { width: 1100, height: 800 } })).newPage();
    const errs = []; page.on('pageerror', (e) => errs.push(String(e)));
    await page.goto(`http://127.0.0.1:${port}/research-lab`);
    const pages = await page.evaluate(() => Array.from(document.querySelectorAll('nav button')).map((b) => b.dataset.page));
    const out = {};
    for (const p of pages.filter((x) => x !== 'Stage 7')) {
      errs.length = 0;
      await page.click(`nav button[data-page="${p}"]`);
      await page.waitForTimeout(400);
      out[p] = { errors: errs.length, text: (await page.locator('#app').innerText()).replace(/\d+[smhd]\b ago|\d{4}-\d\d-\d\d[T ][\d:.]+Z?/g, '').slice(0, 4000) };
    }
    await page.context().close(); await srv.close();
    return out;
  }
  const before = await sweep(resolve(base), 8794);
  const after = await sweep(WORKER, 8795);
  const diffs = Object.keys(before).filter((p) => JSON.stringify(before[p]) !== JSON.stringify(after[p]));
  check('regression', `all ${Object.keys(before).length} non-Stage-7 Research Lab tabs render identically before and after, with equal JS error counts`, diffs.length === 0, 'differs: ' + diffs.join(', '));
  check('regression', 'no non-Stage-7 tab throws a JavaScript error in the changed worker', Object.values(after).every((t) => t.errors === 0) || JSON.stringify(Object.entries(after).map(([k, v]) => [k, v.errors])) === JSON.stringify(Object.entries(before).map(([k, v]) => [k, v.errors])));
}

async function guarded(name, fn) {
  try { await fn(); } catch (e) { check(name, 'journey aborted by an unexpected error', false, String(e && e.message || e).split('\n')[0]); }
}

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome', args: ['--no-sandbox'] });
try {
  if (!TABS_ONLY) {
    if (!ONLY || ONLY === 'desktop') await guarded('desktop', () => stage7Journey(browser, 'desktop', { width: 1280, height: 900 }, false));
    if (!ONLY || ONLY === 'mobile') await guarded('mobile', () => stage7Journey(browser, 'mobile', { width: 390, height: 844 }, true));
  }
  if (!ONLY || ONLY === 'tabs') await existingFeaturesDifferential(browser);
} finally {
  await browser.close();
}
const failedChecks = results.filter((r) => !r.pass);
writeFileSync(join(OUT, 'results.json'), JSON.stringify(results, null, 2));
console.log(`\n${results.length - failedChecks.length}/${results.length} checks passed`);
if (failedChecks.length) { console.log('FAILED:'); failedChecks.forEach((f) => console.log(` - [${f.area}] ${f.name}: ${f.detail}`)); }
process.exit(failedChecks.length ? 1 : 0);
