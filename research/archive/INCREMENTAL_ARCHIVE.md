# Incremental, append-only Google Drive archive

Status, 2026-10-10:
- **Implemented and tested locally:** the planner, the mirror and restore.
- **Not done:** nothing is installed, scheduled or connected. No Drive file was changed.

## Baseline (verified, untouched)

`My Drive/CryptoPulseV2_Research_Archive`:
- **Contents:** 4,676 files, 396,985,967 bytes, uploaded by the Colab notebook from code 2c67e9b.
- **Report:** `_verification/upload_verification_2026-10-10T114950Z.json`.
  - `remote_verification.ok = true`; 4,676/4,676 files match Drive's MD5 and size.
  - 4,674 files are byte-identical to the session's reference build; the git bundle (27,061,498 B) matches by its
    branch heads.
  - The two earlier reports in `_verification/` (114513Z, 114706Z) record the same run (re-runs of the last
    cell).
- **Description:** `baseline/feed-000000.json` (committed) describes it file by file, adding kind, dataset,
  snapshot, size, SHA-256 and MD5.
  - Two files are recorded by size only and adopted from Drive as uploaded: the bundle and `SHA256SUMS`, because
    rebuilt bundles differ in pack bytes.
  - Its totals equal the report's: 4,676 files, 396,985,967 bytes.

## Architecture

```
 git (public repo)                    GitHub Actions (proposed, inert)        Owner's Google account
 ─────────────────                    ────────────────────────────────        ─────────────────────────────
 research branches, data branches ──► increment.py plan (daily 13:20)  ──►  feed.json on branch archive-feed
 (commit-pinned raw URLs)              append-only, no secrets                (+ blobs/<sha256>)
                                                                                   │
                                                                                   ▼
                                                     Apps Script mirror (daily 14:00, in the owner's account)
                                                     mirror_core.gs: fetch → SHA-256/MD5 check → create file
                                                     → Drive MD5/size check → state manifest LAST → report
                                                                                   │
                                                                                   ▼
                                                     My Drive/CryptoPulseV2_Research_Archive (append-only)
```

**Why this split:**
- **Mirror in Apps Script:** it runs inside your own Google account, so no Google token ever exists outside
  Google. It is free, runs on a schedule with nothing of ours open, and can email you.
- **Planner in Python:** the logic that needs git (snapshots, bundles, schemas, cutoffs) stays in Python, where it
  is tested against real git.
- **Coupling:** the two meet only through `feed.json`, which is public, checksummed and append-only.

### Feed (`cryptopulse-archive-feed-v1`)

Each entry has:
- `path`
- `kind`: preregistration | raw | derived | experiment_output | code_bundle | manifest | report
- `dataset`, `snapshot`, `size`, `sha256`, `md5`
- `origin`:
  - `baseline` / `baseline_adopt`: already in Drive;
  - `git`: a commit-pinned raw URL, immutable;
  - `feed_blob`: generated, content-addressed at `archive-feed/blobs/<sha256>`.
- `url`

`files_sha256` is the SHA-256 of the canonical JSON of `files`. The Python and JS encoders are byte-identical
(tested). `code_heads` records the branch heads whose code is archived.

### What the planner adds (`increment.py plan`)

- **Append-only stores** (the forward collector): each new partition once, under `…/partitions/`. A partition
  whose bytes changed upstream is a **PlanConflict**, and the job fails. Mutable files (the index) go into a new
  `snapshot=<date>_git-<commit>/`.
- **Frozen datasets, pre-registrations, run records, exports:** a new `snapshot=` folder only when the dataset's
  content differs from every archived snapshot. Old snapshots are never replaced. Daily export versions therefore
  become daily snapshots.
- **Per-snapshot manifest** (`90_manifests/<dataset>__<snapshot>.json`, a feed blob) records:
  - git commit, ref and commit time, plus provenance;
  - per-file SHA-256;
  - schema (field names), row count, and **data cutoff** (`data_last_utc`, `available_last_utc`).
- **Code:** when branch heads move, an **incremental git bundle** (`05_code/increments/…bundle`) of only the new
  commits. Its prerequisites are the previous heads, so it applies on top of the baseline bundle (tested).
- **Never:** the planner never drops or edits a previous feed entry; a later feed is a strict extension.

### What the mirror does (`apps_script/mirror_core.gs`)

**State:** a hash chain of manifests in `90_manifests/archive_state/manifest-NNNNNN.json`.
- Each manifest lists the files it added (with Drive file id), its parent's SHA-256, and the SHA-256 of the whole
  cumulative state.
- `manifest-000000` adopts the baseline.

