# Hyperliquid asset-universe audit: zero-cost cross-asset data (Candidate #1, Risk Regime Shock)

Research only. Candidate #1 stays **NEW_SIGNAL / Risk Regime Shock / REGIME_MODIFIER /
DATA_COLLECTION_REQUIRED**. Nothing here is a V1 source: no weight, no methodology version, no Risk Regime
Shock score. GDELT (input 1) stays frozen. Hyperliquid BTC funding/premium/volume stay rejected
(`HYPERLIQUID_LEVERAGE_STRESS.md`).

Files: `hyperliquid_asset_universe.py` (pure functions), `hyperliquid_asset_universe_run.py` (runner),
`test_hyperliquid_asset_universe.py` (23 tests, FIXTURE payloads only), artifact
`results/hyperliquid_asset_universe.json` (live run of 2026-10-06; 330 requests, all to the public Info API).

## 1. Inventory (discovered from the API, not from a list)

Discovery: `perpDexs` → `metaAndAssetCtxs` per dex (native + each HIP-3 dex) → `perpConciseAnnotations` →
`perpAnnotation` per candidate → `spotMetaAndAssetCtxs`.

| Dex | Deployer | Assets | Live |
|---|---|---|---|
| native | validators | 234 | 178 (crypto only) |
| `xyz` (XYZ) | 0x8880…0888 | 131 | 112 |
| `para` (Paragon) | 0x8888…6ed3 | 38 | 29 |
| `mkts` (Markets by Kinetiq) | 0x71f0…29ec | 24 | 5 |
| `io` (EntropyIO) | 0x320c…ac6 | 11 | 9 |
| `flx`, `vntl`, `hyna`, `km`, `abcd`, `cash` | various | 97 | **0 (all delisted)** |

Live HIP-3 perps by the deployers' own category: 122 single stocks, 7 indices, 9 commodities, 3 FX, 3 rates,
5 crypto aggregates/tokens, 3 pre-IPO, 2 unannotated. Delisted macro markets include `xyz:DXY`, `xyz:VIX`,
`xyz:VOL`, `xyz:WHEAT`, `xyz:CORN`, `xyz:KRW`, `xyz:ALUMINIUM` and every gold/oil/index market on `flx`,
`km`, `vntl`, `cash`, `abcd`. **Markets disappear: continuity is a real risk.**

### Relevant live instruments (all verified in the API; description = deployer's `perpAnnotation`)

| Category | Symbol | Underlying per deployer | 1st candle | Day vol. ($) |
|---|---|---|---|---|
| GOLD | `xyz:GOLD` | USD spot price of 1 oz gold (XAU/USD) | 2025-12-22 | 51 M |
| GOLD | `PAXG` (native) | perp on the PAXG token, validator oracle | 2025-03-27 | 1.9 M |
| OIL / ENERGY | `xyz:BRENTOIL` | 1 bbl Brent crude | 2026-03-04 | 174 M |
| OIL / ENERGY | `xyz:CL` | 1 bbl WTI (display name WTIOIL) | 2026-01-06 | yes |
| OIL / ENERGY | `xyz:NATGAS` | 1 MMBtu Henry Hub | 2026-01-21 | yes |
| OIL / ENERGY | `xyz:HO` | 1 gal NY Harbor ULSD (diesel) | 2026-10-01 | too new |
| OIL / ENERGY | `xyz:XLE` | 1 share Energy Select SPDR ETF | 2026-05-04 | yes |
| EQUITY INDEX | `xyz:SP500` | S&P 500 Index | 2026-03-18 | 235 M |
| EQUITY INDEX | `xyz:XYZ100` | Nasdaq-100-style index (100 largest non-financials) | 2025-10-13 | yes |
| EQUITY INDEX | `mkts:US500`, `mkts:USTECH`, `mkts:SMALL2000` | "exposure to" S&P 500 / Nasdaq-100 / Russell-2000-style; level is ~1/10 of the index, oracle docs not in the API | 2026-07-01 / 07-01 / 08-26 | 1.1 M / 1.9 M / 0.1 M |
| EQUITY INDEX | `xyz:JP225`, `xyz:KR200` | native JPY / KRW index level | 2026-04-09 | yes |
| EQUITY INDEX | `xyz:EWJ/EWY/EWZ/EWT/SMH/MAGS/SOXL/KORU` | 1 ETF share (country / sector; SOXL, KORU 3x leveraged) | 2026-03 to 08 | yes |
| FX / USD | `xyz:EUR`, `xyz:GBP`, `xyz:JPY` | EUR/USD, GBP/USD, USD/JPY spot | 2025-12-23, 2026-05-18, 2025-12-23 | 2.5 M (EUR) |
| RATES | `para:10Y`, `para:2Y`, `para:30Y` | on-the-run UST yield, in percent | 2026-08-12, 09-24, 09-24 | 0.24 M / 0.11 M / small |
| RATES | `xyz:TLT`, `mkts:USBOND` | 1 TLT share / 20y+ Treasuries exposure | 2026-09-17, 08-26 | small |
| COMMODITY | `xyz:SILVER`, `COPPER`, `PLATINUM`, `PALLADIUM`, `URNM` | XAG, Cu lb, XPT, XPD; uranium-miners ETF | 2025-12 to 2026-03 | 152 M (silver) |
| UNKNOWN | `mkts:BVIV` | **no annotation**; not assumed to be a volatility index | 2026-09-21 | 0.24 M |
| UNKNOWN | `xyz:STRC` | Strategy's STRC preferred share (not a rate) | 2026-06-22 | — |
| CRYPTO | native `SPX` | **SPX6900 memecoin at $0.41, not the S&P 500** | — | — |

