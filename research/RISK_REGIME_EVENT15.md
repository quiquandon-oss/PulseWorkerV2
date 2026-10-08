# Event #15: raw-data reconstruction

For human inspection. Raw values only: no score, no ranking, no statement about which signal "wins".
Machine-readable: `results/risk_regime_event15.json`; interactive: the Event Research page.

- Prediction boundary: **Sun 2026-09-27 12:01 UTC** (`PRED-1125`, p_up 0.73, BTC 84,925)
- Event: **Mon 2026-09-28 03:01 UTC** (`RE-15`, V2_FAILURE_CLUSTER BTC_24h), BTC 84,429 → 83,480 (about −1.1 %)
- Every value below is the latest one **available** at that time (see availability rules in
  `RISK_REGIME_DATA_SOURCES.md`). Hourly Hyperliquid values are the last closed hour, so they are about
  1 hour old; GDELT counts are the last 15-minute batch, 16.5 minutes old.

## Which sources cover Event #15

| Source | Covers Event #15 | Available before the boundary |
|---|---|---|
| Hyperliquid cross-asset (9 instruments) | yes | yes |
| Hyperliquid BTC funding/premium | yes | yes |
| GDELT 2.0 events (counts, geo_shock, 38,492 conflict-category event rows in −24h…+24h) | yes | yes (11,356 rows published before 12:01) |
| Deribit DVOL (hourly) | yes | yes |
| DeFiLlama stablecoins / DEX volume / TVL (daily) | yes | yes (the 26 Sep daily point, available 27 Sep 00:00) |
| GDELT DOC article volume + articles (4 topics) | yes (buckets from 27 Sep 00:15; the provider returned none earlier) | yes |
| Bybit / Binance OI and funding | would (history exists; Binance OI only until ~27 Oct 2026) | **not collected: provider geo-restriction (HTTP 403 / 451)** |
| Deribit options OI / put-call / max pain | no (no history) | no |
| Liquidations | no (Xoomar unidentified; exchanges have no free history) | no |
| Whale / miner / LTH / exchange flows | no (no €0 source) | no |

## Raw timeline

| | −24h | −12h | −6h | −3h | −1h | −30m | **boundary** | **event** | +1h | +3h | +6h | +12h | +24h |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| UTC | 26 Sep 12:01 | 27 Sep 00:01 | 06:01 | 09:01 | 11:01 | 11:31 | **12:01** | **28 Sep 03:01** | 04:01 | 06:01 | 09:01 | 15:01 | 29 Sep 03:01 |
| BTC (HL close) | 84,142 | 84,413 | 84,450 | 84,777 | 84,851 | 84,851 | **84,881** | **83,367** | 83,295 | 83,143 | 82,934 | 83,003 | 82,923 |
| HL BTC funding (/h) | 1.25e-5 | 1.25e-5 | 1.25e-5 | 1.25e-5 | 1.25e-5 | 1.25e-5 | **1.25e-5** | **−6.5e-6** | −1.0e-5 | −5.7e-6 | 1.07e-5 | 1.25e-5 | −8.7e-7 |
| HL BTC premium | −0.000352 | −0.000368 | −0.000294 | −0.000218 | −0.000247 | −0.000247 | **−0.000207** | **−0.000552** | −0.000582 | −0.000545 | −0.000414 | −0.000297 | −0.000507 |
| xyz:SP500 | 7,728.6 | 7,728.2 | 7,729.0 | 7,739.7 | 7,739.1 | 7,739.1 | **7,735.5** | **7,712.4** | 7,710.0 | 7,712.3 | 7,706.1 | 7,670.7 | 7,668.1 |
| xyz:XYZ100 | 30,585 | 30,581 | 30,615 | 30,672 | 30,648 | 30,648 | **30,638** | **30,397** | 30,367 | 30,372 | 30,304 | 30,133 | 30,186 |
| xyz:GOLD | 4,282.1 | 4,283.2 | 4,280.6 | 4,281.6 | 4,281.4 | 4,281.4 | **4,281.2** | **4,195.3** | 4,197.6 | 4,180.3 | 4,148.0 | 4,121.1 | 4,138.6 |
| xyz:SILVER | 64.21 | 64.19 | 64.11 | 64.16 | 64.16 | 64.16 | **64.14** | **61.98** | 62.11 | 61.68 | 61.06 | 60.92 | 60.69 |
| xyz:COPPER | 6.789 | 6.791 | 6.788 | 6.787 | 6.788 | 6.788 | **6.789** | **6.663** | 6.673 | 6.668 | 6.614 | 6.612 | 6.623 |
| xyz:BRENTOIL | 99.16 | 99.64 | 99.66 | 99.48 | 99.63 | 99.63 | **99.63** | **98.48** | 98.95 | 99.13 | 100.73 | 100.27 | 99.27 |
| xyz:EUR | 1.1405 | 1.1400 | 1.1409 | 1.1398 | 1.1395 | 1.1395 | **1.1399** | **1.1395** | 1.1393 | 1.1387 | 1.1384 | 1.1368 | 1.1374 |
| para:10Y (%) | 5.170 | 5.170 | 5.165 | 5.165 | 5.165 | 5.165 | **5.165** | **5.199** | 5.199 | 5.202 | 5.210 | 5.272 | 5.239 |
| GDELT total events / batch | 725 | 335 | 382 | 434 | 794 | 670 | **2,644** | **844** | 730 | 921 | 1,010 | 1,014 | 884 |
| GDELT conflict events / batch | 97 | 72 | 45 | 135 | 137 | 119 | **395** | **152** | 120 | 121 | 179 | 179 | 164 |
| GDELT escalation events / batch | 36 | 32 | 19 | 57 | 69 | 59 | **218** | **65** | 40 | 56 | 75 | 80 | 57 |
| GDELT corridor escalation / batch | 6 | 9 | 2 | 8 | 23 | 13 | **93** | **28** | 9 | 17 | 12 | 10 | 7 |
| GDELT publishing domains / batch | 18 | 18 | 22 | 30 | 32 | 27 | **34** | **37** | 28 | 33 | 45 | 52 | 49 |
| geo_shock (frozen component) | 16.0 | 8.3 | 14.2 | 85.7 | 30.1 | 55.5 | **60.2** | **48.1** | 25.1 | 36.6 | 78.6 | 78.0 | 66.0 |
| Deribit DVOL close | 34.36 | 34.92 | 34.84 | 34.90 | 34.73 | 34.73 | **34.75** | **35.57** | 35.62 | 35.59 | 36.17 | 35.87 | 36.17 |
| Stablecoins total (bn USD, daily) | 308.51 | 307.80 | 307.80 | 307.80 | 307.80 | 307.80 | **307.80** | **310.09** | 310.09 | 310.09 | 310.09 | 310.09 | 311.47 |
| USDT (bn USD, daily) | 183.75 | 183.79 | 183.79 | 183.79 | 183.79 | 183.79 | **183.79** | **183.79** | 183.79 | 183.79 | 183.79 | 183.79 | 183.79 |
| USDC (bn USD, daily) | 76.44 | 75.47 | 75.47 | 75.47 | 75.47 | 75.47 | **75.47** | **75.42** | 75.42 | 75.42 | 75.42 | 75.42 | 75.25 |
| DEX volume (bn USD, daily) | 10.10 | 7.46 | 7.46 | 7.46 | 7.46 | 7.46 | **7.46** | **7.50** | 7.50 | 7.50 | 7.50 | 7.50 | 11.47 |
| DeFi TVL (bn USD, daily) | 95.12 | 95.43 | 95.43 | 95.43 | 95.43 | 95.43 | **95.43** | **95.48** | 95.48 | 95.48 | 95.48 | 95.48 | 95.44 |
| DOC crypto articles / 15 min | 0 (stale) | 0 (stale) | 1 | 4 | 1 | 2 | **1** | **0 (stale)** | 1 | 2 | 2 | 0 | 4 |
| DOC monetary-policy articles / 15 min | 1 (stale) | 1 (stale) | 10 | 5 | 6 | 13 | **0** | **0 (stale)** | 23 | 32 | 8 | 11 | 27 |
| DOC conflict articles / 15 min | 3 (stale) | 3 (stale) | 48 | 38 | 91 | 63 | **14** | **6 (stale)** | 56 | 61 | 29 | 29 | 90 |
| DOC energy articles / 15 min | 0 (stale) | 0 (stale) | 2 | 2 | 4 | 9 | **1** | **3 (stale)** | 27 | 18 | 11 | 13 | 45 |
| DOC monitored articles / 15 min | 229 (stale) | 229 (stale) | 1,455 | 914 | 2,053 | 1,870 | **404** | **197 (stale)** | 1,630 | 2,527 | 1,126 | 1,146 | 2,484 |
| Bybit/Binance OI & funding | n/c | n/c | n/c | n/c | n/c | n/c | **n/c** | **n/c** | n/c | n/c | n/c | n/c | n/c |

