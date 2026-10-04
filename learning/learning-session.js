// Single-user write authorization for the Research Lab.
//
// A browser signs in once per device at /research-lab/signin by proving it knows STAGE7_ADMIN_TOKEN. The Worker then
// sets a signed session cookie (HttpOnly; Secure; SameSite=Strict; 180 days). The cookie holds only an expiry and an
// HMAC-SHA256 signature. The signing key is derived from STAGE7_ADMIN_TOKEN, so rotating that secret invalidates every
// session at once. No server-side state and no schema change.
//
// authorizeWrite() is the single gate for every learning write. It accepts:
//   * a valid Bearer token (CI / smoke checks; any Origin header present must be this Worker's own), or
//   * a valid session cookie AND an Origin equal to this Worker's origin AND the X-CryptoPulse-Research: 1 header.
// The custom header cannot be sent cross-origin without a CORS preflight, which this Worker does not grant (it only
// allows Content-Type), and SameSite=Strict keeps the cookie off cross-site requests: two independent CSRF defences.
// Anything else is refused before any database access.

export const SESSION_COOKIE = 'cp_rl_session';
export const SESSION_MAX_AGE_S = 180 * 24 * 3600;
export const WRITE_HEADER = 'X-CryptoPulse-Research';
const SESSION_VERSION = 'v1';
const SIGNING_CONTEXT = 'cryptopulse-research-lab-session|';

export function constantTimeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const n = Math.max(a.length, b.length);
  let diff = a.length === b.length ? 0 : 1;
  for (let i = 0; i < n; i++) diff |= (i < a.length ? a.charCodeAt(i) : 0) ^ (i < b.length ? b.charCodeAt(i) : 0);
  return diff === 0;
}

