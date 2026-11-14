# Sentiment V2 — current architecture map (verified 2026-11-14)

Verified against the code on `main` (PulseWorkerV2 `df10186`, CryptoPulseV2 `e959052`), not against older
documentation. This map describes what exists before any Sentiment V2 work.

## Status of the product specification

The owner's mandate names `CryptoPulse_Sentiment_V2_Build_Spec.md` as the authoritative specification and asks for it
to be preserved as `docs/SENTIMENT_V2_BUILD_SPEC.md` in both repositories. As of this map it was not found in either
repository (any branch), the Codespace file system, the owner's Google Drive or their Claude artifacts. The owner
then supplied it; it is now `docs/SENTIMENT_V2_BUILD_SPEC.md`, and the implementation is described in
`docs/SENTIMENT_V2_IMPLEMENTATION.md`. This map records the system as it was before that implementation.

## Systems

| System | Where | Deploy | Notes |
|---|---|---|---|
| V1 sentiment producer | original PulseWorker (`sentiment-ff75`), separate repository | outside this workspace | writes V1 tables in the shared D1 database; must stay unchanged |
| PulseWorkerV2 backend | `worker.js` (12,036 lines), Cloudflare Worker `pulseworker-v2` | `.github/workflows/deploy.yml` on push to `main` touching `worker.js`, `wrangler.toml`, `package*.json` or `deploy.yml`; needs the full CI suite and approval through the `production` environment (`required_reviewers`, `branch_policy`) | staging: `wrangler.staging.toml`, Worker `pulseworker-v2-staging`, database `pulseworker-v2-staging` |
| CryptoPulseV2 frontend | single `index.html` + `assets/`, GitHub Pages | `.github/workflows/deploy.yml` on push to `main` | calls `WORKER_URL = https://pulseworker-v2.quiquandon.workers.dev` |

## Shared D1 database `sentiment-history` (binding `DB`)

| Table | Owner | Relevant content |
|---|---|---|
| `history` | V1 (not in this repo's migrations) | V1 observations by `ts`, including `sources_json` |
| `btc_data` | V1 (not in this repo's migrations) | `ts`, `btc_price` |
| `research_sentiment_archive` (0015) | PulseWorkerV2 | per `observation_ts`: V1 composite `score`, `technical_score`, `btc_price`, `sources_json`, `gold_regime`, `source_weights_version`, `schema_version`, `content_hash` |
| `event_ledger`, `event_observations`, `event_source_health`, `event_runs`, `event_documents` (0023) | PulseWorkerV2 | event detection: events, observations, source health, runs, documents |
| `research_events`, `research_analyses`, `research_event_evidence`, `research_hypotheses`, `research_experiment_registry` (0005–0010) | PulseWorkerV2 | research evidence and experiment registry |
| `stage7_research_requests`, `stage7_research_responses`, `stage7_event_sentiment` (0016) | PulseWorkerV2 | stage-7 research pipeline |
| `experiment_4_timesfm` (0002) | PulseWorkerV2 | TimesFM experiment results |
| `prediction_snapshots` (0024), `selection_provenance` (0025) | PulseWorkerV2 | prediction snapshots and model-selection provenance |
| `d1_usage_daily` (0026) | PulseWorkerV2 | daily D1 read/write accounting (`recordD1UsageDaily`, `/d1-usage`) |
| `gemini_quota_ledger` | PulseWorkerV2 | existing free-tier quota control for Gemini calls |

Migrations live in `.ai/migrations/` (0002–0026) and are applied outside the Worker.

## Scheduled execution

- Worker crons (`wrangler.toml`): `0 * * * *` (production tick at hours divisible by 3, Event Detection otherwise) and
  `0 7 * * *` (daily), dispatched in `scheduled()`; every scheduled run records D1 usage.
- GitHub workflows (26): CI (`ci-tests.yml`, `test.yml`), deploys (`deploy.yml`, staging and research dispatchers),
  research schedules (exp004/005/008/009/010, exp23 forward evaluation, research archive feed, forward and funding
  schedules), event detection, PEV-1P counts and candle archive, audits and freshness checks.

## AI and external sources in use today

- Workers AI: binding `AI` declared in `wrangler.toml`; no `AI.run` call and no `@cf/` model in `worker.js`.
  Model availability, licence and free quota for this account are not yet verified.
- Gemini (free tier) for narration, metered by `gemini_quota_ledger`.
- Public Hyperliquid candles for research (no key).

## Frontend

- Views (desktop and mobile navigation, `data-view`): `dashboard`, `lab`, `experiments`, `history`.
- Charts: Chart.js 4.4.4 with the date-fns adapter; Tailwind from CDN; range selectors exist (`range-tab`).
- Tests: `node --test tests/*.test.mjs` (18 tests at `e959052`).

## What a Sentiment V2 comparison needs, and what already exists

| Need | Exists today |
|---|---|
| V1 sentiment with timestamps | `research_sentiment_archive.score` by `observation_ts` (also `history`) |
| BTC price with timestamps | `research_sentiment_archive.btc_price`, `btc_data` |
| V2 sentiment with timestamps | nothing: requires the V2 calculation from the approved specification |
| Usage accounting | `d1_usage_daily`, `gemini_quota_ledger` patterns |
