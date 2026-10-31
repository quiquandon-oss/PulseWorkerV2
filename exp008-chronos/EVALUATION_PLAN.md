# EXP-008 evaluation plan: PROPOSED (not frozen)

**Status:** PROPOSED by the implementation, 2026-10-31, before any Chronos-2 forecast exists. It becomes
the pre-registered plan only when the owner approves it unchanged. Approval should be recorded by commit
here before the first checkpoint below is reached. Until then, every EXP-008 result is descriptive.

## Population

- **Included:** every Chronos-2 forecast in `research-data/exp008-chronos` with a resolution, paired on
  (horizon, `input_end_ts`) with a resolved EXP-004 TimesFM forecast and with a production k-NN prediction
  from the same horizon logged within [−3 h, +10 min] of the origin.
- **Exclusions,** each reported with its count:
  - unresolved;
  - no TimesFM pair;
  - no k-NN prediction;
  - ground truth disagreeing with TimesFM.
- **No cherry-picking:** nothing else is excluded, and windows are never chosen after looking.
- **Sealed period:** no origin before 2026-10-09T21:00Z can exist. Collection starts after it.

## Outcome and metric

- **Correct:** the predicted direction (median return > 0 means UP) equals the realized direction (first
  price at or after the target, return > 0 means UP).
- **Metric:** directional hit rate per horizon (12h, 24h).

## Comparisons

| Baseline | Definition |
|---|---|
| Always-UP | UP for every origin |
| Last-tick persistence | the direction of the last deduplicated tick before the origin (recorded in each forecast) |
| TimesFM (EXP-004) | its stored direction at the same origin |
| Production k-NN | `p_up ≥ 0.5` means UP, from the matched prediction |

## Test

- **Statistic:** two-sided exact McNemar test on discordant pairs, Chronos-2 vs each baseline, per horizon.
- **Corrections:** α = 0.05, Holm-corrected across the 8 comparisons (4 baselines × 2 horizons).
- **Checkpoint:** the first analysis is at **100** paired resolved origins per horizon (about 100 days),
  computed once. There is no interim analysis for success.

## Success criterion

At the checkpoint, Chronos-2's hit rate is significantly higher (Holm-corrected) than **both** always-UP
and last-tick persistence at **both** horizons.

**Replication:** the same result in the **next 100** non-overlapping paired origins per horizon.

The comparisons with TimesFM and k-NN are reported, but they are not part of the success criterion. That
matches EXP-004's frozen criterion.

## Decision rule

- **Both windows meet the criterion:** eligible for an owner decision on research-only use.
- **Otherwise at the replication checkpoint:** reported as "no evidence of skill".
- **In every case,** the owner decides. Nothing in production changes automatically.