async function sign(adminToken, message) {
  const enc = new TextEncoder();
  // Derived key: HMAC(adminToken, context) -> session key, so the raw admin token is never the direct signing key.
  const rootKey = await crypto.subtle.importKey('raw', enc.encode(adminToken), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const derived = await crypto.subtle.sign('HMAC', rootKey, enc.encode(SIGNING_CONTEXT + SESSION_VERSION));
  const key = await crypto.subtle.importKey('raw', derived, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function createSessionValue(adminToken, now = Date.now()) {
  const exp = now + SESSION_MAX_AGE_S * 1000;
  return `${SESSION_VERSION}.${exp}.${await sign(adminToken, `${SESSION_VERSION}.${exp}`)}`;
}

export function readCookie(request, name = SESSION_COOKIE) {
  const raw = request.headers.get('Cookie') || '';
  for (const part of raw.split(';')) {
    const i = part.indexOf('=');
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return null;
}

// Returns { valid, expires_ts }.
export async function verifySessionValue(adminToken, value, now = Date.now()) {
  if (!adminToken || typeof value !== 'string') return { valid: false };
  const m = /^v1\.(\d{13})\.([0-9a-f]{64})$/.exec(value);
  if (!m) return { valid: false };
  const exp = Number(m[1]);
  if (!(exp > now) || exp > now + SESSION_MAX_AGE_S * 1000 + 60000) return { valid: false };
  const expected = await sign(adminToken, `${SESSION_VERSION}.${m[1]}`);
  return constantTimeEqual(m[2], expected) ? { valid: true, expires_ts: exp } : { valid: false };
}

export function sessionCookieHeader(value) {
  return `${SESSION_COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_MAX_AGE_S}`;
}
export function clearedCookieHeader() {
  return `${SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`;
}

function sameOrigin(request) {
  const origin = request.headers.get('Origin');
  return { present: origin !== null, matches: origin !== null && origin === new URL(request.url).origin };
}

export async function getSessionState(env, request, now = Date.now()) {
  const s = await verifySessionValue(env.STAGE7_ADMIN_TOKEN, readCookie(request), now);
  return { signed_in: s.valid, expires_ts: s.valid ? s.expires_ts : null, writes_configured: !!env.STAGE7_ADMIN_TOKEN };
}

// The single gate for learning writes. { ok: true, via } or { ok: false, status, error }.
export async function authorizeWrite(env, request, now = Date.now()) {
  if (!env.STAGE7_ADMIN_TOKEN) return { ok: false, status: 503, error: 'Writing is disabled on this Worker: STAGE7_ADMIN_TOKEN is not configured.' };
  const origin = sameOrigin(request);
  const auth = request.headers.get('Authorization') || '';
  if (auth.startsWith('Bearer ')) {
    if (origin.present && !origin.matches) return { ok: false, status: 403, error: 'Cross-origin write refused.' };
    return constantTimeEqual(auth.slice(7), env.STAGE7_ADMIN_TOKEN) ? { ok: true, via: 'bearer' } : { ok: false, status: 401, error: 'Unauthorized' };
  }
  const session = await verifySessionValue(env.STAGE7_ADMIN_TOKEN, readCookie(request), now);
  if (!session.valid) return { ok: false, status: 401, error: 'Not signed in on this device. Open /research-lab/signin once.' };
  if (!origin.matches) return { ok: false, status: 403, error: 'Cross-origin write refused.' };
  if (request.headers.get(WRITE_HEADER) !== '1') return { ok: false, status: 403, error: `Missing ${WRITE_HEADER} header.` };
  return { ok: true, via: 'session' };
}

// POST /research-lab/signin body {token}: same-origin + custom header + correct token -> Set-Cookie.
export async function signIn(env, request, body, now = Date.now()) {
  if (!env.STAGE7_ADMIN_TOKEN) return { ok: false, status: 503, error: 'Writing is disabled on this Worker: STAGE7_ADMIN_TOKEN is not configured.' };
  if (!sameOrigin(request).matches || request.headers.get(WRITE_HEADER) !== '1') return { ok: false, status: 403, error: 'Sign-in must come from this page.' };
  const token = body && typeof body.token === 'string' ? body.token : '';
  if (!token || !constantTimeEqual(token, env.STAGE7_ADMIN_TOKEN)) return { ok: false, status: 401, error: 'Wrong token.' };
  const value = await createSessionValue(env.STAGE7_ADMIN_TOKEN, now);
  return { ok: true, status: 200, setCookie: sessionCookieHeader(value), expires_ts: now + SESSION_MAX_AGE_S * 1000 };
}

export const SIGNIN_HTML = `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign in this device</title>
<style>
  :root { --bg:#0f1117; --card:#171a23; --line:#262b38; --text:#e7e9ee; --muted:#9aa3b2; --accent:#5b8cff; --bad:#e2555a; --good:#2fbf71; }
  @media (prefers-color-scheme: light) { :root { --bg:#f6f7f9; --card:#fff; --line:#e3e6ec; --text:#141821; --muted:#5d6676; } }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  main { max-width:420px; margin:48px auto; padding:0 16px; } .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; }
  h1 { font-size:20px; margin:0 0 6px; } p { color:var(--muted); font-size:14px; }
  input { width:100%; box-sizing:border-box; background:var(--bg); color:var(--text); border:1px solid var(--line); border-radius:8px; padding:10px; font:inherit; }
  button { margin-top:12px; width:100%; background:var(--accent); color:#fff; border:0; border-radius:10px; padding:12px; font:inherit; font-weight:650; cursor:pointer; }
  #msg { margin-top:10px; font-size:13px; } a { color:var(--accent); }
</style></head>
<body><main><div class="card">
<h1>Sign in this device</h1>
<p>Enter the Research Lab admin token once. This browser then stays signed in for 180 days, and you will not be asked again. The token is not stored by the page; only a signed, HttpOnly session cookie is kept.</p>
<form id="f"><input type="password" id="token" autocomplete="current-password" placeholder="admin token" required>
<button type="submit">Sign in</button></form>
<div id="msg"></div>
<p><a href="/research-lab">&larr; Back to Research Lab</a></p>
</div></main>
<script>
(function () {
  document.getElementById('f').onsubmit = function (e) {
    e.preventDefault();
    var msg = document.getElementById('msg'); msg.textContent = 'Signing in...';
    fetch('/research-lab/signin', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CryptoPulse-Research': '1' },
      body: JSON.stringify({ token: document.getElementById('token').value }) })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        document.getElementById('token').value = '';
        if (r.ok) { msg.style.color = 'var(--good)'; msg.textContent = 'Signed in. Opening Research Lab...'; window.location.href = '/research-lab'; }
        else { msg.style.color = 'var(--bad)'; msg.textContent = r.error || 'Sign-in failed.'; }
      }, function () { msg.style.color = 'var(--bad)'; msg.textContent = 'Network error.'; });
  };
})();
</script></body></html>`;
