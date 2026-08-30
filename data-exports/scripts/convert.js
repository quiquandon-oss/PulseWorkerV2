// Converts the raw `wrangler d1 execute --json` output (one file per
// table, written by the export workflow) into clean CSVs plus a dynamic
// STATUS.md. Run after all raw/*.json files exist for this cycle.
//
// wrangler's --json output shape: an array with one element,
// { results: [...], success: true, meta: {...} } -- same shape the
// Cloudflare D1 MCP tool returns, handled defensively here in case either
// wraps it slightly differently across versions.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const RAW_DIR = path.join(__dirname, '..', 'raw');
const OUT_DIR = path.join(__dirname, '..');

const TABLES = [
  { raw: 'btc_predictions.json', csv: 'btc_predictions.csv', label: 'BTC predictions' },
  { raw: 'link_predictions.json', csv: 'link_predictions.csv', label: 'LINK predictions' },
  { raw: 'eth_predictions.json', csv: 'eth_predictions.csv', label: 'ETH predictions' },
  { raw: 'challenger_predictions.json', csv: 'challenger_predictions.csv', label: 'Challenger predictions (all coins)' },
  { raw: 'selection_decisions.json', csv: 'selection_decisions.csv', label: 'Selection decisions' },
  { raw: 'selection_decisions_momentum.json', csv: 'selection_decisions_momentum.csv', label: 'Momentum experiment (Learning Roadmap §3 Experiment 3, logged-only)' },
];

function extractRows(parsed) {
  if (Array.isArray(parsed) && parsed[0] && Array.isArray(parsed[0].results)) return parsed[0].results;
  if (parsed && Array.isArray(parsed.results)) return parsed.results;
  if (Array.isArray(parsed)) return parsed; // already a bare row array, just in case
  throw new Error('Unrecognized D1 JSON shape');
}

function csvEscape(value) {
  if (value === null || value === undefined) return '';
  const s = String(value);
  if (/[",\n\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}

function toCsv(rows) {
  if (rows.length === 0) return '';
  const cols = Object.keys(rows[0]);
  const lines = [cols.join(',')];
  for (const r of rows) {
    lines.push(cols.map((c) => csvEscape(r[c])).join(','));
  }
  return lines.join('\n') + '\n';
}

function main() {
  const summary = [];
  for (const t of TABLES) {
    const rawPath = path.join(RAW_DIR, t.raw);
    if (!fs.existsSync(rawPath)) {
      summary.push({ ...t, ok: false, error: 'raw file missing' });
      continue;
    }
    let rows;
    try {
      const parsed = JSON.parse(fs.readFileSync(rawPath, 'utf8'));
      rows = extractRows(parsed);
    } catch (err) {
      summary.push({ ...t, ok: false, error: String(err) });
      continue;
    }
    fs.writeFileSync(path.join(OUT_DIR, t.csv), toCsv(rows));
    const timestamps = rows.map((r) => r.ts).filter((v) => typeof v === 'number');
    summary.push({
      ...t,
      ok: true,
      rowCount: rows.length,
      earliestTs: timestamps.length ? Math.min(...timestamps) : null,
      latestTs: timestamps.length ? Math.max(...timestamps) : null,
    });
  }

  const nowIso = new Date().toISOString();
  const lines = [
    '# Export status (auto-generated, do not hand-edit)',
    '',
    `Last export: ${nowIso}`,
    '',
    '| File | Rows | Earliest | Latest |',
    '|---|---|---|---|',
  ];
  for (const s of summary) {
    if (!s.ok) {
      lines.push(`| ${s.csv} | ERROR: ${s.error} | | |`);
      continue;
    }
    const earliest = s.earliestTs ? new Date(s.earliestTs).toISOString() : 'n/a';
    const latest = s.latestTs ? new Date(s.latestTs).toISOString() : 'n/a';
    lines.push(`| ${s.csv} | ${s.rowCount} | ${earliest} | ${latest} |`);
  }
  lines.push('');
  lines.push('See README.md in this directory for column documentation and context.');
  lines.push('');
  fs.writeFileSync(path.join(OUT_DIR, 'STATUS.md'), lines.join('\n'));

  const anyFailed = summary.some((s) => !s.ok);
  if (anyFailed) {
    console.error('One or more exports failed:', summary.filter((s) => !s.ok));
    process.exit(1);
  }
  console.log('Export complete:', summary.map((s) => `${s.csv}=${s.rowCount}`).join(', '));
}

main();
