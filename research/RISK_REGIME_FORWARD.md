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

**Proposal: daily at 06:40 UTC.**
- **Retention:** daily is far inside Hyperliquid's 5,000-hour limit, so one missed run loses nothing.
- **Publication:** Binance publishes each day's file after the day ends. 06:40 UTC usually catches the previous
  day, and a late file is simply picked up by the next run.
- **Evaluation fit:** V1 calls resolve in 24 h, so day-level collection matches the day-level unit of analysis.
- **Monthly funding:** daily runs pick up the monthly file within a day of its publication.
- **Collisions:** 06:40 avoids the existing jobs at 07:00 (V1), 07:30, 08:00 and the 6-hourly :00 runs.
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
- **Binance archive:** pending the one verification run on the GitHub runner (see the run records).
- **Evaluation:** no evaluated calls exist yet (the start is 2026-10-10). The result is `INSUFFICIENT_SAMPLE` by
  construction.

## 6. Authorisation needed before collection runs automatically

1. Explicit approval to run a scheduled research workflow on GitHub-hosted runners. These are US-located; Binance
   REST and Bybit refuse them; the Binance archive serves them.
2. In `.github/workflows/research-forward-collection.yml`, uncomment the two `schedule` lines.
3. Make the workflow reach the **default branch** (`main`), because GitHub runs schedules only from there. That
   means a merge or a cherry-pick of the workflow, the collector and its dependencies, which needs separate
   approval since main deploys the Worker on some paths. The workflow itself touches no Worker file. Its commits
   would then go to `main` unless it is edited to push to a dedicated data branch, which is the recommended edit
   at that point.
4. Separately, for the evaluation: a person (or an approved read-only job) produces the V1 and BTC extracts above.
   No D1 write is involved.
