// Local tests for the Drive mirror (mirror_core.gs) against an in-memory Drive. Run: node --test research/archive/apps_script/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';

const core = {};
vm.runInNewContext(fs.readFileSync(new URL('./mirror_core.gs', import.meta.url), 'utf8') + '\nthis.exports = {runMirror, canonicalJson, parseSums, STATE_DIR, listTree};', core);
const { canonicalJson, STATE_DIR } = core.exports;
const runMirror = (ctx) => JSON.parse(JSON.stringify(core.exports.runMirror(ctx)));   // plain objects (the core runs in its own realm)

const h = (b, a) => crypto.createHash(a).update(Buffer.from(b)).digest('hex');

/** Drive-like store: names are not unique, every file has md5 + size, failures can be injected. */
class MockDrive {
  constructor() { this.nodes = new Map([['root', { name: '', folder: true, parent: null }]]); this.n = 0; this.creates = 0; this.failAfter = Infinity; this.calls = []; }
  listChildren(id) {
    return [...this.nodes].filter(([, v]) => v.parent === id && !v.trashed).map(([k, v]) => ({
      id: k, name: v.name, mimeType: v.folder ? 'application/vnd.google-apps.folder' : 'application/octet-stream',
      md5: v.folder ? undefined : (v.md5 ?? h(v.bytes, 'md5')), size: v.folder ? undefined : String(v.bytes.length) }));
  }
  createFolder(parent, name) { const id = 'd' + this.n++; this.nodes.set(id, { name, folder: true, parent }); return id; }
  createFile(parent, name, bytes) {
    if (++this.creates > this.failAfter) throw new Error('simulated interruption');
    const id = 'f' + this.n++; this.nodes.set(id, { name, parent, bytes: Buffer.from(bytes) }); this.calls.push(['create', name]); return id;
  }
  getMeta(id) { const v = this.nodes.get(id); return { md5: v.md5 ?? h(v.bytes, 'md5'), size: String(v.bytes.length) }; }
  readBytes(id) { return this.nodes.get(id).bytes; }
  // test helpers (not part of the adapter)
  put(path, bytes) {
    const parts = path.split('/'); let parent = 'root';
    for (const p of parts.slice(0, -1)) {
      const hit = [...this.nodes].find(([, v]) => v.parent === parent && v.folder && v.name === p);
      parent = hit ? hit[0] : this.createFolder(parent, p);
    }
    const id = 'f' + this.n++; this.nodes.set(id, { name: parts.at(-1), parent, bytes: Buffer.from(bytes) }); return id;
  }
  find(path) {
    const parts = path.split('/'); let parent = 'root', hit;
    for (const p of parts) { hit = [...this.nodes].filter(([, v]) => v.parent === parent && v.name === p); if (!hit.length) return []; parent = hit[0][0]; }
    return hit;
  }
}

function makeCtx(drive, web, feed, extra = {}) {
  let t = 0;
  web.set('FEED', Buffer.from(JSON.stringify(feed)));
  return Object.assign({
    drive, rootId: 'root', feedUrl: 'FEED', budgetMs: 1e9, deepVerifyShare: 0,
    fetch: (u) => web.has(u) ? { status: 200, bytes: web.get(u) } : { status: 404, bytes: Buffer.alloc(0) },
    hash: h, utf8: (s) => Buffer.from(s, 'utf8'), text: (b) => Buffer.from(b).toString('utf8'),
    now: () => '2026-10-11T14:00:00.000Z', clock: () => t++,
  }, extra);
}

function entry(path, bytes, origin, url) {
  return { path, kind: 'raw', dataset: 'ds', snapshot: 's', origin, size: bytes.length, sha256: h(bytes, 'sha256'), md5: h(bytes, 'md5'), ...(url ? { url } : {}) };
}
function feedOf(files, seq = 1) {
  return { format: 'cryptopulse-archive-feed-v1', feed_seq: seq, files, files_sha256: h(Buffer.from(canonicalJson(files)), 'sha256') };
}

