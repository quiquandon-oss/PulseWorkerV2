# Risk Regime €0 source collection: live run of 2026-10-08

Data collection only. No Risk Regime Shock score, no weights, no methodology version, no V1 change, no
Candidate #1 activation, no D1 write, no deployment. Candidate #1 stays **NEW_SIGNAL / Risk Regime Shock /
REGIME_MODIFIER / DATA_COLLECTION_REQUIRED**. Nothing below is interpreted.

Command (unchanged runner, `--live`):

```
python3 research/risk_regime_reconstruction.py --v1 <v1_full.json> --predictions <predictions.json> \
  --events <research_events.json> --btc <btc_data.json> --hl-cache <dir> --hl-funding-cache <dir> \
  --gdelt-cache <dir> --out-dir research/results --live
```

Inputs: read-only `SELECT`s on the production D1 (`changes: 0`, `rows_written: 0` on every query); see
`RISK_REGIME_DATASET.md → Reproducing` for the hashes.

**Event #15 prediction boundary used: 2026-09-27 12:01:27 UTC** (`PRED-1125`, p_up 0.73, the existing
`EVENT15` constant). The event itself (`RE-15`) is 2026-09-28 03:01 UTC.

## Code changes in this run (only what real provider responses required)

| Change | Why |
|---|---|
| `Http`: HTTP 403/451 with the provider's country/location refusal → status `BLOCKED_BY_PROVIDER_GEO_RESTRICTION` (not retried) | Bybit and Binance answered the now-allowed hosts with a geo-block; it was being reported as a generic `PROVIDER_ERROR` |
| `Http`: GDELT's plain-text "Please limit requests" body is treated as HTTP 429 | GDELT DOC sends that body as its rate-limit answer |
| `Http`: optional `max_backoff_s` and `cache_dir` (successful responses only, stored with their real retrieval time) | GDELT DOC answers ~1 in 4-10 requests from the shared egress IP; the run took hours and must resume rather than restart |
| `collect_gdelt_doc`: a request that still fails is recorded in `failed_requests` (status `PARTIAL`), the other windows are kept; all failing → the error status | one rate-limited request was discarding all 128 |
| Runner: DOC client `retries=40, max_backoff_s=10, cache_dir=<gdelt-cache>/_doc_api` | as above |
| Runner inventory: `first`/`last` skip series with nothing inside the research window; adds `first_any`/`last_any` | the live Deribit options snapshot (timestamped 2026-10-08) crashed the inventory step (`TypeError`) |

Each change has a test (`test_risk_regime_sources.py`, `test_risk_regime_reconstruction.py`). Parsers, the
observation contract, availability rules and the V1 definitions are unchanged.

## Per-source report

`≤ B` = observations whose `available_at` is at or before the boundary (whole history collected). Look-ahead
protection is the per-observation `available_at` rule; "violations" counts observations with
`available_at < timestamp` (0 everywhere).

### Bybit OI · Bybit funding
1. ACCESS: **BLOCKED_BY_PROVIDER_GEO_RESTRICTION**. Host reachable; Bybit CloudFront HTTP 403 "configured to block access from your country".
2. Endpoint: `GET https://api.bybit.com/v5/market/open-interest` (linear BTCUSDT, 1h) · `GET /v5/market/funding/history`.
3. Historical coverage: none collected (documented: OI depth by paging; funding full history).
4. Resolution: 1h OI · 8h funding.
5. Observations: 0 · 0.
6. Event #15 coverage: none.
7. Available ≤ B: 0.
8. Look-ahead protection: built (OI available at bar + 1h; funding at settlement); not exercised.
9. Missing periods: everything.
10. Limitations: refuses this environment's egress location. Official alternates `api.bytick.com`, `public.bybit.com` are not allowlisted (proxy 403) and were not tried further.
11. Incremental vs V1: OI **NEW DIMENSION**; funding **other-venue version of V1 `funding`**.
12. Cost: €0 (public, no key).

