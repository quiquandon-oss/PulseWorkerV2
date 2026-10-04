// Research Lab primary UI: MARKET -> RESEARCH -> LEARNING. The previous 12-tab technical page is unchanged and lives
// at /research-lab/advanced. Plain ES5 + fetch; every value from the API (including pasted AI text) is HTML-escaped,
// and only http(s) URLs are ever rendered as links.
export const LEARNING_LAB_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>CryptoPulse Research</title>
<style>
  :root { --bg:#0f1117; --card:#171a23; --line:#262b38; --text:#e7e9ee; --muted:#9aa3b2; --accent:#5b8cff;
          --good:#2fbf71; --warn:#e3a33b; --bad:#e2555a; --chip:#20253200; }
  @media (prefers-color-scheme: light) { :root { --bg:#f6f7f9; --card:#fff; --line:#e3e6ec; --text:#141821; --muted:#5d6676; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 16px 0; max-width:880px; margin:0 auto; }
  h1 { font-size:20px; margin:0; } .sub { color:var(--muted); font-size:13px; margin:2px 0 12px; }
  nav { display:flex; gap:6px; border-bottom:1px solid var(--line); max-width:880px; margin:0 auto; padding:0 16px; overflow-x:auto; }
  nav button, nav a { background:none; border:0; color:var(--muted); font:inherit; font-weight:600; padding:10px 12px; cursor:pointer; border-bottom:2px solid transparent; text-decoration:none; white-space:nowrap; }
  nav button.active { color:var(--text); border-bottom-color:var(--accent); } nav a { margin-left:auto; font-weight:500; font-size:13px; }
  main { max-width:880px; margin:0 auto; padding:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:14px; }
  .card h2 { font-size:13px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); margin:0 0 10px; }
  .headline { font-size:19px; font-weight:650; margin:0 0 6px; } .muted { color:var(--muted); } .small { font-size:13px; }
  .row { display:flex; gap:12px; flex-wrap:wrap; } .kv { flex:1 1 140px; } .kv .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; } .kv .v { font-size:18px; font-weight:650; }
  .chip { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:650; border:1px solid var(--line); }
  .c-good { color:var(--good); border-color:var(--good); } .c-warn { color:var(--warn); border-color:var(--warn); } .c-bad { color:var(--bad); border-color:var(--bad); } .c-muted { color:var(--muted); }
  table { width:100%; border-collapse:collapse; } td { padding:7px 4px; border-top:1px solid var(--line); vertical-align:top; } td.icon { width:28px; font-weight:700; text-align:center; }
  .btn { display:inline-block; background:var(--accent); color:#fff; border:0; border-radius:10px; padding:11px 16px; font:inherit; font-weight:650; cursor:pointer; text-decoration:none; }
  .btn.secondary { background:none; color:var(--text); border:1px solid var(--line); } .btn:disabled { opacity:.45; cursor:not-allowed; }
  .btns { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  .steps { display:flex; gap:4px; flex-wrap:wrap; margin:4px 0 0; } .step { flex:1 1 80px; text-align:center; font-size:11px; font-weight:650; padding:6px 2px; border-radius:8px; background:var(--bg); color:var(--muted); border:1px solid var(--line); }
  .step.done { color:var(--good); border-color:var(--good); } .step.now { color:#fff; background:var(--accent); border-color:var(--accent); }
  .ev { padding:10px 0; border-top:1px solid var(--line); cursor:pointer; } .ev:first-child { border-top:0; } .ev.sel { background:linear-gradient(90deg, rgba(91,140,255,.12), transparent); }
  textarea, input, select { width:100%; background:var(--bg); color:var(--text); border:1px solid var(--line); border-radius:8px; padding:9px; font:inherit; font-size:14px; }
  textarea { min-height:90px; } label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; text-transform:uppercase; letter-spacing:.05em; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:0 12px; } @media (max-width:600px) { .grid2 { grid-template-columns:1fr; } }
  .warn { color:var(--warn); font-size:13px; } .err { color:var(--bad); font-size:13px; } .ok { color:var(--good); font-size:13px; }
  details summary { cursor:pointer; color:var(--muted); font-size:13px; } pre { white-space:pre-wrap; font-size:12px; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:10px; max-height:320px; overflow:auto; }
  ul { margin:6px 0; padding-left:20px; } a { color:var(--accent); }
</style>
</head>
<body>
<header><h1>CryptoPulse Research</h1><div class="sub">Understand the market &rarr; discover what V1 is missing &rarr; improve V1</div></header>
<nav id="nav"></nav>
<main id="app"><div class="card muted">Loading&hellip;</div></main>
<script>
(function () {
  var TABS = ['Market', 'Research', 'Learning'];
  var STEPS = ['UNDERSTAND', 'INVESTIGATE', 'DISCOVER', 'LEARN', 'ADJUST', 'RECALCULATE', 'VALIDATE', 'APPROVE'];
  var current = 'Market', market = null, focusId = null, caseCache = {}, draft = null, draftWarnings = [];
  var nav = document.getElementById('nav'), app = document.getElementById('app');

  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function link(url, text) { return /^https?:\\/\\//i.test(url || '') ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(text || url) + '</a>' : esc(text || ''); }
  function day(ts) { return ts ? new Date(ts).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : ''; }
  function getJson(path) { return fetch(path).then(function (r) { return r.json(); }); }
  function postJson(path, body, token) {
    var h = { 'Content-Type': 'application/json' }; if (token) h.Authorization = 'Bearer ' + token;
    return fetch(path, { method: 'POST', headers: h, body: JSON.stringify(body) }).then(function (r) { return r.json(); });
  }
  var VERDICT_CLASS = { EXPLAINED: 'c-good', PARTIALLY_EXPLAINED: 'c-warn', NOT_EXPLAINED: 'c-bad' };
  var ICON = { EXPLAINS: ['&#10003;', 'c-good'], PARTIAL: ['?', 'c-warn'], CONTRADICTS: ['&#10005;', 'c-bad'], SILENT: ['&ndash;', 'c-muted'], MISSING: ['&#8709;', 'c-muted'], NOT_APPLICABLE: ['&ndash;', 'c-muted'] };
  var STATUS_TEXT = { EXPLAINS: 'explains it', PARTIAL: 'partly', CONTRADICTS: 'pointed the other way', SILENT: 'silent', MISSING: 'no reading', NOT_APPLICABLE: 'n/a' };
  function chip(text, cls) { return '<span class="chip ' + (cls || 'c-muted') + '">' + esc(text) + '</span>'; }
  function dirWord(d) { return d === 'UP' ? 'UP' : d === 'DOWN' ? 'DOWN' : d === 'NEUTRAL' ? 'NEUTRAL' : d === 'FLAT' ? 'FLAT' : 'unknown'; }

  function renderNav() {
    nav.innerHTML = TABS.map(function (t) { return '<button data-tab="' + t + '" class="' + (t === current ? 'active' : '') + '">' + t + '</button>'; }).join('') +
      '<a href="/research-lab/advanced">Advanced / technical &rarr;</a>';
    var b = nav.querySelectorAll('button');
    for (var i = 0; i < b.length; i++) b[i].onclick = function (e) { current = e.currentTarget.dataset.tab; render(); };
  }
  function stepper(stage) {
    var idx = STEPS.indexOf(stage);
    return '<div class="steps">' + STEPS.map(function (s, i) { return '<div class="step ' + (i < idx ? 'done' : i === idx ? 'now' : '') + '">' + (i < idx ? '&#10003; ' : '') + s + '</div>'; }).join('') + '</div>';
  }
  function focusEvent() { if (!market) return null; for (var i = 0; i < market.events.length; i++) if (market.events[i].event_id === focusId) return market.events[i]; return null; }

  function groupTable(groups) {
    return '<table>' + groups.map(function (g) {
      var ic = ICON[g.status] || ICON.SILENT;
      return '<tr><td class="icon ' + ic[1] + '">' + ic[0] + '</td><td>' + esc(g.label) + '</td><td class="muted small" style="text-align:right">' + esc(STATUS_TEXT[g.status] || '') + '</td></tr>';
    }).join('') + '</table>';
  }

  function renderMarket() {
    if (!market) { app.innerHTML = '<div class="card muted">Loading&hellip;</div>'; return; }
    if (!market.ok) { app.innerHTML = '<div class="card err">Could not load market data: ' + esc(market.error) + '</div>'; return; }
    var e = focusEvent();
    var html = '<div class="card"><h2>Right now</h2><div class="row">' +
      '<div class="kv"><div class="k">BTC</div><div class="v">' + (market.latest.btc ? '$' + Math.round(market.latest.btc.btc_price).toLocaleString('en-US') : '&ndash;') + '</div></div>' +
      '<div class="kv"><div class="k">V1 sentiment</div><div class="v">' + (market.latest.v1 ? esc(market.latest.v1.score) + ' / 100' : '&ndash;') + '</div></div></div></div>';
    if (!e) { app.innerHTML = html + '<div class="card muted">No market events recorded yet.</div>'; return; }
    html += '<div class="card"><h2>Where are we in the learning cycle?</h2>' + stepper(e.stage) + '</div>';
    html += '<div class="card"><h2>What happened</h2><p class="headline">' + esc(e.headline) + '</p><p class="muted small">' + esc(e.btc_move_text) + '</p>' +
      '<div class="row" style="margin-top:8px"><div class="kv"><div class="k">V1 expected</div><div class="v">' + dirWord(e.v1_lean_before) + '</div></div>' +
      '<div class="kv"><div class="k">Actual</div><div class="v">' + dirWord(e.actual_direction) + '</div></div>' +
      '<div class="kv"><div class="k">News collected</div><div class="v">' + esc(e.evidence_count) + '</div></div></div></div>';
    html += '<div class="card"><h2>Why? What V1\\'s current sources say</h2>' + groupTable(e.groups) +
      '<p style="margin:12px 0 0">' + chip(e.verdict_text, VERDICT_CLASS[e.verdict]) + ' <span class="muted small">' + esc(e.explained_share) + '% of V1\\'s source weight pointed the way BTC actually moved.</span></p>' +
      '<div class="btns"><button class="btn" id="goResearch">' + (e.case && e.case.finding ? 'See the finding &rarr;' : 'Investigate why &rarr;') + '</button></div></div>';
    html += '<div class="card"><h2>Other recent events</h2>' + market.events.map(function (x) {
      return '<div class="ev ' + (x.event_id === focusId ? 'sel' : '') + '" data-id="' + x.event_id + '"><div>' + esc(x.headline) + '</div><div class="small" style="margin-top:3px">' +
        chip(x.verdict_text, VERDICT_CLASS[x.verdict]) + ' ' + chip('Stage: ' + x.stage, x.stage === 'LEARN' ? 'c-good' : 'c-muted') + '</div></div>';
    }).join('') + '</div>';
    app.innerHTML = html;
    document.getElementById('goResearch').onclick = function () { current = 'Research'; render(); };
    var evs = app.querySelectorAll('.ev');
    for (var i = 0; i < evs.length; i++) evs[i].onclick = function (ev) { focusId = Number(ev.currentTarget.dataset.id); draft = null; render(); window.scrollTo(0, 0); };
  }

  function findingView(f, meta) {
    var src = f.proposed_new_source || {};
    var h = '<p class="headline" style="font-size:16px">' + esc(f.primary_driver || 'Finding') + '</p><p>' + esc(f.explanation) + '</p>';
    h += '<table>' +
      '<tr><td class="muted small">Type</td><td>' + esc(f.finding_type) + ' &middot; ' + esc(f.driver_category) + '</td></tr>' +
      '<tr><td class="muted small">Already in V1?</td><td>' + esc(f.covered_by_existing_v1_source === 'none' ? 'No' : f.covered_by_existing_v1_source) + '</td></tr>' +
      (src.name ? '<tr><td class="muted small">Potential new source</td><td>' + link(src.url, src.name) + (src.what_it_measures ? '<div class="small muted">' + esc(src.what_it_measures) + '</div>' : '') + '</td></tr>' : '') +
      (f.proposed_signal ? '<tr><td class="muted small">Potential signal</td><td>' + esc(f.proposed_signal) + '</td></tr>' : '') +
      (f.trend ? '<tr><td class="muted small">Trend</td><td>' + esc(f.trend) + '</td></tr>' : '') +
      '<tr><td class="muted small">Confidence</td><td>' + esc(f.confidence) + '</td></tr></table>';
    if (f.evidence && f.evidence.length) h += '<label>Evidence</label><ul>' + f.evidence.map(function (x) { return '<li>' + esc(x.claim) + ' ' + (x.url ? '(' + link(x.url, x.publisher || 'source') + (x.date ? ', ' + esc(x.date) : '') + ')' : '<span class="warn">(no link)</span>') + '</li>'; }).join('') + '</ul>';
    if (f.alternative_explanations && f.alternative_explanations.length) h += '<label>Alternative explanations</label><ul>' + f.alternative_explanations.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>';
    if (meta) h += '<p class="small muted">Researched with ' + esc(meta.provider || 'an external AI') + ' &middot; confirmed ' + esc(day(meta.registered_ts)) + '</p>';
    return h;
  }

  function opt(values, sel) { return values.map(function (v) { return '<option value="' + esc(v) + '"' + (v === sel ? ' selected' : '') + '>' + esc(v) + '</option>'; }).join(''); }
  var CATS = ['OPTIONS','LIQUIDATIONS','STABLECOIN_FLOWS','WHALES_MINERS','SPECIFIC_CATALYST','FLOWS','DERIVATIVES','MACRO','EQUITIES','NEWS','BREADTH','ONCHAIN','TREASURY','OTHER'];
  var TYPES = ['NEW_SOURCE','NEW_TREND','NEW_SIGNAL','EXISTING_SOURCE_MISREAD','NO_NEW_DRIVER'];

  function draftForm(c) {
    var f = draft, s = f.proposed_new_source || {};
    var v1ids = ['none'].concat(c.assessment.sources.map(function (x) { return x.id; }));
    return '<div class="card"><h2>3. The finding (check and edit)</h2>' +
      (draftWarnings.length ? '<ul>' + draftWarnings.map(function (w) { return '<li class="warn">' + esc(w) + '</li>'; }).join('') + '</ul>' : '') +
      '<label>What explains the move</label><textarea data-f="explanation">' + esc(f.explanation) + '</textarea>' +
      '<div class="grid2"><div><label>Primary driver</label><input data-f="primary_driver" value="' + esc(f.primary_driver) + '"></div>' +
      '<div><label>Driver category</label><select data-f="driver_category">' + opt(CATS, f.driver_category) + '</select></div>' +
      '<div><label>Finding type</label><select data-f="finding_type">' + opt(TYPES, f.finding_type) + '</select></div>' +
      '<div><label>Already covered by V1 source</label><select data-f="covered_by_existing_v1_source">' + opt(v1ids, f.covered_by_existing_v1_source) + '</select></div>' +
      '<div><label>Potential new source</label><input data-s="name" value="' + esc(s.name) + '"></div>' +
      '<div><label>Source URL</label><input data-s="url" value="' + esc(s.url) + '"></div></div>' +
      '<label>What the source measures</label><input data-s="what_it_measures" value="' + esc(s.what_it_measures) + '">' +
      '<label>Potential signal (how it becomes a 0-100 reading)</label><textarea data-f="proposed_signal">' + esc(f.proposed_signal) + '</textarea>' +
      '<label>Trend</label><input data-f="trend" value="' + esc(f.trend) + '">' +
      '<label>Evidence &mdash; one per line: claim | url | publisher | date</label><textarea data-list="evidence">' + esc((f.evidence || []).map(function (e) { return [e.claim, e.url, e.publisher, e.date].join(' | '); }).join('\\n')) + '</textarea>' +
      '<label>Alternative explanations &mdash; one per line</label><textarea data-list="alternative_explanations">' + esc((f.alternative_explanations || []).join('\\n')) + '</textarea>' +
      '<div class="grid2"><div><label>Confidence</label><select data-f="confidence">' + opt(['LOW','MEDIUM','HIGH'], f.confidence) + '</select></div>' +
      '<div><label>Sentiment for BTC</label><select data-f="sentiment_assessment">' + opt(['POSITIVE','NEGATIVE','MIXED','INDETERMINATE'], f.sentiment_assessment) + '</select></div></div>' +
      '<label>Limitations</label><input data-f="limitations" value="' + esc(f.limitations) + '"></div>' +
      '<div class="card"><h2>4. Confirm</h2><label style="text-transform:none; font-size:14px; color:var(--text)"><input type="checkbox" id="reviewed" style="width:auto"> I checked this finding and its citations. It is not fabricated.</label>' +
      '<label>Admin token (not stored)</label><input type="password" id="token" autocomplete="off">' +
      '<div class="btns"><button class="btn" id="confirm">Confirm finding</button></div><div id="confirmMsg" class="small"></div></div>';
  }

  function readDraft() {
    var f = JSON.parse(JSON.stringify(draft));
    var els = app.querySelectorAll('[data-f]'); for (var i = 0; i < els.length; i++) f[els[i].dataset.f] = els[i].value;
    var ss = app.querySelectorAll('[data-s]'); f.proposed_new_source = f.proposed_new_source || {}; for (var j = 0; j < ss.length; j++) f.proposed_new_source[ss[j].dataset.s] = ss[j].value;
    var ev = app.querySelector('[data-list="evidence"]').value.split('\\n').filter(function (l) { return l.trim(); });
    f.evidence = ev.map(function (l) { var p = l.split('|').map(function (x) { return x.trim(); }); return { claim: p[0] || '', url: p[1] || '', publisher: p[2] || '', date: p[3] || '' }; });
    f.alternative_explanations = app.querySelector('[data-list="alternative_explanations"]').value.split('\\n').map(function (x) { return x.trim(); }).filter(Boolean);
    return f;
  }

  function renderResearch() {
    var e = focusEvent();
    if (!e) { app.innerHTML = '<div class="card muted">Pick an event on the Market tab first.</div>'; return; }
    var c = caseCache[focusId];
    if (!c) {
      app.innerHTML = '<div class="card muted">Loading research case&hellip;</div>';
      getJson('/api/learning/case?event_id=' + focusId).then(function (d) { caseCache[focusId] = d; if (current === 'Research') renderResearch(); });
      return;
    }
    if (!c.ok) { app.innerHTML = '<div class="card err">' + esc(c.error) + '</div>'; return; }
    var rc = c.research_case;
    var html = '<div class="card"><h2>Where are we in the learning cycle?</h2>' + stepper(c.stage) + '</div>';
    html += '<div class="card"><h2>Research case ' + (c.case ? esc(c.case.request_id) : '(not opened yet)') + '</h2><p class="headline" style="font-size:16px">' + esc(c.event.headline) + '</p>' +
      '<p><b>Question:</b> ' + esc(rc.question) + '</p>' +
      (rc.reasons.length ? '<ul>' + rc.reasons.map(function (r) { return '<li>' + esc(r) + '</li>'; }).join('') + '</ul>' : '') +
      '<div class="grid2"><div><label>Explained by</label><div>' + (rc.explained_by.length ? esc(rc.explained_by.join(', ')) : '<span class="muted">nothing</span>') + '</div></div>' +
      '<div><label>Weak or silent areas</label><div>' + esc(rc.weak_or_missing_areas.join(', ') || 'none') + '</div></div></div>' +
      '<label>Not measured by V1 at all</label><ul class="small">' + rc.uncovered_drivers.map(function (d) { return '<li>' + esc(d) + '</li>'; }).join('') + '</ul>' +
      '<details><summary>' + esc(c.evidence.length) + ' news headlines collected around the event</summary><ul class="small">' + c.evidence.map(function (x) { return '<li>' + esc(x.publisher) + ': ' + link(x.url, x.headline) + '</li>'; }).join('') + '</ul></details></div>';
    if (c.case && c.case.finding) {
      html += '<div class="card"><h2>Finding ' + chip(c.case.finding.validation_status === 'VALIDATED' ? 'CONFIRMED BY HUMAN' : c.case.finding.validation_status, c.case.finding.validation_status === 'VALIDATED' ? 'c-good' : 'c-warn') + '</h2>' + findingView(c.case.finding.findings || {}, c.case.finding) +
        '<div class="btns"><button class="btn" id="toLearning">Next: turn it into a Learning Candidate &rarr;</button></div></div>';
      app.innerHTML = html;
      document.getElementById('toLearning').onclick = function () { current = 'Learning'; render(); };
      return;
    }
    html += '<div class="card"><h2>1. Ask an external AI</h2><p class="small muted">Copy the research pack, paste it into one of these tools, and let it research. Nothing is sent automatically.</p>' +
      '<div class="btns"><button class="btn" id="copyPack">Copy AI research pack</button>' +
      '<a class="btn secondary" href="https://chatgpt.com" target="_blank" rel="noopener">ChatGPT</a><a class="btn secondary" href="https://claude.ai/new" target="_blank" rel="noopener">Claude</a>' +
      '<a class="btn secondary" href="https://gemini.google.com" target="_blank" rel="noopener">Gemini</a><a class="btn secondary" href="https://grok.com" target="_blank" rel="noopener">Grok</a></div>' +
      '<div id="copyMsg" class="small"></div><details style="margin-top:10px"><summary>Preview the pack</summary><pre id="pack">' + esc(c.research_pack) + '</pre></details></div>';
    html += '<div class="card"><h2>2. Paste the AI answer</h2><textarea id="aiText" placeholder="Paste the full answer, including the JSON block at the end"></textarea>' +
      '<div class="grid2"><div><label>Which AI?</label><select id="provider">' + opt(['chatgpt','claude','gemini','grok','other'], (draft && draft._provider) || 'chatgpt') + '</select></div><div></div></div>' +
      '<div class="btns"><button class="btn" id="parse">Structure the answer</button></div><div id="parseMsg" class="small"></div></div>';
    if (draft) html += draftForm(c);
    app.innerHTML = html;
    document.getElementById('copyPack').onclick = function () {
      var text = c.research_pack, msg = document.getElementById('copyMsg');
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(function () { msg.className = 'small ok'; msg.textContent = 'Copied. Paste it into your AI tool.'; },
        function () { msg.className = 'small warn'; msg.textContent = 'Copy blocked by the browser. Open the preview and copy it by hand.'; });
    };
    document.getElementById('parse').onclick = function () {
      var text = document.getElementById('aiText').value, provider = document.getElementById('provider').value, msg = document.getElementById('parseMsg');
      msg.textContent = 'Structuring...';
      postJson('/api/learning/parse', { text: text }).then(function (r) {
        if (!r.finding) { msg.className = 'small err'; msg.textContent = r.error || 'Could not read the answer.'; return; }
        draft = r.finding; draft._provider = provider; draftWarnings = (r.ok ? [] : [r.error]).concat(r.warnings || []);
        renderResearch(); var f = app.querySelector('[data-f="explanation"]'); if (f) f.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    };
    var btn = document.getElementById('confirm');
    if (btn) btn.onclick = function () {
      var msg = document.getElementById('confirmMsg');
      if (!document.getElementById('reviewed').checked) { msg.className = 'small err'; msg.textContent = 'Tick the review box first: a human must confirm the finding.'; return; }
      var f = readDraft(), provider = draft._provider; delete f._provider;
      msg.className = 'small'; msg.textContent = 'Saving...';
      postJson('/api/learning/findings', { event_id: focusId, provider: provider, finding: f }, document.getElementById('token').value).then(function (r) {
        if (!r.ok) { msg.className = 'small err'; msg.textContent = r.error || 'Could not save.'; return; }
        draft = null; delete caseCache[focusId]; load(function () { renderResearch(); });
      });
    };
  }

  function renderLearning() {
    if (!market) { app.innerHTML = '<div class="card muted">Loading&hellip;</div>'; return; }
    var confirmed = market.events.filter(function (x) { return x.case && x.case.finding && x.case.finding.validation_status === 'VALIDATED'; });
    var html = '<div class="card"><h2>The loop</h2><p class="small">Every confirmed finding becomes a <b>Learning Candidate</b>: a precise proposed change to V1\\'s sources. V1 is then recalculated with that change, the result is validated against what BTC actually did, and only an explicit human approval creates a new V1 methodology version. Nothing here changes V1 automatically.</p>' + stepper('LEARN') + '</div>';
    html += '<div class="card"><h2>Confirmed findings ready to learn from (' + confirmed.length + ')</h2>';
    if (!confirmed.length) html += '<p class="muted">None yet. Investigate an event on the Market tab and confirm what you discover.</p>';
    confirmed.forEach(function (x) {
      html += '<div class="ev" data-id="' + x.event_id + '"><div class="small muted">' + esc(x.headline) + '</div>' + findingView(x.case.finding.findings || {}, x.case.finding) +
        '<div class="btns"><button class="btn" disabled title="Next build slice">Create Learning Candidate</button><span class="small muted" style="align-self:center">Next build step: source adjustment &rarr; V1 recalculation</span></div></div>';
    });
    app.innerHTML = html + '</div>';
  }

  function render() { renderNav(); if (current === 'Market') return renderMarket(); if (current === 'Research') return renderResearch(); return renderLearning(); }
  function load(after) {
    getJson('/api/learning/market').then(function (d) {
      market = d; if (d.ok && (focusId === null || !focusEvent())) focusId = d.focus_event_id; if (after) after(); else render();
    }, function (err) { market = { ok: false, error: String(err) }; render(); });
  }
  render(); load();
})();
</script>
</body>
</html>`;
