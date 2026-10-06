# Hyperliquid leverage / market-stress component (Risk Regime Shock, input 2)

Research only. Candidate #1 stays **NEW_SIGNAL / Risk Regime Shock / REGIME_MODIFIER /
DATA_COLLECTION_REQUIRED**. This component is not a V1 source. It has no weight, creates no methodology
version, does not replace `funding`, `longshort` or `hypefunding`, and is not combined with GDELT (input 1,
frozen). CoinGlass is not used (paid). Hyperliquid is one venue and does not describe the whole crypto market.

## Status

**Live data: not read yet.** From this research container, `api.hyperliquid.xyz` and
`hyperliquid.gitbook.io` (the official docs) are both refused by the environment's network allowlist
(HTTP 403 at the proxy). The runner records this as `LIVE_FETCH_FAILED` and claims no Hyperliquid
result. Everything below marked *expected* has not yet been checked against the official Info-endpoint page.

## What V1 already knows (inspected, not assumed)

V1 computes its sources in the browser (`CryptoPulse/index.html`). One `metaAndAssetCtxs` call feeds
three Hyperliquid sources, and all three use **only the current funding rate**:

| V1 source | Weight / conf. | Input | Transformation |
|---|---|---|---|
| `funding` | 15 / 1.0 | BTC + ETH `funding` (current) | minus 0.00125 %/h floor, x8 (8h-equivalent), `to0_100(tanh(x*100/0.05))` |
| `longshort` | 10 / 0.5 | average `funding` of BTC, ETH, HYPE, SOL, LINK | same as above |
| `hypefunding` | 4 / 0.4 | HYPE `funding` | minus floor, annualised %, `to0_100(tanh(x/50))` |

- **Stored:** only the 0-100 scores, in `history.sources_json` / `research_sentiment_archive`. The raw
  rate, open interest, premium, volume, mark and oracle prices are **not stored** by V1.
- **Open interest** is shown in the Sentiment tab (`loadHypeFunding`) but never scored or stored.
- The Worker uses Hyperliquid `candleSnapshot` for BTC/ETH/LINK prices and stores LINK funding
  (`link_data.funding_adj`). No open-interest history exists anywhere locally.
- Across the 575 V1 observations the funding sources are mostly **pinned at exactly 50**: `funding`
  67.7 %, `hypefunding` 74.8 %, `longshort` 36.1 %. Hyperliquid funding normally sits at its fixed
  interest floor, which V1 subtracts. At the floor V1 sees nothing, even when the underlying premium moves.

## What Hyperliquid could add (expected, to verify live)

| Dim. | Raw dimension | Field | Expected class | Already in V1? |
|---|---|---|---|---|
| A | OI level | `metaAndAssetCtxs.openInterest` | AVAILABLE LIVE (no history request known) | No |
| B | OI change | from collected snapshots | DERIVABLE, forward only | No |
| C | OI acceleration | from collected snapshots | DERIVABLE, forward only | No |
| D | Funding level | `fundingHistory.fundingRate` | AVAILABLE HISTORICALLY | **Yes** (all three sources) |
| D2 | Premium level | `fundingHistory.premium` | AVAILABLE HISTORICALLY | Partly: same mechanism, finer than V1's floor-clamped funding |
| E | Funding change | from D | DERIVABLE | No (V1 is point-in-time) |
| F | Funding extreme vs own baseline | from D/D2 | DERIVABLE | No (V1 uses a fixed saturation scale) |
| G | Price change | `candleSnapshot` | AVAILABLE HISTORICALLY (recent candles only) | Yes, elsewhere (technical score) |
| H | Volume acceleration | `candleSnapshot.v` | DERIVABLE | No |
| I | OI/price divergence | A with G | DERIVABLE, forward only | No |
| J | Liquidations | none aggregate | NOT AVAILABLE (no aggregate Info request known) | No |

## Incremental information test (preliminary, from V1 data only)

| V1 dimension | Hyperliquid dimension | Overlap | New information | Verdict |
|---|---|---|---|---|
| funding level (BTC/ETH, 5-asset, HYPE) | D funding level | Same field | None | **Not incremental** |
| funding level | D2 premium, F extreme vs baseline | Same mechanism (positioning pressure) | Resolution while funding sits at the floor | Better implementation, not new |
| funding level | E funding change | Derived from the same field | Dynamics V1 does not keep | Weakly incremental |
| none | A/B/C/I open interest | None | Leverage build-up / unwind | **Genuinely new**, but **no free history** |
| none | H volume acceleration | None (V1 has no volume source) | Activity bursts | New, historical |
| none | J liquidations | n/a | n/a | Not available |

The only clearly new leverage dimension (open interest) is the one with no free historical backfill
expected from the Info API. It would have to be collected going forward, which needs a human-approved
collection plan, outside this read-only build.

## Event #15 (V1 side)

- BTC 84,429 → 83,480 (-1.1 %) in the 24 h to 2026-09-28 03:01 UTC (`btc_data`). Most of the fall came
  after 00:01 UTC (84,441 → 83,480). The GDELT runner's event label "-4.1 %" is wrong; GDELT is frozen,
  so this is noted rather than changed.
- The cluster started with the first failed V1 UP prediction at **2026-09-27 15:01 UTC** (p_up 0.73, at
  the 84,925 high). Timing classes: PRE-EVENT = usable before 15:01 on 27 Sep; CONTEMPORANEOUS = 15:01 to
  03:01; POST-EVENT = after 03:01; UNUSABLE = no point-in-time value.
- V1 funding / longshort / hypefunding: 50/50/50 at 18:59, 49/49/50 at 20:19, 48/49/50 at 20:41 (a
  contemporaneous drift), then 42/46/50 at 04:23 after the event (post-event).

## Running it

```
python3 research/hyperliquid_research_run.py --v1 <read-only V1 extract.json> --cache <dir> \
  --out research/results/hyperliquid_leverage_stress.json
```

The V1 extract is the same read-only `SELECT` as the GDELT run (575 observations), plus the stored
`funding`, `longshort` and `hypefunding` scores. It is not committed. The runner reads `fundingHistory`
(paginated), 1h `candleSnapshot`, and one `metaAndAssetCtxs` snapshot (to confirm which live fields exist).
It then scores Event #15 hourly from -48 h to +24 h and every V1 observation point-in-time. Responses
are cached, so a rerun makes no network calls. "Abnormal" means outside the dimension's own trailing
7-day 5th-95th percentile (volume: at least 2x the 7-day median). This is a disclosed reporting
convention, not a fitted threshold.

## Limitations

- Not yet verified against the official docs or the live API (network block).
- One venue; BTC only for the event test.
- Open-interest history is not expected to be backfillable for free; no third-party archive is used.
- No causality is claimed; n = 1 event of interest.
