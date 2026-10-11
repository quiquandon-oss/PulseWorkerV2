// Worker entry: the Durable Object wrapper and fetch handler. All logic lives in worker.js (unit-tested).
import { DurableObject } from 'cloudflare:workers';
import { handle, SqlGuard } from './worker.js';

export class DispatchGuard extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.core = new SqlGuard(ctx.storage.sql);
  }
  rateCheck(key, limit, windowMs, now, record) { return this.core.rateCheck(key, limit, windowMs, now, record); }
  acquire(key, ttlMs, now) { return this.core.acquire(key, ttlMs, now); }
  release(key) { return this.core.release(key); }
  appendAudit(entry) { return this.core.appendAudit(entry); }
  listAudit(limit) { return this.core.listAudit(limit); }
}

export default {
  async fetch(request, env) {
    const guard = env.GUARD.get(env.GUARD.idFromName('global'));
    return handle(request, env, { fetch: (u, i) => fetch(u, i), guard, now: () => Date.now(), uuid: () => crypto.randomUUID() });
  },
};
