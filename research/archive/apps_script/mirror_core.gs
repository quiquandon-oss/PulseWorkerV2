/**
 * CryptoPulseV2 research archive: append-only Google Drive mirror (core logic).
 *
 * Plain JavaScript shared by Google Apps Script (Code.gs supplies the real Drive/URL adapters) and by the local
 * tests (test_mirror.mjs supplies an in-memory Drive). It never deletes, never overwrites and never renames a
 * Drive file. A new state manifest is written only after every file of the batch has been uploaded and verified.
 *
 * Archive state lives in Drive as a hash-chained series of manifests:
 *   90_manifests/archive_state/manifest-000000.json   adoption of the verified baseline
 *   90_manifests/archive_state/manifest-000001.json   first increment (only the files it added), ...
 * Each manifest names its parent's SHA-256, so a changed or missing manifest breaks the chain and stops the run.
 */

var STATE_DIR = '90_manifests/archive_state';
var REPORT_DIR = '_verification';
var SUMS = 'SHA256SUMS';
var FEED_FORMAT = 'cryptopulse-archive-feed-v1';
var STATE_FORMAT = 'cryptopulse-archive-state-v1';
var FOLDER_MIME = 'application/vnd.google-apps.folder';

/* ---------------------------------------------------------------- helpers */

function canonicalJson(v) {
  // Same bytes as Python json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=True).
  if (v === null || typeof v === 'number' || typeof v === 'boolean') return JSON.stringify(v);
  if (typeof v === 'string') return JSON.stringify(v).replace(/[\u007f-￿]/g, function (c) {
    return '\\u' + ('0000' + c.charCodeAt(0).toString(16)).slice(-4);
  });
  if (Array.isArray(v)) return '[' + v.map(canonicalJson).join(',') + ']';
  return '{' + Object.keys(v).sort().map(function (k) { return canonicalJson(k) + ':' + canonicalJson(v[k]); }).join(',') + '}';
}

function stateDigest(ctx, state) {
  var rows = Object.keys(state).sort().map(function (p) { return [p, state[p].sha256]; });
  return ctx.hash(ctx.utf8(canonicalJson(rows)), 'sha256');
}

function fail(report, code, detail) {
  report.problems.push({ code: code, detail: detail });
}

function isSystemPath(p) {
  return p.indexOf(REPORT_DIR + '/') === 0 || p.indexOf(STATE_DIR + '/') === 0;
}

/** Every file under the archive folder: {path: [{id, md5, size}]} (a list, so duplicates are visible). */
function listTree(ctx, rootId) {
  var files = {}, folders = { '': rootId }, stack = [['', rootId]];
  while (stack.length) {
    var cur = stack.pop();
    ctx.drive.listChildren(cur[1]).forEach(function (f) {
      var path = cur[0] ? cur[0] + '/' + f.name : f.name;
      if (f.mimeType === FOLDER_MIME) {
        if (folders[path]) (files[path + '/'] = files[path + '/'] || []).push({ id: f.id, folder: true });   // duplicate folder
        folders[path] = f.id;
        stack.push([path, f.id]);
      } else {
        (files[path] = files[path] || []).push({ id: f.id, md5: f.md5, size: Number(f.size) });
      }
    });
  }
  return { files: files, folders: folders };
}

function ensureFolder(ctx, tree, dir) {
  if (tree.folders[dir] !== undefined) return tree.folders[dir];
  var cut = dir.lastIndexOf('/');
  var parent = ensureFolder(ctx, tree, cut < 0 ? '' : dir.slice(0, cut));
  var id = ctx.drive.createFolder(parent, cut < 0 ? dir : dir.slice(cut + 1));
  tree.folders[dir] = id;
  return id;
}

/** Create a new file; refuses if anything already has that name. Returns the verified Drive record. */
function createVerified(ctx, tree, path, bytes, mime, md5, size) {
  if (tree.files[path]) throw new Error('refusing to create over an existing name: ' + path);
  var cut = path.lastIndexOf('/');
  var parent = ensureFolder(ctx, tree, cut < 0 ? '' : path.slice(0, cut));
  var id = ctx.drive.createFile(parent, cut < 0 ? path : path.slice(cut + 1), bytes, mime);
  var meta = ctx.drive.getMeta(id);
  tree.files[path] = [{ id: id, md5: meta.md5, size: Number(meta.size) }];
  if (meta.md5 !== md5 || Number(meta.size) !== size) throw new Error('Drive copy does not match what was sent: ' + path);
  return tree.files[path][0];
}