/** A Drive holding a verified baseline (two files + SHA256SUMS), and the feed entries describing it. */
function baseline() {
  const drive = new MockDrive();
  const a = Buffer.from('baseline a'), b = Buffer.from('bundle bytes');
  drive.put('10_raw/frozen/ds/a.json', a);
  drive.put('05_code/x.bundle', b);
  const sums = `${h(a, 'sha256')}  10_raw/frozen/ds/a.json\n${h(b, 'sha256')}  05_code/x.bundle\n`;
  drive.put('SHA256SUMS', sums);
  drive.put('_verification/upload_verification.json', '{}');
  const files = [entry('10_raw/frozen/ds/a.json', a, 'baseline'),
                 { ...entry('05_code/x.bundle', b, 'baseline_adopt'), sha256: null, md5: null },
                 { path: 'SHA256SUMS', kind: 'manifest', dataset: null, snapshot: null, origin: 'baseline_adopt', size: Buffer.byteLength(sums), sha256: null, md5: null }];
  return { drive, files };
}

function adopted() {
  const { drive, files } = baseline();
  const web = new Map();
  const r = runMirror(makeCtx(drive, web, feedOf(files)));
  assert.equal(r.status, 'ADOPTED', JSON.stringify(r.problems));
  return { drive, files, web };
}

test('first run adopts the verified baseline, writes manifest 0, and a re-run is a no-op', () => {
  const { drive, files, web } = adopted();
  assert.equal(drive.find(`${STATE_DIR}/manifest-000000.json`).length, 1);
  const m = JSON.parse(drive.readBytes(drive.find(`${STATE_DIR}/manifest-000000.json`)[0][0]));
  assert.equal(m.added.length, 3);
  assert.equal(m.parent, null);
  const again = runMirror(makeCtx(drive, web, feedOf(files)));
  assert.equal(again.status, 'NOOP');
  assert.equal(drive.find(`${STATE_DIR}/manifest-000001.json`).length, 0);     // nothing new: no new manifest
});

test('adoption refuses a baseline whose bytes differ from the reference, an unexpected file, or a duplicate', () => {
  for (const spoil of [
    (d) => { d.nodes.get(d.find('10_raw/frozen/ds/a.json')[0][0]).md5 = '0'.repeat(32); },
    (d) => { d.put('10_raw/frozen/ds/stray.txt', 'x'); },
    (d) => { d.put('10_raw/frozen/ds/a.json', 'baseline a'); },
  ]) {
    const { drive, files } = baseline();
    spoil(drive);
    const r = runMirror(makeCtx(drive, new Map(), feedOf(files)));
    assert.equal(r.status, 'FAIL');
    assert.equal(drive.find(`${STATE_DIR}/manifest-000000.json`).length, 0);
  }
});

test('incremental run uploads only new files, then publishes the manifest last', () => {
  const { drive, files, web } = adopted();
  const n1 = Buffer.from('partition 1'), n2 = Buffer.from('partition 2');
  web.set('u1', n1); web.set('u2', n2);
  const feed = feedOf([...files, entry('10_raw/prospective/f/partitions/1.gz', n1, 'git', 'u1'), entry('10_raw/prospective/f/partitions/2.gz', n2, 'git', 'u2')], 2);
  const r = runMirror(makeCtx(drive, web, feed));
  assert.equal(r.status, 'OK', JSON.stringify(r.problems));
  assert.deepEqual(r.uploaded, ['10_raw/prospective/f/partitions/1.gz', '10_raw/prospective/f/partitions/2.gz']);
  assert.deepEqual(drive.calls.map((c) => c[1]).slice(-3), ['1.gz', '2.gz', 'manifest-000001.json']);   // manifest after the files
  const m = JSON.parse(drive.readBytes(drive.find(`${STATE_DIR}/manifest-000001.json`)[0][0]));
  assert.equal(m.added.length, 2);
  assert.equal(m.parent.name, `${STATE_DIR}/manifest-000000.json`);
  assert.equal(m.totals.files, 5);
  assert.equal(runMirror(makeCtx(drive, web, feed)).status, 'NOOP');
});