**Each run, in order:**
1. **Load the feed** and check its digest and that it has no duplicate paths.
2. **List the whole Drive folder.** Replay and check the manifest chain. Any change, gap or extra manifest:
   **stop**.
3. **Adoption (first run only):**
   - every baseline path must exist exactly once;
   - its `SHA256SUMS` entry must equal the reference SHA-256;
   - its Drive MD5/size must equal the reference;
   - there must be nothing unexpected.
   Then write `manifest-000000`.
4. **Check the folder against the state:**
   - every archived file present once with its MD5/size; otherwise MISSING, DUPLICATE or MISMATCH, and **stop**;
   - any file not in state, feed or `_verification/`: UNEXPECTED, **stop**;
   - the feed must not drop or change archived paths: FEED_DROPPED_PATH / FEED_CHANGED_PATH, **stop**.
5. **For each new file:**
   - fetch the public URL;
   - require SHA-256, MD5 and size equal to the feed, otherwise SOURCE_MISMATCH and nothing is uploaded;
   - create the file; Drive refuses nothing, so the mirror itself refuses any existing name;
   - re-read Drive's MD5/size.
   - A file already at that path from an interrupted run is kept only if identical; otherwise CONFLICT, and it is
     left untouched.
6. **Publish** `manifest-(N+1)` only after every file of the batch passed.
   - Out of time: status **PARTIAL**, no manifest; the next run resumes.
   - Any error: **FAIL**, no manifest.
7. **Deep verification:** re-hash about 1/28 of the archive with **SHA-256** each run (everything about every 4
   weeks). This catches corruption that Drive's MD5 metadata would not show.
8. **Report:** write `_verification/sync-<time>-<status>.json` (never overwriting) and **email the owner** on
   FAIL, on recovery after a FAIL, or after 3 PARTIAL runs in a row.

**Never:** the mirror deletes, renames, trashes or overwrites nothing.

## Tests (actual results, 2026-10-10)

| Suite | Result | Covers |
|---|---|---|
| `apps_script/test_entrypoints.mjs` (node, fakes of every Google service `Code.gs` calls) | **4/4 pass** | configure installs no trigger; manual adopt → NOOP retry → full verify OK; failure written to Drive and emailed; enable/disable touch only this script's trigger; editor wrappers select the pinned feeds; nothing runs unconfigured |
| `apps_script/test_mirror.mjs` (node, in-memory Drive) | **15/15 pass** | as below, plus resumable read-only full verification and Python-identical escaping (DEL, Latin-1, astral) |
| (13 original mirror cases) | pass | adoption + no-op re-run; adoption refusing changed bytes, stray file or duplicate; incremental upload with the manifest strictly last; interrupted upload, safe restart without re-upload; time-budget PARTIAL then completion; source checksum mismatch never uploaded; MISSING / MISMATCH / DUPLICATE / UNEXPECTED stop before any upload; name conflict left untouched; feed rewriting or dropping history refused; feed digest; manifest-chain tampering; deep SHA-256 verification catching corruption hidden from MD5 metadata; error reports carry no credentials; Python/JS canonical JSON identical |
| `test_increment.py` (pytest, real git) | **10/10 pass** | as below, plus the single paste file equals the tested sources (ASCII) and the staged feed extends the baseline by checksum-verified blobs |
| (8 original planner cases) | pass | unchanged repo adds nothing (identical feed); new partition + index snapshot, strictly appended, commit-pinned URLs; changed frozen dataset gives a new snapshot with old kept, plus schema and cutoff; rewritten partition is a PlanConflict; incremental code bundle applies on the previous heads, and no new commits means no bundle; **end to end:** planner → mirror (same `mirror_core.gs`, run by node on a local folder) → adopt → PARTIAL → resume → NOOP → restore manifest 0 and latest → corruption caught by restore and by the mirror; broken chain refused while an older state still restores; committed baseline equals the Drive report |
| Dry run on the real 4,676-file package (local folder) | pass | ADOPTED (1.4 s) → NOOP → the real code increment (219,854 B bundle) OK → NOOP → restore manifest 0: 4,676 files; latest: 4,677 files; all SHA-256 checked |
| Existing suites | `pytest research/`: 1,084 passed, 4 skipped | unchanged behaviour |