function parseSums(text) {
  var out = {};
  text.split('\n').forEach(function (line) {
    if (!line) return;
    var i = line.indexOf('  ');
    out[line.slice(i + 2)] = line.slice(0, i);
  });
  return out;
}

/* ---------------------------------------------------------------- state */

/** Replay the manifest chain. Returns {state, seq, lastName, lastSha} or records problems. */
function loadState(ctx, tree, report) {
  var names = Object.keys(tree.files).filter(function (p) { return p.indexOf(STATE_DIR + '/') === 0; }).sort();
  var state = {}, prev = null, seq = -1;
  for (var i = 0; i < names.length; i++) {
    var p = names[i], m = /manifest-(\d{6})\.json$/.exec(p);
    if (!m || tree.files[p].length !== 1) { fail(report, 'STATE_UNEXPECTED_FILE', p); return null; }
    if (Number(m[1]) !== seq + 1) { fail(report, 'STATE_CHAIN_GAP', p); return null; }
    var bytes = ctx.drive.readBytes(tree.files[p][0].id);
    var doc = JSON.parse(ctx.text(bytes));
    var parentOk = prev === null ? doc.parent === null : (doc.parent && doc.parent.sha256 === prev.sha);
    if (doc.format !== STATE_FORMAT || doc.seq !== seq + 1 || !parentOk) { fail(report, 'STATE_CHAIN_BROKEN', p); return null; }
    doc.added.forEach(function (e) {
      if (state[e.path]) throw new Error('manifest adds a path twice: ' + e.path);
      state[e.path] = e;
    });
    if (stateDigest(ctx, state) !== doc.state_sha256) { fail(report, 'STATE_DIGEST_MISMATCH', p); return null; }
    seq = doc.seq;
    prev = { name: p, sha: ctx.hash(bytes, 'sha256') };
  }
  return { state: state, seq: seq, last: prev };
}

function checkRemoteAgainstState(tree, state, allowed, report) {
  Object.keys(state).forEach(function (p) {
    var r = tree.files[p], e = state[p];
    if (!r) fail(report, 'MISSING', p);
    else if (r.length > 1) fail(report, 'DUPLICATE', p);
    else if (r[0].md5 !== e.md5 || r[0].size !== e.size) fail(report, 'MISMATCH', p);
  });
  Object.keys(tree.files).forEach(function (p) {
    if (p.slice(-1) === '/') fail(report, 'DUPLICATE_FOLDER', p);
    else if (!state[p] && !isSystemPath(p) && !allowed[p]) fail(report, 'UNEXPECTED', p);
    else if (isSystemPath(p) && tree.files[p].length > 1) fail(report, 'DUPLICATE', p);
  });
}

function publishManifest(ctx, tree, loaded, added, feed, nextState) {
  var seq = loaded ? loaded.seq + 1 : 0;
  var doc = {
    format: STATE_FORMAT, seq: seq, created_utc: ctx.now(),
    parent: loaded && loaded.last ? { name: loaded.last.name, sha256: loaded.last.sha } : null,
    feed: { feed_seq: feed.feed_seq, files_sha256: feed.files_sha256 },
    added: added,
    totals: { files: Object.keys(nextState).length,
              bytes: Object.keys(nextState).reduce(function (s, p) { return s + nextState[p].size; }, 0) },
    state_sha256: stateDigest(ctx, nextState)
  };
  var bytes = ctx.utf8(JSON.stringify(doc, null, 1) + '\n');
  var name = STATE_DIR + '/manifest-' + ('00000' + seq).slice(-6) + '.json';
  createVerified(ctx, tree, name, bytes, 'application/json', ctx.hash(bytes, 'md5'), bytes.length);
  return { seq: seq, name: name, files: doc.totals.files, bytes: doc.totals.bytes };
}

/* ---------------------------------------------------------------- feed */