No live VIX/volatility instrument, no DXY, no SOFR / rate futures, no Brent-WTI spread instrument.

## 2. What each instrument actually is (section 10)

| Representation | Instruments | Evidence |
|---|---|---|
| REAL UNDERLYING EXPOSURE | **none** | Hyperliquid holds no gold, oil, shares, bonds or currency. |
| PERPETUAL on a TOKENIZED ASSET | native `PAXG` | Native perps use the validator oracle: a weighted median of crypto-exchange spot prices of the PAXG token (Oracle docs). |
| BUILDER/ORACLE-BASED SYNTHETIC EXPOSURE | every `xyz:`, `para:`, `mkts:`, `io:` market | HIP-3 docs: the deployer defines the market and **sets the oracle price**. The API exposes only the deployer's free-text description, no feed or source. |
| TOKENIZED ASSET (spot) | `QQQ`, `GLD`, `SPY`, `SLV`, `XAUT0`, `SPYX`, `QQQX`, `USPYX`, `XAUM`, `THBILL`, ... | **Names are not evidence.** `XAUT0/USDC` trades at 0.37 (gold perp 4,159), `QQQ/USDC` at 1,354 (Nasdaq-100 perp ~760), `SPY/USDC` at 83.5, `USPYX` at 0.99: none is consistent with the named underlying (GLD, SLV not checkable 1:1). Most have zero volume. Excluded. |

The HIP-3 macro perps trade 24/7, and their hourly candles are **never flat at weekends** (`xyz:GOLD`,
`xyz:SP500`, `xyz:BRENTOIL`: 0 % flat weekend hours), while the underlying markets are shut. Weekend prices are
therefore Hyperliquid traders' own price discovery, not the underlying market. That is different
information, but it is not "the S&P 500 at the weekend".

## 3. Zero-cost test

| Question | Answer (all candidates) |
|---|---|
| Public access / auth / API key / subscription | Yes / none / none / none. **€0 = YES** |
| Endpoint | `POST https://api.hyperliquid.xyz/info` |
| Live data | `metaAndAssetCtxs` (dex-scoped): mark, oracle, mid, funding, premium, **open interest**, day volume |
| Historical candles | `candleSnapshot`, intervals 1m to 1M, **most recent 5,000 candles per interval** (1h ≈ 208 days), 500 rows/response |
| Historical funding | `fundingHistory` works for HIP-3 coins (e.g. `xyz:GOLD`), hourly |
| Open-interest history | **None** (no documented request); live only, same as BTC |
| Volume | per candle (`v`, `n`) and 24h notional |
| Rate limit | 1,200 weight/min per IP; info requests 20 (+1 per 60 candles / 20 funding rows) |
| Timestamps | candle `t` open / `T` close in ms UTC; used only after `T` (no look-ahead) |
| Undocumented | `perpConciseAnnotations`, `perpAnnotation` (deployer descriptions) are used but not in the official docs |

