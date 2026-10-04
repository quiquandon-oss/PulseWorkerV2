// STAGING ONLY. Run by the dispatch-only "staging-session-walkthrough" job in .github/workflows/test.yml.
// Drives the deployed staging Research Lab in Chromium as a real user: sign in once, then Market -> Research ->
// Confirm Finding -> Learning -> Create Candidate -> Save Adjustment -> V1 Impact -> Validation, checking that no token
// is ever requested again. It also proves that unauthenticated, cross-origin, header-less, expired and tampered
// writes are refused and create no rows. The admin token comes from the environment and is never printed.
const { chromium } = require('playwright');
const BASE = 'https://pulseworker-v2-staging.quiquandon.workers.dev';
const TOKEN = process.env.STAGING_ADMIN_TOKEN;
const EVENT_ID = Number(process.env.EVENT_ID || '999004');
if (!TOKEN) { console.error('STAGING_ADMIN_TOKEN missing'); process.exit(1); }
if (!BASE.includes('-staging.')) { console.error('refusing: not the staging Worker'); process.exit(1); }

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
const SAMPLE = `SAMPLE ANSWER for the staging session walkthrough (synthetic fixture event; not real research)
\`\`\`json
{ "explanation": "SAMPLE: staging fixture explanation.", "primary_driver": "Sample driver (session walkthrough)", "driver_category": "FLOWS",
  "finding_type": "SOURCE_WEIGHTING", "covered_by_existing_v1_source": "etfflows",
  "proposed_new_source": { "name": "", "url": "", "what_it_measures": "", "update_frequency": "", "free_or_paid": "" },
  "proposed_signal": "Lower ETF flow weight", "trend": "Sample",
  "evidence": [ { "claim": "SAMPLE claim", "url": "https://example.com/sample", "publisher": "Example", "date": "2026-10-04" } ],
  "inference": [], "speculation": [], "alternative_explanations": [], "limitations": "Sample only", "confidence": "LOW", "sentiment_assessment": "NEGATIVE" }
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
  check('the journey created exactly one case and one candidate', after.candidates === before.candidates + 1 && after.case !== null && after.versions === before.versions);

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
