// Finds every SQL SELECT literal in the Worker's source and classifies it as bounded or unbounded.
// Bounded: has a LIMIT, an equality on a selective column bound to a parameter (`id = ?`), or a range on a
// timestamp column.
// Everything else can read a whole table and grows with it.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

export const SOURCES = ['worker.js', 'events', 'learning', 'sentiment-v2'];

export function readSources(root) {
  const out = [];
  for (const s of SOURCES) {
    const p = join(root, s);
    if (s.endsWith('.js')) out.push([s, readFileSync(p, 'utf8')]);
    else for (const f of readdirSync(p).filter((f) => f.endsWith('.js')).sort()) out.push([`${s}/${f}`, readFileSync(join(p, f), 'utf8')]);
  }
  return out;
}

export const LOW_CARDINALITY = new Set(['coin', 'horizon_hours', 'horizon', 'status', 'subject', 'variant', 'source', 'kind', 'category', 'mode', 'direction', 'coin_filter']);

export const normalise = (sql) => sql.replace(/\$\{[^}]*\}/g, '${}').replace(/\s+/g, ' ').trim();

export function isBounded(sql) {
  if (/\bLIMIT\b/i.test(sql)) return true;
  const where = sql.split(/\bWHERE\b/i).slice(1).join(' WHERE ');
  if (!where) return false;
  // Equality on a bound parameter bounds the read only for selective columns: a filter on coin or
  // horizon still reads every row of that coin or horizon, and grows with the table.
  for (const m of where.matchAll(/\b(?:\w+\.)?(\w+)\s*=\s*\?/g)) if (!LOW_CARDINALITY.has(m[1].toLowerCase())) return true;
  if (/\b[\w.]*(ts|_at|time)\s*(>=|>|<=|<|BETWEEN)\s*/i.test(where)) return true;
  return false;
}

export function scan(root) {
  const found = [];
  for (const [file, src] of readSources(root)) {
    for (const m of src.matchAll(/(`|'|")((?:SELECT|WITH)\b[\s\S]*?)\1/g)) {
      const sql = normalise(m[2]);
      if (!/\bFROM\b/i.test(sql)) continue;               // SELECT 1, SELECT ? AS x, ...
      found.push({ file, line: src.slice(0, m.index).split('\n').length, sql, bounded: isBounded(sql) });
    }
  }
  return found;
}

export function unboundedCounts(root) {
  const counts = {};
  for (const q of scan(root)) if (!q.bounded) counts[q.sql] = (counts[q.sql] || 0) + 1;
  return counts;
}

if (process.argv[1] && process.argv[1].endsWith('scan.mjs') && process.argv[2] === '--write-baseline') {
  const { writeFileSync } = await import('node:fs');
  const counts = unboundedCounts(process.argv[3] || '.');
  const sorted = Object.fromEntries(Object.entries(counts).sort((a, b) => (a[0] < b[0] ? -1 : 1)));
  writeFileSync(join(process.argv[3] || '.', 'tests/query-guard/unbounded-baseline.json'), JSON.stringify(sorted, null, 1) + '\n');
  console.log(Object.keys(sorted).length, 'distinct unbounded statements,', Object.values(sorted).reduce((a, b) => a + b, 0), 'occurrences');
}
