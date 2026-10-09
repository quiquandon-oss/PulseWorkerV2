# Risk Regime research data layer: €0 sources

Research only. This layer collects raw evidence. It has no Risk Regime Shock score, no weights, no thresholds
and no model, and it changes nothing in V1. Candidate #1 stays **NEW_SIGNAL / Risk Regime Shock /
REGIME_MODIFIER / DATA_COLLECTION_REQUIRED**.

Code: `risk_regime_sources.py` (registry and collectors), `risk_regime_data.py` (contract, point-in-time store,
quality), `risk_regime_reconstruction.py` (runner). Machine-readable inventory:
`results/risk_regime_source_inventory.json`.

## Status of every source (live run of 2026-10-08)

Full per-source report, Event #15 pre-boundary table and coverage matrix: `RISK_REGIME_COLLECTION_RUN.md`.

| # | Source | Status | Why |
|---|---|---|---|
| 1 | Bybit BTC open interest | **BLOCKED_BY_PROVIDER_GEO_RESTRICTION** | `api.bybit.com` is now reachable through the proxy, but Bybit's CloudFront answers HTTP 403 "configured to block access from your country" to this environment's (US) egress |
| 2 | Binance BTC open interest | **BLOCKED_BY_PROVIDER_GEO_RESTRICTION** | `fapi.binance.com` answers HTTP 451 "Service unavailable from a restricted location". Binance keeps only **30 days**: Event #15 leaves that window on about **27 Oct 2026** |
| 3 | Bybit BTC funding | **BLOCKED_BY_PROVIDER_GEO_RESTRICTION** | as 1 |
| 4 | Binance BTC funding | **BLOCKED_BY_PROVIDER_GEO_RESTRICTION** | as 2 (full history available once reachable) |
| 2b | Binance BTC open interest, public data archive (`binance_oi_archive`) | **OK** (2026-10-09) | `data.binance.vision` daily 5-minute metrics, 2026-01-01 → 2026-10-07, 280/280 files SHA-256-verified, 80,640 obs per metric; collected on a GitHub-hosted runner verified to be in the US (`loc=US`); same provider, see `RISK_REGIME_OI_RESEARCH.md` |
| 4b | Binance BTC funding, public data archive (`binance_funding_archive`) | **OK** (2026-10-09) | monthly files 2026-01 → 2026-09, 819 settlements |
| 2c | Forward collection (Binance OI/funding archive, Hyperliquid) | **STORE BUILT; NOT SCHEDULED** | `risk_regime_forward.py`, `RISK_REGIME_FORWARD.md`: append-only, idempotent; Hyperliquid collected from 2026-10-06 08:00; Binance pending publication |
| 5 | Liquidations (Xoomar) | **RESEARCH_REQUIRED** | no public "Xoomar" endpoint could be identified (not in any CryptoPulse repository, not found by web search). Exact URL needed. Not substituted |
| 5b | Liquidations (exchange REST) | **NOT_AVAILABLE** | Bybit/Binance publish liquidations only on live WebSockets; no free REST history |
| 6a | Deribit DVOL (history) | **OK** | 4,700 hourly OHLC observations, 2026-08-18 → 2026-10-06 |
| 6b | Deribit options OI / put-call / max pain | **OK** (live snapshot, 2026-10-08) and **NOT_AVAILABLE** (history) | the free API returns only the current book; the snapshot is available from its retrieval time and is never attached to the past |
| 7 | DeFiLlama stablecoins (total, USDT, USDC, 5 chains) | **OK** | 632 daily observations |
| 8 | DeFiLlama DEX volume, DeFi TVL | **OK** | 79 + 316 daily observations |
| 9 | Hyperliquid HIP-3 cross-asset (BTC, SP500, XYZ100, GOLD, SILVER, COPPER, BRENTOIL, EUR, 10Y) | **OK** | re-collected with the asset-universe request bodies |
| 9b | Hyperliquid BTC funding/premium | **OK** | re-collected with the leverage-study request |
| 10 | GDELT 2.0 Events (batch counts, geo_shock, raw event rows) | **OK** | 4,122 export files re-fetched, every one MD5-checked against `masterfilelist.txt`; geo_shock grid recomputed by the frozen component |
| 11 | GDELT DOC 2.0 (article volume, articles) | **OK** | all 128 requests (16 windows × 4 topics × timeline/articles) answered. DOC only searches the **last ~3 months**: late-August data expires about late November 2026 |
| 12 | Whale flows | **NOT_AVAILABLE** | entity-labelled flows need paid / key-gated providers (Glassnode, CryptoQuant, Arkham, Whale Alert) |
| 13 | Miner flows | **NOT_AVAILABLE** | miner address labelling is paid. Free mempool.space / blockchain.com data is miner *activity*, not flows, and is not substituted |
| 14 | Long-term-holder flows | **NOT_AVAILABLE** | UTXO-age analytics are paid |
| 15 | Genuine exchange inflow/outflow | **NOT_AVAILABLE** | exchange wallet labelling is paid. DeFiLlama stablecoin/DEX data is **not** labelled as flows |

