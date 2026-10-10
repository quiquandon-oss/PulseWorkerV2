/**
 * CryptoPulseV2 research archive: Google Apps Script entry points (adapters around mirror_core.gs).
 *
 * Install (owner, once): see research/archive/INCREMENTAL_ARCHIVE.md. Nothing here runs until the owner runs
 * setupArchiveSync() and approves Google's permission screen. No credential is stored in the repository.
 */

var PROPS = PropertiesService.getScriptProperties();
var DEFAULT_FEED_URL = 'https://raw.githubusercontent.com/quiquandon-oss/PulseWorkerV2/archive-feed/feed.json';
var BUDGET_MS = 4.5 * 60 * 1000;          // stay under Apps Script's 6-minute execution limit
var DEEP_VERIFY_SHARE = 28;               // re-hash about 1/28 of the archive per run: everything every ~4 weeks

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
    utf8: function (s) { return Utilities.newBlob(s).getBytes(); },
    text: function (bytes) { return Utilities.newBlob(bytes).getDataAsString('UTF-8'); },
    now: function () { return new Date().toISOString(); },
    clock: function () { return Date.now(); }
  };
}

/** Scheduled entry point. Safe to run by hand at any time. */
function runArchiveSync() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;                  // another run is still going
  try {
    var ctx = adapters_();
    ctx.rootId = PROPS.getProperty('ARCHIVE_FOLDER_ID');
    ctx.feedUrl = PROPS.getProperty('FEED_URL') || DEFAULT_FEED_URL;
    ctx.budgetMs = BUDGET_MS;
    ctx.deepVerifyShare = DEEP_VERIFY_SHARE;
    var report = runMirror(ctx);
    saveReport_(ctx, report);
    notify_(report);
  } finally {
    lock.releaseLock();
  }
}

function saveReport_(ctx, report) {
  try {
    var tree = listTree(ctx, ctx.rootId);
    var base = REPORT_DIR + '/sync-' + report.started_utc.replace(/[:.]/g, '') + '-' + report.status;
    var name = base + '.json', n = 1;
    while (tree.files[name]) name = base + '-' + (n++) + '.json';       // never overwrite a report
    var bytes = ctx.utf8(JSON.stringify(report, null, 1) + '\n');
    createVerified(ctx, tree, name, bytes, 'application/json', ctx.hash(bytes, 'md5'), bytes.length);
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
  var lines = ['CryptoPulseV2 archive sync: ' + report.status, 'started ' + report.started_utc, ''];
  (report.problems || []).slice(0, 20).forEach(function (p) { lines.push(p.code + ': ' + p.detail); });
  if (report.report_save_error) lines.push('report not saved: ' + report.report_save_error);
  if (partialStreak >= 3) lines.push('incomplete for ' + partialStreak + ' runs in a row');
  lines.push('', 'Details: My Drive / ' + DriveApp.getFolderById(PROPS.getProperty('ARCHIVE_FOLDER_ID')).getName() + ' / _verification/');
  MailApp.sendEmail(Session.getEffectiveUser().getEmail(), '[CryptoPulseV2 archive] ' + report.status, lines.join('\n'));
}

/** One-time setup by the owner: records the folder and installs ONE daily trigger. Run it only when approved. */
function setupArchiveSync() {
  var folders = DriveApp.getRootFolder().getFoldersByName('CryptoPulseV2_Research_Archive');
  if (!folders.hasNext()) throw new Error('folder CryptoPulseV2_Research_Archive not found in My Drive');
  var folder = folders.next();
  if (folders.hasNext()) throw new Error('more than one CryptoPulseV2_Research_Archive folder in My Drive');
  PROPS.setProperty('ARCHIVE_FOLDER_ID', folder.getId());
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'runArchiveSync') ScriptApp.deleteTrigger(t);   // only this script's own trigger
  });
  ScriptApp.newTrigger('runArchiveSync').timeBased().everyDays(1).atHour(14).create();
}

/** Stop scheduled runs (the archive is left exactly as it is). */
function stopArchiveSync() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'runArchiveSync') ScriptApp.deleteTrigger(t);
  });
}
