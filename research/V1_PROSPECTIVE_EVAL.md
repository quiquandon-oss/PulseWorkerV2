# T-A1 / T-A2: prospective fixed-horizon evaluation

Research only. No V1 source, weight, score or methodology change. No Learning candidate or methodology version is created
or activated, Candidate #1 is untouched, and nothing writes to production D1.

## Frozen plan
- **Plan:** `v1_candidate_registration_addendum_1.json`, kept byte-for-byte.
  - sha256 of the file bytes: `c2ebdfcb75a1690b25f3a327bf7537f8ee6853e16a0dc6295bebd18b8ac7ae9c`; commit `fbedd20`.
  - Its `frozen_utc` (17:05Z) is **wrong**: it was an anticipated value typed in, not read from a clock.
- **Erratum 1:** `v1_candidate_registration_addendum_1_erratum_1.json` (sha256 `5397fe70…`, commit `5d66f7d`).
  - Metadata only. It records the actual chronology from git and file-system metadata: file created 16:58:27Z,
    committed 16:58:48Z, pushed 16:58:51Z, first post-boundary extract written 17:03:09Z.
  - Those times come from the committing machine's clock. The ordering does not depend on them: no post-boundary BTC
    price existed when the plan was pushed.
  - The operative plan is the original addendum plus this erratum. No rule changed.
- **Parent registration:** `v1_candidate_registration.json`, copied byte-identical from `7dd5ab6`.
  - Canonical sha256 `66067b32…`; file-bytes sha256 `ac88bf2d…`.
- **Evaluator:** `v1_prospective_eval.mjs` (`fc91def`).
  - It imports the deployed validator from commit `bdfb2cb` and refuses to run if `learning-method.js` or
    `learning-core.js` differ from their pinned blob hashes.

## Unit and method
- **Holdout:** observations strictly after 2026-10-09 16:00:00 UTC.
- **Independent disagreement day:** a settled UTC day that contains at least one eligible observation. Its unit is
  the earliest eligible observation of that day.
  - **Eligible:** the reconstructed and adjusted scores can both be computed, the 24h outcome resolved and is not FLAT,
    and the adjusted call differs from the reconstructed V1 call.
  - **Settled:** the extract was taken at least 36h after the day ended, and BTC prices exist at least 24h past the
    day's end.
- **Fixed look:** once per candidate, at its 30th unit, on the observations through the end of that unit's day.
  - **Product level:** `validateRecalculation` with the deployed rules (one-sided exact binomial, alpha 0.1, mismatch
    robustness rule).
  - **Research claim:** the same call with alpha 0.1/3. SUPPORTED needs at least 21 of 30 units improved; NOT_SUPPORTED
    needs at least 21 of 30 worse.
  - **Effect reported:** improved/30 with an exact 95% Clopper-Pearson interval.
- **Before the look:** only coverage, exclusion counts, per-day integrity digests and the unit count. Status stays
  AWAITING_HOLDOUT, or LOOK_ELIGIBLE once 30 units exist. No hits, accuracies, p-values or verdicts are computed or written.
- **If 30 units are never reached:** AWAITING_HOLDOUT indefinitely. There is no calendar cutoff.

## Progress run versus fixed look
- **Progress mode** (the default, and the only mode the scheduled run uses):
  - Never forms an outcome direction. Units are counted with `isResolvable`, which checks only that both prices
    exist and differ.
  - Reports a candidate with 30 units as `LOOK_ELIGIBLE`.
  - Before writing, checks the record against a key whitelist (`PROGRESS_KEYS`) and a status list. Any statistic,
    verdict or look makes the run fail and nothing is written.
- **Look mode** (`--mode look --candidate T-A1|T-A2`): run deliberately by a person, for one eligible candidate.
  - The validator must count the same 30 units: re-run on only the frozen-rule unit observations, it must reproduce
    its own independent result.
  - If any check fails: INTEGRITY_ERROR, with no verdict.
- **Integrity:** every earlier record is compared. A day that was settled in any of them must keep its digest,
  otherwise the run becomes INTEGRITY_CONFLICT.

## Daily progress run (inert until installed)
- **Stub:** `research/scheduler/v1-prospective-progress.yml`. Schedule-only, 11:40 UTC (one hour after the forward
  collection).
  - Permissions: `contents: write` only.
  - Checkouts keep no credentials; actions are pinned.
  - **Extract step:** the only step with the secret `RESEARCH_D1_READONLY_TOKEN`. There is no fallback, and the
    production `CLOUDFLARE_API_TOKEN` is never used. It runs three fixed SELECTs (`v1_prospective_extract.mjs`) and
    aborts if any D1 response reports a change.
  - **Progress step:** no credentials.
  - **Commit step:** the only step with the GitHub token. It adds files only, under `v1_prospective/runs/` on
    `research-data/v1-prospective`, with a single fixed-refspec push.
