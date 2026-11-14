// Sentiment V2 independent evidence collector (scope lock 2026-11-14): RSS/Atom feeds and official public endpoints
// only, from an explicit registry of approved URLs. No search API, no crawling, no paid provider, no keys.
//
// Runs inside PulseWorkerV2's existing hourly non-tick cron (no new Cron Trigger) with a hard per-run budget, because
// the account is on Workers Free: 10 ms CPU and 50 external subrequests per invocation (Event Detection shares the
// invocation). Writes only the three sv2_* tables of migration 0029; never touches V1 tables.

export const SV2_COLLECTOR_VERSION = 'sv2-collector-1';
export const SV2_COLLECTOR_USER_AGENT = 'CryptoPulse-SentimentV2/sv2-collector-1 (+https://github.com/quiquandon-oss/PulseWorkerV2)';

// ---- Source registry ----------------------------------------------------------------------------------------------
// Every entry is an approved public URL. `family` is a Sentiment V2 information family (sv2Config().families).
// Feeds V1 already reads (Cointelegraph, CoinDesk, The Block, BBC World, Investing.com) are deliberately NOT here, so
// V2's evidence is independent of V1's readings; `v1_overlap` names a V1 source when overlap is unavoidable (none now).
// interval_min: minimum minutes between fetches; freshness_h: an item older than this no longer counts at decision time.
// ua: 'sec' sources need the owner-configured EVENT_SEC_USER_AGENT (SEC fair-access policy); without it they report
// NOT_CONFIGURED and are skipped.
export const SV2_SOURCE_REGISTRY = [
  { id: 'decrypt', name: 'Decrypt', family: 'market_sentiment_news', kind: 'rss', url: 'https://decrypt.co/feed', interval_min: 180, freshness_h: 24, v1_overlap: null },
  { id: 'bitcoinmagazine', name: 'Bitcoin Magazine', family: 'market_sentiment_news', kind: 'rss', url: 'https://bitcoinmagazine.com/.rss/full/', interval_min: 180, freshness_h: 24, v1_overlap: null },
  { id: 'cryptoslate', name: 'CryptoSlate', family: 'market_sentiment_news', kind: 'rss', url: 'https://cryptoslate.com/feed/', interval_min: 180, freshness_h: 24, v1_overlap: null },
  { id: 'sec_press', name: 'SEC press releases', family: 'regulation_geopolitics', kind: 'rss', url: 'https://www.sec.gov/news/pressreleases.rss', interval_min: 480, freshness_h: 168, ua: 'sec', v1_overlap: null },
  { id: 'cftc_press', name: 'CFTC press releases', family: 'regulation_geopolitics', kind: 'rss', url: 'https://www.cftc.gov/RSS/RSSGP/rssgp.xml', interval_min: 480, freshness_h: 168, v1_overlap: null },
  { id: 'fed_press', name: 'Federal Reserve press releases', family: 'macroeconomic', kind: 'rss', url: 'https://www.federalreserve.gov/feeds/press_all.xml', interval_min: 480, freshness_h: 168, v1_overlap: null },
  { id: 'ecb_press', name: 'ECB press releases', family: 'macroeconomic', kind: 'rss', url: 'https://www.ecb.europa.eu/rss/press.html', interval_min: 480, freshness_h: 168, v1_overlap: null },
  { id: 'mstr_8k', name: 'Strategy (MSTR) 8-K filings, SEC EDGAR', family: 'corporate_treasury_catalysts', kind: 'atom', url: 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001050446&type=8-K&dateb=&owner=include&count=20&output=atom', interval_min: 720, freshness_h: 720, ua: 'sec', v1_overlap: null },
];

// ---- Explicit budgets (Workers Free) ------------------------------------------------------------------------------
export const SV2_COLLECTOR_LIMITS = {
  // Sized for Workers Free (10 ms CPU per invocation, shared with Event Detection and V2): parsing costs about
  // 6 ms per MB in a local measurement, so ≤ 768 KiB per run keeps the collector near 4-5 ms in the worst case.
  maxSourcesPerRun: 3,          // ≤ 3 feeds per non-tick hour → capacity 48 fetches/day (registry needs ~38)
  maxSubrequestsPerRun: 8,      // fetches incl. redirects and retries; Event Detection keeps the rest of the 50
  maxBytesPerResponse: 256 * 1024,
  maxBytesPerRun: 768 * 1024,
  timeoutMs: 8000,
  retries: 1,                   // one retry on network error, timeout, 429 or 5xx
  maxRedirects: 2,
  maxItemsPerFeed: 25,
  titleMax: 300, summaryMax: 600,
  backoffMaxFactor: 16,         // consecutive failures stretch the interval up to 16×
  dupWindowMs: 7 * 86400000,    // same normalised title from another source within 7 days = duplicate
};

const ALLOWED_CONTENT = /^(application\/(rss\+xml|atom\+xml|xml|rdf\+xml)|text\/(xml|html|plain))\b/i;

export function sv2RegistryHosts(registry = SV2_SOURCE_REGISTRY) {
  return new Set(registry.map(s => new URL(s.url).hostname.toLowerCase()));
}

// ---- URL safety (SSRF) -------------------------------------------------------------------------------------------
// Only https, only hosts in the registry allowlist, default port, no credentials, no IP literals or internal names.
export function sv2CheckUrl(raw, allowedHosts) {
  let u;
  try { u = new URL(raw); } catch (_e) { return { ok: false, reason: 'invalid URL' }; }
  if (u.protocol !== 'https:') return { ok: false, reason: 'scheme must be https' };
  if (u.username || u.password) return { ok: false, reason: 'credentials in URL' };
  if (u.port && u.port !== '443') return { ok: false, reason: 'non-default port' };
  const host = u.hostname.toLowerCase();
  if (/^\[.*\]$/.test(host) || host.includes(':')) return { ok: false, reason: 'IP literal' };
  if (/^\d+$/.test(host) || /^(\d{1,3}\.){3}\d{1,3}$/.test(host) || /^0x[0-9a-f]+$/i.test(host)) return { ok: false, reason: 'IP literal' };
  if (host === 'localhost' || /\.(localhost|local|internal|lan|home|corp|intranet)$/.test(host) || !host.includes('.')) return { ok: false, reason: 'internal host name' };
  if (!allowedHosts.has(host)) return { ok: false, reason: `host not in registry allowlist: ${host}` };
  return { ok: true, url: u };
}

// Reads at most maxBytes from a response body; reports truncation instead of buffering an unbounded body.
async function readCapped(res, maxBytes) {
  const len = Number(res.headers.get('content-length'));
  if (Number.isFinite(len) && len > maxBytes) { try { await res.body?.cancel(); } catch (_e) { /* ignore */ } return { text: null, bytes: 0, tooLarge: true }; }
  if (!res.body || !res.body.getReader) {
    const t = await res.text();
    const bytes = new TextEncoder().encode(t).length;
    return bytes > maxBytes ? { text: null, bytes, tooLarge: true } : { text: t, bytes, tooLarge: false };
  }
  const reader = res.body.getReader();
  const chunks = []; let bytes = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    bytes += value.byteLength;
    if (bytes > maxBytes) { try { await reader.cancel(); } catch (_e) { /* ignore */ } return { text: null, bytes, tooLarge: true }; }
    chunks.push(value);
  }
  const all = new Uint8Array(bytes); let off = 0;
  for (const c of chunks) { all.set(c, off); off += c.byteLength; }
  return { text: new TextDecoder('utf-8').decode(all), bytes, tooLarge: false };
}

