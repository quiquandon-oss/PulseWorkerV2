// Infers a permissive SQLite schema from the SQL text in worker.js and its local modules.
// Production has no checked-in base schema, so every table gets an INTEGER PRIMARY KEY `id`
// plus every column the code inserts, updates, filters or selects. Columns are untyped
// (SQLite affinity NONE) so values round-trip exactly as the code wrote them.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SQL_KW = new Set(('select from where and or not null is in as on join left inner outer group by order asc desc limit ' +
  'offset having count sum avg min max case when then else end distinct exists values insert into update set delete ' +
  'ignore replace conflict do nothing like between coalesce abs round cast integer real text julianday strftime date ' +
  'datetime ifnull nullif total length substr lower upper instr json_extract over partition rowid true false all union ' +
  'with recursive primary key default unique index create table if collate nocase glob escape returning iif printf').split(' '));

export function collectSources(root) {
  const files = [join(root, 'worker.js')];
  for (const d of ['events', 'learning']) {
    try { for (const f of readdirSync(join(root, d))) if (f.endsWith('.js')) files.push(join(root, d, f)); } catch {}
  }
  return files.map((f) => readFileSync(f, 'utf8')).join('\n');
}

export function inferSchema(src) {
  const tables = new Map();
  const add = (t, cols) => {
    if (!/^[a-z_][a-z0-9_]*$/i.test(t) || SQL_KW.has(t.toLowerCase())) return;
    if (!tables.has(t)) tables.set(t, new Set());
    for (const c of cols) if (/^[a-z_][a-z0-9_]*$/i.test(c) && !SQL_KW.has(c.toLowerCase())) tables.get(t).add(c);
  };
  const strings = [...src.matchAll(/(`|'|")((?:SELECT|INSERT|UPDATE|DELETE|WITH)\b[\s\S]*?)\1/gi)].map((m) => m[2]);
  for (let s of strings) {
    s = s.replace(/\$\{[^}]*\}/g, ' __T__ ');
    for (const m of s.matchAll(/INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]*)\)/gi)) add(m[1], m[2].split(',').map((x) => x.trim()));
    for (const m of s.matchAll(/UPDATE\s+(\w+)\s+SET\s+([\s\S]*?)(?:WHERE|$)/gi)) add(m[1], [...m[2].matchAll(/(\w+)\s*=/g)].map((x) => x[1]));
    // SELECT columns + WHERE identifiers attributed to the single FROM table (joins handled by alias prefix)
    const from = [...s.matchAll(/FROM\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?/gi)];
    if (from.length === 1) {
      const t = from[0][1];
      const idents = [...s.replace(/'[^']*'/g, '').matchAll(/\b([a-z_][a-z0-9_]*)\b/gi)].map((m) => m[1]);
      const aliases = new Set([...s.matchAll(/\bAS\s+(\w+)/gi)].map((m) => m[1]));
      add(t, idents.filter((i) => !aliases.has(i) && i !== t && i !== '__T__' && !/^\d/.test(i)));
    } else {
      for (const m of from) {
        const alias = m[2];
        if (alias) add(m[1], [...s.matchAll(new RegExp(`\\b${alias}\\.(\\w+)`, 'g'))].map((x) => x[1]));
        else add(m[1], []);
      }
    }
  }
  return tables;
}

export function ddl(tables, extraCols = {}) {
  const out = [];
  for (const [t, cols] of [...tables].sort()) {
    const all = new Set([...cols, ...(extraCols[t] || [])]);
    all.delete('id');
    out.push(`CREATE TABLE IF NOT EXISTS ${t} (id INTEGER PRIMARY KEY AUTOINCREMENT${[...all].sort().map((c) => `, ${c}`).join('')});`);
  }
  return out;
}
