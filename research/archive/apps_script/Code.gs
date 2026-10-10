/**
 * CryptoPulseV2 research archive: Google Apps Script entry points (adapters around mirror_core.gs).
 *
 * Functions you run by hand, in this order (research/archive/APPS_SCRIPT_SETUP.md):
 *   1. configureArchive()      records the archive folder and the feed URL; installs NOTHING
 *   2. runArchiveSync()        one manual sync: first run adopts the verified baseline (manifest 0)
 *   3. verifyArchiveNow()      independent SHA-256 check of every archived file; re-run until it says done
 *   4. enableDailySchedule()   ONLY when approved: installs one daily trigger
 *      disableDailySchedule()  removes it again (the archive is left as it is)
 * No credential is stored anywhere; Google keeps the authorization.
 */

var PROPS = PropertiesService.getScriptProperties();
var ARCHIVE_FOLDER_NAME = 'CryptoPulseV2_Research_Archive';
// First runs read the committed, verified baseline feed at a pinned commit (immutable URL). The daily feed
// (branch archive-feed) is switched in by configureArchive('daily') only once that feed is approved and published.
var FEED_URLS = {
  baseline: 'https://raw.githubusercontent.com/quiquandon-oss/PulseWorkerV2/405cd8a514e5a8f8b38bec3ba7f36951affb562c/research/archive/baseline/feed-000000.json',
  daily: 'https://raw.githubusercontent.com/quiquandon-oss/PulseWorkerV2/refs/heads/archive-feed/feed.json'
};
var BUDGET_MS = 3.5 * 60 * 1000;          // leaves room under the 6-minute limit for the report and email
var DEEP_VERIFY_SHARE = 28;               // each sync re-hashes about 1/28 of the archive (all of it every ~4 weeks)

function hex_(bytes) {
  return bytes.map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
}

function adapters_() {
  return {
    drive: {
      listChildren: function (folderId) {
        var out = [], token = null;
        do {
          var page = Drive.Files.list({ q: "'" + folderId + "' in parents and trashed = false", pageSize: 1000, pageToken: token,
                                         fields: 'nextPageToken, files(id,name,mimeType,md5Checksum,size)' });
          (page.files || []).forEach(function (f) { out.push({ id: f.id, name: f.name, mimeType: f.mimeType, md5: f.md5Checksum, size: f.size }); });
          token = page.nextPageToken;
        } while (token);
        return out;
      },
      createFolder: function (parentId, name) {
        return Drive.Files.create({ name: name, mimeType: FOLDER_MIME, parents: [parentId] }).id;
      },
      createFile: function (parentId, name, bytes, mime) {
        return Drive.Files.create({ name: name, parents: [parentId] }, Utilities.newBlob(bytes, mime, name)).id;
      },
      getMeta: function (id) {
        var f = Drive.Files.get(id, { fields: 'md5Checksum,size' });
        return { md5: f.md5Checksum, size: f.size };
      },
      readBytes: function (id) { return DriveApp.getFileById(id).getBlob().getBytes(); }
    },
    fetch: function (url) {
      var r = UrlFetchApp.fetch(url, { muteHttpExceptions: true, followRedirects: true });   // public URLs only; no auth header
      return { status: r.getResponseCode(), bytes: r.getContent() };
    },
    hash: function (bytes, algo) {
      return hex_(Utilities.computeDigest(algo === 'md5' ? Utilities.DigestAlgorithm.MD5 : Utilities.DigestAlgorithm.SHA_256, bytes));
    },
    utf8: function (s) { return Utilities.newBlob('').setDataFromString(s, 'UTF-8').getBytes(); },
    text: function (bytes) { return Utilities.newBlob(bytes).getDataAsString('UTF-8'); },
    now: function () { return new Date().toISOString(); },
    clock: function () { return Date.now(); }
  };
}

function context_() {
  var ctx = adapters_();
  ctx.rootId = PROPS.getProperty('ARCHIVE_FOLDER_ID');
  ctx.feedUrl = PROPS.getProperty('FEED_URL');
  if (!ctx.rootId || !ctx.feedUrl) throw new Error('run configureArchive() first');
  ctx.budgetMs = BUDGET_MS;
  ctx.deepVerifyShare = DEEP_VERIFY_SHARE;
  return ctx;
}

/** Step 1. Finds the archive folder (exactly one, in My Drive) and selects the feed. Installs no trigger. */
function configureArchive(feed) {
  var which = feed || 'baseline';
  if (!FEED_URLS[which]) throw new Error('feed must be "baseline" or "daily"');
  var folders = DriveApp.getRootFolder().getFoldersByName(ARCHIVE_FOLDER_NAME);
  if (!folders.hasNext()) throw new Error('folder ' + ARCHIVE_FOLDER_NAME + ' not found in My Drive');
  var folder = folders.next();
  if (folders.hasNext()) throw new Error('more than one ' + ARCHIVE_FOLDER_NAME + ' folder in My Drive');
  PROPS.setProperty('ARCHIVE_FOLDER_ID', folder.getId());
  PROPS.setProperty('FEED_URL', FEED_URLS[which]);
  Logger.log('configured: folder ' + folder.getId() + ', feed ' + which + ' (' + FEED_URLS[which] + '); no trigger installed');
}

