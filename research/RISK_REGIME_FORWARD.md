# Risk Regime forward collection and pre-registered evaluation

Research only. No signal design, no score, no weights, no V1 or Candidate #1 change, no production write, no
deployment.

This phase starts after the completed historical investigation (`RISK_REGIME_EVENT15_RESEARCH.md`,
`RISK_REGIME_OI_RESEARCH.md`). What that work established is not reopened:
- no abnormal OI build-up at either locked Event #15 boundary;
- the deleveraging happened 3–4 days earlier;
- the exploratory 7-day OI decline did not separate failed from correct V1 UP calls;
- Bybit and liquidations are missing.

Here the question becomes forward-looking: **does venue-OI / funding context, measured before each new V1 call,
differ between failed and correct calls on data nobody has seen yet?**

## 1. What is collected (`research/risk_regime_forward.py`)

| Source id | Data | Route | Period / grid | Retention and limits |
|---|---|---|---|---|
| `binance_oi_archive` | BTCUSDT perpetual OI: `open_interest` (BTC) and `open_interest_usd` (USD), kept separate | Binance public data archive, daily metrics files + `.CHECKSUM` (reuses `risk_regime_oi_collect.collect_archive`) | day / 5 min (288 slots per series) | daily files back to 2021-12; a day's file appears after the day ends; **only reachable from the GitHub runner** (this container's allowlist does not include it) |
| `binance_funding_archive` | BTCUSDT funding settlements | archive, monthly files | month / 8 h | **monthly only**: October's settlements arrive in early November (up to ~31 days of research latency). Values were public at settlement, so point-in-time use is unaffected |
| `hyperliquid_hip3` | BTC, SP500, XYZ100, GOLD, SILVER, COPPER, BRENTOIL, EUR, 10Y: hourly close and volume | established `hyperliquid_asset_universe_run.candles` + `risk_regime_sources.hyperliquid_obs` | day / 1 h | `candleSnapshot` serves only the latest 5,000 candles (≈ 208 days): collection must not lapse that long |
| `hyperliquid_btc_funding` | BTC hourly funding and premium | established `hyperliquid_research_run.fetch_funding` | day / 1 h | full history |

**Not collected (still missing).**
- **Bybit OI and funding:** refused from every location available (HTTP 403).
- **Binance REST** `openInterestHist` / `fundingRate`: HTTP 451.
- **Liquidations:** no €0 historical source; Xoomar still RESEARCH_REQUIRED.

None of these is substituted. A permitted route, if one appears, gets its own source id and documentation; it is
not merged into these series.

## 2. Storage design

`research/results/risk_regime_forward/`:

```
<source>/<YYYY>/<partition>.jsonl.gz       observations of one UTC day (funding archive: one month), common contract
<source>/<YYYY>/<partition>.meta.json      fingerprint, file sha256, raw sha256, counts, expected / missing slots,
                                           first_collected_at, last_written_at, superseded fingerprints, units
raw/<source>/<YYYY>/<partition>.jsonl.gz   raw records behind the partition (archive CSV text + zip sha256 + Binance
                                           checksum line; Hyperliquid rows as returned)
runs/<UTC time>_<run id>.json             one record per run: location, every attempt, status, errors, staleness
index.json                                rebuilt from the partitions each run (deterministic)
```

**Rules (tested in `test_risk_regime_forward.py`).**
- **No duplication of the frozen data.** Each series has a cutoff: its last timestamp in the frozen
  `risk_regime_raw` and `risk_regime_oi` files (read-only). Only later timestamps are stored. Expected slots
  start at the first slot after the cutoff.
- **Deterministic files.** Rows are de-duplicated by (series, timestamp) through the existing `ObservationStore`
  and sorted. They are gzipped with `mtime=0`, so identical content gives identical bytes. Writes are atomic
  (temp file + rename); the metadata is written last.
- **Idempotent.**
  - Identity is a content fingerprint that excludes `retrieved_at`.
  - Re-collecting identical content is a no-op: the first collection time is kept.
  - A strict superset (an earlier gap now filled, every earlier value identical) supersedes the partition. Its
    earlier rows and their collection times are kept, and the old fingerprint is recorded.
  - Any differing measured value is a **CONFLICT**: the stored partition is kept, nothing is overwritten, and the
    run records the differing rows.
  - Conflicts are judged on the measurement (value, units, timestamps, availability), not on file-level
    provenance in `raw`.
