// SEC EDGAR (official, free): https://data.sec.gov/submissions/CIK##########.json (recent filings per company) and the
// filing's primary document under https://www.sec.gov/Archives/edgar/data/<cik>/<accession without dashes>/<doc>.
// SEC fair access: a declared User-Agent with contact details is required and the documented ceiling is 10 requests
// per second; this adapter sends at most ~4/s, sequentially, and only when EVENT_SEC_USER_AGENT is configured
// (no contact address is hard-coded). Coverage: US SEC registrants in the tracked list only.
//
// Classification is deterministic and sentence-level. A COMPLETED transaction needs past-tense completion language in
// the same sentence as the asset or instrument; intention / proposal / conditional language yields PROPOSED, and a
// sentence that mixes both is UNKNOWN. An actual purchase or sale is never inferred from an intention.
import { SOURCE_STATUS, SourceError, severityFor, makeEvent, textFingerprint } from '../event-core.js';

const H = 3600_000, D = 24 * H;
export const SEC_SUBMISSIONS = 'https://data.sec.gov/submissions';
export const SEC_ARCHIVES = 'https://www.sec.gov/Archives/edgar/data';

// Each entry is verified at runtime: the submissions response must list the expected ticker, otherwise the
// company is skipped and reported (so a wrong CIK can never attribute a filing to the wrong company).
export const SEC_COMPANIES = [
  { cik: 1050446, ticker: 'MSTR' },
  { cik: 1507605, ticker: 'MARA' },
  { cik: 1167419, ticker: 'RIOT' },
  { cik: 1679788, ticker: 'COIN' },
  { cik: 1318605, ticker: 'TSLA' },
  { cik: 1512673, ticker: 'XYZ' },
  { cik: 1326380, ticker: 'GME' },
  { cik: 1554859, ticker: 'SMLR' },
];

export const SEC_FORMS = ['8-K', '8-K/A', '10-Q', '10-K', '424B2', '424B3', '424B5', '424B7', 'S-3', 'S-3ASR', '6-K'];
export const SEC_CONFIG = { lookbackMs: 7 * D, maxDocsPerRun: 12, maxDocChars: 400_000, spacingMs: 250 };

const COND = /\b(intends?|intention|plans?|planned|may|might|could|would|will|expects?|anticipates?|proposes?|proposed|seeks?|if|subject to|contemplat\w*|potential(ly)?|from time to time)\b/i;
const CRYPTO = /\b(bitcoins?|btc|ether(eum)?|eth|digital assets?|crypto(currenc(y|ies))?)\b/i;
const QTY = /([\d][\d,]*(?:\.\d+)?)\s*(?:bitcoins?|btc)\b/i;

export const SEC_RULES = [
  { type: 'CRYPTO_TREASURY_PURCHASE', test: (s) => CRYPTO.test(s) && /\b(acquired|purchased|bought|acquire|purchase|buy)\b/i.test(s), completed: (s) => /\b(acquired|purchased|bought)\b/i.test(s), proposal: (s) => /\b(acquire|purchase|buy)\b/i.test(s) && COND.test(s) },
  { type: 'CRYPTO_TREASURY_SALE', test: (s) => CRYPTO.test(s) && /\b(sold|disposed of|divested|sell|dispose of)\b/i.test(s), completed: (s) => /\b(sold|disposed of|divested)\b/i.test(s), proposal: (s) => /\b(sell|dispose)\b/i.test(s) && COND.test(s) },
  { type: 'TREASURY_POLICY_CHANGE', test: (s) => /\btreasury (reserve )?(policy|strategy)\b/i.test(s) && /\b(adopt\w*|approv\w*|amend\w*|updat\w*|revis\w*)\b/i.test(s), completed: (s) => /\b(adopted|approved|amended|updated|revised)\b/i.test(s), proposal: (s) => COND.test(s) },
  { type: 'FINANCING_CONVERTIBLE', test: (s) => /\bconvertible (senior )?(notes?|debentures?|bonds?)\b/i.test(s), completed: (s) => /\b(closed|completed|issued|consummated)\b/i.test(s), proposal: (s) => /\b(proposed|intends? to offer|pricing|priced)\b/i.test(s) || COND.test(s) },
  { type: 'FINANCING_EQUITY', test: (s) => /\b(at[- ]the[- ]market|atm (offering|program)|registered direct offering|underwritten (public )?offering|private placement|preferred stock offering|sales agreement)\b/i.test(s), completed: (s) => /\b(closed|completed|sold|issued)\b/i.test(s), proposal: (s) => /\b(proposed|may (offer|sell)|from time to time|up to)\b/i.test(s) || COND.test(s) },
  { type: 'LIQUIDITY_RISK', test: (s) => /\b(substantial doubt|going concern|event of default|margin call|forbearance|liquidity (risk|constraints?|shortfall))\b/i.test(s), completed: () => true, proposal: () => false },
];

