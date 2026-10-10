// Run the Drive mirror (mirror_core.gs) against a LOCAL folder instead of Google Drive: dry runs and tests.
//   node research/archive/apps_script/local_run.mjs --drive DIR --feed FEED.json --repo REPO --blobs BLOBDIR [--budget-ms N]
// Sources are resolved offline: commit-pinned raw GitHub URLs via `git show` in REPO, feed blobs from BLOBDIR.
// It never deletes or overwrites (the same rules as in Drive); prints the run report as JSON.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';

const args = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, all) => (v.startsWith('--') ? a.concat([[v.slice(2), all[i + 1]]]) : a), []));
const core = {};
vm.runInNewContext(fs.readFileSync(new URL('./mirror_core.gs', import.meta.url), 'utf8') + '\nthis.runMirror = runMirror;', core);
const h = (b, a) => crypto.createHash(a).update(Buffer.from(b)).digest('hex');
const root = path.resolve(args.drive);

const drive = {
  listChildren: (dir) => fs.readdirSync(dir, { withFileTypes: true }).map((d) => {
    const p = path.join(dir, d.name);
    if (d.isDirectory()) return { id: p, name: d.name, mimeType: 'application/vnd.google-apps.folder' };
    const b = fs.readFileSync(p);
    return { id: p, name: d.name, mimeType: 'application/octet-stream', md5: h(b, 'md5'), size: String(b.length) };
  }),
  createFolder: (parent, name) => { const p = path.join(parent, name); fs.mkdirSync(p); return p; },
  createFile: (parent, name, bytes) => { const p = path.join(parent, name); fs.writeFileSync(p, Buffer.from(bytes), { flag: 'wx' }); return p; },
  getMeta: (id) => { const b = fs.readFileSync(id); return { md5: h(b, 'md5'), size: String(b.length) }; },
  readBytes: (id) => fs.readFileSync(id),
};

const RAW = /^https:\/\/raw\.githubusercontent\.com\/[^/]+\/[^/]+\/([^/]+)\/(.+)$/;
function fetchLocal(url) {
  if (url === 'FEED') return { status: 200, bytes: fs.readFileSync(args.feed) };
  const m = RAW.exec(url);
  try {
    if (m && m[1] === 'archive-feed' && m[2].startsWith('blobs/')) return { status: 200, bytes: fs.readFileSync(path.join(args.blobs, m[2].slice(6))) };
    if (m) return { status: 200, bytes: execFileSync('git', ['-C', args.repo, 'show', `${m[1]}:${m[2]}`], { maxBuffer: 1 << 30 }) };
  } catch { /* fall through */ }
  return { status: 404, bytes: Buffer.alloc(0) };
}

let t = 0;
const report = core.runMirror({
  drive, rootId: root, feedUrl: 'FEED', budgetMs: Number(args['budget-ms'] || 1e12), deepVerifyShare: Number(args['deep-share'] || 0),
  fetch: fetchLocal, hash: h, utf8: (s) => Buffer.from(s, 'utf8'), text: (b) => Buffer.from(b).toString('utf8'),
  now: () => args.now || new Date().toISOString(), clock: () => (args['tick-ms'] ? (t += Number(args['tick-ms'])) : Date.now()),
});
process.stdout.write(JSON.stringify(report, null, 1) + '\n');
process.exit(['OK', 'NOOP', 'ADOPTED', 'PARTIAL'].includes(report.status) ? 0 : 1);
