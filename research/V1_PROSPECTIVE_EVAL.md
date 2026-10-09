# T-A1 / T-A2: prospective fixed-horizon evaluation

Research only. No V1 source, weight, score or methodology change. No Learning candidate or methodology version is created
or activated, Candidate #1 is untouched, and nothing writes to production D1.

## Frozen plan
- **Plan:** `v1_candidate_registration_addendum_1.json`.
  - sha256 of the file bytes: `c2ebdfcb75a1690b25f3a327bf7537f8ee6853e16a0dc6295bebd18b8ac7ae9c`.
  - Committed in `fbedd20` at 2026-10-09 16:58:48 UTC, before any post-boundary price or outcome was read.
  - The file's `frozen_utc` field says 17:05Z, a few minutes after the actual commit. The commit time is authoritative.
    The field is left unchanged because any edit would create a new addendum.
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
  AWAITING_HOLDOUT. No hits, accuracies, p-values or verdicts are computed or written.
- **If 30 units are never reached:** AWAITING_HOLDOUT indefinitely. There is no calendar cutoff.

## Running it (read-only)
1. Extract with the statements recorded in the manifest. All are SELECTs on `research_sentiment_archive`, `history` and
   `btc_data`. Save the results as `v1_extract.json` (`{manifest, archive, history}`) and `btc_extract.json`
   (`{manifest, rows}`), with `manifest.as_of_ms` set to the time of the extract.
2. Run:
   `node research/v1_prospective_eval.mjs --v1 v1_extract.json --btc btc_extract.json --out progress.json --previous <last progress.json> --code-commit $(git rev-parse HEAD)`
3. Pass `--previous` every time. If an already-settled day has changed between records, the run becomes
   INTEGRITY_CONFLICT and no look is taken.

## Records
- **First record:** `results/v1_prospective/2026-10-09/`, as of 2026-10-09 ~17:10 UTC.
  - 2 post-boundary observations, 0 settled days.
  - Both candidates at 0 of 30 units: AWAITING_HOLDOUT.
- **Expected timing:** at roughly 70–75 % disagreement days (counted on the exploratory window from calls only, without
  outcomes), 30 units need about 40–45 days of V1 coverage. That puts each look around late November 2026, provided V1
  keeps recording daily.

## Exploratory versus prospective
| Exploratory (hypothesis-generating) | Prospective (decides) |
|---|---|
| 575 frozen observations, 2026-08-27 to 2026-10-06 (`results/v1_failure_analysis.json`) | observations after 2026-10-09 16:00 UTC only |

The 30 archive rows between 2026-10-06 and the boundary belong to neither side: they are excluded.