export function htmlToText(html) {
  return String(html || '')
    .replace(/<(script|style)[\s\S]*?<\/\1>/gi, ' ')
    .replace(/<br\s*\/?>|<\/(p|div|tr|li|h\d)>/gi, '. ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;|&#160;/gi, ' ')
    .replace(/&amp;/gi, '&').replace(/&#8217;|&rsquo;/gi, "'").replace(/&#8220;|&#8221;|&ldquo;|&rdquo;/gi, '"')
    .replace(/&[a-z#0-9]+;/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export function sentences(text) {
  return String(text).split(/(?<=[.;])\s+(?=[A-Z(])/).map((s) => s.trim()).filter((s) => s.length > 20 && s.length < 1500);
}

// Classifies one document's text. Returns at most one finding per event type, with up to 3 evidence sentences.
export function classifyFilingText(text) {
  const findings = new Map();
  const seen = new Set();
  for (const s of sentences(text)) {
    const fp = textFingerprint(s);
    if (seen.has(fp)) continue;                     // repeated boilerplate counted once
    seen.add(fp);
    for (const rule of SEC_RULES) {
      if (!rule.test(s)) continue;
      const done = rule.completed(s), cond = rule.proposal(s);
      const status = done && !cond ? 'COMPLETED' : (!done && cond) ? 'PROPOSED' : (done && cond) ? 'UNKNOWN' : 'UNKNOWN';
      const qtyM = /TREASURY_(PURCHASE|SALE)/.test(rule.type) ? QTY.exec(s) : null;
      const qty = qtyM ? Number(qtyM[1].replace(/,/g, '')) : null;
      const f = findings.get(rule.type) || { event_type: rule.type, statuses: new Set(), quantities: [], evidence: [] };
      f.statuses.add(status);
      if (Number.isFinite(qty) && status === 'COMPLETED') f.quantities.push(qty);
      if (f.evidence.length < 3) f.evidence.push({ sentence: s.slice(0, 400), transaction_status: status, quantity_btc: Number.isFinite(qty) ? qty : null });
      findings.set(rule.type, f);
    }
  }
  return [...findings.values()].map((f) => ({
    event_type: f.event_type,
    // COMPLETED only if every matching sentence agreed; any disagreement degrades to UNKNOWN.
    transaction_status: f.statuses.size === 1 ? [...f.statuses][0] : 'UNKNOWN',
    quantity_btc: f.quantities.length ? Math.max(...f.quantities) : null,
    evidence: f.evidence,
  }));
}

function severityForFinding(f) {
  if (f.event_type === 'LIQUIDITY_RISK') return 'HIGH';
  if (/TREASURY_(PURCHASE|SALE)/.test(f.event_type)) {
    if (f.transaction_status !== 'COMPLETED') return 'LOW';
    const q = f.quantity_btc;
    return q === null ? 'MEDIUM' : severityFor(q >= 10_000 ? 4 : q >= 1_000 ? 2 : q >= 100 ? 1.4 : 1);
  }
  return f.transaction_status === 'COMPLETED' ? 'MEDIUM' : 'LOW';
}
function confidenceForFinding(f) {
  if (f.transaction_status === 'UNKNOWN') return 0.3;
  if (f.transaction_status === 'PROPOSED') return 0.5;
  return f.quantity_btc !== null ? 0.8 : 0.6;
}

export function recentFilings(sub, { sinceTs, forms = SEC_FORMS }) {
  const r = sub && sub.filings && sub.filings.recent;
  if (!r || !Array.isArray(r.accessionNumber)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'SEC submissions: malformed');
  const out = [];
  for (let i = 0; i < r.accessionNumber.length; i += 1) {
    const accepted = Date.parse(r.acceptanceDateTime && r.acceptanceDateTime[i]);
    if (!Number.isFinite(accepted) || accepted < sinceTs || !forms.includes(r.form[i])) continue;
    out.push({ accession: r.accessionNumber[i], form: r.form[i], filing_date: r.filingDate[i], accepted_ts: accepted, primary_document: r.primaryDocument[i], items: r.items ? r.items[i] : null });
  }
  return out.sort((a, b) => b.accepted_ts - a.accepted_ts);
}

export function filingUrl(cik, accession, doc) {
  return `${SEC_ARCHIVES}/${Number(cik)}/${String(accession).replace(/-/g, '')}/${doc}`;
}

export async function filingEvents({ company, filing, findings, nowTs, source }) {
  const url = filingUrl(company.cik, filing.accession, filing.primary_document);
  const out = [];
  for (const f of findings) {
    out.push(await makeEvent({
      event_type: f.event_type, asset: /TREASURY/.test(f.event_type) ? 'BTC' : null, market: `${company.name} (${company.ticker})`,
      scope: `US SEC registrant CIK ${company.cik}`, source, source_url: url, source_ts: Math.min(filing.accepted_ts, nowTs), detected_ts: nowTs,
      direction: 'UNKNOWN', severity: severityForFinding(f), confidence: confidenceForFinding(f), rule_id: 'ed-sec-filing-v1',
      observed_value: f.quantity_btc, unit: f.quantity_btc !== null ? 'BTC (as stated in filing)' : null,
      evidence: [{ kind: 'sec_filing', company: company.name, ticker: company.ticker, cik: company.cik, form: filing.form, accession: filing.accession, filing_date: filing.filing_date, accepted: new Date(filing.accepted_ts).toISOString(), url, items: filing.items, transaction_status: f.transaction_status, sentences: f.evidence }],
      explanation: `${company.ticker} ${filing.form} (${filing.accession}) contains ${f.event_type.toLowerCase().replace(/_/g, ' ')} language; transaction status ${f.transaction_status}${f.quantity_btc !== null ? `, ${f.quantity_btc} BTC stated` : ''}. Deterministic sentence rules; see the quoted evidence.`,
      dedup_parts: [f.event_type, company.cik, filing.accession],
      expires_ts: nowTs + 7 * D,
    }));
  }
  return out;
}

export const secEdgarAdapter = {
  id: 'sec_edgar',
  label: 'SEC EDGAR filings (tracked crypto-treasury companies)',
  kind: 'corporate_filings',
  freshnessLimitMs: 2 * D,
  async run({ env, http, nowTs, config, sleep }) {
    const cfg = { ...SEC_CONFIG, ...(config || {}) };
    const ua = env && env.EVENT_SEC_USER_AGENT ? String(env.EVENT_SEC_USER_AGENT) : '';
    if (!/\S+\s+\S+@\S+/.test(ua)) {
      return { status: SOURCE_STATUS.NOT_CONFIGURED, observations: [], events: [], coverage: 'none', last_observation_ts: null, details: { reason: 'EVENT_SEC_USER_AGENT must be set to "<name> <contact email>" (SEC fair-access policy).' } };
    }
    const wait = sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
    const headers = { 'User-Agent': ua, Accept: 'application/json, text/html' };
    const events = [], companies = {};
    let docs = 0, lastObs = null, status = SOURCE_STATUS.OK;
    for (const c of (cfg.companies || SEC_COMPANIES)) {
      const cikPad = String(c.cik).padStart(10, '0');
      let sub;
      try { sub = await http.json(`${SEC_SUBMISSIONS}/CIK${cikPad}.json`, { headers }); }
      catch (e) { companies[c.ticker] = { status: e.status || SOURCE_STATUS.PROVIDER_ERROR, error: e.message }; status = e.status || SOURCE_STATUS.PROVIDER_ERROR; continue; }
      await wait(cfg.spacingMs);
      if (!Array.isArray(sub.tickers) || !sub.tickers.map((t) => String(t).toUpperCase()).includes(c.ticker)) {
        companies[c.ticker] = { status: 'SKIPPED_IDENTITY_MISMATCH', reported_tickers: sub.tickers || null };
        continue;
      }
      const company = { ...c, name: sub.name || c.ticker };
      const filings = recentFilings(sub, { sinceTs: nowTs - cfg.lookbackMs });
      companies[c.ticker] = { status: 'OK', recent_filings: filings.length, classified: 0 };
      for (const f of filings) {
        lastObs = Math.max(lastObs || 0, f.accepted_ts);
        if (docs >= cfg.maxDocsPerRun || !f.primary_document) continue;
        let text;
        try { text = htmlToText((await http.text(filingUrl(c.cik, f.accession, f.primary_document), { headers })).slice(0, cfg.maxDocChars)); }
        catch (e) { companies[c.ticker].doc_error = e.message; continue; }
        docs += 1;
        await wait(cfg.spacingMs);
        const findings = classifyFilingText(text);
        events.push(...(await filingEvents({ company, filing: f, findings, nowTs, source: this.id })));
        companies[c.ticker].classified += 1;
      }
    }
    return {
      status, observations: [], events, last_observation_ts: lastObs,
      coverage: `US SEC only; ${(cfg.companies || SEC_COMPANIES).length} tracked registrants; last ${Math.round(cfg.lookbackMs / D)} days; up to ${cfg.maxDocsPerRun} documents per run`,
      details: { companies, documents_read: docs, requests: { ...http.counters } },
    };
  },
};
