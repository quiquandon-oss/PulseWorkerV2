// Deterministic synthetic fixtures (seeded PRNG). They exercise the code paths; they are NOT
// production data. A production-data fixture needs an authorised bounded export (see report).
export function prng(seed) { let s = seed >>> 0; return () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
const H = 3600 * 1000;
export function seed(db, nowTs, { days = 120, seedValue = 20261108 } = {}) {
  const r = prng(seedValue);
  const start = nowTs - days * 24 * H;
  const series = (table, col, p0, extra) => {
    let p = p0; const ins = db.prepare(`INSERT INTO ${table} (ts, ${col}, technical_score${extra ? ', funding_adj' : ''}) VALUES (?,?,?${extra ? ',?' : ''})`);
    for (let t = start; t <= nowTs - 3 * H; t += 3 * H) {
      p *= 1 + (r() - 0.5) * 0.03;
      const tech = Math.round(20 + r() * 60);
      if (extra) ins.run(t, +p.toFixed(4), tech, +((r() - 0.5) * 1e-4).toFixed(8)); else ins.run(t, +p.toFixed(2), tech);
    }
  };
  series('btc_data', 'btc_price', 60000);
  series('eth_data', 'eth_price', 3000);
  series('link_data', 'link_price', 15, true);
  // V1 history: hourly sentiment rows for the last ~500 h (V1 keeps 500).
  const hi = db.prepare('INSERT INTO history (ts, score, technical_score, btc_price, regime_mag, bottom_score, sources_json) VALUES (?,?,?,?,?,?,?)');
  for (let i = 500; i >= 1; i--) hi.run(nowTs - i * H, Math.round(r() * 100), Math.round(r() * 100), 60000, +(r() * 3).toFixed(3), Math.round(r() * 100), '{}');
}
export function hyperliquidStub(nowTs) {
  const r = prng(nowTs % 100000);
  const px = (b) => (b * (1 + (r() - 0.5) * 0.01)).toFixed(4);
  const body = [{ universe: [{ name: 'BTC' }, { name: 'ETH' }, { name: 'LINK' }] },
    [{ markPx: px(60000), funding: '0.0000125' }, { markPx: px(3000), funding: '0.0000100' }, { markPx: px(15), funding: '0.0000150' }]];
  return async (input) => {
    const url = typeof input === 'string' ? input : input.url;
    if (url.startsWith('https://api.hyperliquid.xyz/info')) return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
    return new Response('replay: external call blocked', { status: 503 });
  };
}
