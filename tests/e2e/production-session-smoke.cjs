// PRODUCTION smoke check for the signed device session. Run by the dispatch-only "production-session-smoke" job.
// It must never create or change data. Every session-authorized write probe targets event 999999999, which does not
// exist: the request passes authorizeWrite() and is then refused (404/409) before any INSERT. Learning-table counts
// are read before and after and must be identical. Event #15 is never touched. The token is never printed.
const { chromium } = require('playwright');
const BASE = 'https://pulseworker-v2.quiquandon.workers.dev';
const TOKEN = process.env.PROD_ADMIN_TOKEN;
const NO_EVENT = 999999999;
if (!TOKEN) { console.error('PROD_ADMIN_TOKEN missing'); process.exit(1); }

const results = [];
function check(name, cond, detail = '') { results.push(cond); console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  (' + detail + ')' : ''}`); }
const json = async (r) => { try { return await r.json(); } catch (_e) { return null; } };
async function state() {
  const c = await json(await fetch(`${BASE}/api/learning/candidates`));
  const v = await json(await fetch(`${BASE}/api/learning/versions`));
  const e15 = await json(await fetch(`${BASE}/api/learning/case?event_id=15`));
  return { candidates: c.candidates.length, versions: v.versions.map((x) => `${x.version_id}:${x.status}`).join(','), event15_case: e15.case ? e15.case.request_id : null };
}
async function post(path, body, headers) {
  const r = await fetch(BASE + path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
  return { status: r.status, body: await json(r) };
}

(async () => {
  const before = await state();
  console.log('learning state before:', JSON.stringify(before));

  // Unauthorized writes are refused.
  const fakeFinding = { explanation: 'smoke' };
  check('no credentials -> 401', (await post('/api/learning/findings', { event_id: NO_EVENT, finding: fakeFinding }, { Origin: BASE, 'X-CryptoPulse-Research': '1' })).status === 401);
  check('wrong Bearer -> 401', (await post('/api/learning/candidates', { event_id: NO_EVENT }, { Authorization: 'Bearer wrong' })).status === 401);
  check('right Bearer from a foreign Origin -> 403', (await post('/api/learning/candidates', { event_id: NO_EVENT }, { Authorization: `Bearer ${TOKEN}`, Origin: 'https://evil.example' })).status === 403);
  // Existing Bearer path still authorizes (stops at 409: no finding for that event).
  check('existing Bearer path still works (409 before any write)', (await post('/api/learning/candidates', { event_id: NO_EVENT }, { Authorization: `Bearer ${TOKEN}` })).status === 409);

  // Browser: pages load, token field gone, sign in once.
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage(); const errors = []; page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(`${BASE}/research-lab`); await page.waitForSelector('#act', { timeout: 30000 });
  check('/research-lab loads with real data', /What happened/i.test(await page.innerText('#app')));
  check('admin-token field is gone', (await page.locator('input[type=password]').count()) === 0);
  check('signed-out header offers "Sign in this device"', /Sign in this device/.test(await page.innerText('#sessionBox')));
  await page.goto(`${BASE}/research-lab/signin`);
  check('/research-lab/signin loads', /Sign in this device/.test(await page.innerText('body')));
  await page.fill('#token', TOKEN); await page.click('button[type=submit]');
  await page.waitForURL(`${BASE}/research-lab`); await page.waitForFunction(() => /Signed in on this device/.test(document.getElementById('sessionBox').innerText), null, { timeout: 30000 });
  const cookie = (await ctx.cookies(BASE)).find((c) => c.name === 'cp_rl_session');
  check('signed session cookie: HttpOnly, Secure, SameSite=Strict, ~180 days', !!cookie && cookie.httpOnly && cookie.secure && cookie.sameSite === 'Strict' && Math.abs(cookie.expires * 1000 - Date.now() - 180 * 86400000) < 3600000);
  check('page JavaScript cannot read the cookie', !(await page.evaluate(() => document.cookie)).includes('cp_rl_session'));
  check('no admin-token field after sign-in', (await page.locator('input[type=password]').count()) === 0);

  // Session-authorized save paths, probed WITHOUT writing (non-existent event).
  const c = `cp_rl_session=${cookie.value}`;
  const sess = { Cookie: c, Origin: BASE, 'X-CryptoPulse-Research': '1' };
  const f = await post('/api/learning/findings', { event_id: NO_EVENT, provider: 'chatgpt', finding: fakeFinding }, sess);
  check('Confirm Finding route accepts the signed session (stops at 404: no such event, nothing written)', f.status === 404 && f.body && f.body.error === 'event_not_found', `${f.status}`);
  const k = await post('/api/learning/candidates', { event_id: NO_EVENT }, sess);
  check('Create Learning Candidate route accepts the signed session (stops at 409, nothing written)', k.status === 409, `${k.status}`);
  check('valid cookie + foreign Origin -> 403', (await post('/api/learning/candidates', { event_id: NO_EVENT }, { ...sess, Origin: 'https://evil.example' })).status === 403);
  check('valid cookie without the custom header -> 403', (await post('/api/learning/candidates', { event_id: NO_EVENT }, { Cookie: c, Origin: BASE })).status === 403);
  const tampered = cookie.value.slice(0, -1) + (cookie.value.endsWith('0') ? '1' : '0');
  check('tampered signature -> 401', (await post('/api/learning/candidates', { event_id: NO_EVENT }, { ...sess, Cookie: `cp_rl_session=${tampered}` })).status === 401);
  const expired = 'v1.' + (Date.now() - 1000) + '.' + cookie.value.split('.')[2];
  check('expired session -> 401', (await post('/api/learning/candidates', { event_id: NO_EVENT }, { ...sess, Cookie: `cp_rl_session=${expired}` })).status === 401);

  // Existing behaviour.
  const health = await json(await fetch(`${BASE}/`));
  check('Worker health unchanged', health && health.ok === true && health.service === 'PulseWorkerV2');
  const dash = await json(await fetch(`${BASE}/api/research-lab/dashboard`));
  check('existing dashboard API unchanged', dash && dash.ok === true && dash.research_events_count === 15, `events=${dash && dash.research_events_count}`);
  const db = await json(await fetch(`${BASE}/db-check`));
  check('V1 history readable (/db-check)', db && db.ok === true && db.history && db.history.cnt > 0, `history=${db && db.history && db.history.cnt}`);
  check('/research-lab/advanced served', /Research Lab/.test(await (await fetch(`${BASE}/research-lab/advanced`)).text()));

  // Sign out; no data changed.
  await page.click('#sessionBox a[href="/research-lab/signout"]'); await page.waitForFunction(() => /Sign in this device/.test(document.getElementById('sessionBox').innerText));
  check('sign-out works', true);
  check('no page errors', errors.length === 0, errors.join(' | '));
  await browser.close();
  const after = await state();
  console.log('learning state after: ', JSON.stringify(after));
  check('learning tables unchanged; Event #15 untouched', JSON.stringify(after) === JSON.stringify(before));
  const failed = results.filter((r) => !r).length;
  console.log(`\nRESULT: ${failed ? 'FAIL' : 'PASS'} (${results.length - failed}/${results.length} checks)`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error('SMOKE ERROR:', e.message); process.exit(1); });
