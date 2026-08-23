#!/usr/bin/env node
// Structural safety check, run in CI before the audit itself: confirms
// every SQL template literal in d1-checks.js is SELECT-only. This is a
// static check independent of (and in addition to) runQuery()'s own
// runtime guard against non-SELECT statements -- belt and suspenders.
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('./d1-checks.js', import.meta.url), 'utf8');
// Targets specifically the SQL argument position in runQuery(accountId,
// databaseId, token, `SQL...`, ...) calls -- not every backtick template
// literal in the file (which would also match URLs and error-message
// strings that legitimately contain words like "query failed").
const sqlLiterals = [...src.matchAll(/runQuery\([^,]+,[^,]+,[^,]+,\s*`([^`]*)`/g)].map((m) => m[1]);

if (sqlLiterals.length === 0) {
  console.error('No SQL template literals found at all -- unexpected, investigate before trusting this check.');
  process.exit(1);
}

let failed = false;
for (const sql of sqlLiterals) {
  const trimmed = sql.trim().toUpperCase();
  if (trimmed && !trimmed.startsWith('SELECT')) {
    console.error('Non-SELECT SQL literal found in d1-checks.js:', sql.slice(0, 100));
    failed = true;
  }
}

if (failed) process.exit(1);
console.log(`All ${sqlLiterals.length} SQL literal(s) in d1-checks.js are SELECT-only.`);