### Binance OI · Binance funding
1. ACCESS: **BLOCKED_BY_PROVIDER_GEO_RESTRICTION**. Host reachable; HTTP 451 "Service unavailable from a restricted location".
2. Endpoint: `GET https://fapi.binance.com/futures/data/openInterestHist` (1h) · `GET /fapi/v1/fundingRate`.
3. Historical coverage: none collected (OI: provider keeps **30 days only**; funding: full).
4. Resolution: 1h · 8h.
5. Observations: 0 · 0.
6. Event #15 coverage: none. **Binance OI for Event #15 expires from the provider about 27 Oct 2026.**
7. Available ≤ B: 0.
8. Look-ahead protection: built (bar + 1h; fundingTime); not exercised.
9. Missing periods: everything.
10. Limitations: geo-restriction; `data.binance.vision` / `api.binance.com` not allowlisted (proxy 403).
11. Incremental vs V1: OI **NEW DIMENSION**; funding **other-venue version of V1 `funding`**.
12. Cost: €0.

### Deribit DVOL
1. ACCESS: **OK**.
2. Endpoint: `GET https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600`.
3. Historical coverage: 2026-08-18 09:00 → 2026-10-06 07:00 (collection window; provider history since 2021).
4. Resolution: 1h OHLC.
5. Observations: 4,700 (4 series × 1,175).
6. Event #15 coverage: full (252 observations in boundary −24 h … event +24 h).
7. Available ≤ B: 3,852 (96 in the last 24 h).
8. Look-ahead: candle start + 1 h; the value at the boundary is the 11:00 candle, available 12:00 (61.5 min old).
9. Missing periods: none (100 % of own range, 0 gaps).
10. Limitations: index, not instrument-level history.
11. Incremental vs V1: **NEW DIMENSION** (V1 has no implied volatility).
12. Cost: €0.

### Deribit options OI / put-call / max pain
1. ACCESS: **OK (live snapshot)**; history **NOT_AVAILABLE**.
2. Endpoint: `GET https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option`.
3. Historical coverage: one snapshot, 2026-10-08 16:40 UTC.
4. Resolution: snapshot.
5. Observations: 14 (total OI, put/call OI ratio, max pain for 12 expiries).
6. Event #15 coverage: **none** (POST-EVENT by 10 days).
7. Available ≤ B: 0.
8. Look-ahead: available from its retrieval time only; never attached to a past timestamp.
9. Missing periods: all history.
10. Limitations: the free API has no historical book.
11. Incremental vs V1: **NEW DIMENSION**.
12. Cost: €0.

### DeFiLlama stablecoins
1. ACCESS: **OK**.
2. Endpoint: `https://stablecoins.llama.fi/stablecoincharts/all`, `/stablecoin/1` (USDT), `/stablecoin/2` (USDC), `/stablecoincharts/{Ethereum,Tron,BSC,Solana,Arbitrum}`.
3. Historical coverage: 2026-07-20 → 2026-10-06 kept (provider multi-year).
4. Resolution: 1 day.
5. Observations: 632 (8 series).
6. Event #15 coverage: yes (24 observations in the window).
7. Available ≤ B: 552 (8 in the last 24 h: the 26 Sep point, available 27 Sep 00:00).
8. Look-ahead: day start + 24 h (conservative); the 27 Sep point is not used at the boundary.
9. Missing periods: none.
10. Limitations: daily only; supply, **not** exchange flows.
11. Incremental vs V1: **NEW DIMENSION** (V1 `global` is total crypto market cap).
12. Cost: €0.

### DeFiLlama DeFi activity (DEX volume, TVL)
1. ACCESS: **OK**.
2. Endpoint: `https://api.llama.fi/overview/dexs?dataType=dailyVolume`, `/v2/historicalChainTvl`, `/v2/historicalChainTvl/{Ethereum,Solana,Tron}`.
3. Historical coverage: 2026-07-20 → 2026-10-06.
4. Resolution: 1 day.
5. Observations: 79 DEX + 316 TVL (5 series).
6. Event #15 coverage: yes (3 + 12 in the window).
7. Available ≤ B: 69 + 276 (1 + 4 in the last 24 h).
8. Look-ahead: day start + 24 h.
9. Missing periods: none.
10. Limitations: daily; DEX volume is a single aggregate.
11. Incremental vs V1: **NEW DIMENSION**.
12. Cost: €0.

