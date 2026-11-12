# Change record: increment 2 (read paths, query guard, selection provenance, collector trigger)

**Status:** implemented and tested locally on top of increment 1 (`claude/epic-planck-uyapsw-served-predictions`,
`8c226ed`). **Not deployed, not committed. Migration 0025 not applied.** Effective dates are to be added at deploy.

## What changes

| # | Change | Data-generating effect from deploy |
|---|---|---|
| 1 | **Read-route cache.** 18 dashboard read routes, each verified write-free and repeatable, are cached in Worker memory. The key is the full URL plus a data epoch: newest `prediction_snapshots.computed_ts` plus the current hour. Any invocation that writes clears the cache. `ROUTE_CACHE="0"` disables it. | None. Responses are byte-identical; adds header `X-Route-Cache`. |
| 2 | **Read-once per scheduled run.** Within one scheduled invocation, an identical SELECT is answered from the first result. Invalidated per table on any write, and globally on writes it cannot attribute; nothing is stored while a write to the same table is in flight. `TICK_READ_MEMO="0"` disables it. | None. 73 replayed ticks are byte-identical with and without it. |
| 3 | **Unbounded-query CI guard.** `tests/query-guard.test.js` fails on any new SELECT that can read a whole table (88 reviewed statements in the baseline), and when the baseline lists one that no longer exists. | None (CI only). |
| 4 | **Selection provenance.** New table `selection_provenance` (migration 0025). For each new selection row it records the resolved prediction `chosen_p_up` came from (`chosen_p_up_source_ts`), the chosen variant's own value for the cycle (`cycle_p_up`), the trigger (`cron`, `cron_daily`, `request_refresh`) and a semantics version. Selections also return `selection_row_id`, `prediction_ts`, `chosen_p_up_source_ts`, `cycle_p_up` and `cycle_p_up_ts` (additive). | New table only. Existing selection tables and their INSERT statements are unchanged; selection rows are byte-identical with and without the table. Nothing is backfilled. |
| 5 | **Reliable trigger for the hourly funding collector.** The Worker's hourly cron dispatches `research-funding-intraday-schedule.yml`, which gains `workflow_dispatch`. Same pinned collector, same single write target, same concurrency group; the GitHub schedule stays as a fallback. Requires secret `RESEARCH_DISPATCH_TOKEN`. | **Yes, for EXP-23 source variants.** Funding observations become hourly instead of about every 5 hours, so first-observation lag falls. The collection rules and evaluation definitions are unchanged. Must be dated in the EXP-23 record. |
| 6 | **CryptoPulseV2 dashboard** (separate repository, patch only). The as-of line says "Logged prediction from the HH:MM scheduled run" when served from a snapshot. A non-original variant's headline uses `selection.cycle_p_up`; for older responses it falls back to `chosen_p_up`, labelled as the variant's most recent resolved call. The Lab's `/select-variant` note no longer claims the decision is recomputed on request. | Display only. |

## Evidence

**Local replay** (synthetic data, local SQLite D1 stand-in; not production):

| Check | Result |
|---|---|
| Every table that exists on `main`, after the replayed ticks | byte-identical to `main`'s worker (11 tables) |
| SELECTs per scheduled tick | 122 → 120, rows returned 21,051 → 18,849 (the memo only helps between writes) |
| Second request to each of the 18 cached routes | 1 statement (the epoch read), 0 table scans, identical body |
| Provenance | one row per selection; `chosen_p_up` equals the value at `chosen_p_up_source_ts` on a resolved row; `cycle_p_up` equals the chosen variant's value on the cycle's own row |
| Tests | 1,155 JavaScript tests (52 files) and 992 research tests pass |

**Browser** (real CryptoPulseV2 page in Chromium against the local Worker):
- Load: 6 Worker calls, all 200; `/predict` served from the snapshot; the headline equals the stored prediction.
- Current page: "Live read computed … (on request, not a logged prediction)", which is wrong for a snapshot.
- Patched page: "Logged prediction from the … scheduled run".
- Non-original variant with distinct values: the current page shows the resolved value (81%); the patched page shows the cycle value (27%), and labels the fallback when `cycle_p_up` is absent.

**Production evidence for change 5** (GitHub run history, read-only, 2026-11-12):
- 118 scheduled runs in 624 hours (19%); median gap 5.2 h, maximum 11.0 h.
- EXP-23 run 43088304021 (2026-11-11): Hyperliquid variant not evaluable at 738 of 771 hours; median first-observation lag 4.1 h.

## Findings recorded, not changed

- **Two `btc_data` rows per 3-hourly tick.** The cron calls `predictAndLog` once per horizon, and each call runs `logBtcData` (code fact; reproduced in the replay: +2 rows per tick). The price series that feeds the k-NN history therefore contains near-duplicate rows. Removing them changes model inputs, so it needs an owner decision and a versioned change. Production row counts are not verified (needs A4).
- **Unauthenticated write routes on `main`.** `/btc-backfill`, `/recalibrate-refresh` and similar routes. The reviewed fix exists on `security/v2-ops-routes` (`2e500a1`), not merged.
- **8 November outage.** Event detection, evidence collection, export and TimesFM jobs failed between 12:36 and 22:57 UTC. The event-detection log shows `D1_ERROR … exceeded D1's free tier daily row read limit`.
- **EXP-23 primary funding source** (Binance monthly archive) is stale at 240 of 771 hours until each month's archive is published. That is a property of the frozen source choice, not a collection fault.

## Production steps required (owner approval each)

1. Apply `.ai/migrations/0025_selection_provenance.sql`. Additive; the Worker skips recording until it exists.
2. Create a fine-grained GitHub token (PulseWorkerV2 only, Actions: write) and set it as Worker secret `RESEARCH_DISPATCH_TOKEN`.
3. Merge to `main` (workflow change) and deploy the Worker.

## Rollback

`ROUTE_CACHE="0"`, `TICK_READ_MEMO="0"` and `SELECTION_PROVENANCE="0"`; unset `RESEARCH_DISPATCH_TOKEN`; or redeploy the
previous version. Migration rollback: `DROP TABLE selection_provenance;`.