/** Step 2 (and the scheduled entry point later). Safe to run by hand at any time. */
function runArchiveSync() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) { Logger.log('another run is still going'); return; }
  try {
    var ctx = context_();
    var report = runMirror(ctx);
    report.feed_url = ctx.feedUrl;
    saveReport_(ctx, 'sync', report);
    notify_(report);
    Logger.log(summary_(report));
  } finally {
    lock.releaseLock();
  }
}

/** Step 3. Independent SHA-256 check of every archived file; resumes where it stopped. Writes only its report. */
function verifyArchiveNow() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) { Logger.log('another run is still going'); return; }
  try {
    var ctx = context_();
    var start = Number(PROPS.getProperty('VERIFY_NEXT') || 0);
    var r = fullVerifyStep(ctx, start);
    var acc = JSON.parse(PROPS.getProperty('VERIFY_ACC') || '{"checked":0,"bytes":0,"mismatched":[]}');
    acc.checked += r.checked; acc.bytes += r.bytes; acc.mismatched = acc.mismatched.concat(r.mismatched);
    if (r.status === 'PARTIAL') {
      PROPS.setProperty('VERIFY_NEXT', String(r.next));
      PROPS.setProperty('VERIFY_ACC', JSON.stringify(acc));
      Logger.log('verified ' + r.next + ' of ' + r.total + ' files so far; run verifyArchiveNow again');
      return;
    }
    PROPS.deleteProperty('VERIFY_NEXT');
    PROPS.deleteProperty('VERIFY_ACC');
    r.total_checked = acc.checked; r.total_bytes = acc.bytes; r.all_mismatched = acc.mismatched;
    saveReport_(ctx, 'verify', r);
    notify_(r);
    Logger.log('verification ' + r.status + ': ' + acc.checked + ' files, ' + acc.bytes + ' bytes, mismatched ' + acc.mismatched.length +
               ', manifest ' + r.manifest_seq + ', state ' + r.state_sha256);
  } finally {
    lock.releaseLock();
  }
}

function summary_(r) {
  return 'sync ' + r.status + ': uploaded ' + r.uploaded.length + ', kept from an earlier run ' + r.adopted_existing.length +
         (r.manifest ? ', manifest ' + r.manifest.seq + ' (' + r.manifest.files + ' files, ' + r.manifest.bytes + ' bytes, state ' +
                       r.manifest.state_sha256 + ')' : ', no new manifest') +
         (r.problems.length ? ', problems: ' + JSON.stringify(r.problems.slice(0, 5)) : '');
}

function saveReport_(ctx, kind, report) {
  try {
    var tree = listTree(ctx, ctx.rootId);
    var base = REPORT_DIR + '/' + kind + '-' + report.started_utc.replace(/[:.]/g, '') + '-' + report.status;
    var name = base + '.json', n = 1;
    while (tree.files[name]) name = base + '-' + (n++) + '.json';       // never overwrite a report
    var bytes = ctx.utf8(JSON.stringify(report, null, 1) + '\n');
    createVerified(ctx, tree, name, bytes, 'application/json', ctx.hash(bytes, 'md5'), bytes.length);
    report.report_file = name;
  } catch (e) {
    report.report_save_error = String(e && e.message || e);
  }
}

function notify_(report) {
  var last = PROPS.getProperty('LAST_STATUS');
  PROPS.setProperty('LAST_STATUS', report.status);
  var partialStreak = report.status === 'PARTIAL' ? Number(PROPS.getProperty('PARTIAL_STREAK') || 0) + 1 : 0;
  PROPS.setProperty('PARTIAL_STREAK', String(partialStreak));
  var bad = report.status === 'FAIL' || report.report_save_error || partialStreak >= 3;
  if (!bad && !(last === 'FAIL' && report.status !== 'FAIL')) return;
  var lines = ['CryptoPulseV2 archive: ' + report.status, 'started ' + report.started_utc, ''];
  (report.problems || []).slice(0, 20).forEach(function (p) { lines.push(p.code + ': ' + p.detail); });
  if (report.report_save_error) lines.push('report not saved: ' + report.report_save_error);
  if (partialStreak >= 3) lines.push('incomplete for ' + partialStreak + ' runs in a row');
  lines.push('', 'Details: My Drive / ' + ARCHIVE_FOLDER_NAME + ' / ' + (report.report_file || '_verification/'));
  MailApp.sendEmail(Session.getEffectiveUser().getEmail(), '[CryptoPulseV2 archive] ' + report.status, lines.join('\n'));
}

/** Step 4, ONLY when approved: one daily trigger (14:00 UTC). Replaces this script's own trigger, nothing else. */
function enableDailySchedule() {
  disableDailySchedule();
  ScriptApp.newTrigger('runArchiveSync').timeBased().everyDays(1).atHour(14).create();
  Logger.log('daily schedule enabled (14:00 UTC)');
}

function disableDailySchedule() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'runArchiveSync') ScriptApp.deleteTrigger(t);
  });
}