test('an interrupted upload leaves no manifest; the restart keeps the finished file and completes', () => {
  const { drive, files, web } = adopted();
  const items = [1, 2, 3].map((i) => { const b = Buffer.from('p' + i); web.set('u' + i, b); return entry(`p/${i}.gz`, b, 'git', 'u' + i); });
  const feed = feedOf([...files, ...items], 2);
  drive.failAfter = drive.creates + 2;                // the 3rd create (first after 2 more) fails
  const r1 = runMirror(makeCtx(drive, web, feed));
  assert.equal(r1.status, 'FAIL');
  assert.equal(drive.find(`${STATE_DIR}/manifest-000001.json`).length, 0);
  drive.failAfter = Infinity;
  const r2 = runMirror(makeCtx(drive, web, feed));
  assert.equal(r2.status, 'OK', JSON.stringify(r2.problems));
  assert.deepEqual(r2.adopted_existing, ['p/1.gz', 'p/2.gz']);
  assert.deepEqual(r2.uploaded, ['p/3.gz']);
  assert.equal(drive.find('p/1.gz').length, 1);       // never uploaded twice
});

test('a run that hits its time budget stops cleanly (PARTIAL) and the next run finishes', () => {
  const { drive, files, web } = adopted();
  const items = [1, 2, 3, 4].map((i) => { const b = Buffer.from('q' + i); web.set('v' + i, b); return entry(`q/${i}.gz`, b, 'git', 'v' + i); });
  const feed = feedOf([...files, ...items], 2);
  let t = 0;
  const r1 = runMirror(makeCtx(drive, web, feed, { budgetMs: 30, clock: () => (t += 20) }));
  assert.equal(r1.status, 'PARTIAL');
  assert.ok(r1.remaining > 0 && r1.uploaded.length > 0);
  assert.equal(drive.find(`${STATE_DIR}/manifest-000001.json`).length, 0);
  const r2 = runMirror(makeCtx(drive, web, feed));
  assert.equal(r2.status, 'OK');
  assert.equal(r2.uploaded.length + r2.adopted_existing.length, 4);
});

test('source bytes that differ from the feed checksum are never uploaded', () => {
  const { drive, files, web } = adopted();
  web.set('u', Buffer.from('tampered'));
  const r = runMirror(makeCtx(drive, web, feedOf([...files, entry('p/x.gz', Buffer.from('original'), 'git', 'u')], 2)));
  assert.equal(r.status, 'FAIL');
  assert.equal(r.problems[0].code, 'SOURCE_MISMATCH');
  assert.equal(drive.find('p/x.gz').length, 0);
});

test('missing, corrupted, duplicated and unexpected remote files stop the run before any upload', () => {
  const cases = {
    MISSING: (d) => { d.nodes.get(d.find('10_raw/frozen/ds/a.json')[0][0]).trashed = true; },
    MISMATCH: (d) => { d.nodes.get(d.find('10_raw/frozen/ds/a.json')[0][0]).bytes = Buffer.from('rot'); },
    DUPLICATE: (d) => { d.put('05_code/x.bundle', 'bundle bytes'); },
    UNEXPECTED: (d) => { d.put('10_raw/other.txt', 'x'); },
  };
  for (const [code, spoil] of Object.entries(cases)) {
    const { drive, files, web } = adopted();
    spoil(drive);
    const b = Buffer.from('new'); web.set('u', b);
    const r = runMirror(makeCtx(drive, web, feedOf([...files, entry('p/n.gz', b, 'git', 'u')], 2)));
    assert.equal(r.status, 'FAIL', code);
    assert.equal(r.problems[0].code, code);
    assert.equal(drive.find('p/n.gz').length, 0, code);
  }
});