// Bounded GET: allowlisted https only, manual redirects (each target re-validated), timeout, size cap, one retry on
// network error/timeout/429/5xx, conditional request headers. Every attempt and redirect counts against the run budget.
export async function sv2SafeFetch(url, { allowedHosts, budget, fetchImpl = fetch, headers = {}, limits = SV2_COLLECTOR_LIMITS, sleep } = {}) {
  const wait = sleep || (ms => new Promise(r => setTimeout(r, ms)));
  let current = url, redirects = 0, attempt = 0;
  for (;;) {
    const chk = sv2CheckUrl(current, allowedHosts);
    if (!chk.ok) return { ok: false, status: 'BLOCKED_URL', error: chk.reason, redirects };
    if (budget.subrequests >= limits.maxSubrequestsPerRun) return { ok: false, status: 'BUDGET_EXHAUSTED', error: 'per-run subrequest budget reached', redirects };
    if (budget.bytes >= limits.maxBytesPerRun) return { ok: false, status: 'BUDGET_EXHAUSTED', error: 'per-run byte budget reached', redirects };
    budget.subrequests++;
    let res;
    try {
      const signal = AbortSignal.timeout ? AbortSignal.timeout(limits.timeoutMs) : undefined;
      res = await fetchImpl(chk.url.toString(), { method: 'GET', redirect: 'manual', signal,
        headers: { 'User-Agent': SV2_COLLECTOR_USER_AGENT, Accept: 'application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8', ...headers } });
    } catch (e) {
      if (attempt < limits.retries) { attempt++; await wait(500); continue; }
      return { ok: false, status: 'NETWORK_ERROR', error: e && e.name === 'TimeoutError' ? 'timeout' : String((e && e.message) || e).slice(0, 160), redirects };
    }
    if (res.status >= 300 && res.status < 400 && res.status !== 304) {
      const loc = res.headers.get('location');
      try { await res.body?.cancel(); } catch (_e) { /* ignore */ }
      if (!loc) return { ok: false, status: 'BAD_REDIRECT', error: `HTTP ${res.status} without Location`, redirects };
      if (redirects >= limits.maxRedirects) return { ok: false, status: 'TOO_MANY_REDIRECTS', error: `more than ${limits.maxRedirects} redirects`, redirects };
      redirects++;
      current = new URL(loc, chk.url).toString();
      continue;                                     // re-validated at the top of the loop
    }
    if (res.status === 304) return { ok: true, status: 'NOT_MODIFIED', notModified: true, finalUrl: chk.url.toString(), redirects, bytes: 0 };
    if (res.status === 429 || res.status >= 500) {
      try { await res.body?.cancel(); } catch (_e) { /* ignore */ }
      if (attempt < limits.retries) { attempt++; await wait(1000); continue; }
      return { ok: false, status: res.status === 429 ? 'RATE_LIMITED' : 'HTTP_ERROR', error: `HTTP ${res.status}`, redirects };
    }
    if (!res.ok) { try { await res.body?.cancel(); } catch (_e) { /* ignore */ } return { ok: false, status: 'HTTP_ERROR', error: `HTTP ${res.status}`, redirects }; }
    const ct = res.headers.get('content-type') || '';
    if (ct && !ALLOWED_CONTENT.test(ct)) { try { await res.body?.cancel(); } catch (_e) { /* ignore */ } return { ok: false, status: 'BAD_CONTENT_TYPE', error: `content-type ${ct.slice(0, 60)}`, redirects }; }
    let body;
    try {
      body = await readCapped(res, Math.min(limits.maxBytesPerResponse, limits.maxBytesPerRun - budget.bytes));
    } catch (e) {
      // A disconnect or timeout while reading the body gets the same single retry as one before the headers.
      if (attempt < limits.retries) { attempt++; await wait(500); continue; }
      return { ok: false, status: 'NETWORK_ERROR', error: 'body read failed: ' + (e && e.name === 'TimeoutError' ? 'timeout' : String((e && e.message) || e).slice(0, 140)), redirects };
    }
    budget.bytes += body.bytes;
    if (body.tooLarge) return { ok: false, status: 'TOO_LARGE', error: `response over ${limits.maxBytesPerResponse} bytes`, redirects };
    return { ok: true, status: 'OK', text: body.text, bytes: body.bytes, finalUrl: chk.url.toString(), redirects,
      etag: res.headers.get('etag'), lastModified: res.headers.get('last-modified') };
  }
}