n/c = not collected (Bybit/Binance refuse this environment's egress location). n/a = nothing available at that time (DOC: the provider returned no bucket before 27 Sep 00:15). (stale) = latest available value older than the source's freshness limit, shown with its age in the JSON/page. Daily DeFiLlama points are available at day start + 24 h. Hyperliquid volumes, DVOL high/low/open, per-chain stablecoins/TVL and DOC articles are in the JSON and on the page.

## What the raw data shows (observations, not conclusions)

- **Before the boundary**, BTC rose slowly (84,142 → 84,881) and the macro perps were flat. Funding sat at the
  1.25e-5/h floor and the premium was negative throughout.
- **The GDELT batch published just before the boundary (11:45 UTC) is about 4× the surrounding volume**:
  2,644 events vs 335-794, with conflict 395, escalation 218, corridor escalation 93. Because geo_shock is
  built from *shares* of total volume, it read 60.2, not elevated. The highest-mention conflict rows available
  before the boundary are mixed: an Iran-Israel live blog (Al Jazeera), Gaza, a US-China item, a UK airbase
  incident, and many Australian local-news items. GDELT's conflict filter is broad; the rows are kept raw for
  reading.
- **Between boundary and event** (overnight Sunday → Monday, as the underlying markets reopened): GOLD −2.0 %,
  SILVER −3.4 %, COPPER −1.9 %, XYZ100 −0.8 %, SP500 −0.3 %, BRENTOIL −1.2 %, 10Y +3.4 bp; BTC funding left the
  floor and turned negative; the premium more than doubled in magnitude (−0.000207 → −0.000552).
- **After the event** the move continued in metals (GOLD 4,121 at +12h) and the 10Y yield rose to 5.27 % at +12h.
- Since the 2026-10-08 live run, implied volatility (DVOL), stablecoin supply, DEX volume, TVL and DOC article
  volume/first-seen times are in the data for Event #15 (values above, pre-boundary table in
  `RISK_REGIME_COLLECTION_RUN.md`). Venue open interest/funding (Bybit/Binance) and liquidations are still
  missing.

## Other failure episodes that can now be reconstructed

123 episodes have synchronized timelines on the Event Research page: 15 `research_events` (incl. RE-15), 86
individual prediction failures, 22 consecutive-failure runs. 307 V1-call failures are rows of
`risk_regime_history.json`. All currently carry Hyperliquid and GDELT evidence only.
