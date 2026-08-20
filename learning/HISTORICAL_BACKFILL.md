# Historical Backfill — model_version / git_commit_sha

Date: 2026-08-18
Applied directly against the live `sentiment-history` D1 database
(`f91ca980-b886-423a-bd6f-f3baea46d181`) via the Cloudflare D1 MCP connector.

## What was done

Two nullable columns (`model_version TEXT`, `git_commit_sha TEXT`) were added via
`ALTER TABLE` to `predictions`, `link_predictions`, `eth_predictions`, and
`challenger_predictions`. Purely additive — no existing column was altered, no row
was deleted, no existing value was overwritten.

## Backfill values

Per the explicit instruction for this work, no attempt was made to reconstruct which
exact code version produced any historical row (git history doesn't map cleanly to
per-prediction timestamps at this codebase's commit granularity, and guessing would
violate `.ai/DATA_CONTRACT.md`'s immutability/no-fabrication principle). Every
pre-existing row was set to the same explicit sentinel values:

```sql
UPDATE <table> SET model_version='legacy', git_commit_sha='unknown' WHERE model_version IS NULL;
```

| Table | Rows backfilled |
|---|---|
| `predictions` | 647 |
| `link_predictions` | 550 |
| `eth_predictions` | 36 |
| `challenger_predictions` | 916 |

(Row counts here are slightly higher than the Phase-1-audit snapshot counts, taken a
few minutes earlier — the cron kept running in between, as expected of a live system.)

## What's NOT backfilled

`coin_catalyst_log`'s 2 pre-existing rows (written by V1, unrelated to this contract)
were left with their new columns (`category`, `direction`, `source_url`,
`discovery_timestamp`, `confidence`, `market_classification`) as `NULL`. They predate
the V2 catalyst contract and their true category/timing can't be reconstructed from
what's stored — reported as unknown, not guessed.

## Going forward

Every new prediction row from this deploy onward carries a real `model_version` (from
the `MODEL_VERSIONS` constant in `worker.js`) and a real `git_commit_sha` (injected at
deploy time from `$GITHUB_SHA`, see `.github/workflows/deploy.yml` and
`wrangler.toml`). `'legacy'` / `'unknown'` should only ever appear on rows created
before this change shipped.