Daily candles reach each market's listing date; 1h candles for this window's start (2026-08-18) **roll off
about 2027-03-14**. Any follow-up research on hourly data must snapshot it before then.

## 4. V1 already uses Hyperliquid HIP-3 data (inspected `CryptoPulse/index.html`)

| V1 source | What it measures | Distinct values over 575 obs. |
|---|---|---|
| `gold` | **`xyz:GOLD`** 24h % (markPx vs prevDayPx), sign flipped in a "competing-haven" regime | 85 |
| `oil` | **`xyz:BRENTOIL`** 24h %, inverted | 96 |
| `usd` | **`xyz:EUR`** 24h % (dollar strength) | 41 |
| `sp500`, `nasdaq` | FRED daily close vs previous close | **22, 25** |
| `yield10y` | FRED DGS10 daily, inverted | **20** |
| `ninemag`, `strc`, `global` | Yahoo 9-stock basket; Yahoo STRC vs par; CoinGecko market cap | 60, 6, 68 |

`sp500`, `nasdaq` and `yield10y` change once per US trading day and are frozen overnight and at weekends.
That, not the asset list, is where Hyperliquid differs. Side finding (not changed): V1's one-off
`usd` backfill used `xyz:DXY` daily closes, which V1's own comments call a dead market (now delisted).

### Overlap

| Hyperliquid | V1 | Overlap | Genuinely new |
|---|---|---|---|
| `xyz:GOLD`, `xyz:BRENTOIL`, `xyz:EUR` | gold, oil, usd | **Same instruments** | Only hourly resolution and own-baseline abnormality |
| `PAXG`, `xyz:CL`, `xyz:GBP`, `xyz:XLE` | gold, oil, usd | Same factor | Little |
| `xyz:SP500`, `xyz:XYZ100`, `mkts:US500/USTECH` | sp500, nasdaq (FRED daily) | Same index | **Hourly, 24/7 price; abnormality vs own 7-day baseline** |
| `para:10Y` | yield10y (FRED daily) | Same yield | **Hourly, 24/7**, but thin market |
| `para:2Y`, `para:30Y`, `xyz:TLT`, `mkts:USBOND` | yield10y | Partial | Curve shape; too new (≤ 6 weeks) |
| `xyz:SILVER`, `COPPER`, `PLATINUM`, `PALLADIUM` | none | None | **New assets** |
| `xyz:JPY` | usd (via EUR) | Partial | Yen safe-haven leg |
| `xyz:NATGAS`, `JP225`, `KR200`, country ETFs | none | None | New, weak link to BTC (below) |
| Open interest on any macro perp | none | None | New, but **live only** |

## 5. 575-observation coverage (same timestamps as the GDELT and BTC runs)

Coverage is **100 %** with a full 7-day baseline at all 575 observations (188 at weekends) for: GOLD, PAXG,
SILVER, COPPER, PLATINUM, PALLADIUM, URNM, BRENTOIL, CL, NATGAS, XLE, EUR, JPY, GBP, SP500, XYZ100, US500,
USTECH, JP225, KR200, EWJ/EWY/EWZ/EWT, SMH, SOXL, KORU, MAGS, `para:10Y`. Partial: SMALL2000 and USBOND 100 %
coverage but 492 with baseline (listed 26 Aug); XBI 98 %; TLT 50 %; BVIV 39 %; 2Y/30Y 32 %; HO 14 %.

Thin markets: `para:10Y` has flat hourly candles 39 % of weekday hours and 63 % of weekend hours ($0.24 M/day);
`mkts:BVIV` 62 %/64 %; `para:2Y` 45 %/86 %. The liquid `xyz` markets have none.

## 6. Cross-asset relationships the free data supports (descriptive, not a score)

