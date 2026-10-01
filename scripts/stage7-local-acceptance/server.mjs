// Serves the REAL worker.js over HTTP on localhost against a local SQLite file.
// Usage: node server.mjs <workerPath> <dbPath> <port>
import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { D1Shim } from './d1_shim.mjs';

// `enabled` mirrors the deployment switch STAGE7_ENABLED (set only by wrangler.staging.toml in real deployments).
export async function startServer({ workerPath, dbPath, port, token = 'local-acceptance-token', enabled = true, sha = 'local-acceptance' }) {
  const worker = (await import(pathToFileURL(workerPath).href)).default;
  const db = new D1Shim(dbPath);
  const env = { DB: db, STAGE7_ADMIN_TOKEN: token, GIT_COMMIT_SHA: sha, ...(enabled ? { STAGE7_ENABLED: 'true' } : {}) };
  const server = http.createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      const body = chunks.length ? Buffer.concat(chunks) : undefined;
      const request = new Request(`http://localhost:${port}${req.url}`, {
        method: req.method, headers: req.headers, body: ['GET', 'HEAD'].includes(req.method) ? undefined : body,
      });
      const response = await worker.fetch(request, env, { waitUntil() {} });
      res.writeHead(response.status, Object.fromEntries(response.headers));
      res.end(Buffer.from(await response.arrayBuffer()));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'text/plain' });
      res.end(String(err && err.stack || err));
    }
  });
  await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));
  return { server, db, close: () => new Promise((r) => { server.close(r); db.close(); }) };
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [workerPath, dbPath, port] = process.argv.slice(2);
  await startServer({ workerPath, dbPath, port: Number(port) });
  console.log(`serving ${workerPath} on http://127.0.0.1:${port}`);
}
