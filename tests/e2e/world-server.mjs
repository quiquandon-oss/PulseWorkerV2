// Serves the local PulseWorkerV2 world over HTTP on 127.0.0.1, so a browser test can run in another process.
import http from 'node:http';
const [,, worldModule, root, port, ...flags] = process.argv;
const { makeWorld, H } = await import(worldModule);
const quiet = console.log; console.log = () => {};
const T0 = Date.UTC(2026, 10, 1, 0, 0, 0);
const w = await makeWorld({ root, t0: T0, warmupTicks: 72, env: Object.fromEntries(flags.filter((f) => f.includes('=')).map((f) => f.split('='))) });
if (flags.includes('force-variant')) {
  const snap = JSON.parse(w.d1.db.prepare("SELECT body_json FROM prediction_snapshots WHERE coin='BTC' AND horizon_hours=24").get().body_json);
  snap.selection = { ...snap.selection, chosen_variant: 'calibrated', cleared_gate: true, chosen_p_up: 0.81, cycle_p_up: 0.27 };
  if (flags.includes('no-cycle')) delete snap.selection.cycle_p_up;
  w.d1.db.prepare("UPDATE prediction_snapshots SET body_json=? WHERE coin='BTC' AND horizon_hours=24").run(JSON.stringify(snap));
}
w.clock.now = T0 - 2 * H;
http.createServer(async (req, res) => {
  if (req.url === '/__snapshot') {
    res.end(w.d1.db.prepare("SELECT body_json FROM prediction_snapshots WHERE coin='BTC' AND horizon_hours=24").get().body_json); return;
  }
  const r = await w.worker.fetch(new Request('https://pulseworker-v2.replay' + req.url, { method: req.method }), w.env, { waitUntil: () => {} });
  res.writeHead(r.status, Object.fromEntries(r.headers.entries())); res.end(await r.text());
}).listen(Number(port), '127.0.0.1', () => quiet(`ready ${port} now=${new Date(T0 - 2 * H).toISOString()}`));
