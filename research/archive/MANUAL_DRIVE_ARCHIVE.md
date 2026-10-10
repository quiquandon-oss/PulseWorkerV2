# Manual Google Drive archive: build, upload, verify, restore

Who: the owner, on their own computer. Nothing here uses the Drive API, D1 or any credential.
Time: about 30–45 min, most of it the one-off GDELT download (293 MB from GDELT's public server).

## 0. Build the package locally (once)

The session container that collected the GDELT files is temporary, so the package is rebuilt on your machine.
The GDELT files are re-downloaded from GDELT and must match the size, MD5 and SHA-256 recorded in
`research/archive/gdelt/cache_sha256.json` byte for byte. Everything else comes from git, pinned by
`research/archive/package_lock.json`.

```
git clone https://github.com/quiquandon-oss/PulseWorkerV2.git && cd PulseWorkerV2
git checkout claude/epic-planck-uyapsw-archive
python3 -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install pyarrow duckdb                              # Python 3.11 or newer
python3 research/archive/package.py fetch-gdelt --out ../cp-work/gdelt      # re-run until "complete": true
python3 research/archive/package.py assemble --repo . --gdelt ../cp-work/gdelt --out ../cp-package
python3 research/archive/package.py verify   --pkg ../cp-package            # must print "ok": true
python3 research/archive/package.py summary  --pkg ../cp-package > ../cp-package-summary.json
```

- `fetch-gdelt` never overwrites a file that is already there. A file with different bytes is listed under
  `conflicts` and left alone. A download whose bytes differ from the record is listed under `failed` and not
  saved.
- Expected result (built in the session on 2026-10-10): **4,676 files, 396,618,468 bytes**, 16 datasets. A
  rebuild from the same lock gives the same raw files. Parquet bytes may differ with another pyarrow version;
  `verify` checks Parquet by exact row round trip, not by bytes.

## 1. Create the Drive folder

1. In Google Drive → My Drive → **New → New folder** → `CryptoPulseV2-Research-Archive`.
2. Right-click → **Share** → General access: **Restricted**, and no other people. Do not create a link.
3. Use this folder only for the archive. If it already exists and is not empty, stop and compare it with
   `SHA256SUMS` first (step 3); never delete or replace what is there.

## 2. Upload in batches

Upload the *contents* of `cp-package/CryptoPulseV2-Research-Archive/` into the Drive folder, keeping the
subfolders. Drive web (**New → Folder upload**) or Google Drive for desktop both work. Before uploading, make sure
**Settings → Convert uploads** is off, so CSV and JSON files are not turned into Google Docs or Sheets.

| Batch | Upload these folders/files | Files | Size |
|---|---|---|---|
| 1 | `00_preregistrations/`, `30_run_records/`, `90_manifests/`, `README.txt` | 37 | 1.7 MB |
| 2 | `05_code/` (git bundle) | 1 | 26.7 MB |
| 3 | `10_raw/frozen/`, `10_raw/exports/`, `10_raw/prospective/` | 72 | 53.5 MB |
| 4 | `10_raw/session_extracts/` (GDELT cache) | 4,541 | 297.5 MB |
| 5 | `20_parquet/` | 24 | 16.3 MB |
| 6 | `SHA256SUMS`: **last** | 1 | 0.9 MB |

Rules while uploading:
- If Drive offers **Replace** or **Keep both** for a name that already exists, choose **neither**: cancel, and
  note the file for review. Drive allows two files with the same name, so "Keep both" would create a silent
  duplicate.
- Do not move, rename or delete anything in the folder.
- If a batch is interrupted, upload that batch again. Step 3 then shows any duplicates or gaps.

## 3. Verify every file against the manifest

Drive's web UI shows no checksums, so verify a downloaded copy:

1. In Drive, right-click the folder → **Download**. Drive zips it, and large folders arrive as several zips.
2. Unzip all parts into one empty folder, e.g. `restore/`.
3. Run:
   ```
   cd restore/CryptoPulseV2-Research-Archive
   shasum -a 256 -c SHA256SUMS          # Linux: sha256sum -c SHA256SUMS ; must end with no FAILED line
   ```
4. Also run `python3 research/archive/package.py verify --pkg restore`. It checks four things:
   - **files:** missing, extra or changed files;
   - **frozen hashes:** the pre-registrations (T-A1–T-A3 registration `66067b32…`, addendum `c2ebdfcb…`,
     erratum `5397fe70…`, OI pre-registration `46c0d52b…`, market-move definition and plan);
   - **Parquet:** every file rebuilds its source rows exactly;
   - **GDELT:** all 4,436 files match the record, and the credential scan is clean.
5. Report any `FAILED`, `missing`, `not in SHA256SUMS` or duplicate name (`file (1).json`) for review. Do not
   fix it in Drive by deleting or replacing.

## 4. Confirm the counts and total size

Run `package.py summary --pkg restore` and compare it with `cp-package-summary.json` from step 0:
- Totals must be equal: **4,675** files in `SHA256SUMS` plus `SHA256SUMS` itself, **396,618,468** bytes in all.
- Each dataset must match: the 16 dataset rows, snapshot ids, file counts and bytes.

In Drive, the folder's **File information → Size** should show about 396.6 MB.

## 5. Restore and run the offline check

On any machine, with no Cloudflare credentials and no network needed after `pip install`:

```
git clone restore/CryptoPulseV2-Research-Archive/05_code/PulseWorkerV2-research.bundle PulseWorkerV2
cd PulseWorkerV2 && git checkout claude/epic-planck-uyapsw-archive      # or use the GitHub clone
python3 -m venv .venv && . .venv/bin/activate && pip install pyarrow duckdb
python3 research/archive/package.py offline-check --pkg ../restore --repo .
```

`offline-check` disables network sockets, then:
1. repeats `verify`;
2. runs the DuckDB analyses and the unchanged production event detectors (8/8 production events reproduced);
3. re-runs the GDELT study from the archived files and index, and compares it with the committed artifact
   (22/22 analytic fields equal).

It must print `"ok": true`.

## After the first upload

- Keep the git branches. They are the second copy. Do not delete the session extracts from git until step 3 has
  passed and you have decided where they should live (see the repository visibility finding in
  RESEARCH_ARCHIVE.md).
- Later uploads add new snapshot folders only. Existing files are never replaced.
- The automated uploader stays deferred until this manual copy is verified.