function loadFeed(ctx, report) {
  var r = ctx.fetch(ctx.feedUrl);
  if (r.status !== 200) { fail(report, 'FEED_UNAVAILABLE', 'HTTP ' + r.status); return null; }
  var feed = JSON.parse(ctx.text(r.bytes));
  if (feed.format !== FEED_FORMAT) { fail(report, 'FEED_FORMAT', String(feed.format)); return null; }
  if (ctx.hash(ctx.utf8(canonicalJson(feed.files)), 'sha256') !== feed.files_sha256) { fail(report, 'FEED_DIGEST', 'files_sha256'); return null; }
  var seen = {};
  for (var i = 0; i < feed.files.length; i++) {
    if (seen[feed.files[i].path]) { fail(report, 'FEED_DUPLICATE_PATH', feed.files[i].path); return null; }
    seen[feed.files[i].path] = 1;
  }
  return feed;
}

/* ---------------------------------------------------------------- run */

/**
 * One idempotent pass. ctx: {drive, fetch, hash, utf8, text, now, clock, budgetMs, rootId, feedUrl, deepVerifyShare}.
 * Returns a report {status: OK | NOOP | ADOPTED | PARTIAL | FAIL, ...}. Never throws for archive problems.
 */
function runMirror(ctx) {
  var t0 = ctx.clock();
  var report = { started_utc: ctx.now(), status: 'FAIL', problems: [], uploaded: [], adopted_existing: [], manifest: null };
  try {
    var feed = loadFeed(ctx, report);
    if (!feed) return report;
    report.feed_seq = feed.feed_seq;
    var tree = listTree(ctx, ctx.rootId);
    var loaded = loadState(ctx, tree, report);
    if (report.problems.length) return report;
    var byPath = {};
    feed.files.forEach(function (f) { byPath[f.path] = f; });

    if (loaded.seq < 0) return adoptBaseline(ctx, tree, feed, report);

    var state = loaded.state;
    Object.keys(state).forEach(function (p) {          // append-only: the feed may never drop or change history
      if (!byPath[p]) fail(report, 'FEED_DROPPED_PATH', p);
      else if (!(byPath[p].origin === 'baseline_adopt' && byPath[p].sha256 === null) && byPath[p].sha256 !== state[p].sha256) {
        fail(report, 'FEED_CHANGED_PATH', p);           // (adopted baseline files carry their checksum in the state, not the feed)
      }
    });
    var todo = feed.files.filter(function (f) { return !state[f.path]; });
    var allowed = {};
    todo.forEach(function (f) { allowed[f.path] = 1; });
    checkRemoteAgainstState(tree, state, allowed, report);
    if (report.problems.length) return report;

    var added = [];
    for (var i = 0; i < todo.length; i++) {
      if (ctx.clock() - t0 > ctx.budgetMs) {
        report.status = 'PARTIAL';
        report.remaining = todo.length - i;
        return report;                                      // no manifest: the next run resumes safely
      }
      var f = todo[i];
      if (f.origin !== 'git' && f.origin !== 'feed_blob') { fail(report, 'BASELINE_NOT_IN_STATE', f.path); return report; }
      var src = ctx.fetch(f.url);
      if (src.status !== 200) { fail(report, 'SOURCE_UNAVAILABLE', f.path + ' (HTTP ' + src.status + ')'); return report; }
      var sha = ctx.hash(src.bytes, 'sha256'), md5 = ctx.hash(src.bytes, 'md5');
      if (sha !== f.sha256 || src.bytes.length !== f.size || (f.md5 && md5 !== f.md5)) { fail(report, 'SOURCE_MISMATCH', f.path); return report; }
      var rec, existing = tree.files[f.path];
      if (existing) {                                       // left by an interrupted run: keep only if identical
        if (existing.length !== 1 || existing[0].md5 !== md5 || existing[0].size !== f.size) { fail(report, 'CONFLICT', f.path); return report; }
        rec = existing[0];
        report.adopted_existing.push(f.path);
      } else {
        rec = createVerified(ctx, tree, f.path, src.bytes, 'application/octet-stream', md5, f.size);
        report.uploaded.push(f.path);
      }
      added.push({ path: f.path, kind: f.kind, dataset: f.dataset, snapshot: f.snapshot, origin: f.origin, url: f.url,
                   size: f.size, sha256: sha, md5: md5, file_id: rec.id,
                   verified: { utc: ctx.now(), method: 'sha256+md5 of source bytes; Drive md5+size after upload' } });
    }
    if (!added.length) {
      report.status = 'NOOP';
    } else {
      var next = {};
      Object.keys(state).forEach(function (p) { next[p] = state[p]; });
      added.forEach(function (e) { next[e.path] = e; });
      report.manifest = publishManifest(ctx, tree, loaded, added, feed, next);
      report.status = 'OK';
      state = next;
    }
    report.deep_verify = deepVerify(ctx, tree, state, t0);
    if (report.deep_verify.mismatched.length) {
      report.status = 'FAIL';
      report.deep_verify.mismatched.forEach(function (p) { fail(report, 'DEEP_VERIFY_MISMATCH', p); });
    }
    return report;
  } catch (e) {
    fail(report, 'ERROR', String(e && e.message || e));
    report.status = 'FAIL';
    return report;
  } finally {
    report.finished_utc = ctx.now();
  }
}

