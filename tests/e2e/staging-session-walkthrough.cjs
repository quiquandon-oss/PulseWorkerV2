// STAGING ONLY. Run by the dispatch-only "staging-session-walkthrough" job in .github/workflows/test.yml.
// Drives the deployed staging Research Lab in Chromium as a real user: sign in once, then Market -> Research ->
// Confirm Finding -> Learning -> Create Candidate -> Save Adjustment -> V1 Impact -> Validation, checking that no token
// is ever requested again. It also proves that unauthenticated, cross-origin, header-less, expired and tampered
// writes are refused and create no rows. The admin token comes from the environment and is never printed.
// With SIGNAL_EVENT_ID set it also walks a NEW_SIGNAL finding (Event #15 shape) through Learning and checks that it
// becomes a data-collection prototype: no fake V1 impact, no validation claim, submit stays a human action.
// LOCAL_BASE (loopback only, e.g. http://localhost:8787) runs the same walk against a local worker.fetch server.
const { chromium } = require('playwright');
const STAGING = 'https://pulseworker-v2-staging.quiquandon.workers.dev';
const LOCAL = /^http:\/\/(localhost|127\.0\.0\.1):\d+$/.test(process.env.LOCAL_BASE || '') ? process.env.LOCAL_BASE : null;
const BASE = LOCAL || STAGING;
const TOKEN = process.env.STAGING_ADMIN_TOKEN;
const EVENT_ID = Number(process.env.EVENT_ID || '999004');
const SIGNAL_EVENT_ID = process.env.SIGNAL_EVENT_ID ? Number(process.env.SIGNAL_EVENT_ID) : null;
// A staging copy of production Candidate #1 (legacy proxy shape, PENDING_REVIEW), prepared before the run.
const PROXY_CANDIDATE_ID = process.env.PROXY_CANDIDATE_ID ? Number(process.env.PROXY_CANDIDATE_ID) : null;
if (!TOKEN) { console.error('STAGING_ADMIN_TOKEN missing'); process.exit(1); }
if (!LOCAL && !BASE.includes('-staging.')) { console.error('refusing: not the staging Worker'); process.exit(1); }

