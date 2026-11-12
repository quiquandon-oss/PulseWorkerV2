# Controlled release checklist: knn-core-v2 (PR #110, pipeline fp-asof-v2)

**Status:** READY FOR OWNER REVIEW. This is not a deploy approval.

**Change:**
- **Version tags:** `knn-core-v2-asof` (BTC, LINK) and `knn-core-v2-selfcontained` (ETH), pipeline `fp-asof-v2`.
- **What changes:** the as-of V1 context join, train-only imputation and scaling, availability-time backfill stamps
  for new rows, and the read-time restamp of the stored mis-stamped rows (`bf-restamp-v1`).
- **What does not change:** no schema change, no migration, no data rewrite, no new route.

## Before merge

- [x] CI `test / tests` green on the PR head (JS suite and research pytest).
- [x] Leakage regression tests, mutation-checked:
  - reverting the symmetric join fails 2 tests;
  - removing the restamp fails 1 test.
- [x] Read-only scope investigation of the backfill rows (audit, "Scope of #5").
- [ ] **Owner decision:** release now, or hold. EXP-002 and EXP-003 compare against production choices, so a
  mid-collection model change is a confound. The model-version boundary below makes it separable.
- [ ] Owner merges the PR.

## Deploy path (existing safeguards)

- **Trigger:** merging to `main` triggers `deploy.yml`.
- **Gates:**
  1. `test`;
  2. `approval-gate` (`verify_production_environment.py`: production environment with required reviewers and a
     main-only branch policy);
  3. `deploy` (`environment: production`), which **pauses for the owner's approval** in GitHub.
- **No bypass:** none is needed and none should be used.

## Model-version boundary

The boundary is the first prediction row whose `model_version` is `knn-core-v2-*`. It is recorded automatically per
row in `predictions`, `link_predictions` and `eth_predictions`.

**After deploy, check:**
1. The first hourly tick writes rows tagged with the v2 versions and the deployed `git_commit_sha`.
2. No `insufficient_data` or `insufficient_resolved_analogs` status appears that did not appear before. The restamp
   removes about 321 duplicate LINK rows, and the LINK minimum is 30 rows, far below the remaining ~2,200.
3. `/chart-data` is unchanged; charts read stored timestamps.

**Analyses must stratify by `model_version`:** EXP-002, EXP-003, calibration, Model Health and selection.

**Calibration in transition:** `refreshCalibrationCurve` pools all resolved predictions. It will blend v1 and v2
until v1 predictions age out. This is a known transitional effect; it is not fixed here because the change would
alter selection behaviour.

## Rollback

1. Revert the merge commit on `main`. The revert goes through the same gates, and the owner approves the deploy.
2. **Data:** none to undo. Rows written by v2 stay, tagged `knn-core-v2-*`, and remain distinguishable.
3. **Faster path:** Cloudflare dashboard → Workers → pulseworker-v2 → Deployments → roll back to the previous
   version. Afterwards, revert on `main` so the next deploy does not re-ship v2.

## Expected effect

**Integrity only.** PEV-1 found no detectable accuracy difference between the legacy and corrected pipelines at the
available sample size. The change is justified by removing look-ahead, not by a demonstrated performance gain.
