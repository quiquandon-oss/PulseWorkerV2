// The Apps Script entry points (Code.gs) wired to fakes of every Google service they call. Run: node --test
import { test } from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import vm from 'node:vm';

const h = (b, a) => crypto.createHash(a).update(Buffer.from(b)).digest('hex');
const src = ['mirror_core.gs', 'Code.gs'].map((f) => fs.readFileSync(new URL('./' + f, import.meta.url), 'utf8')).join('\n');

function google(feedBytes) {
  const nodes = new Map([['root', { name: 'My Drive', folder: true }]]);
  let n = 0;
  const put = (parent, name, bytes, folder) => { const id = (folder ? 'd' : 'f') + n++; nodes.set(id, { name, parent, folder, bytes: bytes && Buffer.from(bytes) }); return id; };
  const archive = put('root', 'CryptoPulseV2_Research_Archive', null, true);
  const props = new Map(), triggers = [], mails = [], logs = [];
  const blob = (bytes) => ({ getBytes: () => [...Buffer.from(bytes)].map((b) => (b > 127 ? b - 256 : b)),
    getDataAsString: () => Buffer.from(bytes).toString('utf8'),
    setDataFromString: (s) => blob(Buffer.from(s, 'utf8')) });
  const toBuf = (signed) => Buffer.from(signed.map((b) => b & 0xff));
  const g = {
    nodes, archive, props, triggers, mails, logs, put,
    PropertiesService: { getScriptProperties: () => ({ getProperty: (k) => (props.has(k) ? props.get(k) : null), setProperty: (k, v) => props.set(k, String(v)), deleteProperty: (k) => props.delete(k) }) },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} }) },
    Logger: { log: (m) => logs.push(m) },
    MailApp: { sendEmail: (to, subject, body) => mails.push({ to, subject, body }) },
    Session: { getEffectiveUser: () => ({ getEmail: () => 'owner@example.invalid' }) },
    ScriptApp: { getProjectTriggers: () => triggers.slice(), deleteTrigger: (t) => triggers.splice(triggers.indexOf(t), 1),
      newTrigger: (fn) => ({ timeBased: () => ({ everyDays: (d) => ({ atHour: (hr) => ({ create: () => triggers.push({ getHandlerFunction: () => fn, d, hr }) }) }) }) }) },
    UrlFetchApp: { fetch: (url) => ({ getResponseCode: () => (url.includes('405cd8a') ? 200 : 404), getContent: () => [...feedBytes()].map((b) => (b > 127 ? b - 256 : b)) }) },
    Utilities: { DigestAlgorithm: { MD5: 'md5', SHA_256: 'sha256' },
      computeDigest: (algo, bytes) => [...crypto.createHash(algo).update(toBuf(bytes)).digest()].map((b) => (b > 127 ? b - 256 : b)),
      newBlob: (data, mime, name) => (typeof data === 'string' ? blob(Buffer.from(data, 'utf8')) : Object.assign(blob(toBuf(data)), { raw: toBuf(data) })) },
    DriveApp: {
      getRootFolder: () => ({ getFoldersByName: (name) => { const hits = [...nodes].filter(([, v]) => v.parent === 'root' && v.folder && v.name === name);
        let i = 0; return { hasNext: () => i < hits.length, next: () => { const id = hits[i++][0]; return { getId: () => id }; } }; } }),
      getFileById: (id) => ({ getBlob: () => blob(nodes.get(id).bytes) }),
    },
    Drive: { Files: {
      list: ({ q }) => { const parent = /'(.+)' in parents/.exec(q)[1];
        return { files: [...nodes].filter(([, v]) => v.parent === parent).map(([id, v]) => ({ id, name: v.name,
          mimeType: v.folder ? 'application/vnd.google-apps.folder' : 'application/octet-stream',
          md5Checksum: v.folder ? undefined : h(v.bytes, 'md5'), size: v.folder ? undefined : String(v.bytes.length) })) }; },
      create: (meta, media) => ({ id: put(meta.parents[0], meta.name, media && media.raw, meta.mimeType === 'application/vnd.google-apps.folder') }),
      get: (id) => ({ md5Checksum: h(nodes.get(id).bytes, 'md5'), size: String(nodes.get(id).bytes.length) }),
    } },
  };
  return g;
}