**Bybit and Binance** cannot be fixed by the network allowlist: the providers refuse the egress location.
Their official alternates (`api.bytick.com`, `public.bybit.com`, `data.binance.vision`, `api.binance.com`) are
not on the allowlist (proxy CONNECT 403) and were not used; no other provider is substituted. Collecting them
needs an egress location the providers serve (for example a self-hosted runner outside the restricted
regions), which is a human decision. Binance open interest has a hard deadline (~27 Oct 2026 for Event #15).

**GDELT DOC** rate-limits per IP and this environment's egress IP is shared, so roughly 1 request in 4-10 is
answered (HTTP 429 or a plain-text "Please limit requests" otherwise). The DOC client therefore retries up to
40 times with a capped backoff, caches each successful response (with its real retrieval time) under
`<gdelt-cache>/_doc_api/` so an interrupted run resumes, and records a request that still fails as a named gap
(`failed_requests`, status `PARTIAL`) instead of discarding the other windows. In this run no request failed.

## Source details

| Source | Endpoint | Resolution | History / retention | Pagination & rate limit | Availability timestamp used |
|---|---|---|---|---|---|
| Bybit OI | `GET /v5/market/open-interest` (linear, BTCUSDT, 1h) | 1h | depth measured by paging back until empty | 200 rows, `nextPageCursor`; 600 req / 5 s / IP | bar timestamp + 1h |
| Bybit funding | `GET /v5/market/funding/history` | 8h | full | 200 rows, page by `endTime` | settlement time |
| Binance OI | `GET /futures/data/openInterestHist` (1h) | 1h | **30 days only** (requests clipped, never faked) | 500 rows, page by `startTime`; 1,000 req / 5 min | bar timestamp + 1h |
| Binance funding | `GET /fapi/v1/fundingRate` | 8h | full | 1,000 rows; shared 500 req / 5 min | fundingTime |
| Deribit DVOL | `GET /public/get_volatility_index_data` (3600) | 1h OHLC | since 2021 | 1,000 candles, `continuation` | candle start + 1h |
| Deribit options | `GET /public/get_book_summary_by_currency?kind=option` | snapshot | **live only** | — | retrieval time (never used for the past) |
| DeFiLlama stablecoins | `/stablecoincharts/all`, `/stablecoin/{1,2}`, `/stablecoincharts/{chain}` | 1d | multi-year | full series per call; fair use | day start + 24h |
| DeFiLlama DEX / TVL | `/overview/dexs?dataType=dailyVolume`, `/v2/historicalChainTvl[/{chain}]` | 1d | multi-year | fair use | day start + 24h |
| Hyperliquid | `POST /info {candleSnapshot, fundingHistory}` | 1h | last 5,000 candles (1h ≈ 208 days, rolling; this window rolls off ~14 Mar 2027) | 500 rows; 1,200 weight / min | candle close + 1 ms; funding record time |
| GDELT Events | `{batch}.export.CSV.zip` + `masterfilelist.txt` (MD5) | 15 min | 2015-present | one file per batch | batch time + 15 min; `SQLDATE` is day-level only |
| GDELT DOC | `/api/v2/doc/doc` `TimelineVolRaw`, `ArtList` | 15 min / minute | rolling ~3 months | ArtList ≤ 250 per call; ≤ 1 request / 5 s | article first-seen + 15 min |

Fixed DOC topics (declared, not tuned): crypto, monetary policy, geopolitical conflict, energy
(`risk_regime_sources.DOC_TOPICS`).

## New dimension vs existing V1 dimension

| Source | Classification |
|---|---|
| Bybit / Binance open interest | **NEW DIMENSION** (V1 has no open interest) |
| Deribit DVOL, options | **NEW DIMENSION** (V1 has no implied volatility) |
| DeFiLlama stablecoins, DEX volume, TVL | **NEW DIMENSION** (V1 `global` is total crypto market cap). Activity/supply, not exchange flows |
| GDELT events, GDELT DOC | **NEW DIMENSION**. Uncorrelated with V1 `geopolitics` (ρ −0.07); DOC partly overlaps V1's headline sources as text volume |
| Liquidations | **NEW DIMENSION** (not obtained) |
| Bybit / Binance funding | **HIGHER-RESOLUTION / OTHER-VENUE VERSION OF EXISTING V1 DIMENSION** (V1 funding reads Hyperliquid) |
| Hyperliquid BTC funding/premium | **SAME INSTRUMENT AS V1** `funding` / `longshort` |
| Hyperliquid `xyz:GOLD`, `xyz:BRENTOIL`, `xyz:EUR` | **SAME INSTRUMENT AS V1** `gold`, `oil`, `usd` (V1 scores their 24h change) |
| Hyperliquid `xyz:SP500`, `xyz:XYZ100`, `para:10Y` | **HIGHER-RESOLUTION VERSION OF V1** `sp500`, `nasdaq`, `yield10y` (V1 = FRED daily) |
| Hyperliquid `xyz:SILVER`, `xyz:COPPER` | **NEW DIMENSION** |
| BTC price | outcome/context only |

## Raw data contract

Every observation: `source, provider, instrument, metric, timestamp, value, unit, interval, retrieved_at,
historical_or_live, source_url, coverage_status, available_at, raw`. `raw` keeps the provider's original
fields. Availability is judged on `available_at` (publication), never on `retrieved_at` (this run). A live-only
value is available from its retrieval time, so it can never be attached to a past prediction. Nothing is
interpolated or forward-filled; every lookup returns the value's own timestamp and age, and stale values are
labelled, not hidden. All observations are in `results/risk_regime_raw/observations.jsonl.gz`.

## Data quality (the research window 2026-08-18 → 2026-10-07)

| Series | First → last | Obs. | Coverage (own range) | Gaps | Duplicates | Anomalies |
|---|---|---|---|---|---|---|
| Hyperliquid 9 instruments × close/volume | 08-18 10:00 → 10-06 07:00 | 1,174 each | 100 % | 0 | 0 | none |
| Hyperliquid BTC funding/premium | 08-18 10:00 → 10-06 07:00 | 1,174 each | 100 % | 0 | 0 | timestamps carry +ms offsets from the hour (provider-side; flagged, kept) |
| GDELT batch counts (6 metrics) | 08-24 08:00 → 10-06 06:15 | 4,122 each | 100 % (87.7 % of the wider window: starts at the GDELT baseline) | 0 | 0 | none |
| GDELT geo_shock | 08-27 09:15 → 10-06 06:30 | 3,830 | 100 % | 0 | 0 | none |
| Deribit DVOL open/high/low/close | 08-18 10:00 → 10-06 07:00 | 1,174 each | 100 % | 0 | 0 | none |
| DeFiLlama stablecoins (8), DEX volume (1), TVL (4) | 08-19 → 10-06 (daily) | 49 each | 100 % | 0 | 0 | none |
| Deribit options snapshot (14 metrics) | 2026-10-08 (retrieval time; outside the window) | 1 each | n/a | n/a | 0 | live only |
| GDELT DOC 4 topics × article count / monitored total | 08-19 00:15 → 09-29 02:45, **16 event windows only** | 751 each | 19 % of own range (non-contiguous windows by design; plus provider-omitted buckets) | 135 each | 1,090 each (overlapping windows; 0 conflicting) | none |

Per-series figures are in `risk_regime_source_inventory.json → series_quality`.

## Limitations

- Bybit/Binance collectors have still produced **no live data** (provider geo-restriction). Their parsers follow
  the providers' documented response shapes and are fixture-tested only.
- Binance OI (30 days) and GDELT DOC (~3 months) are rolling: waiting loses Event #15 coverage for good. The DOC
  responses of this run are kept in `results/risk_regime_raw/gdelt_doc_articles.jsonl.gz` and the raw store.
- GDELT DOC timelines omit some 15-minute buckets: for the Event #15 query the first bucket returned is
  2026-09-27 00:15 although the request starts 2026-09-26 12:01, and 35 buckets inside the window are absent.
  Missing buckets are not filled. ArtList returns at most 250 articles per call (oldest first), so a long window
  holds only its earliest articles; overlapping windows return the same article more than once (14,223 rows,
  8,363 unique by topic + URL).
- DeFiLlama daily points are given availability at day start + 24 h (conservative).
- Hyperliquid instruments are deployer-oracle synthetic perps; weekend prices are Hyperliquid-internal.
- GDELT's conflict filter is broad (many local-news items); it is preserved raw, not cleaned.