**Not tested** (it can't be tested here): real Google services. `Code.gs` is tested against fakes that follow the
documented APIs (Drive v3 Advanced Service, DriveApp, UrlFetchApp, Utilities, MailApp, ScriptApp, LockService,
PropertiesService). The real behaviour, quotas and timing are confirmed only by the manual first run in
`APPS_SCRIPT_SETUP.md`.

## Authentication and scheduling (recommendation, not installed)

| Option | Unattended? | Credential location | Verdict |
|---|---|---|---|
| Drive connector in a chat session | no | claude.ai connector | Reads only. Uploads would pass every byte through the model, and background routines get it only if explicitly granted. Not suitable. |
| This cloud container | no | none; nothing persists | Not suitable. |
| rclone / Drive API from GitHub Actions | yes | refresh token as a repository secret | Works, but a Google token would live in a public repo's Actions secrets and needs full `drive` scope (see below). Second choice. |
| Colab | no free scheduling (scheduled notebooks are paid) | none | Keep for one-off rebuilds only. |
| **Apps Script in your account + feed from GitHub Actions** | **yes** | **none outside Google** | **Recommended** |

**Scope:**
- **What it needs:** `drive` (full Drive). The baseline was created by Colab, a different app, so the narrower
  `drive.file` scope could not see or verify it.
- **What it touches:** the script only opens the folder whose ID `setupArchiveSync()` records. It contains no
  delete, trash, rename or overwrite call. That is reviewable in a few hundred lines.
- **Other scopes:** `script.external_request` (fetch public GitHub URLs), `script.scriptapp` (its own trigger),
  `script.send_mail` (email to you), `userinfo.email`.

**One-time setup:** see `APPS_SCRIPT_SETUP.md` (phone steps, first manual run, evidence checklist).
- `configureBaseline()` records the folder and installs no trigger.
- First runs read the committed baseline feed at pinned commit 405cd8a.
- A staged one-file increment (`feed_stage/`, pinned at 518c0fc) lets the real upload path be tested by hand.
- Only `enableDailySchedule()` installs the trigger, and only after approval. It also needs `archive-feed` seeded
  and the feed workflow installed (separate approvals).

**Persistence:** the authorization and trigger survive indefinitely, independent of Claude Code, this container or
your browser. Google may ask for re-consent if the script's scopes change.

**Revoke:**
- Google Account → Security → Third-party apps → `CryptoPulseV2 archive sync` → Remove access;
- or run `stopArchiveSync`;
- or delete the project.

The archive stays as it is.

**Failures:** email to you (Apps Script also emails trigger errors itself), plus a JSON report per run in
`_verification/`. Optionally, a weekly Claude Code routine can read the latest report through the Drive connector
and summarise it. That needs the connector explicitly granted to the routine.

## Storage and transfer (assumptions stated)

| Stream | Basis | Per day | Per year |
|---|---|---|---|
| Forward store partitions + run records | 66 KB measured for a full day (2026-10-08) | ~66 KB | ~24 MB |
| Learning-export snapshots | 1.67 MB per version, +14 KB/day measured; one version per daily export | ~1.7 MB | ~0.65 GB (gzip would make it ~0.13 GB; not implemented) |
| Code bundles | 219,854 B for one day of archive-branch commits (measured) | ≤ ~0.2 MB | ≤ ~70 MB |
| Snapshot manifests + state manifests | ~0.5 KB per added file | ~25 KB | ~10 MB |
| **Total** | | **~2 MB** | **~0.75 GB**, about 0.2% of 320 GB |

- **Transfer:** about 2 MB a day from GitHub to Apps Script, and the same written to Drive.
- **Deep verification:** reads about 14 MB a day from Drive.
- **Quotas:** well within Apps Script's consumer quotas (6 min per run, 90 min of trigger time per day, 20,000 URL
  fetches per day).
- **Cost:** none.

## Recovery

- **Restore** any known-good state: download the folder, then
  `python3 research/archive/restore.py --archive <dir> --seq N --out <restore>`.
  - It replays manifests 0..N, checks the chain and state digest, copies only that state's files and checks every
    SHA-256.
  - Later files are ignored. It refuses to overwrite.
- **Code:** `git clone` the baseline bundle, then `git fetch` each `05_code/increments/*.bundle` in date order.
- **Missing or corrupted file in Drive:** the mirror stops and reports it, and repairs nothing by itself. Repair is
  a separate, approved operation: re-create the missing file from its pinned source URL, after checking.
- **Interrupted run:** nothing to do; the next run resumes and never duplicates.

## Limitations and manual steps

- **Not exercised in real Google Apps Script yet:** the Advanced Drive Service calls and quotas.
- **Parquet is not generated for increments.** Raw files are archived; Parquet can be rebuilt offline with
  `archive.py build`.
- **Not covered:** data that lives only in production D1 (see `D1_EXTRACTION_PLAN.md`, Step 1 refined).
- **Public repository:** feed entries, URLs and generated blobs are public, like the repository. Production
  extracts reach Drive through the public repo unless you choose a manual upload for them.
- **Approvals still needed:** seeding `archive-feed`, installing the feed workflow, the Apps Script setup, and
  enabling its trigger.
