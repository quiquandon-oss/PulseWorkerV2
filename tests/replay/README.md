# Local replay harness

`world.mjs` builds a local world (used by `replay.mjs` and by the vitest integration tests). It runs the real `worker.js` against an in-memory SQLite stand-in for D1, with a frozen
clock, seeded randomness, deterministic UUIDs and stubbed network (Hyperliquid prices scripted,
every other external call answered with HTTP 503). It records a golden result per scenario:
response body and SHA-256, a SHA-256 of every table's contents, and each SQL statement with its
local query plan (SCAN vs SEARCH).

    node tests/replay/replay.mjs <scenario> [--root <repo>] [--worker <other worker.js>] [--omit-tables a,b] > out.json
    # scenarios: cron_tick predict eth_predict link_predict chart_data calibration
    #            calibration_history prediction_freshness challenger_calibration
    #            selection_history select_variant

Each scenario first replays 72 three-hourly cron ticks (9 days) on synthetic seeded data, so
predictions resolve and variant selection runs. Run every scenario twice and compare the files
byte-for-byte to check determinism.

Limitations, by design:
- Synthetic data, not production data. Counts show the shape of the work, not production volume.
- The schema is inferred from the SQL in the code (production has no checked-in base schema);
  indexes come only from `.ai/migrations`. Production plans may differ (Phase 0, item A5).
- Not covered yet: the 07:00 daily jobs, event detection, Research Lab routes.
- `--worker` runs another copy of `worker.js` (for example the previous version) in the same world, to compare
  table contents before and after a change.
- The harness files are not tests themselves. `tests/served-predictions.test.js` and `tests/d1-usage.test.js` use
  `world.mjs`. Nothing here touches production.