const results = [];
function check(name, cond, detail = '') { results.push([cond ? 'PASS' : 'FAIL', name, detail]); console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  (' + detail + ')' : ''}`); }
const json = async (r) => { try { return await r.json(); } catch (_e) { return null; } };
async function learningState() {
  const cands = await json(await fetch(`${BASE}/api/learning/candidates`));
  const cs = await json(await fetch(`${BASE}/api/learning/case?event_id=${EVENT_ID}`));
  const vers = await json(await fetch(`${BASE}/api/learning/versions`));
  return { candidates: cands.candidates.length, case: cs.case ? cs.case.request_id : null, versions: vers.versions.length };
}
async function post(path, body, headers) {
  const r = await fetch(BASE + path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
  return r.status;
}
async function postFull(path, body, headers) {
  const r = await fetch(BASE + path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
  return { status: r.status, json: await json(r) };
}
const candidateRow = async (id) => { const v = await json(await fetch(`${BASE}/api/learning/candidate?id=${id}`)); return v && v.candidate; };
const SAMPLE = `SAMPLE ANSWER for the staging session walkthrough (synthetic fixture event; not real research)
\`\`\`json
{ "explanation": "SAMPLE: staging fixture explanation.", "primary_driver": "Sample driver (session walkthrough)", "driver_category": "FLOWS",
  "finding_type": "SOURCE_WEIGHTING", "covered_by_existing_v1_source": "etfflows",
  "proposed_new_source": { "name": "", "url": "", "what_it_measures": "", "update_frequency": "", "free_or_paid": "" },
  "proposed_signal": "Lower ETF flow weight", "trend": "Sample",
  "evidence": [ { "claim": "SAMPLE claim", "url": "https://example.com/sample", "publisher": "Example", "date": "2026-10-04" } ],
  "inference": [], "speculation": [], "alternative_explanations": [], "limitations": "Sample only", "confidence": "LOW", "sentiment_assessment": "NEGATIVE" }
\`\`\``;
// Same shape as production Event #15's confirmed finding, marked as a sample.
const SIGNAL_SAMPLE = `SAMPLE ANSWER for the new-signal walkthrough (synthetic fixture event; not real research)
\`\`\`json
{ "explanation": "SAMPLE: cross-asset risk-off shock (geopolitics, oil, yields) drove the move.", "primary_driver": "Sample geopolitical risk-off shock",
  "driver_category": "MACRO", "finding_type": "NEW_SIGNAL", "covered_by_existing_v1_source": "macrogeo",
  "proposed_new_source": { "name": "Sample cross-asset feed", "url": "https://example.com/feed", "what_it_measures": "", "update_frequency": "Intraday", "free_or_paid": "" },
  "proposed_signal": "Create a 0-100 Risk Regime Shock score from standardized changes in geopolitical event severity, Brent/WTI, Treasury yields, equity futures, USD/rate expectations and crypto liquidation intensity. Apply the signal as a regime modifier.",
  "trend": "Ongoing regime condition", "evidence": [ { "claim": "SAMPLE claim", "url": "https://example.com/sample", "publisher": "Example", "date": "2026-10-04" } ],
  "inference": [], "speculation": [], "alternative_explanations": [], "limitations": "Sample only", "confidence": "HIGH", "sentiment_assessment": "NEGATIVE" }
\`\`\``;

(async () => {
  // 1. Unauthenticated writes are refused and change nothing.
  const before = await learningState();
  check('fixture event has no research case yet', before.case === null, `event ${EVENT_ID}`);
  const finding = { explanation: 'x' };
  check('no credentials -> 401', (await post('/api/learning/findings', { event_id: EVENT_ID, finding }, { Origin: BASE, 'X-CryptoPulse-Research': '1' })) === 401);
  check('wrong Bearer -> 401', (await post('/api/learning/candidates', { event_id: EVENT_ID }, { Authorization: 'Bearer wrong' })) === 401);
  check('bogus session cookie -> 401', (await post('/api/learning/findings', { event_id: EVENT_ID, finding }, { Cookie: 'cp_rl_session=v1.9999999999999.' + '0'.repeat(64), Origin: BASE, 'X-CryptoPulse-Research': '1' })) === 401);

  // 2. Browser: the page has no token field; sign in once.
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE });
  const page = await ctx.newPage();
  const errors = []; page.on('pageerror', (e) => errors.push(e.message));
  let tokenPrompts = 0;
  const countPrompts = async () => { tokenPrompts += await page.locator('#app input[type=password], header input[type=password]').count(); const t = await page.innerText('body'); if (/not signed in|admin token/i.test(t)) tokenPrompts++; };
  await page.goto(`${BASE}/research-lab`); await page.waitForSelector('#act');
  check('Research Lab has no admin-token field', (await page.locator('input[type=password]').count()) === 0);
  check('signed-out header offers "Sign in this device"', /Sign in this device/.test(await page.innerText('#sessionBox')));
  await page.goto(`${BASE}/research-lab/signin`);
  await page.fill('#token', TOKEN); await page.click('button[type=submit]');
  await page.waitForURL(`${BASE}/research-lab`); await page.waitForFunction(() => /Signed in on this device/.test(document.getElementById('sessionBox').innerText));
  const cookie = (await ctx.cookies(BASE)).find((c) => c.name === 'cp_rl_session');
  check('session cookie is HttpOnly, Secure, SameSite=Strict, ~180 days', !!cookie && cookie.httpOnly && cookie.secure && cookie.sameSite === 'Strict' && Math.abs(cookie.expires * 1000 - Date.now() - 180 * 86400000) < 3600000);
  check('page JavaScript cannot read the cookie', !(await page.evaluate(() => document.cookie)).includes('cp_rl_session'));

  // 3. Full journey, no token asked again.
  await page.click(`.ev[data-id="${EVENT_ID}"]`); await page.waitForTimeout(300); await countPrompts();
  await page.click('#act'); await page.waitForSelector('#copyPack');
  await page.click('#copyPack'); await page.waitForTimeout(200);
  check('research pack copied', (await page.evaluate(() => navigator.clipboard.readText())).startsWith('Analyse this CryptoPulse research case.'));
  await page.fill('#aiText', SAMPLE); await page.click('#parse'); await page.waitForSelector('#confirm'); await countPrompts();
  await page.check('#reviewed'); await page.click('#confirm');
  await page.waitForSelector('#mkCand', { timeout: 20000 }); await countPrompts();
  check('Confirm Finding saved with one click', true);
  await page.click('#mkCand'); await page.waitForSelector('#save', { timeout: 20000 }); await countPrompts();
  check('Create Learning Candidate saved with one click', true);
  await page.selectOption('#adjType', 'CHANGE_WEIGHT'); await page.waitForSelector('[data-a="source_id"]');
  await page.selectOption('[data-a="source_id"]', 'etfflows'); await page.fill('[data-a="weight"]', '3'); await page.fill('[data-a="confidence"]', '0.8');
  await page.click('#save'); await page.waitForFunction(() => /Current V1 \(reconstructed\)/i.test(document.body.innerText), null, { timeout: 20000 }); await countPrompts();
  const body = (await page.innerText('#app')).replace(/\s+/g, ' ');
  check('Save Adjustment -> V1 Impact shown (stored / reconstructed / proposed)', /Stored V1 \(reference\).*Current V1 \(reconstructed\).*Proposed V1/i.test(body));
  check('Validation shown', /Does it improve CryptoPulse\?\s*(Not enough data|Validating|Supported|Not supported|Inconclusive)/i.test(body), (body.match(/Does it improve CryptoPulse\? .{0,60}/i) || [''])[0]);
  check('no admin token requested at any step after sign-in', tokenPrompts === 0, `prompts=${tokenPrompts}`);
  await page.reload(); await page.waitForFunction(() => /Signed in on this device/.test(document.getElementById('sessionBox').innerText));
  check('still signed in after reload', true);

  // 3b. NEW_SIGNAL: research confirmed -> candidate -> new signal -> historical data required -> no fake V1 impact ->
  // no validation claim -> submit for review is a human click; nothing is approved and no V1 version appears.
  if (SIGNAL_EVENT_ID !== null) {
    const versionsBefore = (await learningState()).versions;
    await page.click('nav button[data-tab="Market"]'); await page.waitForSelector(`.ev[data-id="${SIGNAL_EVENT_ID}"]`);
    await page.click(`.ev[data-id="${SIGNAL_EVENT_ID}"]`); await page.waitForTimeout(300);
    await page.click('#act'); await page.waitForSelector('#aiText');
    await page.fill('#aiText', SIGNAL_SAMPLE); await page.click('#parse'); await page.waitForSelector('#confirm');
    await page.check('#reviewed'); await page.click('#confirm');
    await page.waitForSelector('#mkCand', { timeout: 20000 });
    check('NEW_SIGNAL: research confirmed', true);
    await page.click('#mkCand'); await page.waitForSelector('#newSignal', { timeout: 20000 }); await countPrompts();
    const t = (await page.innerText('#app')).replace(/\s+/g, ' ');
    check('NEW_SIGNAL: candidate created, status DATA COLLECTION REQUIRED', /Learning candidate #\d+ DATA COLLECTION REQUIRED/i.test(t));
    check('NEW_SIGNAL: new signal identified (reusable name, regime modifier)', (await page.inputValue('[data-a="signal_name"]')) === 'Risk Regime Shock' && (await page.inputValue('[data-a="role"]')) === 'REGIME_MODIFIER');
    check('NEW_SIGNAL: required inputs listed', /Brent\/WTI/.test(await page.inputValue('[data-al="inputs"]')));
    check('NEW_SIGNAL: "historical data required" shown', /New signal discovered . historical data required/.test(t) && /V1 does not currently collect the required inputs/.test(t));
    check('NEW_SIGNAL: no weight / confidence fields', (await page.locator('[data-a="weight"], [data-a="confidence"]').count()) === 0);
    check('NEW_SIGNAL: not mapped to an existing source', !/Macro economy news|24h change of/i.test((await page.innerText('#app')).split('Related V1 source')[0]));
    check('NEW_SIGNAL: V1 impact NOT CALCULABLE YET, no Proposed V1', /NOT CALCULABLE YET/.test(t) && /no V1 numerical adjustment was applied/.test(t) && (await page.locator('.kv .k').filter({ hasText: /Proposed V1|Current V1/i }).count()) === 0,
      (t.match(/What would V1 become\? .{0,160}/i) || [''])[0]);
    check('NEW_SIGNAL: validation NOT SUPPORTED YET / DATA REQUIRED, no accuracy claim', /NOT SUPPORTED YET \/ DATA REQUIRED/.test(t) && !/Current V1 right|Adjusted V1 right/.test(t));
    check('NEW_SIGNAL: journey shows data required', /Not calculable yet: historical data required/.test(t) && /Not supported yet: data required/.test(t));
    await page.click('#submit'); await page.waitForSelector('[data-d="APPROVE"]', { timeout: 20000 }); await countPrompts();
    const t2 = (await page.innerText('#app')).replace(/\s+/g, ' ');
    check('NEW_SIGNAL: submit for review was a human click -> PENDING REVIEW', /Learning candidate #\d+ PENDING REVIEW/i.test(t2));
    check('NEW_SIGNAL: decision offers only the data-collection plan', /Approve data-collection plan/.test(t2) && !/Approve V1 change/.test(t2) && (await page.locator('#ack').count()) === 0);
    check('NEW_SIGNAL: nothing approved, no V1 version created', (await learningState()).versions === versionsBefore);
  }

  // 3c. Legacy proxy candidate (copy of production Candidate #1): labelled, not approvable as a V1 change, and the
  // switch to a prototype changes nothing until Save is clicked.
  if (PROXY_CANDIDATE_ID !== null) {
    const versionsBefore = (await learningState()).versions;
    const row0 = await candidateRow(PROXY_CANDIDATE_ID);
    check('PROXY: fixture is the legacy proxy shape', !!row0 && row0.adjustment.type === 'ADD_SIGNAL' && row0.adjustment.derived_from === 'macrogeo' && row0.status === 'PENDING_REVIEW', row0 ? row0.status : 'missing');
    check('PROXY: API labels it PROXY_INVALID_FOR_SIGNAL_VALIDATION', row0 && row0.signal_validity === 'PROXY_INVALID_FOR_SIGNAL_VALIDATION');
    await page.goto(`${BASE}/research-lab`); await page.waitForSelector('#act');
    await page.click('nav button[data-tab="Learning"]'); await page.waitForSelector(`[data-cand="${PROXY_CANDIDATE_ID}"]`);
    const listText = await page.innerText(`[data-cand="${PROXY_CANDIDATE_ID}"]`);
    check('PROXY: learning list shows the proxy label', /PROXY: invalid for signal validation/i.test(listText));
    await page.click(`[data-cand="${PROXY_CANDIDATE_ID}"]`); await page.waitForSelector('#proxyWarn', { timeout: 20000 }); await countPrompts();
    const p1 = (await page.innerText('#app')).replace(/\s+/g, ' ');
    check('PROXY: warning "PROXY: INVALID FOR SIGNAL VALIDATION" shown', /PROXY: INVALID FOR SIGNAL VALIDATION/i.test(p1) && /measure that proxy, not the new signal/i.test(p1));
    check('PROXY: validation result marked as proxy, not signal validation', /PROXY RESULT Measured on the proxy adjustment/i.test(p1));
    check('PROXY: no "Approve V1 change" button', (await page.locator('[data-d="APPROVE"]').count()) === 0);
    const c = `cp_rl_session=${cookie.value}`;
    const denied = await postFull('/api/learning/candidate/decide', { candidate_id: PROXY_CANDIDATE_ID, decision: 'APPROVE', approver: 'walkthrough', acknowledge_unsupported: true }, { Cookie: c, Origin: BASE, 'X-CryptoPulse-Research': '1' });
    check('PROXY: approving it as a V1 change is refused (409)', denied.status === 409 && denied.json && denied.json.signal_validity === 'PROXY_INVALID_FOR_SIGNAL_VALIDATION', String(denied.status));
    await page.click('#toProto'); await page.waitForSelector('#newSignal');
    check('PROXY: switch fills the prototype editor (Risk Regime Shock, no weight/confidence)', (await page.inputValue('[data-a="signal_name"]')) === 'Risk Regime Shock' && (await page.locator('[data-a="weight"], [data-a="confidence"]').count()) === 0);
    await page.waitForTimeout(1500);
    const row1 = await candidateRow(PROXY_CANDIDATE_ID);
    check('PROXY: nothing stored before Save (adjustment, status, updated_ts, analysis unchanged)', JSON.stringify([row1.adjustment, row1.status, row1.updated_ts, row1.analysis]) === JSON.stringify([row0.adjustment, row0.status, row0.updated_ts, row0.analysis]));
    // #impactNotCalc already renders for the unsaved prototype; the save is done when the reloaded view drops the proxy warning.
    await page.click('#save'); await page.waitForSelector('#proxyWarn', { state: 'detached', timeout: 20000 }); await page.waitForSelector('#impactNotCalc'); await countPrompts();
    const row2 = await candidateRow(PROXY_CANDIDATE_ID);
    check('PROXY: after Save it is a SIGNAL_PROTOTYPE, DATA_COLLECTION_REQUIRED, no proxy label', row2.adjustment.type === 'SIGNAL_PROTOTYPE' && row2.status === 'DATA_COLLECTION_REQUIRED' && row2.signal_validity === null && !('weight' in row2.adjustment) && !('confidence' in row2.adjustment), row2.status);
    const hist = (row2.analysis && row2.analysis.history) || [];
    check('PROXY: the earlier proxy result is kept in history', hist.length >= 1 && hist[0].validation_status === row0.analysis.validation_status && hist[0].adjustment_text === row0.analysis.adjustment_text && hist[0].adjustment.derived_from === 'macrogeo' && hist[0].signal_validity === 'PROXY_INVALID_FOR_SIGNAL_VALIDATION');
    check('PROXY: no V1 methodology version created', (await learningState()).versions === versionsBefore);
  }

  // 4. A real, valid session cookie is still refused cross-origin, without the header, expired or tampered.
  const mid = await learningState();
  const c = `cp_rl_session=${cookie.value}`;
  check('valid cookie + foreign Origin -> 403', (await post('/api/learning/candidates', { event_id: EVENT_ID }, { Cookie: c, Origin: 'https://evil.example', 'X-CryptoPulse-Research': '1' })) === 403);
  check('valid cookie, no custom header -> 403', (await post('/api/learning/candidates', { event_id: EVENT_ID }, { Cookie: c, Origin: BASE })) === 403);
  check('valid cookie, no Origin -> 403', (await post('/api/learning/candidates', { event_id: EVENT_ID }, { Cookie: c, 'X-CryptoPulse-Research': '1' })) === 403);
  const tampered = cookie.value.slice(0, -1) + (cookie.value.endsWith('0') ? '1' : '0');
  check('tampered signature -> 401', (await post('/api/learning/candidates', { event_id: EVENT_ID }, { Cookie: `cp_rl_session=${tampered}`, Origin: BASE, 'X-CryptoPulse-Research': '1' })) === 401);
  const expired = 'v1.' + (Date.now() - 1000) + '.' + cookie.value.split('.')[2];
  check('expired session -> 401', (await post('/api/learning/candidates', { event_id: EVENT_ID }, { Cookie: `cp_rl_session=${expired}`, Origin: BASE, 'X-CryptoPulse-Research': '1' })) === 401);
  const after = await learningState();
  check('rejected requests created no rows', JSON.stringify(after) === JSON.stringify(mid), JSON.stringify(after));
  check('the journey created exactly one case and one candidate per walked event', after.candidates === before.candidates + (SIGNAL_EVENT_ID !== null ? 2 : 1) && after.case !== null && after.versions === before.versions);

  // 5. Existing behaviour unchanged.
  const health = await json(await fetch(`${BASE}/`));
  check('Worker health endpoint unchanged', health && health.ok === true && health.service === 'PulseWorkerV2');
  const dash = await json(await fetch(`${BASE}/api/research-lab/dashboard`));
  check('existing research-lab dashboard API unchanged', dash && dash.ok === true);
  check('advanced page still served', /Research Lab/.test(await (await fetch(`${BASE}/research-lab/advanced`)).text()));

  // 6. Sign out.
  await page.click('#sessionBox a[href="/research-lab/signout"]'); await page.waitForFunction(() => /Sign in this device/.test(document.getElementById('sessionBox').innerText));
  check('sign-out returns the page to read-only', true);
  check('no page errors', errors.length === 0, errors.join(' | '));
  await browser.close();
  const failed = results.filter((r) => r[0] === 'FAIL');
  console.log(`\nRESULT: ${failed.length ? 'FAIL' : 'PASS'} (${results.length - failed.length}/${results.length} checks)`);
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error('WALKTHROUGH ERROR:', e.message); process.exit(1); });
