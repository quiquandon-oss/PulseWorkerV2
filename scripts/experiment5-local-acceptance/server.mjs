// Serves the REAL worker.js over HTTP on localhost against a local SQLite file (no Cloudflare, no network).
// `wrapDb(db)` lets a scenario make specific statements fail (e.g. a transient D1 error) without touching the file.
import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { D1Shim } from './d1_shim.mjs';

export async function startServer({ workerPath, dbPath, port, wrapDb = (d) => d }) {
  const worker = (await import(pathToFileURL(workerPath).href)).default;
  const shim = new D1Shim(dbPath);
  const env = { DB: wrapDb(shim), GIT_COMMIT_SHA: 'local-acceptance' };
  const server = http.createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      const request = new Request(`http://localhost:${port}${req.url}`, {
        method: req.method, headers: req.headers, body: ['GET', 'HEAD'].includes(req.method) ? undefined : Buffer.concat(chunks),
      });
      const response = await worker.fetch(request, env, { waitUntil() {} });
      res.writeHead(response.status, Object.fromEntries(response.headers));
      res.end(Buffer.from(await response.arrayBuffer()));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'text/plain' });
      res.end(String((err && err.stack) || err));
    }
  });
  await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));
  return { server, db: shim, close: () => new Promise((r) => { server.close(r); shim.close(); }) };
}
