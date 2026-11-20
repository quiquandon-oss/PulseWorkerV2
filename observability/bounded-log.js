// Bounded diagnostic logging (Observability volume reduction, 2026-11).
// Workers Logs are diagnostic only: audit records live in D1 (glm_runs, predictions, d1_usage_daily, ...).
// Every logged payload is capped at LOG_MAX_BYTES (UTF-8). An oversized payload is replaced by a short
// marker that keeps the event name, the original size and a truncated head, so the line stays parseable.
// Callers must log IDs, counts, statuses and reason codes only: never evidence text, prompts, model output
// or portfolio data. Logging never throws.

export const LOG_MAX_BYTES = 512;

const enc = new TextEncoder();
const byteLen = (s) => enc.encode(s).length;

// Longest prefix of `s` whose UTF-8 length is <= max bytes (never splits a code point).
function headBytes(s, max) {
  if (max <= 0) return '';
  if (byteLen(s) <= max) return s;
  let lo = 0, hi = s.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (byteLen(s.slice(0, mid)) <= max) lo = mid; else hi = mid - 1;
  }
  let out = s.slice(0, lo);
  // Do not end on a lone high surrogate.
  if (out.length && /[\uD800-\uDBFF]$/.test(out)) out = out.slice(0, -1);
  return out;
}

// JSON line for `obj`, at most `max` bytes.
export function boundedJson(obj, max = LOG_MAX_BYTES) {
  let s;
  try { s = JSON.stringify(obj); } catch { s = JSON.stringify({ evt: 'log_unserializable' }); }
  if (s === undefined) s = 'null';
  const n = byteLen(s);
  if (n <= max) return s;
  const evt = obj && typeof obj === 'object' ? String(obj.evt ?? obj.kind ?? '').slice(0, 64) : '';
  const shell = { evt, truncated: true, bytes: n, head: '' };
  // JSON-escaping can grow the head; shrink the head budget until the whole line fits.
  let budget = Math.max(0, max - byteLen(JSON.stringify(shell)));
  for (;;) {
    shell.head = headBytes(s, budget);
    const line = JSON.stringify(shell);
    const over = byteLen(line) - max;
    if (over <= 0) return line;
    if (budget === 0) return JSON.stringify({ truncated: true, bytes: n });
    budget = Math.max(0, budget - over);
  }
}

// console[level](prefix, payload) with prefix + payload <= LOG_MAX_BYTES. `payload` is an object (serialised
// with boundedJson) or a string (truncated). Never throws.
export function logBounded(level, prefix, payload) {
  try {
    const fn = (console[level] || console.log).bind(console);
    const pre = prefix ? headBytes(String(prefix), 64) : '';
    const room = LOG_MAX_BYTES - (pre ? byteLen(pre) + 1 : 0);
    const body = typeof payload === 'string' ? headBytes(payload, room) : boundedJson(payload, room);
    if (pre) fn(pre, body); else fn(body);
  } catch { /* logging must never affect the invocation */ }
}
