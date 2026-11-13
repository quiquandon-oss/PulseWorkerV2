# CryptoPulse V2 release ledger

One line per increment across `PulseWorkerV2` (Worker) and `CryptoPulseV2` (dashboard). States are used strictly:
**merged** (on `main`, CI passed), **deploy waiting** (run exists, not deployed), **deployed** (run succeeded),
**verified** (behaviour checked on the live system). A waiting deployment is never described as live.

Last updated: 2026-11-13 12:10 UTC.

## Production state

| | Value |
|---|---|
| Worker version live | `ed70965` (Deploy Worker run 43266952163, deployed 2026-11-13 09:46:58 UTC). Verified: predictions written at the 12:00 UTC run carry `git_commit_sha` `ed70965`. |
| Worker deploy waiting | Run 43371883351, commit `e0a174e`, waiting for the `production` environment approval since 2026-11-13 09:47:54 UTC. |
| Dashboard live | `34bfc7a` on GitHub Pages. |
| D1 migrations applied | 0024 `prediction_snapshots`, 0025 `selection_provenance`, 0026 `d1_usage_daily` (additive, tested on staging first). |

## Increments

| # | Purpose | Repo | PR → merge | Deploy | State and evidence |
|---|---|---|---|---|---|
| 1+2 | Served prediction snapshots, read cache, per-run read memo, selection provenance, hourly collector trigger | Worker | #115 → `32a357d` | Run 43265965867, 2026-11-12 17:16 UTC | **Verified.** `/predict` served from snapshot; one selection row and one provenance row per cycle since deploy; hourly collector runs every hour since 2026-11-12 18:00 UTC. |
| — | Labels for stored predictions and the cycle's own probability | Dashboard | #16 → `34bfc7a` | Pages | **Verified** in a real browser against the live Worker (desktop and phone), 2026-11-13 12:05 UTC. |
| 3 | Authentication on operational routes (8 routes) | Worker | #116 → `ed70965` | Run 43266952163, 2026-11-13 09:46 UTC | **Verified.** `/recalibrate-refresh`, `/btc-backfill` return 503 without a configured admin token (fail closed). |
| 4 | Analyst-relay write authentication, 5 further routes gated, fallback to the existing admin secret | Worker | #117 → `cdb7e39` | In run 43371883351 | **Deploy waiting.** Live today: `/backtest-technical` public, relay accepts unauthenticated POST. |
| 4b | Dashboard relay write key (session-only, never in source) | Dashboard | #17 (open) | — | **Held** until increment 4 is live. |
| 5 | `d1_usage_daily` recording | Worker | #118 → `e0a174e` | In run 43371883351 | **Deploy waiting.** |
| 6 | Export of price series | Worker repo | #119 → `d40f52f` | none needed | **Verified.** Export run 43372515598. |
| 7 | One price row per run (flag, off) + measurement + model replay | Worker repo | branch `research/price-row-per-tick` (`ade4202`), not merged | never | **Research only.** Awaiting owner decision. |
| 8 | EXP-002 progress counter | Worker repo | #120 → `38a5df9` | none needed | **Merged**, run on the export. |
| 9 | Prediction staleness alert (3-hourly) | Worker repo | #121 → `0f2987f` | none needed | **Verified** by manual run 43378449499. Scheduled runs not yet observed. |
| 10 | Scheduled-workflow health alert (11 workflows) | Worker repo | #122 → `0b3e92e` | none needed | **Merged**; live check reports all 11 healthy. |
| 11 | EXP-002 evaluator (counts only below the checkpoint) | Worker repo | #123 → `9e9bed5` | none needed | **Merged**, run on the export. |

## Open risks

- The analyst-relay write route and four backtest routes stay public until run 43371883351 is approved.
- Operational routes are unusable (503) until that same deploy adds the fallback to the existing admin secret.
- Real D1 consumption is unmeasured until `d1_usage_daily` is live.
- The route cache is per isolate; cross-isolate hit rate in production is low.

## Research status (separate from engineering)

- **Goal A (early detection):** not evaluable; fewer than 20 watched episodes.
- **Goal B (EXP-23):** 7 of 60 events (UP 1/25, DOWN 6/25) at 2026-11-12 19:52 UTC. No metric below the checkpoint.
- **EXP-002:** 10 cycles per horizon since 2026-11-01, 0 informative units (checkpoint 111 per horizon).
- **Duplicate price rows:** about 36% of calls would differ on a one-row-per-run series; no evidence either predicts better.