/** First run: adopt the uploaded baseline after checking it against SHA256SUMS and the feed. */
function adoptBaseline(ctx, tree, feed, report) {
  var sumsRec = tree.files[SUMS];
  if (!sumsRec || sumsRec.length !== 1) { fail(report, 'BASELINE_SUMS', 'SHA256SUMS missing or duplicated'); return report; }
  var sumsBytes = ctx.drive.readBytes(sumsRec[0].id);
  var sums = parseSums(ctx.text(sumsBytes));
  var base = feed.files.filter(function (f) { return f.origin === 'baseline' || f.origin === 'baseline_adopt'; });
  var baseSet = {};
  base.forEach(function (f) { baseSet[f.path] = f; });
  Object.keys(sums).forEach(function (p) { if (!baseSet[p]) fail(report, 'BASELINE_EXTRA_IN_SUMS', p); });
  var added = [];
  base.forEach(function (f) {
    var r = tree.files[f.path];
    if (!r) return fail(report, 'MISSING', f.path);
    if (r.length > 1) return fail(report, 'DUPLICATE', f.path);
    var sha = f.path === SUMS ? ctx.hash(sumsBytes, 'sha256') : sums[f.path];
    if (!sha) return fail(report, 'BASELINE_NOT_IN_SUMS', f.path);
    if (f.origin === 'baseline' && (sha !== f.sha256 || r[0].md5 !== f.md5 || r[0].size !== f.size)) return fail(report, 'MISMATCH', f.path);
    if (f.origin === 'baseline_adopt' && f.size !== undefined && f.size !== null && r[0].size !== f.size) return fail(report, 'MISMATCH', f.path);
    added.push({ path: f.path, kind: f.kind, dataset: f.dataset, snapshot: f.snapshot, origin: f.origin,
                 size: r[0].size, sha256: sha, md5: r[0].md5, file_id: r[0].id,
                 verified: { utc: ctx.now(), method: f.origin === 'baseline'
                   ? 'SHA256SUMS + Drive md5/size equal the reference build'
                   : 'adopted from SHA256SUMS + Drive md5/size (verified at upload, see _verification)' } });
  });
  Object.keys(tree.files).forEach(function (p) {
    if (p.slice(-1) === '/') fail(report, 'DUPLICATE_FOLDER', p);
    else if (!baseSet[p] && !isSystemPath(p)) fail(report, 'UNEXPECTED', p);
  });
  if (report.problems.length) return report;
  var state = {};
  added.forEach(function (e) { state[e.path] = e; });
  report.manifest = publishManifest(ctx, tree, null, added, feed, state);
  report.status = 'ADOPTED';
  return report;
}

/** Re-hash a rotating share of the archive with SHA-256 (Drive's md5 alone is not trusted). */
function deepVerify(ctx, tree, state, t0) {
  var share = ctx.deepVerifyShare || 0, out = { checked: 0, mismatched: [] };
  if (!share) return out;
  var paths = Object.keys(state).sort(), day = Math.floor(Date.parse(ctx.now()) / 86400000);
  for (var i = 0; i < paths.length; i++) {
    if (i % share !== day % share) continue;
    if (ctx.clock() - t0 > ctx.budgetMs) { out.stopped_by_budget = true; break; }
    var e = state[paths[i]];
    if (ctx.hash(ctx.drive.readBytes(tree.files[paths[i]][0].id), 'sha256') !== e.sha256) out.mismatched.push(paths[i]);
    out.checked++;
  }
  return out;
}

if (typeof module !== 'undefined') module.exports = { runMirror: runMirror, canonicalJson: canonicalJson, parseSums: parseSums, STATE_DIR: STATE_DIR };