// ---- Feed parsing (RSS 2.0, RSS 1.0/RDF, Atom) ---------------------------------------------------------------------
const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
export function sv2DecodeText(s) {
  if (s === null || s === undefined) return '';
  let t = String(s).replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1');
  t = t.replace(/<[^>]*>/g, ' ');
  t = t.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === '#') { const n = e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10); return Number.isFinite(n) && n > 0 && n < 0x110000 ? String.fromCodePoint(n) : ' '; }
    return ENT[e.toLowerCase()] ?? m;
  });
  return t.replace(/\s+/g, ' ').trim();
}
// Tag content by index search on a pre-lowercased copy, not a lazy regex over the whole block: article bodies can be
// hundreds of KB, and the Workers Free CPU budget is 10 ms per invocation. maxLen cuts long content before decoding.
function tag(block, lower, name, maxLen = 4096) {
  const n = name.toLowerCase();
  let from = 0;
  for (;;) {
    const i = lower.indexOf('<' + n, from);
    if (i < 0) return null;
    const c = lower.charCodeAt(i + n.length + 1);
    if (c === 62 || c === 32 || c === 9 || c === 10 || c === 13) {          // '>' or whitespace: exact tag name
      const open = lower.indexOf('>', i);
      if (open < 0) return null;
      if (lower.charCodeAt(open - 1) === 47) return '';                     // self-closing
      const close = lower.indexOf('</' + n + '>', open + 1);
      const end = close < 0 ? block.length : close;
      return block.slice(open + 1, Math.min(end, open + 1 + maxLen));
    }
    from = i + 1;
  }
}
// Item/entry blocks by index search over the lowercased document (one pass, no backtracking regex).
function blocks(xml, lower, name, max) {
  const out = [];
  let from = 0;
  while (out.length < max) {
    let i = lower.indexOf('<' + name, from);
    while (i >= 0) { const c = lower.charCodeAt(i + name.length + 1); if (c === 62 || c === 32 || c === 9 || c === 10 || c === 13) break; i = lower.indexOf('<' + name, i + 1); }
    if (i < 0) break;
    const j = lower.indexOf('</' + name + '>', i);
    if (j < 0) break;
    out.push([i, j + name.length + 3]);
    from = j + name.length + 3;
  }
  return out;
}
function atomLink(block) {
  const links = [...block.slice(0, 8192).matchAll(/<link\b([^>]*)\/?>/gi)].map(m => m[1]);
  const attr = (a, k) => { const m = a.match(new RegExp(`\\b${k}\\s*=\\s*["']([^"']*)["']`, 'i')); return m ? m[1] : null; };
  const alt = links.find(a => !attr(a, 'rel') || attr(a, 'rel') === 'alternate') || links[0];
  return alt ? attr(alt, 'href') : null;
}
export function sv2ParseTs(s) {
  if (!s) return null;
  const t = Date.parse(sv2DecodeText(s));
  return Number.isFinite(t) ? t : null;
}
export function sv2ParseFeed(xml, { maxItems = SV2_COLLECTOR_LIMITS.maxItemsPerFeed, titleMax = SV2_COLLECTOR_LIMITS.titleMax, summaryMax = SV2_COLLECTOR_LIMITS.summaryMax } = {}) {
  if (typeof xml !== 'string' || !/<(rss|feed|rdf:RDF)\b/i.test(xml)) return { ok: false, error: 'not an RSS/Atom document', items: [] };
  const lowerDoc = xml.toLowerCase();
  const isAtom = lowerDoc.includes('<feed') && !lowerDoc.includes('<rss');
  const spans = blocks(xml, lowerDoc, isAtom ? 'entry' : 'item', maxItems);
  const items = [];
  for (const [i0, i1] of spans) {
    const b = xml.slice(i0, i1), lb = lowerDoc.slice(i0, i1);
    const title = sv2DecodeText(tag(b, lb, 'title', 2 * titleMax)).slice(0, titleMax);
    const link = isAtom ? atomLink(b) : (sv2DecodeText(tag(b, lb, 'link', 2048)) || sv2DecodeText(tag(b, lb, 'guid', 2048)));
    const published = sv2ParseTs(tag(b, lb, 'pubDate', 128) || tag(b, lb, 'published', 128) || tag(b, lb, 'updated', 128) || tag(b, lb, 'dc:date', 128));
    const summary = sv2DecodeText(tag(b, lb, 'description', 4 * summaryMax) || tag(b, lb, 'summary', 4 * summaryMax) || tag(b, lb, 'content', 4 * summaryMax)).slice(0, summaryMax);
    if (!title || !link) continue;
    items.push({ title, link: link.trim(), published_ts: published, summary });
  }
  return { ok: true, format: isAtom ? 'atom' : 'rss', items };
}