function setup() {
  const a = Buffer.from('baseline a');
  const files = [{ path: 'a.json', kind: 'raw', dataset: null, snapshot: null, origin: 'baseline', size: a.length, sha256: h(a, 'sha256'), md5: h(a, 'md5') },
                 { path: 'SHA256SUMS', kind: 'manifest', dataset: null, snapshot: null, origin: 'baseline_adopt', size: null, sha256: null, md5: null }];
  let feed;
  const g = google(() => feed);
  const ctx = vm.createContext(g);
  vm.runInContext(src, ctx);
  const files_sha256 = h(Buffer.from(vm.runInContext('canonicalJson', ctx)(files)), 'sha256');
  feed = Buffer.from(JSON.stringify({ format: 'cryptopulse-archive-feed-v1', feed_seq: 0, files, files_sha256 }));
  g.put(g.archive, 'a.json', a);
  g.put(g.archive, 'SHA256SUMS', `${h(a, 'sha256')}  a.json\n`);
  return { g, ctx };
}

const reports = (g) => [...g.nodes.values()].filter((v) => /^(sync|verify)-/.test(v.name || '')).map((v) => v.name);

test('configureArchive installs no trigger; the first manual sync adopts; a retry is a no-op; verify passes', () => {
  const { g, ctx } = setup();
  vm.runInContext('configureArchive()', ctx);
  assert.equal(g.triggers.length, 0);
  assert.match(g.props.get('FEED_URL'), /405cd8a514e5a8f8b38bec3ba7f36951affb562c\/research\/archive\/baseline\/feed-000000\.json$/);
  vm.runInContext('runArchiveSync()', ctx);
  assert.match(g.logs.at(-1), /^sync ADOPTED: .*manifest 0 \(2 files/);
  vm.runInContext('runArchiveSync()', ctx);
  assert.match(g.logs.at(-1), /^sync NOOP: uploaded 0/);
  vm.runInContext('verifyArchiveNow()', ctx);
  assert.match(g.logs.at(-1), /^verification OK: 2 files/);
  assert.equal(reports(g).length, 3);
  assert.equal(g.mails.length, 0);                        // no email when everything is fine
  assert.equal(g.triggers.length, 0);                     // still nothing scheduled
});

test('a failure is reported in Drive and by email; enable/disable touch only this trigger', () => {
  const { g, ctx } = setup();
  vm.runInContext('configureArchive()', ctx);
  g.put(g.archive, 'stray.txt', 'x');
  vm.runInContext('runArchiveSync()', ctx);
  assert.match(g.logs.at(-1), /^sync FAIL/);
  assert.equal(g.mails.length, 1);
  assert.match(g.mails[0].body, /UNEXPECTED: stray\.txt/);
  assert.ok(reports(g).some((n) => n.endsWith('-FAIL.json')));
  const other = { getHandlerFunction: () => 'somethingElse' };
  g.triggers.push(other);
  vm.runInContext('enableDailySchedule(); enableDailySchedule()', ctx);
  assert.equal(g.triggers.filter((t) => t.getHandlerFunction() === 'runArchiveSync').length, 1);
  vm.runInContext('disableDailySchedule()', ctx);
  assert.deepEqual(g.triggers, [other]);
});

test('the editor wrappers select the pinned baseline and staged feeds', () => {
  const { g, ctx } = setup();
  vm.runInContext('configureStageTest()', ctx);
  assert.match(g.props.get('FEED_URL'), /\/[0-9a-f]{40}\/research\/archive\/feed_stage\/feed\.json$/);
  vm.runInContext('configureBaseline()', ctx);
  assert.match(g.props.get('FEED_URL'), /\/405cd8a5[0-9a-f]{32}\/research\/archive\/baseline\/feed-000000\.json$/);
  assert.equal(g.triggers.length, 0);
});

test('without configureArchive nothing runs', () => {
  const { ctx } = setup();
  assert.throws(() => vm.runInContext('runArchiveSync()', ctx), /configureArchive/);
});