### GDELT DOC 2.0 (article volume and articles)
1. ACCESS: **OK** (128/128 requests answered after rate-limit retries; 0 failed requests).
2. Endpoint: `GET https://api.gdeltproject.org/api/v2/doc/doc` `mode=TimelineVolRaw` and `mode=ArtList` (4 fixed topics: crypto, monetary_policy, geopolitical_conflict, energy).
3. Historical coverage: 16 windows (Event #15 −24 h … +24 h and each `research_event` −24 h … +24 h), 2026-08-19 00:15 → 2026-09-29 02:45; provider searches only the last ~3 months.
4. Resolution: 15 min (timeline); per article (first-seen minute).
5. Observations: 6,008 timeline observations (8 series) + 14,223 article rows (8,363 unique by topic + URL).
6. Event #15 coverage: partial. Timeline buckets from 2026-09-27 00:15 (the provider returned none for 26 Sep 12:01 → 27 Sep 00:15) with 35 buckets missing inside; 4,857 article rows in the window.
7. Available ≤ B: 5,120 timeline observations (192 in the last 24 h); 740 unique articles first seen in the 24 h before the boundary (conflict 382, monetary policy 189, energy 136, crypto 33).
8. Look-ahead: bucket end + 15 min; article first-seen + 15 min.
9. Missing periods: provider-omitted buckets (135 gaps per series over the 16 windows, nothing filled); outside the 16 windows nothing is collected by design.
10. Limitations: ~25 % or fewer requests answered from the shared egress IP; ArtList ≤ 250 articles per call, oldest first, so long windows hold only their earliest articles (conflict articles available before the boundary stop at 06:45 UTC); broad keyword topics return off-topic items (kept raw); overlapping windows duplicate articles (0 conflicting values).
11. Incremental vs V1: **NEW DIMENSION** (partly overlaps V1's headline sources as text volume).
12. Cost: €0.

### GDELT 2.0 Events (existing)
1. ACCESS: **OK** (4,122/4,122 export files fetched, size + MD5 verified against `masterfilelist.txt`).
2. Endpoint: `http://data.gdeltproject.org/gdeltv2/{batch}.export.CSV.zip` + `masterfilelist.txt`.
3. Historical coverage: 2026-08-24 08:00 → 2026-10-06 06:30.
4. Resolution: 15 min.
5. Observations: 28,562 (6 batch counts + geo_shock); 38,492 conflict-category event rows in the Event #15 window.
6. Event #15 coverage: full.
7. Available ≤ B: 22,668 (672 in the last 24 h); 11,356 event rows published before the boundary.
8. Look-ahead: batch time + 15 min; `SQLDATE` is day-level only and not used for timing.
9. Missing periods: none.
10. Limitations: broad conflict filter (local news); Goldstein kept as sign only.
11. Incremental vs V1: **NEW DIMENSION** (ρ −0.07 with V1 `geopolitics`).
12. Cost: €0.

### Hyperliquid cross-asset (existing)
1. ACCESS: **OK**.
2. Endpoint: `POST https://api.hyperliquid.xyz/info {"type":"candleSnapshot"}` (BTC, xyz:SP500, xyz:XYZ100, xyz:GOLD, xyz:SILVER, xyz:COPPER, xyz:BRENTOIL, xyz:EUR, para:10Y).
3. Historical coverage: 2026-08-18 09:00 → 2026-10-06 07:00.
4. Resolution: 1h.
5. Observations: 21,150 (18 series).
6. Event #15 coverage: full.
7. Available ≤ B: 17,334 (432 in the last 24 h).
8. Look-ahead: candle close + 1 ms.
9. Missing periods: none.
10. Limitations: last 5,000 candles only (this window rolls off ~14 Mar 2027); synthetic perps, weekend prices Hyperliquid-internal.
11. Incremental vs V1: GOLD/BRENTOIL/EUR same instrument as V1; SP500/XYZ100/10Y higher-resolution versions of V1; SILVER/COPPER **NEW DIMENSION**.
12. Cost: €0.

### Hyperliquid BTC funding / premium (existing)
1. ACCESS: **OK**.
2. Endpoint: `POST https://api.hyperliquid.xyz/info {"type":"fundingHistory","coin":"BTC"}`.
3. Historical coverage: 2026-08-18 10:00 → 2026-10-06 07:00.
4. Resolution: 1h.
5. Observations: 2,348 (2 series).
6. Event #15 coverage: full.
7. Available ≤ B: 1,926 (48 in the last 24 h).
8. Look-ahead: funding record time.
9. Missing periods: none.
10. Limitations: one venue; record times carry provider-side ms offsets (kept).
11. Incremental vs V1: **same instrument as V1** `funding` / `longshort`.
12. Cost: €0.

### Not collected by design
Liquidations (Xoomar): **RESEARCH_REQUIRED**, no endpoint identified, nothing substituted. Liquidations (exchange
REST), historical options OI, whale / miner / long-term-holder / exchange flows: **NOT_AVAILABLE** at €0.

## Event #15: what was observable at or before 2026-09-27 12:01 UTC

Only values whose `available_at` ≤ the boundary. "24 h earlier" is the latest value available at
2026-09-26 12:01. Labels: **PRE-EVENT** = available at or before the boundary; **CONTEMPORANEOUS** = becomes
available between the boundary and the event (27 Sep 12:01 → 28 Sep 03:01); **POST-EVENT** = only after the
event; **NOT AVAILABLE** = not collected or no €0 source. Only PRE-EVENT values are shown. Later
observations exist in `results/risk_regime_event15.json`, and none of them is used here.

| Dimension | Label | Value at boundary (obs. time → available at) | 24 h earlier |
|---|---|---|---|
| BTC price (Hyperliquid 1h close) | PRE-EVENT | 84,881 (11:00 candle → 12:00) | 84,142 (+0.88 %) |
| BTC price (V1 `btc_data`, read-only) | PRE-EVENT | 84,925 (12:01:27.293) | — |
| BTC open interest (Bybit, Binance) | NOT AVAILABLE | provider geo-restriction | |
| BTC funding (Hyperliquid) | PRE-EVENT | 1.25e-5 /h (12:00:00.075) | 1.25e-5 |
| BTC premium (Hyperliquid) | PRE-EVENT | −0.000207 (12:00:00.075) | −0.000352 |
| BTC funding (Bybit, Binance) | NOT AVAILABLE | provider geo-restriction | |
| Liquidations | NOT AVAILABLE | Xoomar RESEARCH_REQUIRED; no free exchange history | |
| Deribit DVOL close (high / low / open) | PRE-EVENT | 34.75 (34.82 / 34.72 / 34.73) (11:00 candle → 12:00) | 34.36 |
| Deribit options OI / put-call / max pain | POST-EVENT (snapshot 2026-10-08) / NOT AVAILABLE (history) | — | |
| Stablecoins total | PRE-EVENT | 307.80 bn USD (26 Sep → 27 Sep 00:00) | 308.51 bn (−0.72 bn) |
| USDT · USDC | PRE-EVENT | 183.79 bn · 75.47 bn | 183.75 bn · 76.44 bn |
| Stablecoins by chain: Ethereum · Tron · BSC · Solana · Arbitrum | PRE-EVENT | 145.92 · 92.97 · 16.85 · 16.75 · 3.82 bn | 146.01 · 92.95 · 16.87 · 17.45 · 3.86 bn |
| DEX volume (all DEXs, daily) | PRE-EVENT | 7.46 bn USD (26 Sep) | 10.10 bn (25 Sep) |
| DeFi TVL all chains · Ethereum · Solana · Tron | PRE-EVENT | 95.43 · 53.55 · 6.63 · 5.68 bn | 95.12 · 53.59 · 6.48 · 5.67 bn |
| GDELT raw event volume (last batch 11:45 → 12:00) | PRE-EVENT | 2,644 events; 59,333 in the 24 h before the boundary | 725 (batch); 102,029 (prior 24 h) |
| GDELT conflict-category events | PRE-EVENT | 395 (batch); 11,356 rows in the 24 h | 97 (batch); 18,472 (prior 24 h) |
| GDELT escalation volume | PRE-EVENT | 218 (batch); 5,150 in the 24 h | 36; 7,552 |
| GDELT corridor escalation | PRE-EVENT | 93 (batch); 962 in the 24 h | 6; 1,461 |
| GDELT publishing domains / articles (batch) | PRE-EVENT | 34 / 121 | 18 / 33 |
| geo_shock (frozen component, 12:00 grid point) | PRE-EVENT | 60.24 (not elevated) | 15.97 |
| GDELT DOC article volume (11:30 bucket → 12:00): crypto · monetary · conflict · energy / monitored total | PRE-EVENT | 1 · 0 · 14 · 1 / 404 | stale |
| GDELT DOC articles (unique, first seen in the 24 h before) | PRE-EVENT | 740: conflict 382, monetary 189, energy 136, crypto 33 (conflict list truncated at 06:45 by the 250-article cap) | — |
| Hyperliquid SP500 · XYZ100 | PRE-EVENT | 7,735.5 · 30,638 | 7,728.6 (+0.09 %) · 30,585 (+0.17 %) |
| Hyperliquid GOLD · SILVER · COPPER | PRE-EVENT | 4,281.2 · 64.142 · 6.7887 | 4,282.1 (−0.02 %) · 64.21 (−0.11 %) · 6.7889 (0.00 %) |
| Hyperliquid BRENTOIL · EUR · 10Y | PRE-EVENT | 99.627 · 1.1399 · 5.1648 % | 99.158 (+0.47 %) · 1.1405 (−0.05 %) · 5.170 % (−0.5 bp) |
| Bybit / Binance venue-specific data | NOT AVAILABLE | provider geo-restriction | |
| Whale / miner / LTH / exchange flows | NOT AVAILABLE | no €0 source | |

Every collected dimension except the Deribit options snapshot also has CONTEMPORANEOUS and POST-EVENT
observations in the timeline file. They are not used here and are not predictive evidence.

## Source coverage matrix

| Source | Access | Data collected | Event #15 pre-boundary | V1 relation | Cost |
|---|---|---|---|---|---|
| Bybit OI | BLOCKED_BY_PROVIDER_GEO_RESTRICTION | no | NOT AVAILABLE | new | €0 |
| Bybit funding | BLOCKED_BY_PROVIDER_GEO_RESTRICTION | no | NOT AVAILABLE | other venue of V1 | €0 |
| Binance OI | BLOCKED_BY_PROVIDER_GEO_RESTRICTION | no | NOT AVAILABLE (expires ~27 Oct) | new | €0 |
| Binance funding | BLOCKED_BY_PROVIDER_GEO_RESTRICTION | no | NOT AVAILABLE | other venue of V1 | €0 |
| Deribit DVOL | OK | 4,700 | PRE-EVENT | new | €0 |
| Deribit options snapshot | OK (live) | 14 | POST-EVENT | new | €0 |
| Deribit options history | NOT_AVAILABLE | — | NOT AVAILABLE | new | — |
| DeFiLlama stablecoins | OK | 632 | PRE-EVENT | new | €0 |
| DeFiLlama DEX / TVL | OK | 395 | PRE-EVENT | new | €0 |
| GDELT DOC | OK | 6,008 + 14,223 articles | PRE-EVENT (partial buckets) | new | €0 |
| GDELT Events | OK | 28,562 | PRE-EVENT | new | €0 |
| Hyperliquid cross-asset | OK | 21,150 | PRE-EVENT | same / higher-res / new | €0 |
| Hyperliquid funding/premium | OK | 2,348 | PRE-EVENT | same as V1 | €0 |
| Liquidations (Xoomar) | RESEARCH_REQUIRED | — | NOT AVAILABLE | new | not confirmed |
| Liquidations (exchange REST) | NOT_AVAILABLE | — | NOT AVAILABLE | new | — |
| Whale / miner / LTH / exchange flows | NOT_AVAILABLE | — | NOT AVAILABLE | new | paid |

## Verdict

**DATA COLLECTION INCOMPLETE — SOURCE GAP REMAINS**

- **Bybit OI, Bybit funding, Binance OI, Binance funding** (required): the hosts are now allowed, but both
  providers refuse this environment's egress location (Bybit HTTP 403, Binance HTTP 451). This is a geo-block,
  not an allowlist problem, so venue-specific open interest and funding are still missing. It needs a run
  from an egress location these providers serve (human decision). Binance OI for Event #15 can only be
  collected until about 27 Oct 2026.
- **Liquidations**: Xoomar stays RESEARCH_REQUIRED (no endpoint identified); no free exchange history exists.
- Deribit DVOL, DeFiLlama stablecoins/DEX/TVL, GDELT DOC, GDELT Events and Hyperliquid are complete for the
  research window. Deribit options are live-only.