// ---- Canonical URLs, hashing, duplicates -------------------------------------------------------------------------
const TRACKING = /^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|cmpid|guccounter|guce_[a-z_]+|_hs[a-z]+|mkt_tok)$/i;
export function sv2CanonicalUrl(raw) {
  let u;
  try { u = new URL(raw); } catch (_e) { return null; }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
  u.protocol = 'https:';
  u.hash = '';
  u.hostname = u.hostname.toLowerCase().replace(/^www\./, '');
  if (u.port === '443' || u.port === '80') u.port = '';
  const keep = [...u.searchParams.entries()].filter(([k]) => !TRACKING.test(k)).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  u.search = keep.length ? '?' + keep.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&') : '';
  if (u.pathname.length > 1 && u.pathname.endsWith('/')) u.pathname = u.pathname.slice(0, -1);
  return u.toString();
}
export function sv2NormTitle(t) {
  return String(t || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]+/g, ' ').trim();
}
export async function sv2Sha256(text) {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('');
}

// ---- Scheduling ---------------------------------------------------------------------------------------------------
// A source is due when now ≥ last_attempt + interval × 2^min(failures, 4) (capped). Most overdue first.
export function sv2DueSources(registry, stateById, now, limits = SV2_COLLECTOR_LIMITS, disabled = new Set()) {
  const due = [];
  for (const s of registry) {
    if (disabled.has(s.id)) continue;
    const st = stateById[s.id] || {};
    const fails = Number(st.consecutive_failures) || 0;
    const factor = Math.min(limits.backoffMaxFactor, 2 ** Math.min(fails, 4));
    const next = st.last_attempt_ts ? Number(st.last_attempt_ts) + s.interval_min * 60000 * factor : 0;
    if (now >= next) due.push({ s, overdue: now - next });
  }
  due.sort((a, b) => b.overdue - a.overdue || (a.s.id < b.s.id ? -1 : 1));
  return due.slice(0, limits.maxSourcesPerRun).map(d => d.s);
}