### Read-only access: what is and is not known
- Cloudflare's permission reference lists **D1 Read** ("grants read access to D1") and **D1 Edit/Write** ("grants write
  access to D1"). It does not say which of them the D1 query endpoint
  (`POST /accounts/{account_id}/d1/database/{database_id}/query`) accepts.
- **Not yet tested:** no D1 Read token has been tried against this endpoint. The workflow stays inert until the manual
  test below succeeds.
- **If the endpoint refuses D1 Read:** the run fails closed (HTTP 403, nothing written). Do **not** fall back to
  `CLOUDFLARE_API_TOKEN`. The alternative (a D1 Edit token limited to this one database, or a read-only proxy Worker)
  is a separate decision.

### Installation gates (all required, in order)
1. **Create the token.** Cloudflare dashboard → My Profile → API Tokens → Create Token → Create Custom Token.
   - Name: `research-d1-readonly`.
   - Permissions: **Account → D1 → Read**. Nothing else.
   - Account Resources: Include → the account that owns `sentiment-history`.
   - Optional: an expiry date. Client IP filtering is not useful for GitHub-hosted runners.
   - Check the summary screen shows only `D1:Read`, then create the token and copy it once.
2. **Run the manual test** on your own machine, from a checkout of this branch:
   ```sh
   export RESEARCH_D1_READONLY_TOKEN='<paste>'      # do not commit or echo it
   mkdir -p /tmp/v1p-test && node research/v1_prospective_extract.mjs --out-dir /tmp/v1p-test
   node research/v1_prospective_eval.mjs --mode progress --v1 /tmp/v1p-test/v1_extract.json \
     --btc /tmp/v1p-test/btc_extract.json --out /tmp/v1p-test/progress.json
   unset RESEARCH_D1_READONLY_TOKEN
   ```
   - **Success:** the first command prints row counts. The second prints `T-A1: AWAITING_HOLDOUT, units n/30` and
     the same line for T-A2.
   - The extractor runs only three fixed SELECTs and stops on any response that reports a change.
   - **Do not** test the token by attempting a write against production D1.
3. **Store the secret.** GitHub → repository Settings → Secrets and variables → Actions → New repository secret
   `RESEARCH_D1_READONLY_TOKEN`. Or run `gh secret set RESEARCH_D1_READONLY_TOKEN` and paste the token at the prompt.
4. **Review the run's write path:** the stub, its `contents: write` permission, and its target branch
   `research-data/v1-prospective`.
5. **Create the empty data branch:**
   ```sh
   git switch --orphan research-data/v1-prospective && git commit --allow-empty -m "Research data: T-A1/T-A2 progress records"
   git push -u origin research-data/v1-prospective
   ```
6. **Install:** copy `research/scheduler/v1-prospective-progress.yml` to `.github/workflows/` on main, with
   `V1_PROSPECTIVE_CODE_SHA` replaced by the reviewed 40-hex commit of this branch. Merge only with explicit
   authorisation.

## Running it by hand (read-only)
1. Extract with the statements recorded in the manifest. All are SELECTs on `research_sentiment_archive`, `history` and
   `btc_data`. Save the results as `v1_extract.json` (`{manifest, archive, history}`) and `btc_extract.json`
   (`{manifest, rows}`), with `manifest.as_of_ms` set to the time of the extract.
2. Run:
   `node research/v1_prospective_eval.mjs --v1 v1_extract.json --btc btc_extract.json --out progress.json --previous <last progress.json> --code-commit $(git rev-parse HEAD)`
3. Pass `--previous` every time. If an already-settled day has changed between records, the run becomes
   INTEGRITY_CONFLICT and no look is taken.

## Records
- **First record:** `results/v1_prospective/2026-10-09/`, as of 2026-10-09 ~17:03 UTC.
  - 2 post-boundary observations, 0 settled days.
  - Both candidates at 0 of 30 units: AWAITING_HOLDOUT.
- **Second record:** `results/v1_prospective/runs/2026-10-09T1748Z/`.
  - 3 post-boundary observations, 0 settled days, no exclusions.
  - Both candidates at 0 of 30 units: AWAITING_HOLDOUT.
  - Compared with the first record: no conflict.
- **Expected timing:** at roughly 70–75 % disagreement days (counted on the exploratory window from calls only, without
  outcomes), 30 units need about 40–45 days of V1 coverage. That puts each look around late November 2026, provided V1
  keeps recording daily.

## Exploratory versus prospective
| Exploratory (hypothesis-generating) | Prospective (decides) |
|---|---|
| 575 frozen observations, 2026-08-27 to 2026-10-06 (`results/v1_failure_analysis.json`) | observations after 2026-10-09 16:00 UTC only |

The 30 archive rows between 2026-10-06 and the boundary belong to neither side: they are excluded.
