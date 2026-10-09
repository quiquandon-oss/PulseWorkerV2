// Research Lab primary UI: MARKET -> RESEARCH -> LEARNING. The previous 12-tab technical page is unchanged and lives
// at /research-lab/advanced. Plain ES5 + fetch; every value from the API (including pasted AI text) is HTML-escaped,
// and only http(s) URLs are ever rendered as links. The page never handles the admin token: writes ride on the device's
// signed HttpOnly session cookie (see learning-session.js).
export const LEARNING_LAB_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>CryptoPulse Research</title>
<style>
  :root { --bg:#0f1117; --card:#171a23; --line:#262b38; --text:#e7e9ee; --muted:#9aa3b2; --accent:#5b8cff; --good:#2fbf71; --warn:#e3a33b; --bad:#e2555a; }
  @media (prefers-color-scheme: light) { :root { --bg:#f6f7f9; --card:#fff; --line:#e3e6ec; --text:#141821; --muted:#5d6676; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 16px 0; max-width:880px; margin:0 auto; display:flex; gap:12px; align-items:flex-start; justify-content:space-between; flex-wrap:wrap; }
  h1 { font-size:20px; margin:0; } .sub { color:var(--muted); font-size:13px; margin:2px 0 12px; }
  .lock { font-size:12px; display:flex; gap:6px; align-items:center; } .lock input { width:150px; padding:6px 8px; }
  nav { display:flex; gap:6px; border-bottom:1px solid var(--line); max-width:880px; margin:0 auto; padding:0 16px; overflow-x:auto; }
  nav button, nav a { background:none; border:0; color:var(--muted); font:inherit; font-weight:600; padding:10px 12px; min-height:44px; cursor:pointer; border-bottom:2px solid transparent; text-decoration:none; white-space:nowrap; }
  nav button.active { color:var(--text); border-bottom-color:var(--accent); } nav a { margin-left:auto; font-weight:500; font-size:13px; display:flex; align-items:center; }
  nav button span { display:block; font-size:11px; font-weight:500; color:var(--muted); } @media (max-width:600px) { nav button span { display:none; } nav { padding:0 8px; } nav button, nav a { padding:10px 9px; } }
  button:focus-visible, a:focus-visible, summary:focus-visible, select:focus-visible, input:focus-visible, textarea:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .lead { font-size:14px; margin:0 0 10px; } .next { border-left:3px solid var(--accent); padding:4px 0 4px 10px; margin:12px 0 0; font-size:14px; }
  table.sum td:first-child { width:120px; color:var(--muted); font-size:13px; } .src { font-size:11px; font-weight:650; letter-spacing:.05em; text-transform:uppercase; }
  .src.ver { color:var(--good); } .src.ai { color:var(--warn); } .src.hum { color:var(--accent); }
  details.card > summary { font-size:13px; letter-spacing:.06em; text-transform:uppercase; } details.card[open] > summary { margin-bottom:10px; }
  main { max-width:880px; margin:0 auto; padding:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:14px; }
  .card h2 { font-size:13px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); margin:0 0 10px; }
  .headline { font-size:19px; font-weight:650; margin:0 0 6px; } .muted { color:var(--muted); } .small { font-size:13px; }
  .row { display:flex; gap:12px; flex-wrap:wrap; } .kv { flex:1 1 120px; } .kv .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; } .kv .v { font-size:20px; font-weight:650; }
  .chip { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:650; border:1px solid var(--line); }
  .c-good { color:var(--good); border-color:var(--good); } .c-warn { color:var(--warn); border-color:var(--warn); } .c-bad { color:var(--bad); border-color:var(--bad); } .c-muted { color:var(--muted); } .c-acc { color:var(--accent); border-color:var(--accent); }
  table { width:100%; border-collapse:collapse; } td { padding:7px 4px; border-top:1px solid var(--line); vertical-align:top; } td.icon { width:28px; font-weight:700; text-align:center; }
  .btn { display:inline-block; background:var(--accent); color:#fff; border:0; border-radius:10px; padding:11px 16px; min-height:44px; font:inherit; font-weight:650; cursor:pointer; text-decoration:none; }
  .btn.secondary { background:none; color:var(--text); border:1px solid var(--line); } .btn.bad { background:var(--bad); } .btn:disabled { opacity:.45; cursor:not-allowed; }
  .btns { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  .journey td { border-top:1px solid var(--line); } .journey td.lbl { font-weight:650; width:110px; } .journey td.st { width:30px; text-align:center; font-weight:700; }
  .ev { padding:10px 0; border-top:1px solid var(--line); cursor:pointer; } .ev:first-child { border-top:0; } .ev.sel { background:linear-gradient(90deg, rgba(91,140,255,.12), transparent); }
  textarea, input, select { width:100%; background:var(--bg); color:var(--text); border:1px solid var(--line); border-radius:8px; padding:9px; font:inherit; font-size:14px; }
  textarea { min-height:80px; } label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; text-transform:uppercase; letter-spacing:.05em; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:0 12px; } @media (max-width:600px) { .grid2 { grid-template-columns:1fr; } }
  .warn { color:var(--warn); font-size:13px; } .err { color:var(--bad); font-size:13px; } .ok { color:var(--good); font-size:13px; }
  .big { border:1px dashed var(--warn); border-radius:12px; padding:14px; } .big b { color:var(--warn); }
  details summary { cursor:pointer; color:var(--muted); font-size:13px; } pre { white-space:pre-wrap; font-size:12px; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:10px; max-height:320px; overflow:auto; }
  ul { margin:6px 0; padding-left:20px; } a { color:var(--accent); } svg { width:100%; height:auto; display:block; }
</style>
</head>
<body>
<header><div><h1>CryptoPulse Research</h1><div class="sub">Understand the market &rarr; discover what V1 is missing &rarr; improve V1</div></div>
<div class="lock" id="sessionBox"><span class="muted">Checking sign-in&hellip;</span></div></header>
<nav id="nav"></nav>
<main id="app"><div class="card muted">Loading&hellip;</div></main>
<script>
(function () {
  var TABS = ['Market', 'Research', 'Learning'];
  var current = 'Market', market = null, focusId = null, caseCache = {}, draft = null, draftWarnings = [], candId = null, cand = null, versions = null, signedIn = false, editorOpen = null, dirty = false;
  var nav = document.getElementById('nav'), app = document.getElementById('app');
  function renderSession() {
    document.getElementById('sessionBox').innerHTML = signedIn
      ? '<span>&#128275; Signed in on this device</span> <a href="/research-lab/signout">Sign out</a>'
      : '<span>&#128274; Read-only</span> <a href="/research-lab/signin">Sign in this device</a>';
  }
  fetch('/api/learning/session', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) { signedIn = !!(d && d.signed_in); renderSession(); if (market && !draft) render(); }, function () { renderSession(); });

  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function link(url, text) { return /^https?:\\/\\//i.test(url || '') ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(text || url) + '</a>' : esc(text || ''); }
  function day(ts) { return ts ? new Date(ts).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : ''; }
  function getJson(path) { return fetch(path).then(function (r) { return r.json(); }); }
  function postJson(path, body) {
    // Writes are authorized server-side by this device's signed, HttpOnly session cookie (sent automatically, same
    // origin only) plus this header. No token is ever handled by the page.
    return fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CryptoPulse-Research': '1' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); });
  }
  // Shown only to a read-only (signed-out) visitor next to actions that save something.
  function roHint() { return signedIn ? '' : '<p class="small muted ro-hint">Read-only on this device: <a href="/research-lab/signin">sign in</a> to save changes or record a decision.</p>'; }
  function needToken(msgEl) { if (signedIn) return false; msgEl.className = 'small err'; msgEl.innerHTML = 'This device is not signed in. <a href="/research-lab/signin">Sign in once</a>, then save.'; return true; }
  var VERDICT_CLASS = { EXPLAINED: 'c-good', PARTIALLY_EXPLAINED: 'c-warn', NOT_EXPLAINED: 'c-bad' };
  var ICON = { EXPLAINS: ['&#10003;', 'c-good'], PARTIAL: ['?', 'c-warn'], CONTRADICTS: ['&#10005;', 'c-bad'], SILENT: ['&ndash;', 'c-muted'], MISSING: ['&#8709;', 'c-muted'], NOT_APPLICABLE: ['&ndash;', 'c-muted'] };
  var STATUS_TEXT = { EXPLAINS: 'explains it', PARTIAL: 'partly', CONTRADICTS: 'pointed the other way', SILENT: 'silent', MISSING: 'no reading', NOT_APPLICABLE: 'n/a' };
  var JICON = { DONE: ['&#10003;', 'c-good'], ACTIVE: ['&#9679;', 'c-acc'], WARN: ['&#9888;', 'c-warn'], TODO: ['&#9675;', 'c-muted'], LOCKED: ['&#128274;', 'c-muted'] };
  var VCLASS = { AWAITING_HOLDOUT: 'c-acc', DATA_REQUIRED: 'c-warn', SUPPORTED: 'c-good', NOT_SUPPORTED: 'c-bad', INCONCLUSIVE: 'c-warn', VALIDATING: 'c-acc', NOT_ENOUGH_DATA: 'c-muted' };
  // Plain wording for the backend's candidate statuses (same states, nothing added or merged).
  var CSTATUS_TEXT = { DRAFT: 'Draft', DATA_COLLECTION_REQUIRED: 'Data collection required', PENDING_REVIEW: 'Ready for review', NEEDS_MORE_RESEARCH: 'More research requested', ACCEPTED: 'Approved (not active)', DATA_COLLECTION_APPROVED: 'Data collection approved', REJECTED: 'Rejected' };
  // Same text as the backend's VALIDATION_TEXT (learning-method.js); kept identical by a test.
  var VTEXT = { DATA_REQUIRED: 'Not supported yet: data required', NOT_ENOUGH_DATA: 'Not enough data', AWAITING_HOLDOUT: 'Awaiting out-of-sample data', VALIDATING: 'Validating', SUPPORTED: 'Supported', NOT_SUPPORTED: 'Not supported', INCONCLUSIVE: 'Inconclusive' };
  var FINAL_VALIDATION = ['SUPPORTED', 'NOT_SUPPORTED', 'INCONCLUSIVE'];
  function cstatus(st) { return CSTATUS_TEXT[st] || String(st || '').replace(/_/g, ' '); }
  function vtext(st) { return VTEXT[st] || String(st || '').replace(/_/g, ' '); }
  function plain(t) { return String(t || '').replace(/\\b[A-Z][A-Z_]{3,}\\b/g, function (m) { return VTEXT[m] || CSTATUS_TEXT[m] || m; }); }
  var CSTATUS_CLASS = { DATA_COLLECTION_REQUIRED: 'c-warn', DATA_COLLECTION_APPROVED: 'c-good', ACCEPTED: 'c-good', REJECTED: 'c-bad', PENDING_REVIEW: 'c-acc', NEEDS_MORE_RESEARCH: 'c-warn', DRAFT: 'c-muted' };
  var ROLES = ['REGIME_MODIFIER','DIRECTIONAL_SIGNAL','CONFIRMATION_FILTER','UNSPECIFIED'];
  var ROLE_TEXT = { REGIME_MODIFIER: 'Regime modifier', DIRECTIONAL_SIGNAL: 'Directional signal', CONFIRMATION_FILTER: 'Confirmation filter', UNSPECIFIED: 'Role not set' };
  var GROUPS = { FLOWS: 'ETF flows', DERIVATIVES: 'Funding & positioning', MACRO: 'Macro', EQUITIES: 'Equities', NEWS: 'News & narrative', BREADTH: 'Market breadth', ONCHAIN: 'On-chain', TREASURY: 'Corporate treasuries' };
  function chip(text, cls) { return '<span class="chip ' + (cls || 'c-muted') + '">' + esc(text) + '</span>'; }
  function dirWord(d) { return d === 'UP' || d === 'DOWN' || d === 'NEUTRAL' || d === 'FLAT' ? d : 'unknown'; }
  function opt(values, sel, labels) { return values.map(function (v, i) { return '<option value="' + esc(v) + '"' + (v === sel ? ' selected' : '') + '>' + esc(labels ? labels[i] : v) + '</option>'; }).join(''); }

  var TAB_HINT = { Market: 'What happened', Research: 'Find what is missing', Learning: 'Improve V1' };
  function renderNav() {
    nav.innerHTML = TABS.map(function (t) { return '<button data-tab="' + t + '" class="' + (t === current ? 'active' : '') + '"' + (t === current ? ' aria-current="page"' : '') + '>' + t + '<span>' + TAB_HINT[t] + '</span></button>'; }).join('') + '<a href="/research-lab/advanced" title="Data, experiments and diagnostics">Advanced &rarr;</a>';
    var b = nav.querySelectorAll('button');
    for (var i = 0; i < b.length; i++) b[i].onclick = function (e) { current = e.currentTarget.dataset.tab; if (current !== 'Learning') candId = null; render(); };
  }
  function journeyCard(steps, title) {
    return '<div class="card"><h2>' + esc(title || 'Where am I?') + '</h2><table class="journey">' + steps.map(function (s) {
      var ic = JICON[s.state] || JICON.TODO;
      return '<tr><td class="st ' + ic[1] + '">' + ic[0] + '</td><td class="lbl">' + esc(s.label) + '</td><td class="' + (s.state === 'LOCKED' || s.state === 'TODO' ? 'muted' : '') + '">' + esc(plain(s.text)) + '</td></tr>';
    }).join('') + '</table></div>';
  }
  function focusEvent() { if (!market) return null; for (var i = 0; i < market.events.length; i++) if (market.events[i].event_id === focusId) return market.events[i]; return null; }
  function groupTable(groups) {
    return '<table>' + groups.map(function (g) { var ic = ICON[g.status] || ICON.SILENT; return '<tr><td class="icon ' + ic[1] + '">' + ic[0] + '</td><td>' + esc(g.label) + '</td><td class="muted small" style="text-align:right">' + esc(STATUS_TEXT[g.status] || '') + '</td></tr>'; }).join('') + '</table>';
  }
  function nextAction(e) {
    if (e.candidate) return ['Open learning candidate #' + e.candidate.candidate_id + ' &rarr;', function () { openCandidate(e.candidate.candidate_id); }];
    if (e.case && e.case.finding && e.case.finding.validation_status === 'VALIDATED') return ['Create learning candidate &rarr;', function () { createCandidate(e.event_id, document.getElementById('actMsg')); }];
    return [e.case && e.case.finding ? 'See the finding &rarr;' : 'Investigate why &rarr;', function () { current = 'Research'; render(); }];
  }

  // ---------------- MARKET ----------------
  function renderMarket() {
    if (!market) { app.innerHTML = '<div class="card muted">Loading&hellip;</div>'; return; }
    if (!market.ok) { app.innerHTML = '<div class="card err">Could not load market data: ' + esc(market.error) + '</div>'; return; }
    var e = focusEvent();
    var v1now = market.latest.v1 ? Number(market.latest.v1.score) : null;
    var html = '<div class="card"><h2>Right now</h2><div class="row"><div class="kv"><div class="k">BTC</div><div class="v">' + (market.latest.btc ? '$' + Math.round(market.latest.btc.btc_price).toLocaleString('en-US') : '&ndash;') + '</div></div>' +
      '<div class="kv"><div class="k">V1 sentiment</div><div class="v">' + (v1now !== null ? esc(v1now) + ' / 100' : '&ndash;') + '</div></div></div>' +
      '<p class="small muted" style="margin:8px 0 0">V1 reads 50 as neutral: higher leans bullish, lower leans bearish' + (market.latest.v1 ? ' (latest reading ' + esc(day(market.latest.v1.ts)) + ')' : '') + '.</p></div>';
    var guide = '<details class="card"><summary>How this works</summary><ol class="small">' +
      '<li><b>Market</b>: pick an event and see whether V1\\'s current sources explain it.</li>' +
      '<li><b>Research</b>: copy the Research Pack, ask ChatGPT, Claude, Gemini or Grok yourself, paste the answer back and check it.</li>' +
      '<li><b>Learning</b>: a finding you confirm can become a learning candidate. You decide on it; nothing changes production V1 automatically.</li></ol>' +
      '<p class="small muted">Saving needs this device to be signed in once (top right). It then stays signed in for 180 days.</p></details>';
    if (!e) { app.innerHTML = html + '<div class="card"><h2>No events yet</h2><p class="small">CryptoPulse has not detected a market event to look at. Events appear here automatically when BTC makes a large move or becomes unusually volatile.</p></div>' + guide; return; }
    var act = nextAction(e);
    html += '<div class="card"><h2>What happened</h2><p class="headline">' + esc(e.headline) + '</p><p class="muted small">' + esc(e.btc_move_text) + '</p>' +
      '<div class="row" style="margin-top:8px"><div class="kv"><div class="k">V1 expected</div><div class="v">' + dirWord(e.v1_lean_before) + '</div></div><div class="kv"><div class="k">Actual</div><div class="v">' + dirWord(e.actual_direction) + '</div></div><div class="kv"><div class="k">News collected</div><div class="v">' + esc(e.evidence_count) + '</div></div></div></div>';
    html += '<div class="card"><h2>Did V1\\'s sources explain it?</h2><p class="lead">' + chip(e.verdict_text, VERDICT_CLASS[e.verdict]) + ' <span class="muted small">' + esc(e.explained_share) + '% of V1\\'s source weight pointed the way BTC actually moved.</span></p>' +
      '<details><summary>What each group of V1 sources said (' + esc(e.groups.length) + ')</summary>' + groupTable(e.groups) + '</details>' +
      '<div class="btns"><button class="btn" id="act">' + act[0] + '</button></div><div id="actMsg" class="small"></div></div>';
    html += journeyCard(e.journey, 'Progress on this event');
    html += '<div class="card"><h2>Recent events</h2>' + market.events.map(function (x) {
      var last = x.journey[x.journey.length - 1], cur = null; for (var i = 0; i < x.journey.length; i++) if (x.journey[i].state !== 'DONE') { cur = x.journey[i]; break; }
      return '<div class="ev ' + (x.event_id === focusId ? 'sel' : '') + '" data-id="' + x.event_id + '" role="button" tabindex="0"><div>' + esc(x.headline) + '</div><div class="small" style="margin-top:3px">' + chip(x.verdict_text, VERDICT_CLASS[x.verdict]) + ' ' + chip(plain(cur ? cur.label + ': ' + cur.text : last.text), cur ? 'c-muted' : 'c-good') + '</div></div>';
    }).join('') + '</div>' + guide;
    app.innerHTML = html;
    document.getElementById('act').onclick = act[1];
    var evs = app.querySelectorAll('.ev');
    for (var i = 0; i < evs.length; i++) {
      evs[i].onclick = function (ev) { focusId = Number(ev.currentTarget.dataset.id); draft = null; render(); window.scrollTo(0, 0); };
      evs[i].onkeydown = function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); ev.currentTarget.click(); } };
    }
  }

  // ---------------- RESEARCH ----------------
  function findingView(f, meta) {
    var src = f.proposed_new_source || {};
    var h = '<p class="headline" style="font-size:16px">' + esc(f.primary_driver || 'Finding') + '</p><p>' + esc(f.explanation) + '</p><table>' +
      '<tr><td class="muted small">Type</td><td>' + esc(FTYPE_TEXT[f.finding_type] || f.finding_type) + '</td></tr>' +
      '<tr><td class="muted small">Already in V1?</td><td>' + esc(f.covered_by_existing_v1_source === 'none' ? 'No' : 'Partly, via a current V1 source') + '</td></tr>' +
      (src.name ? '<tr><td class="muted small">Potential new source</td><td>' + link(src.url, src.name) + (src.what_it_measures ? '<div class="small muted">' + esc(src.what_it_measures) + '</div>' : '') + '</td></tr>' : '') +
      (f.proposed_signal ? '<tr><td class="muted small">Potential signal</td><td>' + esc(f.proposed_signal) + '</td></tr>' : '') +
      (f.trend ? '<tr><td class="muted small">Trend</td><td>' + esc(f.trend) + '</td></tr>' : '') +
      '<tr><td class="muted small">Confidence</td><td>' + esc(f.confidence) + '</td></tr></table>';
    if (f.evidence && f.evidence.length) h += '<label>Evidence cited (check each link)</label><ul>' + f.evidence.map(function (x) { return '<li>' + esc(x.claim) + ' ' + (x.url ? '(' + link(x.url, x.publisher || 'source') + (x.date ? ', ' + esc(x.date) : '') + ')' : '<span class="warn">(no link)</span>') + '</li>'; }).join('') + '</ul>';
    if (f.inference && f.inference.length) h += '<label>Inference (reasoning, not evidence)</label><ul class="small">' + f.inference.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>';
    if (f.speculation && f.speculation.length) h += '<label>Speculation (unverified)</label><ul class="small">' + f.speculation.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>';
    if (f.alternative_explanations && f.alternative_explanations.length) h += '<label>Alternative explanations</label><ul class="small">' + f.alternative_explanations.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>';
    if (meta) h += '<p class="small muted">Researched with ' + esc(meta.provider || 'an external AI') + ' &middot; confirmed ' + esc(day(meta.registered_ts)) + '</p>';
    return h;
  }
  var FTYPE_TEXT = { NEW_SOURCE: 'New source', NEW_TREND: 'New trend', NEW_SIGNAL: 'New signal', MISSING_DRIVER: 'Missing driver', SOURCE_CLASSIFICATION: 'Source classification', SOURCE_WEIGHTING: 'Source weighting', REGIME_SPECIFIC: 'Regime-specific', NO_CONVINCING_EXPLANATION: 'No convincing explanation', EXISTING_SOURCE_MISREAD: 'Existing source misread', NO_NEW_DRIVER: 'No new driver' };
  var CATS = ['OPTIONS','LIQUIDATIONS','STABLECOIN_FLOWS','WHALES_MINERS','SPECIFIC_CATALYST','FLOWS','DERIVATIVES','MACRO','EQUITIES','NEWS','BREADTH','ONCHAIN','TREASURY','OTHER'];
  var FTYPES = ['NEW_SOURCE','NEW_TREND','NEW_SIGNAL','MISSING_DRIVER','SOURCE_CLASSIFICATION','SOURCE_WEIGHTING','REGIME_SPECIFIC','NO_CONVINCING_EXPLANATION','EXISTING_SOURCE_MISREAD','NO_NEW_DRIVER'];
  function draftForm(c) {
    var f = draft, s = f.proposed_new_source || {};
    var v1ids = ['none'].concat(c.assessment.sources.map(function (x) { return x.id; }));
    return '<div class="card"><h2>4. Check the AI\\'s finding</h2><p class="small"><span class="src ai">AI hypothesis &middot; not confirmed</span> Nothing is saved yet. Correct anything that is wrong and remove claims you cannot verify.</p>' + (draftWarnings.length ? '<ul>' + draftWarnings.map(function (w) { return '<li class="warn">' + esc(w) + '</li>'; }).join('') + '</ul>' : '') +
      '<label>What explains the move</label><textarea data-f="explanation">' + esc(f.explanation) + '</textarea>' +
      '<div class="grid2"><div><label>Primary driver</label><input data-f="primary_driver" value="' + esc(f.primary_driver) + '"></div><div><label>Driver category</label><select data-f="driver_category">' + opt(CATS, f.driver_category) + '</select></div>' +
      '<div><label>Finding type</label><select data-f="finding_type">' + opt(FTYPES, f.finding_type, FTYPES.map(function (k) { return FTYPE_TEXT[k]; })) + '</select></div><div><label>Already covered by V1 source</label><select data-f="covered_by_existing_v1_source">' + opt(v1ids, f.covered_by_existing_v1_source) + '</select></div>' +
      '<div><label>Potential new source</label><input data-s="name" value="' + esc(s.name) + '"></div><div><label>Source URL</label><input data-s="url" value="' + esc(s.url) + '"></div></div>' +
      '<label>What the source measures</label><input data-s="what_it_measures" value="' + esc(s.what_it_measures) + '">' +
      '<label>Potential signal (how it becomes a 0-100 reading)</label><textarea data-f="proposed_signal">' + esc(f.proposed_signal) + '</textarea>' +
      '<div class="grid2"><div><label>New signal: reusable name</label><input data-f="proposed_signal_name" value="' + esc(f.proposed_signal_name) + '"></div><div><label>New signal: role in V1</label><select data-f="proposed_signal_role">' + opt([''].concat(ROLES), f.proposed_signal_role || '', ['(not set)'].concat(ROLES.map(function (r) { return ROLE_TEXT[r]; }))) + '</select></div></div>' +
      '<label>New signal: required inputs &mdash; one per line</label><textarea data-list="required_inputs">' + esc((f.required_inputs || []).join('\\n')) + '</textarea>' +
      '<label>Trend</label><input data-f="trend" value="' + esc(f.trend) + '">' +
      '<label>Evidence &mdash; one per line: claim | url | publisher | date</label><textarea data-list="evidence">' + esc((f.evidence || []).map(function (e) { return [e.claim, e.url, e.publisher, e.date].join(' | '); }).join('\\n')) + '</textarea>' +
      '<label>Inference &mdash; one per line</label><textarea data-list="inference">' + esc((f.inference || []).join('\\n')) + '</textarea>' +
      '<label>Speculation (unverified) &mdash; one per line</label><textarea data-list="speculation">' + esc((f.speculation || []).join('\\n')) + '</textarea>' +
      '<label>Alternative explanations &mdash; one per line</label><textarea data-list="alternative_explanations">' + esc((f.alternative_explanations || []).join('\\n')) + '</textarea>' +
      '<div class="grid2"><div><label>Confidence</label><select data-f="confidence">' + opt(['LOW','MEDIUM','HIGH'], f.confidence) + '</select></div><div><label>Sentiment for BTC</label><select data-f="sentiment_assessment">' + opt(['POSITIVE','NEGATIVE','MIXED','INDETERMINATE'], f.sentiment_assessment) + '</select></div></div>' +
      '<label>Limitations</label><input data-f="limitations" value="' + esc(f.limitations) + '"></div>' +
      '<div class="card"><h2>5. Confirm</h2><label style="text-transform:none; font-size:14px; color:var(--text)"><input type="checkbox" id="reviewed" style="width:auto"> I checked this finding and its citations. It is not fabricated.</label>' +
      roHint() + '<div class="btns"><button class="btn" id="confirm">CONFIRM FINDING</button></div><div id="confirmMsg" class="small"></div></div>';
  }
  function readDraft() {
    var f = JSON.parse(JSON.stringify(draft));
    var els = app.querySelectorAll('[data-f]'); for (var i = 0; i < els.length; i++) f[els[i].dataset.f] = els[i].value;
    var ss = app.querySelectorAll('[data-s]'); f.proposed_new_source = f.proposed_new_source || {}; for (var j = 0; j < ss.length; j++) f.proposed_new_source[ss[j].dataset.s] = ss[j].value;
    f.evidence = app.querySelector('[data-list="evidence"]').value.split('\\n').filter(function (l) { return l.trim(); }).map(function (l) { var p = l.split('|').map(function (x) { return x.trim(); }); return { claim: p[0] || '', url: p[1] || '', publisher: p[2] || '', date: p[3] || '' }; });
    ['inference', 'speculation', 'alternative_explanations', 'required_inputs'].forEach(function (k) { f[k] = app.querySelector('[data-list="' + k + '"]').value.split('\\n').map(function (x) { return x.trim(); }).filter(Boolean); });
    return f;
  }
  function renderResearch() {
    var e = focusEvent();
    if (!e) { app.innerHTML = '<div class="card"><h2>Research</h2><p class="small">Choose an event on the Market tab first. Research starts from an event that V1\\'s sources did not fully explain.</p><div class="btns"><button class="btn" id="toMarket">Go to Market</button></div></div>'; document.getElementById('toMarket').onclick = function () { current = 'Market'; render(); }; return; }
    var c = caseCache[focusId];
    if (!c) { app.innerHTML = '<div class="card muted">Loading research case&hellip;</div>'; getJson('/api/learning/case?event_id=' + focusId).then(function (d) { caseCache[focusId] = d; if (current === 'Research') renderResearch(); }); return; }
    if (!c.ok) { app.innerHTML = '<div class="card err">' + esc(c.error) + '</div>'; return; }
    var rc = c.research_case;
    var html = '<div class="card"><h2>1. What needs explaining</h2><p class="headline" style="font-size:16px">' + esc(c.event.headline) + '</p><p class="lead"><b>Question:</b> ' + esc(rc.question) + '</p>' +
      (rc.reasons.length ? '<ul class="small">' + rc.reasons.map(function (r) { return '<li>' + esc(r) + '</li>'; }).join('') + '</ul>' : '') +
      '<div class="grid2"><div><label>Explained by</label><div class="small">' + (rc.explained_by.length ? esc(rc.explained_by.join(', ')) : '<span class="muted">nothing</span>') + '</div></div><div><label>Weak or silent areas</label><div class="small">' + esc(rc.weak_or_missing_areas.join(', ') || 'none') + '</div></div></div>' +
      '<label>Not measured by V1 at all</label><ul class="small">' + rc.uncovered_drivers.map(function (d) { return '<li>' + esc(d) + '</li>'; }).join('') + '</ul>' +
      '<details><summary><span class="src ver">Collected news</span> ' + esc(c.evidence.length) + ' headlines from the news feeds around the event (original links, not interpreted)</summary><ul class="small">' + c.evidence.map(function (x) { return '<li>' + esc(x.publisher) + ': ' + link(x.url, x.headline) + '</li>'; }).join('') + '</ul></details>' +
      (c.case ? '<details><summary>Case details</summary><p class="small muted">Research case ' + esc(c.case.request_id) + '</p></details>' : '') + '</div>';
    if (c.case && c.case.finding) {
      var confirmed = c.case.finding.validation_status === 'VALIDATED';
      html += '<div class="card"><h2>Finding ' + chip(confirmed ? 'CONFIRMED BY HUMAN' : plain(c.case.finding.validation_status), confirmed ? 'c-good' : 'c-warn') + '</h2>' +
        (confirmed ? '<p class="small"><span class="src hum">Confirmed by you</span> You checked this finding and its citations. It is still a research finding: it changes nothing in V1 by itself.</p>' : '') + findingView(c.case.finding.findings || {}, c.case.finding) +
        (c.candidate ? '<p class="next">This finding already has a learning candidate.</p>' : confirmed ? '<p class="next">Next: turn it into a learning candidate, a precise proposed change to V1 that you review before anything happens.</p>' : '') +
        '<div class="btns">' + (c.candidate ? '<button class="btn" id="toCand">Open learning candidate #' + c.candidate.candidate_id + ' &rarr;</button>' : confirmed ? '<button class="btn" id="mkCand">Create learning candidate &rarr;</button>' : '') + '</div><div id="candMsg" class="small"></div></div>';
      app.innerHTML = html;
      if (document.getElementById('toCand')) document.getElementById('toCand').onclick = function () { openCandidate(c.candidate.candidate_id); };
      if (document.getElementById('mkCand')) document.getElementById('mkCand').onclick = function () { createCandidate(focusId, document.getElementById('candMsg')); };
      return;
    }
    html += '<div class="card"><h2>2. Ask an external AI</h2><p class="small">Copy the Research Pack, open one of these tools in a new tab and paste it there. CryptoPulse never sends anything to an AI by itself.</p>' +
      '<div class="btns"><button class="btn" id="copyPack">COPY RESEARCH PACK</button><a class="btn secondary" href="https://chatgpt.com" target="_blank" rel="noopener">ChatGPT</a><a class="btn secondary" href="https://claude.ai/new" target="_blank" rel="noopener">Claude</a><a class="btn secondary" href="https://gemini.google.com" target="_blank" rel="noopener">Gemini</a><a class="btn secondary" href="https://grok.com" target="_blank" rel="noopener">Grok</a></div>' +
      '<div id="copyMsg" class="small"></div><details style="margin-top:10px"><summary>Preview the pack</summary><pre id="pack">' + esc(c.research_pack) + '</pre></details></div>';
    html += '<div class="card"><h2>3. Paste the AI\\'s answer</h2><p class="small muted">Paste the whole answer, including the JSON block at the end. It is treated as an unconfirmed hypothesis until you check and confirm it.</p><textarea id="aiText" aria-label="AI answer" placeholder="Paste the full answer here"></textarea>' +
      '<div class="grid2"><div><label>Which AI?</label><select id="provider">' + opt(['chatgpt','claude','gemini','grok','other'], (draft && draft._provider) || 'chatgpt') + '</select></div><div></div></div>' +
      '<div class="btns"><button class="btn" id="parse">Read the answer &rarr;</button></div><div id="parseMsg" class="small"></div></div>';
    if (draft) html += draftForm(c);
    app.innerHTML = html;
    document.getElementById('copyPack').onclick = function () {
      var msg = document.getElementById('copyMsg');
      (navigator.clipboard ? navigator.clipboard.writeText(c.research_pack) : Promise.reject()).then(function () { msg.className = 'small ok'; msg.textContent = 'Copied. Paste it into your AI tool.'; }, function () { msg.className = 'small warn'; msg.textContent = 'Copy blocked by the browser. Open the preview and copy it by hand.'; });
    };
    document.getElementById('parse').onclick = function () {
      var text = document.getElementById('aiText').value, provider = document.getElementById('provider').value, msg = document.getElementById('parseMsg');
      msg.textContent = 'Structuring...';
      postJson('/api/learning/parse', { text: text }).then(function (r) {
        if (!r.finding) { msg.className = 'small err'; msg.textContent = r.error || 'Could not read the answer.'; return; }
        draft = r.finding; draft._provider = provider; draftWarnings = (r.ok ? [] : [r.error]).concat(r.warnings || []);
        renderResearch(); var f = app.querySelector('[data-f="explanation"]'); if (f) f.scrollIntoView({ block: 'center' });
      });
    };
    var btn = document.getElementById('confirm');
    if (btn) btn.onclick = function () {
      var msg = document.getElementById('confirmMsg');
      if (!document.getElementById('reviewed').checked) { msg.className = 'small err'; msg.textContent = 'Tick the review box first: a human must confirm the finding.'; return; }
      if (needToken(msg)) return;
      var f = readDraft(), provider = draft._provider; delete f._provider;
      msg.className = 'small'; msg.textContent = 'Saving...';
      postJson('/api/learning/findings', { event_id: focusId, provider: provider, finding: f }).then(function (r) {
        if (!r.ok) { msg.className = 'small err'; msg.textContent = r.error || 'Could not save.'; return; }
        draft = null; delete caseCache[focusId]; load(function () { renderResearch(); });
      });
    };
  }

  // ---------------- LEARNING ----------------
  function createCandidate(eventId, msg) {
    if (msg && needToken(msg)) return;
    if (msg) { msg.className = 'small'; msg.textContent = 'Creating...'; }
    postJson('/api/learning/candidates', { event_id: eventId }).then(function (r) {
      if (!r.ok) { if (msg) { msg.className = 'small err'; msg.textContent = r.error; } return; }
      delete caseCache[eventId]; load(function () { openCandidate(r.candidate_id); });
    });
  }
  function openCandidate(id) { current = 'Learning'; candId = id; cand = null; editorOpen = null; dirty = false; renderNav(); app.innerHTML = '<div class="card muted">Loading candidate&hellip;</div>'; getJson('/api/learning/candidate?id=' + id).then(function (d) { cand = d; render(); window.scrollTo(0, 0); }); }

  function chart(series) {
    if (!series || series.length < 2) return '';
    var W = 600, H = 150, P = 6, t0 = series[0].ts, t1 = series[series.length - 1].ts;
    var vals = []; series.forEach(function (p) { vals.push(p.reconstructed, p.proposed); if (p.stored !== null) vals.push(p.stored); });
    var lo = Math.min.apply(null, vals) - 3, hi = Math.max.apply(null, vals) + 3;
    function x(t) { return P + (W - 2 * P) * (t1 === t0 ? 0 : (t - t0) / (t1 - t0)); } function y(v) { return H - P - (H - 2 * P) * (v - lo) / (hi - lo); }
    function line(k, color, dash) { return '<polyline fill="none" stroke="' + color + '" stroke-width="2"' + (dash ? ' stroke-dasharray="4 3"' : '') + ' points="' + series.map(function (p) { return x(p.ts).toFixed(1) + ',' + y(p[k]).toFixed(1); }).join(' ') + '"/>'; }
    var mid = (50 >= lo && 50 <= hi) ? '<line x1="0" x2="' + W + '" y1="' + y(50) + '" y2="' + y(50) + '" stroke="var(--line)" stroke-dasharray="2 4"/>' : '';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Reconstructed vs proposed V1">' + mid + line('reconstructed', 'var(--muted)', true) + line('proposed', 'var(--accent)') + '</svg>' +
      '<div class="small muted">&#9476; reconstructed V1 &nbsp; <span style="color:var(--accent)">&#9473;</span> proposed V1 &nbsp; (last ' + series.length + ' observations, ' + esc(day(t0)) + ' &ndash; ' + esc(day(t1)) + '; dotted line = 50)</div>';
  }

  function adjustmentEditor(c, a, sources, editable) {
    var types = ['ADD_SOURCE','REMOVE_SOURCE','CHANGE_WEIGHT','CHANGE_CLASSIFICATION','ADD_SIGNAL','ADD_REGIME_CONDITION','SIGNAL_PROTOTYPE'];
    var tlabels = ['Add a new source','Remove a source','Change a source\\'s weight','Reclassify a source','Add a signal derived from an existing source','Add a regime condition','New signal (prototype: collect data first)'];
    var ids = sources.map(function (s) { return s.id; }), lbls = sources.map(function (s) { return s.label + ' (w ' + s.weight + ', c ' + s.confidence + ')'; });
    var g = Object.keys(GROUPS), gl = g.map(function (k) { return GROUPS[k]; });
    a = a || { type: 'ADD_SOURCE' };
    var dis = editable ? '' : ' disabled';
    var h = '<label>What should V1 change?</label><select id="adjType"' + dis + '>' + opt(types, a.type, tlabels) + '</select>';
    var t = a.type;
    if (t === 'ADD_SOURCE') h += '<div class="grid2"><div><label>New source name</label><input data-a="label" value="' + esc(a.label) + '"' + dis + '></div><div><label>Classification</label><select data-a="group"' + dis + '>' + opt(g, a.group, gl) + '</select></div>' +
      '<div><label>Weight</label><input data-a="weight" type="number" step="0.5" value="' + esc(a.weight) + '"' + dis + '></div><div><label>Confidence (0-1)</label><input data-a="confidence" type="number" step="0.05" value="' + esc(a.confidence) + '"' + dis + '></div></div>' +
      '<label>Source URL</label><input data-a="url" value="' + esc(a.url) + '"' + dis + '><label>What it measures</label><input data-a="what_it_measures" value="' + esc(a.what_it_measures) + '"' + dis + '><input type="hidden" data-a="source_id" value="' + esc(a.source_id) + '">';
    if (t === 'REMOVE_SOURCE' || t === 'CHANGE_WEIGHT' || t === 'CHANGE_CLASSIFICATION' || t === 'ADD_REGIME_CONDITION') h += '<label>V1 source</label><select data-a="source_id"' + dis + '>' + opt(ids, a.source_id || ids[0], lbls) + '</select>';
    if (t === 'CHANGE_WEIGHT') h += '<div class="grid2"><div><label>New weight</label><input data-a="weight" type="number" step="0.5" value="' + esc(a.weight) + '"' + dis + '></div><div><label>New confidence (0-1)</label><input data-a="confidence" type="number" step="0.05" value="' + esc(a.confidence) + '"' + dis + '></div></div>';
    if (t === 'CHANGE_CLASSIFICATION') h += '<div class="grid2"><div><label>New classification</label><select data-a="group"' + dis + '>' + opt(g, a.group, gl) + '</select></div><div><label>Reading direction</label><select data-a="invert"' + dis + '>' + opt(['false','true'], String(!!a.invert), ['Normal (high = bullish)','Inverted (high = bearish)']) + '</select></div></div>';
    if (t === 'ADD_SIGNAL') h += '<div class="grid2"><div><label>Signal name</label><input data-a="label" value="' + esc(a.label) + '"' + dis + '></div><div><label>Built from</label><select data-a="derived_from"' + dis + '>' + opt([''].concat(ids), a.derived_from || '', ['Its own new data feed'].concat(lbls)) + '</select></div>' +
      '<div><label>Weight</label><input data-a="weight" type="number" step="0.5" value="' + esc(a.weight) + '"' + dis + '></div><div><label>Confidence (0-1)</label><input data-a="confidence" type="number" step="0.05" value="' + esc(a.confidence) + '"' + dis + '></div></div><input type="hidden" data-a="transform" value="momentum_24h"><input type="hidden" data-a="signal_id" value="' + esc(a.signal_id) + '"><input type="hidden" data-a="group" value="' + esc(a.group || 'NEWS') + '">';
    if (t === 'ADD_REGIME_CONDITION') h += '<div class="grid2"><div><label>When BTC\\'s 24h move is</label><select data-a="op"' + dis + '>' + opt(['>=','<='], a.op || '<=', ['at least','at most']) + '</select></div><div><label>Threshold (%)</label><input data-a="value" type="number" step="0.5" value="' + esc(a.value === undefined ? -2 : a.value) + '"' + dis + '></div>' +
      '<div><label>Multiply its weight by</label><input data-a="multiplier" type="number" step="0.1" value="' + esc(a.multiplier === undefined ? 2 : a.multiplier) + '"' + dis + '></div><div></div></div>';
    if (t === 'SIGNAL_PROTOTYPE') {
      var lines = function (k) { return esc((a[k] || []).join('\\n')); };
      h += '<div class="grid2"><div><label>Signal name (reusable)</label><input data-a="signal_name" value="' + esc(a.signal_name) + '"' + dis + '></div><div><label>Role in V1</label><select data-a="role"' + dis + '>' + opt(ROLES, a.role || 'UNSPECIFIED', ROLES.map(function (r) { return ROLE_TEXT[r]; })) + '</select></div></div>' +
        '<label>Discovery example (the event that revealed it)</label><input data-a="discovery_example" value="' + esc(a.discovery_example) + '"' + dis + '>' +
        '<label>Why it matters</label><textarea data-a="why_it_matters"' + dis + '>' + esc(a.why_it_matters) + '</textarea>' +
        '<label>Required inputs &mdash; one per line</label><textarea data-al="inputs"' + dis + '>' + lines('inputs') + '</textarea>' +
        '<label>Prototype definition</label><textarea data-a="prototype_definition"' + dis + '>' + esc(a.prototype_definition) + '</textarea>' +
        '<label>Data sources to investigate &mdash; one per line</label><textarea data-al="data_sources"' + dis + '>' + lines('data_sources') + '</textarea>' +
        '<label>Collection frequency</label><input data-a="collection_frequency" value="' + esc(a.collection_frequency) + '"' + dis + '>' +
        '<label>Historical backfill requirement</label><textarea data-a="backfill_requirement"' + dis + '>' + esc(a.backfill_requirement) + '</textarea>' +
        '<label>Validation plan (once data exists)</label><textarea data-a="validation_plan"' + dis + '>' + esc(a.validation_plan) + '</textarea>' +
        ((a.related_v1_sources || []).length ? '<p class="small muted">Related V1 source(s), for context only and not used as an input: ' + esc(a.related_v1_sources.join(', ')) + '</p>' : '') +
        '<input type="hidden" data-al="related_v1_sources" value="' + esc((a.related_v1_sources || []).join('\\n')) + '"><input type="hidden" data-a="signal_id" value="' + esc(a.signal_id) + '">' +
        '';
    }
    return h;
  }
  function readAdjustment() {
    var a = { type: document.getElementById('adjType').value }, els = app.querySelectorAll('[data-a]');
    for (var i = 0; i < els.length; i++) { var k = els[i].dataset.a, v = els[i].value; a[k] = ['weight','confidence','value','multiplier'].indexOf(k) >= 0 ? Number(v) : k === 'invert' ? v === 'true' : v; }
    var ls = app.querySelectorAll('[data-al]');
    for (var j = 0; j < ls.length; j++) a[ls[j].dataset.al] = ls[j].value.split('\\n').map(function (x) { return x.trim(); }).filter(Boolean);
    return a;
  }

  function renderCandidate() {
    if (!cand) { app.innerHTML = '<div class="card muted">Loading candidate&hellip;</div>'; return; }
    if (!cand.ok) { app.innerHTML = '<div class="card err">' + esc(cand.error) + '</div>'; return; }
    var c = cand.candidate, r = cand.recalculation, v = cand.validation, editable = ['DRAFT','DATA_COLLECTION_REQUIRED','PENDING_REVIEW','NEEDS_MORE_RESEARCH'].indexOf(c.status) >= 0;
    var proto = !!(c.adjustment && c.adjustment.type === 'SIGNAL_PROTOTYPE'), proxy = !!cand.signal_validity;
    var ev = null; if (market) for (var i = 0; i < market.events.length; i++) if (market.events[i].event_id === c.event_id) ev = market.events[i];
    var openEd = editable && (editorOpen !== null ? editorOpen : (dirty || c.status === 'DRAFT' || c.status === 'NEEDS_MORE_RESEARCH'));
    // ---- 1. Summary: what it proposes, why, impact, validation, and the one next step ----
    var impact = proto ? 'Not calculable yet: historical data required' : !r ? 'Not calculated yet: define the change below' : !r.possible ? 'Data collection required: ' + r.availability.message
      : r.event ? 'Current V1 ' + r.event.reconstructed + ' would become ' + r.event.proposed + ' at the event' : 'Recalculated (no V1 reading at the event itself)';
    var vline = v ? chip(vtext(v.status), VCLASS[v.status]) + (v.status === 'AWAITING_HOLDOUT' && v.independent ? ' <span class="small">' + esc(v.independent.days) + ' of at least ' + esc(v.rules.min_independent_changed_calls) + ' independent days of new evidence so far</span>' : '') : '<span class="muted">Waiting for the change</span>';
    var next = '', nextBtn = '';
    if (c.status === 'PENDING_REVIEW') { next = 'Your decision is needed: see <b>Your decision</b> below.'; nextBtn = '<a class="btn" href="#decision">Go to the decision</a>'; }
    else if (editable) { next = c.status === 'NEEDS_MORE_RESEARCH' ? 'More research was requested. Update the change below, then submit it for review again.' : proto ? 'Check the data-collection plan, then submit it for review. Approving the plan later records it only; V1 does not change.' : 'Check the proposed change and its V1 impact, then submit it for review.'; nextBtn = '<button class="btn" id="submit">Submit for review</button>'; }
    else if (c.status === 'ACCEPTED') next = 'Approved. A new V1 methodology version is ready but not active; activating it is a separate, explicitly authorised step.';
    else if (c.status === 'DATA_COLLECTION_APPROVED') next = 'Data-collection plan approved. V1 is unchanged; the signal becomes testable once its inputs are collected.';
    else if (c.status === 'REJECTED') next = 'Rejected. Nothing changed in V1.';
    var html = '<div class="btns" style="margin:0 0 12px"><button class="btn secondary" id="back">&larr; All learning</button></div>';
    html += '<div class="card" id="candSummary"><h2>Learning candidate #' + c.candidate_id + ' ' + chip(cstatus(c.status), CSTATUS_CLASS[c.status]) + '</h2><p class="headline">' + esc(c.title) + '</p>' +
      (ev ? '<p class="small muted">From: ' + esc(ev.headline) + '</p>' : '') +
      (proto ? '<div class="big" id="newSignal"><p class="headline" style="font-size:16px">New signal discovered &mdash; historical data required</p><p class="small">CryptoPulse cannot validate this signal yet because V1 does not currently collect the required inputs. It has no weight and no confidence: those can only be proposed after its data exists and it has been validated.</p></div>' : '') +
      '<table class="sum"><tr><td>Proposes</td><td>' + esc(r ? r.adjustment_text : 'Not defined yet') + '</td></tr><tr><td>Why</td><td>' + esc(c.reason) + '</td></tr>' +
      '<tr><td>V1 impact</td><td>' + esc(impact) + '</td></tr><tr><td>Validation</td><td>' + vline + '</td></tr><tr><td>Evidence</td><td>' + (c.evidence.length ? esc(c.evidence.length) + ' linked item(s)' : '<span class="warn">No linked evidence</span>') + '</td></tr></table>' +
      (next ? '<p class="next">' + next + '</p>' : '') + (nextBtn ? '<div class="btns">' + nextBtn + '</div>' + (editable ? roHint() : '') : '') + '<div id="submitMsg" class="small"></div></div>';
    if (proxy) html += '<div class="card" id="proxyWarn"><h2>' + chip('PROXY: INVALID FOR SIGNAL VALIDATION', 'c-bad') + '</h2><p>This candidate was generated automatically as the 24h change of an existing V1 source with a default weight and confidence. The V1 impact and validation shown below measure that <b>proxy</b>, not the new signal the research proposed, so they say nothing about whether the new signal works.</p><p class="small muted">The result is kept for audit. Switching to a signal prototype keeps it in the candidate\\'s history.</p>' +
      (editable && cand.prototype_suggestion ? '<div class="btns"><button class="btn" id="toProto">Switch to signal prototype (data collection first)</button></div><p class="small muted">Fills the editor below from the confirmed finding. Nothing is saved until you press Save.</p>' : '') + '</div>';
    // ---- 2. Decision (only the actions the backend accepts for this candidate) ----
    if (c.status === 'PENDING_REVIEW' && proto) {
      html += '<div class="card" id="decision"><h2>Your decision</h2>' + roHint() + '<p class="small">V1 impact: not calculable yet (historical data required). Validation: ' + chip('NOT SUPPORTED YET / DATA REQUIRED', 'c-warn') + '</p>' +
        '<label for="who">Your name</label><input id="who" autocomplete="name"><label for="note">Note</label><input id="note">' +
        '<div class="btns"><button class="btn" data-d="APPROVE">Approve data-collection plan</button><button class="btn bad" data-d="REJECT">Reject</button><button class="btn secondary" data-d="NEEDS_MORE_RESEARCH">Investigate more</button></div><div id="decMsg" class="small"></div>' +
        '<p class="small muted">Approving records the data-collection plan only. It creates no V1 methodology version, activates no source and changes nothing in V1.</p></div>';
    } else if (c.status === 'PENDING_REVIEW') {
      var needAck = !!(v && v.status !== 'SUPPORTED');
      html += '<div class="card" id="decision"><h2>Your decision</h2>' + roHint() + '<p class="small">Validation: ' + (v ? chip(vtext(v.status), VCLASS[v.status]) : '') + '</p>' +
        '<label for="who">Your name</label><input id="who" autocomplete="name"><label for="note">Note</label><input id="note">' +
        (needAck && !proxy ? '<label style="text-transform:none; font-size:14px; color:var(--text)"><input type="checkbox" id="ack" style="width:auto"> Approve without supporting validation (recorded on the version)</label><p class="small muted">The validation has not shown an improvement, so approval is only accepted with this box ticked.</p>' : '') +
        '<div class="btns">' + (proxy ? '' : '<button class="btn" data-d="APPROVE"' + (needAck ? ' disabled' : '') + '>Approve V1 change</button>') + '<button class="btn bad" data-d="REJECT">Reject</button><button class="btn secondary" data-d="NEEDS_MORE_RESEARCH">Investigate more</button></div><div id="decMsg" class="small"></div>' +
        '<p class="small muted">Approving creates a new V1 methodology version that is ready but NOT active. Production V1 does not change.</p></div>';
    } else if (c.status === 'ACCEPTED' && cand.produced_version) {
      var pv = cand.produced_version;
      html += '<div class="card"><h2>Approved V1 version</h2><table class="sum">' +
        '<tr><td>Status</td><td>' + chip('READY (not active)', 'c-good') + '</td></tr><tr><td>Approved by</td><td>' + esc(pv.approved_by) + ' &middot; ' + esc(day(pv.approved_ts)) + '</td></tr>' +
        '<tr><td>Change</td><td>' + esc(pv.reason) + '</td></tr>' +
        '<tr><td>Validation at approval</td><td>' + esc(pv.validation ? vtext(pv.validation.status) : '') + (pv.validation && pv.validation.approved_without_support ? ' <span class="warn">(approved without supporting validation)</span>' : '') + '</td></tr></table>' +
        '<details><summary>Technical details</summary><p class="small muted">Version ' + esc(pv.version_id) + ', based on ' + esc(pv.parent_version_id) + ', formula ' + esc(pv.formula_id) + '; ' + esc(pv.config.sources.length) + ' sources, ' + esc((pv.config.signals || []).length) + ' signal(s).</p></details>' +
        '<p class="small muted">Activating a version in production is a separate, explicitly authorized step. It never happens from this page.</p></div>';
    } else if (c.decision) {
      html += '<div class="card"><h2>Decision</h2><p>' + chip(cstatus(c.status), CSTATUS_CLASS[c.status]) + ' by ' + esc(c.decided_by) + ' &middot; ' + esc(day(c.decided_ts)) + '</p>' + (c.decision_note ? '<p>' + esc(c.decision_note) + '</p>' : '') + '</div>';
    }
    // ---- 3. V1 impact ----
    html += '<div class="card"><h2>What would V1 become?</h2>';
    if (!r) html += '<p class="muted">Define the change in the editor below.</p>';
    else if (proto) html += '<div class="big" id="impactNotCalc"><p><b>NOT CALCULABLE YET</b></p><p class="small">Reason: ' + esc(r.availability.message) + '</p><p class="small">No Proposed V1 is shown and no V1 numerical adjustment was applied. Current V1 is unchanged.</p></div>' +
      '<label>Data required before V1 can be recalculated</label><ul class="small">' + (c.adjustment.inputs || []).map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>';
    else if (!r.possible) html += '<div class="big"><p class="headline" style="font-size:16px">' + esc(r.availability.message) + '</p><p><b>DATA COLLECTION REQUIRED</b></p><p class="small">To recalculate V1, CryptoPulse first has to start recording this input alongside V1\\'s other sources' + (c.adjustment && c.adjustment.url ? ' (' + link(c.adjustment.url, 'source') + ')' : '') + '. No historical values are invented. Once readings accumulate, this candidate can be recalculated and validated.</p></div>';
    else {
      var e = r.event;
      if (e) html += '<div class="row"><div class="kv"><div class="k">Stored V1 (reference)</div><div class="v muted">' + (e.stored === null ? '&ndash;' : esc(e.stored)) + '</div></div><div class="kv"><div class="k">Current V1 (reconstructed)</div><div class="v">' + esc(e.reconstructed) + '</div></div><div class="kv"><div class="k">Proposed V1</div><div class="v" style="color:var(--accent)">' + esc(e.proposed) + '</div></div><div class="kv"><div class="k">Delta</div><div class="v">' + (e.delta > 0 ? '+' : '') + esc(e.delta) + '</div></div></div><p class="small muted">At the event (observation ' + esc(day(e.ts)) + ').</p>';
      html += chart(r.series);
      html += '<p class="small">Across <b>' + esc(r.observations) + '</b> stored V1 observations: average change ' + esc(r.summary.mean_abs_delta) + ' points, largest ' + esc(r.summary.max_abs_delta) + '; the up/down call changes on ' + esc(r.summary.changed_calls) + ' observations.</p>' +
        '<details><summary>Technical details</summary><p class="small muted">' + esc(day(r.from_ts)) + ' &ndash; ' + esc(day(r.to_ts)) + '. ' + esc(r.availability.message) + ' Baseline = V1 methodology ' + esc(cand.base_version.version_id) + ' (' + esc(cand.base_version.reason) + ') applied to the stored per-source readings. Proposed = the same readings with the adjustment. Both use the same formula.</p>' +
        '<p class="small muted">Stored vs reconstructed: stored V1 matches the reconstruction exactly on ' + esc(r.reconstruction.exact_matches) + ' of ' + esc(r.reconstruction.observations) + ' observations (average gap ' + esc(r.reconstruction.mean_abs_gap) + ' points), because V1\\'s writer applied browser-local weight overrides and a temporary Foufi boost that were never saved. That is why impact is measured against the reconstruction, not the stored value.</p></details>';
    }
    html += '</div>';
    // ---- 4. Validation (interim accuracies are not shown while evidence is still being collected) ----
    if (v && v.status === 'DATA_REQUIRED') html += '<div class="card" id="valData"><h2>Does it improve CryptoPulse?</h2><p>' + chip('NOT SUPPORTED YET / DATA REQUIRED', VCLASS.DATA_REQUIRED) + ' ' + esc(v.headline) + '</p><p class="small">No validation result exists for this signal, and none is claimed.</p>' +
      '<label>Validation plan once data exists</label><p class="small">' + esc(c.adjustment.validation_plan) + '</p></div>';
    else html += '<div class="card"><h2>Does it improve CryptoPulse?</h2>' + (v && proxy ? '<p class="small">' + chip('PROXY RESULT', 'c-bad') + ' Measured on the proxy adjustment, not on the proposed signal. Not a validation of the new signal.</p>' : '') + (v ? '<p>' + chip(v.status_text, VCLASS[v.status]) + ' ' + esc(v.headline) + '</p>' +
      (v.validation_scope ? '<p class="small">' + chip('OUT OF SAMPLE', 'c-acc') + ' Judged only on V1 observations made after this candidate was created' + (v.discovered_at ? ' (' + esc(day(v.discovered_at)) + ')' : '') + '. Compared with the <b>reconstructed</b> V1 (published defaults recomputed from stored readings), not with V1\\'s actual historical calls.</p>' : '') +
      (v.current_v1 && FINAL_VALIDATION.indexOf(v.status) >= 0 ? '<div class="row"><div class="kv"><div class="k">Reconstructed V1 right</div><div class="v">' + esc(v.current_v1.accuracy_pct) + '%</div></div><div class="kv"><div class="k">Adjusted V1 right</div><div class="v" style="color:var(--accent)">' + esc(v.adjusted_v1.accuracy_pct) + '%</div></div>' + (v.stored_v1 && v.stored_v1.total ? '<div class="kv"><div class="k">Stored V1 right (actual calls)</div><div class="v muted">' + esc(v.stored_v1.accuracy_pct) + '%</div></div>' : '') + '<div class="kv"><div class="k">Outcomes checked</div><div class="v">' + esc(v.resolved) + '</div></div><div class="kv"><div class="k">Days they disagree</div><div class="v">' + esc(v.independent.days) + '</div></div></div>'
        : v.independent ? '<div class="row"><div class="kv"><div class="k">Independent days so far</div><div class="v">' + esc(v.independent.days) + '</div></div><div class="kv"><div class="k">Needed</div><div class="v">' + esc(v.rules.min_independent_changed_calls) + '</div></div></div><p class="small muted">Accuracy figures are shown once the evidence is complete, not while it is still being collected.</p>' : '') +
      (v.stored_v1 && v.stored_v1.reconstruction_call_disagreements && FINAL_VALIDATION.indexOf(v.status) >= 0 ? '<p class="small muted">On ' + esc(v.stored_v1.reconstruction_call_disagreements) + ' of these observations the reconstructed V1 call differs from V1\\'s actual stored call (browser-local weight overrides and a temporary Foufi boost were never saved). Days like that never count as a win for the candidate.</p>' : '') +
      (v.exploratory_in_sample ? '<details><summary>' + chip('EXPLORATORY', 'c-warn') + ' Before the candidate existed (in-sample, not a validation)</summary><p class="small">' + esc(v.exploratory_in_sample.headline) + ' ' + esc(v.exploratory_in_sample.resolved) + ' outcomes; ' + esc(v.exploratory_in_sample.independent.days) + ' disagreement day(s).</p></details>' : '') +
      '<details><summary>How this is checked</summary><p class="small">' + esc(v.method) + '. The ' + esc(v.rules.excluded_window_hours) + 'h around the originating event are excluded, so the candidate is not judged on the event that inspired it. Hourly observations overlap a 24h horizon, so the verdict uses at most one disagreement per day: it needs at least ' + esc(v.rules.min_independent_changed_calls) + ' such days and a one-sided sign test at p &le; ' + esc(v.rules.alpha) + '.</p></details>' : '<p class="muted">Waiting for the adjustment.</p>') + '</div>';
    // ---- 5. Editor (collapsed unless there is work to do in it) ----
    html += '<details class="card" id="editor"' + (openEd ? ' open' : '') + '><summary>' + (editable ? 'Edit the proposed change' : 'The proposed change (read-only)') + '</summary>' +
      '<div class="grid2"><div><label>Type</label><select id="cType"' + (editable ? '' : ' disabled') + '>' + opt(['NEW_SOURCE','NEW_TREND','NEW_SIGNAL','SOURCE_RECLASSIFICATION','SOURCE_WEIGHT_ADJUSTMENT','REGIME_SPECIFIC_SIGNAL'], c.candidate_type, ['New source','New trend','New signal','Source reclassification','Source weight adjustment','Regime-specific signal']) + '</select></div>' +
      '<div><label>Confidence</label><select id="cConf"' + (editable ? '' : ' disabled') + '>' + opt(['LOW','MEDIUM','HIGH'], c.confidence, ['Low','Medium','High']) + '</select></div></div>' +
      '<label>Title</label><input id="cTitle" value="' + esc(c.title) + '"' + (editable ? '' : ' disabled') + '><label>Why (the finding)</label><textarea id="cReason"' + (editable ? '' : ' disabled') + '>' + esc(c.reason) + '</textarea>' +
      '<label>Expected effect</label><input id="cEffect" value="' + esc(c.expected_effect) + '"' + (editable ? '' : ' disabled') + '>' +
      (c.evidence.length ? '<label>Evidence</label><ul class="small">' + c.evidence.map(function (x) { return '<li>' + esc(x.claim) + ' ' + (x.url ? '(' + link(x.url, x.publisher || 'source') + ')' : '') + '</li>'; }).join('') + '</ul>' : '') +
      '<h2 style="margin-top:16px">' + (proto ? 'New signal prototype' : 'Source adjustment') + '</h2>' + adjustmentEditor(c, c.adjustment, cand.base_version.sources, editable) +
      (editable ? '<div class="btns"><button class="btn secondary" id="save">' + (proto ? 'Save' : 'Save &amp; recalculate V1') + '</button></div><div id="saveMsg" class="small"></div>' : '') + '</details>';
    if (cand.analysis_history && cand.analysis_history.length) html += '<div class="card"><details><summary>Earlier analyses of this candidate (' + cand.analysis_history.length + ', kept for audit)</summary><ul class="small">' + cand.analysis_history.map(function (hh) {
      return '<li>' + esc(hh.adjustment_text) + ' &mdash; validation ' + esc(vtext(hh.validation_status)) + (hh.signal_validity ? ' ' + chip('PROXY: invalid for signal validation', 'c-bad') : '') + ' <span class="muted">(superseded ' + esc(day(hh.superseded_ts)) + ')</span></li>'; }).join('') + '</ul></details></div>';
    app.innerHTML = html;
    document.getElementById('back').onclick = function () { candId = null; render(); };
    var edEl = document.getElementById('editor');
    if (edEl) edEl.ontoggle = function () { editorOpen = edEl.open; };
    var at = document.getElementById('adjType');
    if (at && editable) at.onchange = function () { dirty = true; c.adjustment = at.value === 'SIGNAL_PROTOTYPE' ? (cand.prototype_suggestion || { type: 'SIGNAL_PROTOTYPE', signal_name: c.title, role: 'UNSPECIFIED', inputs: [] }) : { type: at.value, source_id: '', weight: 5, confidence: 0.4, group: 'NEWS', label: c.title }; renderCandidate(); };
    var tp = document.getElementById('toProto');
    if (tp) tp.onclick = function () { dirty = true; editorOpen = true; c.adjustment = cand.prototype_suggestion; renderCandidate(); };
    function save(submit) {
      var msg = document.getElementById(submit ? 'submitMsg' : 'saveMsg'); if (needToken(msg)) return; msg.className = 'small'; msg.textContent = submit ? 'Submitting...' : 'Recalculating...';
      postJson('/api/learning/candidate/update', { candidate_id: c.candidate_id, submit: submit, adjustment: readAdjustment(), fields: { candidate_type: document.getElementById('cType').value, confidence: document.getElementById('cConf').value, title: document.getElementById('cTitle').value, reason: document.getElementById('cReason').value, expected_effect: document.getElementById('cEffect').value } }).then(function (res) {
        if (!res.ok) { msg.className = 'small err'; msg.textContent = res.error; return; }
        load(function () { openCandidate(c.candidate_id); });
      });
    }
    if (document.getElementById('save')) document.getElementById('save').onclick = function () { save(false); };
    if (document.getElementById('submit')) document.getElementById('submit').onclick = function () { save(true); };
    var ackEl = document.getElementById('ack'), apEl = app.querySelector('[data-d="APPROVE"]');
    if (ackEl && apEl) ackEl.onchange = function () { apEl.disabled = !ackEl.checked; };
    var ds = app.querySelectorAll('[data-d]');
    for (var k = 0; k < ds.length; k++) ds[k].onclick = function (e2) {
      var msg = document.getElementById('decMsg'); if (needToken(msg)) return; var ack = document.getElementById('ack');
      postJson('/api/learning/candidate/decide', { candidate_id: c.candidate_id, decision: e2.currentTarget.dataset.d, approver: document.getElementById('who').value, note: document.getElementById('note').value, acknowledge_unsupported: !!(ack && ack.checked) }).then(function (res) {
        if (!res.ok) { msg.className = 'small err'; msg.textContent = res.error; return; }
        versions = null; load(function () { openCandidate(c.candidate_id); });
      });
    };
  }

  function renderLearning() {
    if (candId) return renderCandidate();
    if (!market) { app.innerHTML = '<div class="card muted">Loading&hellip;</div>'; return; }
    if (!versions) { getJson('/api/learning/versions').then(function (d) { versions = d; if (current === 'Learning' && !candId) renderLearning(); }); }
    var withCand = market.events.filter(function (x) { return x.candidate; });
    var ready = market.events.filter(function (x) { return !x.candidate && x.case && x.case.finding && x.case.finding.validation_status === 'VALIDATED'; });
    function row(x) {
      var a = x.candidate.analysis;
      return '<div class="ev" data-cand="' + x.candidate.candidate_id + '" role="button" tabindex="0"><div><b>' + esc(x.candidate.title) + '</b> <span class="small muted">#' + x.candidate.candidate_id + '</span></div>' +
        (a && a.adjustment_text ? '<div class="small">' + esc(a.adjustment_text) + '</div>' : '') + '<div class="small muted">From: ' + esc(x.headline) + '</div><div class="small" style="margin-top:3px">' + chip(cstatus(x.candidate.status), CSTATUS_CLASS[x.candidate.status]) + ' ' +
        (x.candidate.signal_validity ? chip('PROXY: invalid for signal validation', 'c-bad') + ' ' : '') +
        (a ? (a.recalculation_possible ? chip('V1 ' + (a.event ? a.event.reconstructed + ' -> ' + a.event.proposed : 'recalculated'), 'c-acc') : a.availability === 'DATA_COLLECTION_REQUIRED' ? chip('New signal: historical data required', 'c-warn') : chip('Data collection required', 'c-warn')) + ' ' + chip(vtext(a.validation_status), VCLASS[a.validation_status]) : '') + '</div></div>';
    }
    var buckets = [['Needs your decision', ['PENDING_REVIEW']], ['In progress', ['DRAFT', 'DATA_COLLECTION_REQUIRED', 'NEEDS_MORE_RESEARCH']], ['Decided', ['ACCEPTED', 'DATA_COLLECTION_APPROVED', 'REJECTED']]];
    var html = '';
    if (!withCand.length) html += '<div class="card"><h2>Learning candidates</h2><p class="small">No learning candidates yet. A candidate is created from a finding you confirm in Research: it states one precise change to V1 for you to review.</p><div class="btns"><button class="btn" id="toResearch">Go to Research</button></div></div>';
    buckets.forEach(function (b) {
      var xs = withCand.filter(function (x) { return b[1].indexOf(x.candidate.status) >= 0; });
      if (xs.length) html += '<div class="card"><h2>' + b[0] + ' (' + xs.length + ')</h2>' + xs.map(row).join('') + '</div>';
    });
    var other = withCand.filter(function (x) { return !buckets.some(function (b) { return b[1].indexOf(x.candidate.status) >= 0; }); });
    if (other.length) html += '<div class="card"><h2>Other</h2>' + other.map(row).join('') + '</div>';
    if (ready.length) html += '<div class="card"><h2>Confirmed findings ready to learn from</h2>' + ready.map(function (x) {
      var f = x.case.finding.findings || {};
      return '<div class="ev"><div><b>' + esc(f.primary_driver || 'Finding') + '</b></div><div class="small muted">' + esc(x.headline) + '</div><div class="btns"><button class="btn" data-mk="' + x.event_id + '">Create learning candidate</button></div><div class="small" id="mk' + x.event_id + '"></div></div>';
    }).join('') + '</div>';
    var approved = versions && versions.versions ? versions.versions.filter(function (v) { return v.status === 'APPROVED'; }) : [];
    html += '<details class="card"><summary>V1 methodology versions</summary>' + (versions ? (approved.length ? '' : '<p class="small">No new V1 version has been approved. Production V1 uses its published defaults.</p>') + '<table>' + (versions.versions || []).map(function (v) {
      return '<tr><td><b>' + esc(v.version_id) + '</b></td><td>' + chip(v.status === 'APPROVED' ? 'READY (not active)' : plain(v.status), v.status === 'APPROVED' ? 'c-good' : 'c-muted') + '</td><td class="small">' + esc(v.reason) + (v.candidate_id ? ' <span class="muted">(candidate #' + v.candidate_id + ', approved by ' + esc(v.approved_by) + ')</span>' : '') + '</td></tr>';
    }).join('') + '</table><p class="small muted">Production V1 is unchanged. Activating a version is a separate, explicitly authorized step.</p>' : '<p class="muted">Loading&hellip;</p>') + '</details>';
    html += '<details class="card"><summary>How learning works</summary><p class="small">A confirmed finding becomes a <b>learning candidate</b>: a precise change to V1\\'s sources. V1 is recalculated with the change and checked against what BTC actually did on data collected after the candidate was created. Only your explicit approval creates a new V1 version, and it is never activated automatically.</p></details>';
    app.innerHTML = html;
    var tr = document.getElementById('toResearch'); if (tr) tr.onclick = function () { current = 'Research'; render(); };
    var cs = app.querySelectorAll('[data-cand]');
    for (var i = 0; i < cs.length; i++) {
      cs[i].onclick = function (e) { openCandidate(Number(e.currentTarget.dataset.cand)); };
      cs[i].onkeydown = function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.currentTarget.click(); } };
    }
    var mk = app.querySelectorAll('[data-mk]'); for (var j = 0; j < mk.length; j++) mk[j].onclick = function (e) { var id = Number(e.currentTarget.dataset.mk); createCandidate(id, document.getElementById('mk' + id)); };
  }

  function render() { renderNav(); if (current === 'Market') return renderMarket(); if (current === 'Research') return renderResearch(); return renderLearning(); }
  function load(after) {
    getJson('/api/learning/market').then(function (d) { market = d; if (d.ok && (focusId === null || !focusEvent())) focusId = d.focus_event_id; if (after) after(); else render(); },
      function (err) { market = { ok: false, error: String(err) }; render(); });
  }
  render(); load();
})();
</script>
</body>
</html>`;