// ---- One collection run --------------------------------------------------------------------------------------------
// Fetches due sources, stores new items (INSERT OR IGNORE on evidence_id = sha256(canonical URL)), marks cross-source
// duplicates by normalised-title hash, updates per-source reliability state and records the run with its budget use.
export async function sv2CollectEvidence(env, { nowTs, fetchImpl, registry = SV2_SOURCE_REGISTRY, limits = SV2_COLLECTOR_LIMITS, sleep } = {}) {
  if (String(env.SV2_COLLECTOR_ENABLED === undefined ? '0' : env.SV2_COLLECTOR_ENABLED) !== '1') return { ok: true, skipped: true, reason: 'disabled' };
  const now = nowTs || Date.now();
  const disabled = new Set(String(env.SV2_SOURCES_DISABLED || '').split(',').map(x => x.trim()).filter(Boolean));
  const allowedHosts = sv2RegistryHosts(registry);
  const budget = { subrequests: 0, bytes: 0 };
  const run = { ok: true, run_ts: now, version: SV2_COLLECTOR_VERSION, sources: [], items_new: 0, items_duplicate: 0, items_seen: 0, errors: [] };
  let stateRows;
  try {
    const r = await env.DB.prepare('SELECT * FROM sv2_source_state LIMIT 100').all();
    stateRows = r.results || [];
  } catch (err) {
    return { ...run, ok: false, errors: ['state read failed: ' + String((err && err.message) || err).slice(0, 160)] };
  }
  const stateById = Object.fromEntries(stateRows.map(r => [r.source_id, r]));
  const due = sv2DueSources(registry, stateById, now, limits, disabled);
  for (const s of due) {
    const st = stateById[s.id] || {};
    const out = { id: s.id, status: null, items: 0, new: 0, duplicate: 0, bytes: 0, error: null };
    let stmtsFor = [], freshItems = [];
    let headers = {};
    if (s.ua === 'sec') {
      if (!env.EVENT_SEC_USER_AGENT) { out.status = 'NOT_CONFIGURED'; out.error = 'EVENT_SEC_USER_AGENT not set (SEC fair-access policy)'; }
      else headers['User-Agent'] = String(env.EVENT_SEC_USER_AGENT);
    }
    let parsed = null;
    if (!out.status) {
      if (st.etag) headers['If-None-Match'] = st.etag;
      if (st.last_modified) headers['If-Modified-Since'] = st.last_modified;
      const res = await sv2SafeFetch(s.url, { allowedHosts, budget, fetchImpl, headers, limits, sleep });
      out.bytes = res.bytes || 0;
      if (!res.ok) { out.status = res.status; out.error = res.error; }
      else if (res.notModified) out.status = 'NOT_MODIFIED';
      else {
        parsed = sv2ParseFeed(res.text, limits);
        if (!parsed.ok) { out.status = 'PARSE_ERROR'; out.error = parsed.error; }
        else { out.status = 'OK'; out.etag = res.etag; out.lastModified = res.lastModified; }
      }
    }
    if (parsed && parsed.ok) {
      out.items = parsed.items.length;
      // One indexed lookup per feed for already-stored items, so only new items cost hashing, a duplicate check and a write.
      const cands = [];
      const seenIds = new Set();
      for (const it of parsed.items) {
        const canon = sv2CanonicalUrl(it.link);
        if (!canon) continue;
        const evidenceId = 'ev_' + (await sv2Sha256(canon)).slice(0, 32);
        if (seenIds.has(evidenceId)) continue;
        seenIds.add(evidenceId);
        cands.push({ it, canon, evidenceId });
      }
      run.items_seen += cands.length;
      let known = new Set();
      if (cands.length) {
        try {
          const kr = await env.DB.prepare(`SELECT evidence_id FROM sv2_evidence WHERE evidence_id IN (${cands.map(() => '?').join(',')}) LIMIT ?`)
            .bind(...cands.map(c => c.evidenceId), cands.length).all();
          known = new Set((kr.results || []).map(r => r.evidence_id));
        } catch (err) {
          out.status = 'STORE_ERROR'; out.error = 'lookup failed: ' + String((err && err.message) || err).slice(0, 160);
        }
      }
      // D1 limits queries per Worker invocation and counts a db.batch() as one query: per feed this is one lookup
      // (above), one batch of duplicate checks and one batch of writes (items + source state), so a run issues at most
      // 2 + 3 x maxSourcesPerRun D1 queries however many items arrive.
      const fresh = [];
      for (const { it, canon, evidenceId } of out.status === 'STORE_ERROR' ? [] : cands.filter(c => !known.has(c.evidenceId))) {
        // Items dated in the future (clock skew) keep no publication time; their retrieval time still bounds their use.
        const pub = it.published_ts !== null && it.published_ts <= now + 300000 ? it.published_ts : null;
        fresh.push({ it, canon, evidenceId, pub, titleHash: await sv2Sha256(sv2NormTitle(it.title)),
          contentHash: await sv2Sha256([canon, it.title, it.summary, String(pub)].join('\n')) });
      }
      if (fresh.length) {
        try {
          const dups = await env.DB.batch(fresh.map(f => env.DB.prepare('SELECT evidence_id FROM sv2_evidence WHERE title_hash = ? AND source_id <> ? AND retrieved_ts >= ? AND dup_of IS NULL ORDER BY retrieved_ts ASC LIMIT 1')
            .bind(f.titleHash, s.id, now - limits.dupWindowMs)));
          fresh.forEach((f, k) => { const r = dups[k] && dups[k].results && dups[k].results[0]; f.dupOf = r ? r.evidence_id : null; });
        } catch (err) {
          out.status = 'STORE_ERROR'; out.error = 'duplicate check failed: ' + String((err && err.message) || err).slice(0, 160);
        }
      }
      if (out.status === 'STORE_ERROR') fresh.length = 0;
      stmtsFor = fresh.map(f => env.DB.prepare('INSERT OR IGNORE INTO sv2_evidence (evidence_id, source_id, family, canonical_url, title, summary, published_ts, retrieved_ts, title_hash, content_hash, dup_of, collector_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
        .bind(f.evidenceId, s.id, s.family, f.canon, f.it.title, f.it.summary || null, f.pub, now, f.titleHash, f.contentHash, f.dupOf, SV2_COLLECTOR_VERSION));
      freshItems = fresh;
    }
    const stateStmt = (status, error, newCount) => {
      const success = status === 'OK' || status === 'NOT_MODIFIED';
      const notConfigured = status === 'NOT_CONFIGURED';
      return env.DB.prepare(`INSERT INTO sv2_source_state (source_id, last_attempt_ts, last_success_ts, last_status, last_error, consecutive_failures, ok_count, fail_count, last_new_items, etag, last_modified)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET last_attempt_ts = excluded.last_attempt_ts,
          last_success_ts = COALESCE(excluded.last_success_ts, sv2_source_state.last_success_ts), last_status = excluded.last_status,
          last_error = excluded.last_error,
          consecutive_failures = CASE WHEN ? THEN 0 ELSE sv2_source_state.consecutive_failures + 1 END,
          ok_count = sv2_source_state.ok_count + excluded.ok_count, fail_count = sv2_source_state.fail_count + excluded.fail_count,
          last_new_items = excluded.last_new_items, etag = COALESCE(excluded.etag, sv2_source_state.etag),
          last_modified = COALESCE(excluded.last_modified, sv2_source_state.last_modified)`)
        .bind(s.id, now, success ? now : null, status, error, success || notConfigured ? 0 : 1, success ? 1 : 0, success || notConfigured ? 0 : 1,
          newCount, out.etag || null, out.lastModified || null, success || notConfigured ? 1 : 0);
    };
    // Items and the source state are written in one batch; new and duplicate counts come from each insert's changes.
    const newCount = freshItems.filter(f => !f.dupOf).length;
    try {
      const res = await env.DB.batch([...stmtsFor, stateStmt(out.status, out.error, newCount)]);
      freshItems.forEach((f, k) => {
        const changed = res[k] && res[k].meta && Number(res[k].meta.changes) > 0;
        if (!changed) return;
        if (f.dupOf) { out.duplicate++; run.items_duplicate++; } else { out.new++; run.items_new++; }
      });
    } catch (err) {
      out.status = 'STORE_ERROR'; out.error = 'store failed: ' + String((err && err.message) || err).slice(0, 160);
      out.new = 0; out.duplicate = 0;
      try { await stateStmt(out.status, out.error, 0).run(); }
      catch (err2) { run.errors.push(`${s.id}: state write failed: ` + String((err2 && err2.message) || err2).slice(0, 120)); }
    }
    const success = out.status === 'OK' || out.status === 'NOT_MODIFIED';
    const notConfigured = out.status === 'NOT_CONFIGURED';
    if (!success && !notConfigured) run.errors.push(`${s.id}: ${out.status} ${out.error || ''}`.trim());
    delete out.etag; delete out.lastModified;
    run.sources.push(out);
  }
  run.budget = { subrequests: budget.subrequests, subrequest_limit: limits.maxSubrequestsPerRun, bytes: budget.bytes, byte_limit: limits.maxBytesPerRun,
    sources_due_run: due.length, source_limit: limits.maxSourcesPerRun };
  try {
    await env.DB.prepare('INSERT OR IGNORE INTO sv2_collection_runs (run_ts, collector_version, sources_json, items_seen, items_new, items_duplicate, subrequests, bytes, errors_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)')
      .bind(now, SV2_COLLECTOR_VERSION, JSON.stringify(run.sources), run.items_seen, run.items_new, run.items_duplicate, budget.subrequests, budget.bytes, JSON.stringify(run.errors)).run();
  } catch (err) {
    run.errors.push('run record failed: ' + String((err && err.message) || err).slice(0, 120));
  }
  run.ok = run.errors.length === 0;
  return run;
}
