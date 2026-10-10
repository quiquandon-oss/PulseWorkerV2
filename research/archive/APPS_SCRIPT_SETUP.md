# Apps Script archive sync: setup and first manual test (Android phone)

You do this once, about 15 minutes. **Nothing is scheduled.** Every run below is started by you, by hand.

- **What you'll touch:** only `My Drive/CryptoPulseV2_Research_Archive`. Steps 6–8 add exactly two files there:
  `90_manifests/archive_state/manifest-000000.json` and a report in `_verification/`. Step 10 adds one code bundle
  and `manifest-000001.json`.
- **What never happens:** no existing file is changed, moved or deleted.

## A. Create the script (phone, Chrome)

1. In Chrome open **script.google.com**, signed in as quiquandon@gmail.com. Then **⋮ → Desktop site** (the
   editor needs the desktop layout).
2. Tap **New project**, then tap "Untitled project" and rename it to `CryptoPulseV2 archive sync`.
3. In a second tab open
   `https://raw.githubusercontent.com/quiquandon-oss/PulseWorkerV2/refs/heads/claude/epic-planck-uyapsw-archive/research/archive/apps_script/CryptoPulseV2_archive_sync.gs`.
   Long-press the text → **Select all → Copy**.
4. Back in the editor, in `Code.gs`: long-press → **Select all**, paste (this replaces `function myFunction`), then
   tap **Save** (disk icon).
5. Tap **Project Settings** (gear) → tick **Show "appsscript.json" manifest file in editor**. Back in the editor,
   open `appsscript.json` and replace its content with
   `https://raw.githubusercontent.com/quiquandon-oss/PulseWorkerV2/refs/heads/claude/epic-planck-uyapsw-archive/research/archive/apps_script/appsscript.json`
   (same copy/paste). Save.

   This fixes the permissions to the five listed below and turns on the Drive API v3 advanced service.

## B. First manual run: adopt the verified baseline

6. Pick **configureBaseline** in the function list → **Run**.
   - Google asks for permission: **Review permissions** → your account → **Advanced → Go to CryptoPulseV2 archive
     sync (unsafe)** → **Allow**. The "unsafe" warning appears because this is your own unverified script.
   - The log must say `configured: folder …, feed baseline (…405cd8a…); no trigger installed`.
7. Pick **runArchiveSync** → **Run**. It takes 1–3 minutes. The log must end with
   `sync ADOPTED: uploaded 0, kept from an earlier run 0, manifest 0 (4676 files, 396985967 bytes, state <hex>)`.
8. **Retry check:** run **runArchiveSync** again. The log must say `sync NOOP: uploaded 0, …, no new manifest`.
9. **Independent check:** run **verifyArchiveNow**. If the log says "verified N of 4676 … run verifyArchiveNow
   again", run it again until it says
   `verification OK: 4676 files, 396985967 bytes, mismatched 0, manifest 0, state <same hex as step 7>`.

**Stop here and tell me.** I'll read the reports and the manifest in Drive (read-only) and confirm the evidence
below before step 10.

## C. Optional: one real increment, still by hand

10. Run **configureStageTest**, then **runArchiveSync**. The log must say
    `sync OK: uploaded 1, …, manifest 1 (4677 files, 397578952 bytes, …)`.
    The uploaded file is `05_code/increments/PulseWorkerV2-2026-10-10-8044eb53a379.bundle` (592,985 bytes).
11. Run **runArchiveSync** again: `sync NOOP`. Then run **verifyArchiveNow** until
    `verification OK: 4677 files, 397578952 bytes, mismatched 0, manifest 1`.
12. Run **configureBaseline** again, so nothing points at the test feed.

## Do not (until separately approved)

- **enableDailySchedule.** Running it installs the daily trigger. `disableDailySchedule` removes it.
- Edit, move or delete anything in the archive folder by hand.

## Evidence that the first manual run succeeded (I check each item)

| # | Evidence | Expected |
|---|---|---|
| 1 | `_verification/sync-…-ADOPTED.json` | `status: ADOPTED`, `problems: []`, `feed_files_sha256: eec537a99e45873e3edc3cfefc07460ffc8be1a5d9e8c0375431beb9cc8a2470`, `manifest.seq: 0`, `manifest.files: 4676`, `manifest.bytes: 396985967`, `manifest.added: 4676` |
| 2 | New files in the archive folder | exactly `90_manifests/archive_state/manifest-000000.json` plus the reports in `_verification/`; nothing else created, changed or removed |
| 3 | `manifest-000000.json` | `seq 0`, `parent null`, 4,676 `added` entries, `state_sha256` equal to the report's; its SHA-256 equals the report's `manifest_sha256` |
| 4 | Second run | a `sync-…-NOOP.json` report; no `manifest-000001.json` |
| 5 | `_verification/verify-…-OK.json` | `total_checked 4676`, `total_bytes 396985967`, `all_mismatched []`, `manifest_seq 0`, `state_sha256` as in 1 |
| 6 | Email | none (emails are sent only on FAIL, on recovery, or after 3 incomplete runs in a row) |
| 7 | Triggers | **Triggers** (clock icon) is empty |

For the optional step C:
- **Report:** `sync-…-OK.json` with `feed_files_sha256: 64c41500ce2085129c608434b67e83e2de77f2ce659659dcc3bc43245065ffd7`, `uploaded` = the one bundle, and `manifest.seq 1` with 4,677 files and 397,578,952 bytes.
- **Manifest:** `manifest-000001.json` has a parent SHA-256 equal to manifest 0's.
- **Drive:** the bundle in Drive is 592,985 bytes.
- **Retry and check:** a NOOP retry, and verification OK over 4,677 files.

## Permissions requested, and why

| Scope | Needed for |
|---|---|
| `https://www.googleapis.com/auth/drive` | Listing, reading and creating files in the archive folder. The narrower `drive.file` cannot see files created by another app, and the baseline was created by Colab. The script only uses the folder `configureArchive` records, and it contains no delete, trash, rename or update call. |
| `…/auth/script.external_request` | Fetching the feed and new files from public `raw.githubusercontent.com` URLs. No credentials are sent. |
| `…/auth/script.scriptapp` | Only `enableDailySchedule` / `disableDailySchedule` (its own trigger). Requested now so turning scheduling on later needs no new consent. |
| `…/auth/script.send_mail` | The failure email to you. |
| `…/auth/userinfo.email` | Addressing that email to your own address. |

**Revoke any time:** Google Account → Security → Third-party apps & services → `CryptoPulseV2 archive sync` →
Remove access, or delete the project. The archive stays as it is.
