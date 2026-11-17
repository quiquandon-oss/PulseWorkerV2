# Resolution rule: primary (frozen) vs slot-aware sensitivity analysis — 2026-11-17

Production evidence, read-only (D1 `experiment_4_timesfm`, `btc_data`; EXP-008 `derived/ledger.json`), 2026-11-17
~05:30 UTC.

- **Primary (frozen-v1):** the first `btc_data` price with `ts >= target_ts`.
- **Sensitivity (slot-v2):** the first `btc_data` price with `ts >= target_ts - 120 s`.

Per the owner decision of 2026-11-17, the primary result is the only one that counts toward the frozen plan. This is
interim and descriptive: no test is run before the 100-origin checkpoint.

## Summary

The two models share origins, so their observation timestamps are identical.

| Model | Population | H | n resolved | Correct, frozen (primary) | Correct, slot (sensitivity) | Observation differs | Outcome differs |
|---|---|---|---|---|---|---|---|
| TimesFM | prospective | 12 h | 15 | 6 | 5 | 9 | 1 (id 127) |
| TimesFM | prospective | 24 h | 14 | 4 | 4 | 7 | 0 |
| Chronos-2 | prospective | 12 h | 15 | 7 | 6 | 9 | 1 (origin 11-03 15:01) |
| Chronos-2 | prospective | 24 h | 15 | 4 | 4 | 7 | 0 |
| TimesFM | post-seal descriptive | 12 h | 22 | 10 | 10 | 11 | 2 |
| TimesFM | post-seal descriptive | 24 h | 22 | 11 | 10 | 9 | 1 |
| Chronos-2 | post-seal descriptive | 12 h | 2 | 1 | 2 | 2 | 1 |
| Chronos-2 | post-seal descriptive | 24 h | 2 | 1 | 1 | 1 | 0 |

**Check of the primary rule:** stored TimesFM outcomes equal a fresh recompute under the frozen rule for all 73
resolved rows since 2026-10-10 (0 mismatches). The production resolver implements the protocol as written.

**Pending:** TimesFM 3 (ids 152, 153, 154), Chronos-2 2. TimesFM 24 h n is 14, against 15 for Chronos-2, because
id 152 waits for the next TimesFM run; Chronos-2 resolved the same origin at 04:10 UTC.

**Data gap:** no `btc_data` from 2026-11-08 00:01 to 2026-11-09 00:01 UTC. The 24 h target of origin 2026-11-07 12:01
(TimesFM id 136) resolves on 2026-11-09 00:01:06 under **both** rules. It is counted, not excluded.

## Prospective rows where the two rules observe a different price (TimesFM ids; Chronos-2 same origins)

Each target is `origin + H`. "Slot obs." is the price written for that target's own tick.

| id | H | Origin (UTC) | Target (UTC) | Frozen obs. (primary) | Slot obs. (sensitivity) | Call | Actual frozen / slot |
|---|---|---|---|---|---|---|---|
| 127 | 12 | 11-03 15:01:11.612 | 11-04 03:01:11.612 | 11-04 06:00:56.636 ($77,799) | 11-04 03:01:02.259 ($76,096) | UP | UP / **DOWN** |
| 128 | 24 | 11-03 15:01:11.612 | 11-04 15:01:11.612 | 11-04 18:01:07.040 | 11-04 15:01:02.400 | UP | UP / UP |
| 131 | 12 | 11-05 15:01:06.556 | 11-06 03:01:06.556 | 11-06 06:01:03.146 | 11-06 03:01:04.328 | UP | DOWN / DOWN |
| 133 | 12 | 11-06 15:01:17.718 | 11-07 03:01:17.718 | 11-07 06:01:00.742 | 11-07 03:01:03.789 | UP | UP / UP |
| 134 | 24 | 11-06 15:01:17.718 | 11-07 15:01:17.718 | 11-07 18:01:01.783 | 11-07 15:00:58.280 | UP | UP / UP |
| 135 | 12 | 11-07 12:01:07.670 | 11-08 00:01:07.670 | 11-08 03:01:06.315 | 11-08 00:00:57.117 | UP | DOWN / DOWN |
| 139 | 12 | 11-10 15:01:48.485 | 11-11 03:01:48.485 | 11-11 06:01:04.231 | 11-11 03:00:58.040 | UP | DOWN / DOWN |
| 140 | 24 | 11-10 15:01:48.485 | 11-11 15:01:48.485 | 11-11 18:01:03.792 | 11-11 15:01:07.177 | UP | DOWN / DOWN |
| 143 | 12 | 11-12 15:01:07.227 | 11-13 03:01:07.227 | 11-13 06:01:04.224 | 11-13 03:01:00.051 | UP | DOWN / DOWN |
| 144 | 24 | 11-12 15:01:07.227 | 11-13 15:01:07.227 | 11-13 18:01:09.042 | 11-13 15:01:05.459 | UP | DOWN / DOWN |
| 145 | 12 | 11-13 15:01:05.459 | 11-14 03:01:05.459 | 11-14 06:01:03.464 | 11-14 03:01:01.752 | UP | DOWN / DOWN |
| 146 | 24 | 11-13 15:01:05.459 | 11-14 15:01:05.459 | 11-14 18:00:57.049 | 11-14 15:00:59.493 | UP | DOWN / DOWN |
| 147 | 12 | 11-14 12:01:10.588 | 11-15 00:01:10.588 | 11-15 03:00:59.325 | 11-15 00:00:59.896 | UP | DOWN / DOWN |
| 148 | 24 | 11-14 12:01:10.588 | 11-15 12:01:10.588 | 11-15 15:01:03.110 | 11-15 12:01:03.484 | UP | DOWN / DOWN |
| 150 | 24 | 11-15 12:01:03.484 | 11-16 12:01:03.484 | 11-16 15:01:07.811 | 11-16 12:00:57.868 | UP | DOWN / DOWN |
| 151 | 12 | 11-15 18:01:02.399 | 11-16 06:01:02.399 | 11-16 09:01:07.055 | 11-16 06:01:00.019 | DOWN | DOWN / DOWN |

Under the slot rule, each slot price lands 1.8–51.0 s before its target, never more than 120 s. Under the frozen rule
those 16 prospective observations land about 3 h after the target.

## Reading (descriptive, not a result)
- About 55% of the prospective outcomes (16 of 29 for TimesFM) are measured at H + ~3 h under the primary rule. The
  primary 12 h and 24 h results are therefore a 12/15 h and 24/27 h mix. This is disclosed, not corrected.
- One prospective direction outcome differs per model, the same origin for both (2026-11-03 15:01, 12 h). The paired
  model comparison is affected identically for both models.