Over the 575 observations: Spearman of 24h returns vs BTC, and counts of BTC's 6h return below its own 7-day
5th percentile (29 observations) **together with** the asset abnormal in the stated direction (expected count
if independent in brackets; distinct days last).

| Relationship | Instrument | ρ (24h) | Joint obs. (indep.) | Days |
|---|---|---|---|---|
| BTC down + equity down | `xyz:SP500` | +0.56 | 14 (2.1) | 5 |
| | `xyz:XYZ100` / `mkts:USTECH` | +0.63 | 11 (2.0) / 12 (2.1) | 4 / 4 |
| BTC down + 10Y yield up | `para:10Y` | −0.45 | 14 (2.4) | 5 |
| BTC down + gold **down** | `xyz:GOLD` | +0.41 | 8 (1.2) | 4 |
| BTC down + gold up | `xyz:GOLD` | | 0 (1.5) | 0 |
| BTC down + silver / copper / platinum down | metals | +0.31 to +0.41 | 7 / 9 / 13 | 3 / 4 / 4 |
| BTC down + oil up | `xyz:BRENTOIL` | −0.23 | 9 (2.3) | 3 |
| BTC down + USD up (EUR down / USDJPY up) | `xyz:EUR` / `xyz:JPY` | +0.05 / +0.03 | 7 (1.6) / 6 (1.3) | 5 / 3 |
| BTC down + natgas | `xyz:NATGAS` | −0.03 | 5 / 4 | 3 / 1 |
| Equity down + volatility up | none | — | **not possible**: no live volatility market (VIX/VOL delisted, BVIV unannotated and thin) | |

Technically possible with free data: BTC × equity, BTC × rates, BTC × metals, BTC × oil, BTC × USD,
simultaneous abnormal moves across a basket (breadth), acceleration and persistence (hourly), weekend
continuity. Not possible: volatility, OI history, real-asset flows.

In this window **BTC sold off together with equities, metals and gold, while yields and oil rose**: a
broad risk-off/rates signature, not a gold-haven one. Every count above is far above chance, but rests on
**4-5 separate days**; observations within a day are not independent.

## 7. Event #15 (boundary Sun 2026-09-27 12:01 UTC, event Mon 03:01 UTC)

| Instrument | 24h change to boundary | 6h pct. at boundary | Boundary → event | First abnormal | Class |
|---|---|---|---|---|---|
| BTC | | | −1.78 % | 6h-low at 04:01 Mon | POST-EVENT |
| `xyz:GOLD` | −0.02 % | 63 | −2.01 % | 01:01 Mon, 9 h | CONTEMPORANEOUS |
| `xyz:SILVER` | −0.11 % | 70 | −3.37 % | 01:01 Mon, 8 h | CONTEMPORANEOUS |
| `xyz:COPPER` | 0.00 % | 46 | −1.85 % | 01:01 Mon, 4 h | CONTEMPORANEOUS |
| `xyz:NATGAS` | +0.37 % | 51 | −3.60 % | 23:01 Sun, 5 h | CONTEMPORANEOUS |
| `xyz:XYZ100` | +0.17 % | 46 | −0.79 % | 02:01 Mon, 3 h | CONTEMPORANEOUS |
| `xyz:SMH` | +0.27 % | 59 | −2.03 % | 01:01 Mon, 6 h | CONTEMPORANEOUS |
| `xyz:SP500` | +0.09 % | 62 | −0.30 % | 15:01 Mon | POST-EVENT |
| `xyz:EUR` | −0.05 % | 33 | −0.04 % | 6h-high 17:01 Sun (1 h), low 23:01 Sun | CONTEMPORANEOUS |
| `para:10Y` | −0.10 % | 44 | +0.66 % | 21:01 Mon | POST-EVENT |
| `xyz:BRENTOIL` | +0.47 % | 47 | −1.15 % | 09:01 Mon | POST-EVENT |
| `xyz:JPY` | −0.04 % | 27 | +0.22 % | 04:01 Mon | POST-EVENT |

**No instrument was abnormal at the boundary: no PRE-EVENT signal.** The boundary fell on a Sunday, when
Hyperliquid's macro perps were trading on their own. The cross-asset move started at the Sunday-evening
reopening of the underlying markets (23:01-01:01 UTC) and coincided with BTC's own fall (mostly after 00:01).
Abnormal = outside the asset's own trailing 7-day 5th-95th percentile of 6h returns, the same disclosed
convention as the BTC component, not tuned to this event. n = 1.

