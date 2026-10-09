// Read-only extract for the T-A1 / T-A2 progress run. RESEARCH ONLY.
//
// Runs three FIXED SELECT statements against production D1 through the Cloudflare D1 query API and writes
// v1_extract.json and btc_extract.json for research/v1_prospective_eval.mjs. Nothing else.
// - The statements are constants; anything that is not a single plain SELECT is refused before any request.
// - Credential: RESEARCH_D1_READONLY_TOKEN only (a token scoped to D1 read on this account). There is no fallback to
//   any other token; without it the script stops before any request.
// - Every response must report changes = 0, rows_written = 0 and changed_db = false, otherwise nothing is written.
// - The token is never printed or written.
//
// Usage: RESEARCH_D1_READONLY_TOKEN=... node research/v1_prospective_extract.mjs --out-dir <empty directory>
import { existsSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const ACCOUNT_ID = 'f58e761fbc8e62dc404d8684290af264';          // the Worker's own config file (not a secret)
export const DATABASE_ID = 'f91ca980-b886-423a-bd6f-f3baea46d181';     // sentiment-history, production
export const BOUNDARY_MS = 1791561600000;
export const BTC_FROM_MS = BOUNDARY_MS - 4 * 3600000;                  // the outcome rule looks back at most 4h
export const STATEMENTS = Object.freeze({
  archive: `SELECT observation_ts AS ts, score, sources_json FROM research_sentiment_archive WHERE observation_ts > ${BOUNDARY_MS} ORDER BY observation_ts`,
  history: `SELECT ts, score, sources_json FROM history WHERE ts > ${BOUNDARY_MS} ORDER BY ts`,
  btc: `SELECT ts, btc_price FROM btc_data WHERE ts >= ${BTC_FROM_MS} ORDER BY ts`,
});
const WRITE_WORDS = /\b(INSERT|UPDATE|DELETE|REPLACE|UPSERT|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|TRIGGER|BEGIN|COMMIT|ROLLBACK)\b/i;

export function assertReadOnly(sql) {
  if (typeof sql !== 'string' || !/^SELECT\s/i.test(sql) || sql.includes(';') || WRITE_WORDS.test(sql)) throw new Error('refused: not a single plain SELECT');
  return true;
}

export function checkResponse(body) {
  const r = body && Array.isArray(body.result) && body.result.length === 1 ? body.result[0] : null;
  if (!body || body.success !== true || !r || r.success !== true || !Array.isArray(r.results)) throw new Error('D1 query failed');
  const m = r.meta || {};
  if (m.changes !== 0 || m.rows_written !== 0 || m.changed_db !== false) throw new Error('D1 reported a change; nothing written');
  return { rows: r.results, meta: { changes: m.changes, rows_written: m.rows_written, changed_db: m.changed_db, rows_read: m.rows_read } };
}

export async function extract({ token, fetchImpl = fetch, now = Date.now }) {
  if (typeof token !== 'string' || !token) throw new Error('RESEARCH_D1_READONLY_TOKEN is not set; no fallback, nothing requested');
  for (const sql of Object.values(STATEMENTS)) assertReadOnly(sql);
  const asOf = now();                                                    // taken before the reads: settledness is judged conservatively
  const url = `https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/d1/database/${DATABASE_ID}/query`;
  const got = {};
  for (const [key, sql] of Object.entries(STATEMENTS)) {
    const res = await fetchImpl(url, { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ sql }) });
    let body = null;
    try { body = await res.json(); } catch (_e) { body = null; }
    if (!res.ok) throw new Error(`D1 query ${key} failed with HTTP ${res.status}`);
    got[key] = checkResponse(body);
  }
  const base = { extractor: 'v1_prospective_extract', as_of_ms: asOf, as_of_utc: new Date(asOf).toISOString(), database: `sentiment-history (${DATABASE_ID}), production, SELECT only` };
  return {
    v1: { manifest: { ...base, statements: [STATEMENTS.archive, STATEMENTS.history], rows: { archive: got.archive.rows.length, history: got.history.rows.length },
      d1_meta: [got.archive.meta, got.history.meta] }, archive: got.archive.rows, history: got.history.rows },
    btc: { manifest: { ...base, statements: [STATEMENTS.btc], rows: got.btc.rows.length, d1_meta: [got.btc.meta] }, rows: got.btc.rows },
  };
}

async function main(argv) {
  const i = argv.indexOf('--out-dir');
  const dir = i >= 0 ? argv[i + 1] : null;
  if (!dir || !existsSync(dir) || readdirSync(dir).length) { console.error('usage: --out-dir <existing empty directory>'); process.exit(2); }
  const out = await extract({ token: process.env.RESEARCH_D1_READONLY_TOKEN });
  writeFileSync(join(dir, 'v1_extract.json'), `${JSON.stringify(out.v1, null, 1)}\n`);
  writeFileSync(join(dir, 'btc_extract.json'), `${JSON.stringify(out.btc, null, 1)}\n`);
  console.log(`extract as of ${out.v1.manifest.as_of_utc}: archive ${out.v1.manifest.rows.archive}, history ${out.v1.manifest.rows.history}, btc ${out.btc.manifest.rows}`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main(process.argv.slice(2)).catch((e) => { console.error(e.message); process.exit(1); });
