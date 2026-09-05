// Tail Worker for pulseworker-v2 -- forwards ONLY the LINK_* diagnostic
// checkpoint logs (added in PR #34) into a dedicated D1 table, so they
// can be queried after the fact instead of requiring someone to be
// watching wrangler tail / the dashboard's Live view at the exact
// moment a cron tick fires.
//
// Built 2026-09-05 because no available Claude/MCP tool can read
// Workers Real-time Logs or the Workers Logs (Observability) API
// directly -- this Tail Worker is the one mechanism that can get this
// data somewhere queryable (D1) without modifying pulseworker-v2's own
// application logic at all.
//
// IMPORTANT PREREQUISITE: Cloudflare's own docs state Tail Workers are
// available on the Workers Paid and Enterprise tiers, not Free. If the
// account is on Workers Free, attaching this as a tail_consumer may
// simply not take effect. Flagged explicitly in the PR -- not something
// this code can detect or work around.
//
// Scope discipline, matching every other diagnostic added this
// investigation: no filtering/aggregation logic beyond "is this a
// LINK_* checkpoint", no retries, no alerting, no other coins' events
// touched, nothing written except the 8 fields the schema defines.
// A parse failure or unexpected shape on any single log line is
// swallowed (try/catch per line) so it can never affect delivery of
// the other lines in the same batch, and can never affect the producer
// Worker (tail() runs after the producer has already finished; nothing
// here can feed back into pulseworker-v2's own execution regardless).

const LINK_CHECKPOINT_PREFIX = 'LINK_';

export default {
  async tail(events, env, ctx) {
    ctx.waitUntil(processEvents(events, env));
  },
};

export async function processEvents(events, env) {
  const rows = [];
  const receivedTs = Date.now();

  for (const event of events) {
    if (!event || !Array.isArray(event.logs)) continue;
    for (const log of event.logs) {
      try {
        const parsed = extractLinkCheckpoint(log);
        if (!parsed) continue;
        rows.push({
          received_ts: receivedTs,
          event_ts: event.eventTimestamp ?? null,
          coin: parsed.coin ?? null,
          horizon: parsed.horizon ?? null,
          checkpoint: parsed.evt,
          elapsed_ms: parsed.elapsed_ms ?? null,
          outcome: event.outcome ?? null,
          script_name: event.scriptName ?? null,
        });
      } catch (e) {
        // A single malformed log line must never drop the rest of this
        // batch -- skip it and continue.
        continue;
      }
    }
  }

  if (rows.length === 0) return;

  try {
    const statements = rows.map((r) =>
      env.DB.prepare(
        `INSERT INTO link_diagnostic_log
         (received_ts, event_ts, coin, horizon, checkpoint, elapsed_ms, outcome, script_name)
         VALUES (?,?,?,?,?,?,?,?)`
      ).bind(r.received_ts, r.event_ts, r.coin, r.horizon, r.checkpoint, r.elapsed_ms, r.outcome, r.script_name)
    );
    await env.DB.batch(statements);
  } catch (e) {
    // Tail Workers have no caller to report failure to and no producer
    // execution to protect (it already finished) -- just don't crash.
    console.error('link_diagnostic_log batch insert failed:', String(e));
  }
}

// Returns { evt, coin, horizon, elapsed_ms } if `log` is one of the
// LINK_* checkpoint lines from PR #34's instrumentation, or null for
// anything else (BTC's/ETH's own logs, PR #30/#31's batch_complete
// lines, unrelated console output, etc. -- all deliberately ignored).
export function extractLinkCheckpoint(log) {
  if (!log || !Array.isArray(log.message) || log.message.length === 0) return null;
  const first = log.message[0];
  if (typeof first !== 'string' || !first.includes(LINK_CHECKPOINT_PREFIX)) return null;
  let parsed;
  try {
    parsed = JSON.parse(first);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed.evt !== 'string' || !parsed.evt.startsWith(LINK_CHECKPOINT_PREFIX)) return null;
  return parsed;
}