test('a name conflict at a new path is reported and the existing file is left untouched', () => {
  const { drive, files, web } = adopted();
  const b = Buffer.from('ours'); web.set('u', b);
  drive.put('p/n.gz', 'someone else');
  const r = runMirror(makeCtx(drive, web, feedOf([...files, entry('p/n.gz', b, 'git', 'u')], 2)));
  assert.equal(r.status, 'FAIL');
  assert.equal(r.problems[0].code, 'CONFLICT');
  assert.equal(drive.readBytes(drive.find('p/n.gz')[0][0]).toString(), 'someone else');
});

test('a feed that rewrites or drops archived history is refused', () => {
  const { drive, files, web } = adopted();
  const changed = files.map((f) => f.path === '10_raw/frozen/ds/a.json' ? { ...f, sha256: '0'.repeat(64) } : f);
  assert.equal(runMirror(makeCtx(drive, web, feedOf(changed, 2))).problems[0].code, 'FEED_CHANGED_PATH');
  assert.equal(runMirror(makeCtx(drive, web, feedOf(files.slice(1), 2))).problems[0].code, 'FEED_DROPPED_PATH');
  const bad = feedOf(files, 2); bad.files_sha256 = '0'.repeat(64);
  assert.equal(runMirror(makeCtx(drive, web, bad)).problems[0].code, 'FEED_DIGEST');
});

test('a changed manifest breaks the chain and stops everything', () => {
  const { drive, files, web } = adopted();
  const b = Buffer.from('n'); web.set('u', b);
  assert.equal(runMirror(makeCtx(drive, web, feedOf([...files, entry('p/n.gz', b, 'git', 'u')], 2))).status, 'OK');
  const m1 = drive.nodes.get(drive.find(`${STATE_DIR}/manifest-000000.json`)[0][0]);
  m1.bytes = Buffer.from(m1.bytes.toString().replace('"seq": 0', '"seq": 0 '));
  const r = runMirror(makeCtx(drive, web, feedOf([...files, entry('p/n.gz', b, 'git', 'u')], 2)));
  assert.equal(r.status, 'FAIL');
  assert.equal(r.problems[0].code, 'STATE_CHAIN_BROKEN');
});

test('deep verification re-hashes with SHA-256 and catches corruption that Drive metadata hides', () => {
  const { drive, files, web } = adopted();
  const node = drive.nodes.get(drive.find('10_raw/frozen/ds/a.json')[0][0]);
  node.md5 = h(node.bytes, 'md5');                     // Drive still reports the old md5 and size ...
  node.bytes = Buffer.from('baseline b');              // ... but the stored bytes changed (same length)
  const r = runMirror(makeCtx(drive, web, feedOf(files), { deepVerifyShare: 1 }));
  assert.equal(r.status, 'FAIL');
  assert.deepEqual(r.deep_verify.mismatched, ['10_raw/frozen/ds/a.json']);
});

test('error reports carry status codes only, never request headers or credentials', () => {
  const { drive, files, web } = adopted();
  const ctx = makeCtx(drive, web, feedOf([...files, entry('p/n.gz', Buffer.from('n'), 'git', 'https://example.invalid/p?x=1')], 2));
  const r = runMirror(ctx);
  assert.equal(r.problems[0].code, 'SOURCE_UNAVAILABLE');
  assert.match(r.problems[0].detail, /HTTP 404/);
  assert.doesNotMatch(JSON.stringify(r), /authorization|bearer|token|secret/i);
});

test('canonical JSON is byte-identical to Python json.dumps(sort_keys, compact, ensure_ascii)', () => {
  const v = { b: [1, 'é', null, true], a: { 'z': 'x/y', 'é': 2 }, s: 'quote"\\' };
  const py = execFileSync('python3', ['-c', 'import json,sys; print(json.dumps(json.loads(sys.stdin.read()), sort_keys=True, separators=(",", ":"), ensure_ascii=True), end="")'],
    { input: JSON.stringify(v) }).toString();
  assert.equal(canonicalJson(v), py);
});