## 8. Ranked opportunities

| # | Candidate | Instrument | €0 | Historical | V1 overlap | New information | Event #15 | Priority |
|---|---|---|---|---|---|---|---|---|
| 1 | BTC × US equity index | `xyz:SP500` (`XYZ100` as check) | YES | 1h since Mar 2026 | sp500/nasdaq (FRED daily) | Hourly 24/7 joint move; V1 is frozen off-hours | Contemp. (XYZ100) / post (SP500) | **HIGH** |
| 2 | Cross-asset breadth (simultaneous abnormal moves) | fixed basket of liquid `xyz` markets + `para:10Y` | YES | 1h, 100 % coverage | V1 sums independent scores; only gold looks at another asset | Joint/broad stress, not any one asset | All moved together at the reopen (contemp.) | **HIGH** |
| 3 | BTC × US 10Y yield | `para:10Y` | YES | 1h since 12 Aug 2026 | yield10y (FRED daily) | Hourly 24/7; strongest inverse link (ρ −0.45) | Post-event | **MEDIUM** (thin market) |
| 4 | Metals beyond gold | SILVER, COPPER, PLATINUM | YES | 1h since Dec 2025-Jan 2026 | none | New assets | Contemp. | MEDIUM (inside #2) |
| 5 | USD (yen leg) | `xyz:JPY` | YES | yes | usd via EUR | Partial | Post | LOW |
| 6 | Gold / Brent / EUR hourly | same as V1 | YES | yes | **identical instruments** | Resolution only | Contemp. / post | LOW |
| 7 | WTI, NATGAS, JP225, KR200, country ETFs | various | YES | yes | oil / none | Weak link to BTC | mixed | LOW |
| 8 | Macro-perp open interest | any | YES live | **no history** | none | New | — | Needs prospective collection |
| 9 | Rates curve (2Y/30Y), TLT, USBOND | `para`, `xyz`, `mkts` | YES | ≤ 6 weeks | yield10y | Curve shape | — | Wait for history |
| 10 | Volatility | none live | — | — | — | — | — | **Not available** |
| 11 | Spot "tokenized" RWAs | QQQ, GLD, SPY, XAUT0, ... | YES | — | — | Prices inconsistent with names | — | **Reject** |

## Limitations

- HIP-3 oracles are deployer-set; the API carries only the deployer's description. Weekend prices are
  Hyperliquid-internal. A deployer can delist (whole dexes already have).
- 1h history is a rolling 5,000 candles; the window's start rolls off ~2027-03-14.
- One event (n = 1), ~29 abnormal BTC-down observations on ~5 days: enough to show co-movement exists, not
  to show it is useful or predictive. Nothing here is causal.
- `perpAnnotation` / `perpConciseAnnotations` are undocumented and may change.
- `mkts:*` levels are not index levels (deployer docs not reachable through the API); returns are unaffected.

## Final decision

**A. APPROVE CROSS-ASSET RESEARCH**, research only, with these first three free dimensions:

1. **BTC × US equity index**: joint abnormal 6h move of BTC and `xyz:SP500` (`xyz:XYZ100` as a robustness
   check), hourly and point-in-time.
2. **Cross-asset breadth**: how many of a fixed, pre-declared basket of liquid instruments (`xyz:SP500`,
   `xyz:GOLD`, `xyz:SILVER`, `xyz:COPPER`, `xyz:BRENTOIL`, `xyz:EUR`, `para:10Y`) are simultaneously abnormal,
   and in which direction.
3. **BTC × US 10Y yield**: opposite-direction joint move with `para:10Y`, gated on the market having actually
   traded (flat-candle share), because it is thin.

Before that research: snapshot the hourly candles of these instruments (read-only, research artifact) so the
575-observation window survives the 5,000-candle limit. Prospective OI collection is not needed for these
three and stays a separate decision. No V1 weights, no Risk Regime Shock score, Candidate #1 not activated.