- **Gaps.** Missing slots are listed per series (as ranges), never filled. Nothing is ever written as zero or
  synthesised. A partition with no data is not written; the run records `NOT_YET_PUBLISHED` (inside the source's
  publication allowance) or `MISSING` (past it).
- **Recovery.**
  - Every run retries absent and gapped partitions inside a bounded window (`--max-days`, default 60;
    Hyperliquid's 5,000-candle limit is the hard bound).
  - A failed source is recorded (`FETCH_FAILED`, with the error) and retried next run. Partitions already
    written in that run are complete and atomic.
  - The workflow commits the run record even when the run fails (`if: always()`).
- **Staleness.** `index.json → sources.<id>.stale` is true when the latest complete partition is older than the
  source's allowance (Binance OI 1 + 3 days, funding month + 7 days, Hyperliquid 2 days).
- **Closed periods only.** A day is collected after it ends; a month after it ends.

## 3. Cadence (proposed, not enabled)

**Authorised: daily at 10:40 UTC** (06:40 was the first proposal; moved later after the publication check).
- **Retention:** daily is far inside Hyperliquid's 5,000-hour limit, so one missed run loses nothing.
- **Publication:** Binance publishes each day's file after the day ends. 10:40 UTC gives the previous
  day's file more time (it was still unpublished at 05:11 UTC on 9 Oct), and a late file is simply picked up by
  the next run.
- **Evaluation fit:** V1 calls resolve in 24 h, so day-level collection matches the day-level unit of analysis.
- **Monthly funding:** daily runs pick up the monthly file within a day of its publication.
- **Collisions:** 10:40 avoids the existing jobs at :00 (including Monday 10:00 and the daily 11:00), 07:30 and
  08:00.
- **Cost:** about 1–3 minutes of runner time per run.
- **Why not more often:** the archive is daily, so intraday runs would only re-check unchanged partitions.

**Why weekly would be worse.** It is safe for retention, but staleness detection and failure recovery would lag by
up to a week.

## 4. Pre-registered evaluation of new V1 calls (`research/risk_regime_forward_eval.py`)

Registered 2026-10-09, before any evaluated call existed. **Registration sha256
`46c0d52b47350a7c7e83841e83ff646d8e5d0f5b9146d98908dab32411ff3e93`.** It is reported with every result. Any edit
produces a new hash: a new study, which cannot count data already seen as confirmation.

| Item | Fixed choice |
|---|---|
| New data only | V1 calls from **2026-10-10 00:00 UTC** |
| Call and outcome | V1 baseline call unchanged (score ≥ 50 → UP); `outcome_engine` 24 h unchanged; FLAT and unresolved excluded |
| Population | V1 UP calls, failed (realised DOWN) vs correct (realised UP) |
| Unit (independence) | the **first** resolved UP call of each UTC day; later calls that day share the outcome window and are excluded from the tests |
| Abnormality | unchanged existing rule (own trailing 7-day 5th–95th percentile, values available at the call time only) |
| Hypotheses (from the exploration, so testable only on new data) | H1 coin-OI level low tail; H2 24 h coin-OI change high tail; H3 72 h coin-OI change low tail; H4 Binance funding level low tail. Each claims "more frequent before failed than before correct UP calls" |
| Test | one-sided Fisher exact test per hypothesis on the day-unit 2×2 table; Bonferroni over 4 (α = 0.0125 each) |
| Design effect | flag rate 30 % before failed vs 10 % before correct; power 80 % |
| **Minimum sample** | **75 independent day-units per group** (derived from the design effect; not tunable) |
| Consistency | the same sign of difference in the chronological first and second halves |
| Decision | `INSUFFICIENT_SAMPLE` (either group < 75: counts only, **no p-value computed**); `SUPPORTED` (minimum met, adjusted p < α, consistent halves); `NOT_SUPPORTED` otherwise |
| Exploratory | every other measure is reported in a separate section and never decides anything |

**What would justify reconsidering Risk Regime Shock.** At least one of H1–H4 is `SUPPORTED` on ≥ 75 failed and ≥ 75
correct new day-units.
- Even then the result is a reason to *design a new pre-registered study* (cross-venue, more mechanisms). It is
  not grounds to activate anything.
- `NOT_SUPPORTED` on all four, with the minimum met, means venue-OI context does not separate V1 UP failures. The
  OI branch of the hypothesis should then be dropped.

**How long that takes.** In the frozen period, 41 days gave 38 day-units, 47 % of them failed. Reaching 75 *failed*
units therefore needs about 160 day-units: **roughly 5–6 months of uninterrupted daily collection (about March
2027)**. Interim reports show counts only.

**Inputs.** The V1 extract must be built read-only from production D1. `history` alone is incomplete: it holds 474
of the 575 frozen-period timestamps, while `research_sentiment_archive` holds all 575. The extract therefore reads
the archive first and fills from `history`:

```sql
-- V1 observations, read-only (8 days before the start, for trailing V1 baselines in the exploratory section)
SELECT ts, score, sources_json FROM (
  SELECT observation_ts AS ts, score, sources_json, 0 AS pri FROM research_sentiment_archive
  UNION ALL SELECT ts, score, sources_json, 1 AS pri FROM history)
WHERE ts >= 1790899200000 GROUP BY ts HAVING pri = MIN(pri) ORDER BY ts;
-- BTC prices for outcomes, read-only
SELECT ts, btc_price FROM btc_data WHERE ts >= 1791504000000 ORDER BY ts, btc_price;
```

Then run:

```
python3 research/risk_regime_forward_eval.py --v1 v1.json --btc btc.json \
  --forward research/results/risk_regime_forward --out research/results/risk_regime_forward_eval.json
```

The evaluation is not automated: it needs a production read and a human to run it.

## 5. Status at registration (2026-10-09)

- **Collected now (this container):** `hyperliquid_hip3` and `hyperliquid_btc_funding` for 2026-10-06 08:00 →
  2026-10-08 23:00 UTC. That is 3 complete day partitions per source (1,280 observations, 0 missing slots, 0
  overlap with the frozen data). A second run was a no-op with byte-identical data.
- **Binance archive:** one verification run on the GitHub runner, run `37887407604`
  (`runs/20261009T051150Z_37887407604.json`).
  - **Location:** `loc=US`, `colo=IAD`.
  - **OI, 2026-10-08:** `NOT_YET_PUBLISHED`. The archive answered 404 `NoSuchKey` at 05:11 UTC on 9 Oct. Nothing
    was written; the next run retries.
  - **Funding, October:** not attempted, because the month has not closed.
  - **Hyperliquid:** partitions already complete, so not re-attempted.
  - **No new Binance data has been collected yet.**
  - **Publication timing:** on 9 Oct the 7 Oct file was available by 04:24 UTC and the 8 Oct file was not by
    05:11. Same-morning publication of the previous day is therefore not guaranteed, and a single daily run can
    lag one day. The retry rule absorbs this without gaps. Moving the proposed run later (for example 10:40 UTC)
    is a reasonable alternative; the evaluation is unaffected either way, because availability is judged on
    `available_at`, not on collection time.
- **Evaluation:** no evaluated calls exist yet (the start is 2026-10-10). The result is `INSUFFICIENT_SAMPLE` by
  construction.

## 6. Enabling (2026-10-09): branch strategy, isolation, and the one remaining step

**Authorised.** Daily research-only collection of:
- Binance OI (archive);
- Binance funding (monthly archive);
- Hyperliquid cross-asset and BTC funding/premium;
- at **10:40 UTC**, writing research data **only** to a dedicated research-data branch.

**Not authorised.** Merging into main, deployment, D1 writes, V1 / Candidate #1 changes, signal design, paid
sources, new secrets.

**Branch strategy.**
- `research-data/risk-regime-forward` is an **orphan** data-only branch, with no shared history, no code, no
  workflows and no Worker files. It is the store of record from now on.
  - It was seeded with a byte-identical copy of `research/results/risk_regime_forward/` at `8307af6` (22 files,
    sha256-compared).
  - The research-branch copy stays as that frozen seed snapshot and is no longer written.
- The code stays on `claude/sweet-meitner-66ntx8`. Workflows check it out read-only (`persist-credentials: false`)
  and run it against a separate checkout of the data branch.
- **Writes are guarded.** The job commits only from the data checkout, refuses any staged path outside
  `risk_regime_forward/`, and pushes only `HEAD:refs/heads/research-data/risk-regime-forward`.

**The schedule needs one file on `main`. It is not installed.** GitHub runs `schedule` (and `workflow_dispatch` /
`repository_dispatch`) only from workflow files on the default branch. No GitHub-supported scheduling exists for a
workflow that lives only on another branch. The research *implementation* does not need to reach main, but a
scheduler definition must. It is prepared, inert, at `research/scheduler/research-forward-schedule.yml`:
- `on: schedule: cron '40 10 * * *'` only;
- `permissions: contents: write` only;
- no secrets;
- the code checked out at a pinned research SHA.

Installing it means writing that one file to `.github/workflows/` on `main`. Your authorisation covered the
research-data branch only, so **the schedule is left disabled**, as your instruction requires when a safeguard
cannot be completed.

**Deployment-isolation evidence** (static; `test_research_workflow_isolation.py`, plus inspection of every
workflow on `main`):

| Path to production | Why it cannot happen |
|---|---|
| Schedule | the stub's only trigger is `schedule`; its job runs a Python collector and `git push` to the data branch; it has no Cloudflare / wrangler steps and references no secrets |
| Push events | `deploy.yml` runs only on pushes to `main` touching `worker.js`, `wrangler.toml`, `package.json`, `package-lock.json` or `deploy.yml`. Pushes to the data branch are not `main`, and the data branch contains no workflow files, so a push there starts nothing. Installing the stub on `main` touches none of deploy's paths. `test.yml` on a push to main runs only its `test` job; every staging/production job requires `workflow_dispatch` |
| Generated commits | made with `GITHUB_TOKEN`, which by GitHub design never starts new workflow runs (only dispatch events are excepted); and they go to the data branch only |
| Workflow chaining | no workflow on `main` uses `workflow_run`; `stage7-staging-dispatcher` dispatches only `stage7-research-pipeline.yml` on its own branch; our jobs have no `actions` permission, no `gh` / `curl`, and cannot dispatch |
| Credentials | no deployment credentials or secrets; actions pinned to commit SHAs; the code checkout keeps no token |

**Not verifiable from here.** Branch protection on `main`, because the GitHub tools available cannot read it.
Isolation does not rely on it.

**The single authorisation still needed.** Approval to add `research/scheduler/research-forward-schedule.yml`
(with `RESEARCH_CODE_SHA` replaced by the reviewed research commit) as `.github/workflows/research-forward-schedule.yml`
on `main`. That is one file, with no code, Worker or package change. After that the first scheduled run is the next
10:40 UTC.

**Evaluation input.** Point `--forward` at a checkout of the data branch (`<checkout>/risk_regime_forward`). The
pre-registration is unchanged (sha256 `46c0d52b…`).

## 6b. Scheduler installed on main (2026-10-09, approved)

- **`main` commit `d59cc2d`** adds exactly one file, `.github/workflows/research-forward-schedule.yml`, on top of
  `a916855`. No research code, Worker, wrangler or package file went to `main`, and the research branch was not
  merged.
- **Schedule:** `40 10 * * *` (daily 10:40 UTC). GitHub registered workflow id 379718524 as `active`. The first
  scheduled run is due 2026-10-10 10:40 UTC; GitHub may start scheduled runs some minutes late.
- **Code pinned to the reviewed research commit `b5bcff8f924dcbd255b73ca94cb5bb743e461c40`.** It is checked out
  with no credentials. Changing the collector for scheduled runs requires a new reviewed pin on `main`.
- **Credentials:**
  - Neither checkout keeps a token, so the collector runs with no credentials on disk.
  - The job token (`github.token`, `contents: write` only, no `actions` permission) reaches only the final step.
  - That step has exactly one `git push`, to `HEAD:refs/heads/research-data/risk-regime-forward`, after refusing
    any path outside `risk_regime_forward/`.
  - The hardened path was proven by run `37946431300`, whose run record landed on the data branch only.
- **Pre-commit checks:**
  - `verify_installed_scheduler()` passed on the exact file.
  - Its non-comment body equals the reviewed stub except the pinned ref.
  - `deploy.yml`'s path filter does not match it.
  - `test.yml` was the only push-triggered workflow, and on push it runs only its `test` job.
  - No `workflow_run` listeners exist on `main`.
- **Push outcome:** the push to `main` started only `Test` (run 37946632720) and no `Deploy Worker` run.

## 7. Verification runs
