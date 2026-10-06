# Hyperliquid leverage / market-stress component (Risk Regime Shock, input 2)

Research only. Candidate #1 stays **NEW_SIGNAL / Risk Regime Shock / REGIME_MODIFIER /
DATA_COLLECTION_REQUIRED**. This component is not a V1 source. It has no weight, creates no methodology
version, does not replace `funding`, `longshort` or `hypefunding`, and is not combined with GDELT (input 1,
frozen). CoinGlass is not used (paid). Hyperliquid is one venue and does not describe the whole crypto market.

## Status

**Live data: read on 2026-10-06** (status `OK`; the first attempt, `LIVE_FETCH_FAILED` behind the network
allowlist, is kept in `previous_runs`). Field names, limits and request types below were checked against the
official docs (`hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api`: Info endpoint, Perpetuals,
Rate limits). The committed artifact was regenerated from the response cache filled by that live fetch, so its
request log reads `CACHED`; the input was the same 575 V1 observations (timestamps identical to
`gdelt_risk_regime_shock_v1_scores.json`, pinned-at-50 counts identical to the first run).

## Source access (verified)

- `POST https://api.hyperliquid.xyz/info`, public, no key, €0.
- Rate limit: 1,200 weight/min per IP. `metaAndAssetCtxs` and `candleSnapshot` weigh 20; `fundingHistory`
  20 plus 1 per 20 rows returned; `candleSnapshot` plus 1 per 60 rows. This run used 7 requests.
- Time-range responses return at most 500 rows; paginate from the last timestamp (the runner does).
- `candleSnapshot`: only the **most recent 5,000 candles** (1h ≈ 208 days). Fine for this window, but 1h
  history cannot be backfilled further.
- `fundingHistory` (fields `coin`, `fundingRate`, `premium`, `time`) is hourly and covered the whole window
  (1,174 records, 18 Aug to 6 Oct).
- `metaAndAssetCtxs` returns `openInterest`, `funding`, `premium`, `markPx`, `midPx`, `oraclePx`, `impactPxs`,
  `dayNtlVlm`, `dayBaseVlm`, `prevDayPx` for **the current moment only**. The docs list **no
  open-interest history request** and **no aggregate liquidation request**.

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

## What Hyperliquid could add (classes verified against the docs and the live response)

| Dim. | Raw dimension | Field | Expected class | Already in V1? |
|---|---|---|---|---|
| A | OI level | `metaAndAssetCtxs.openInterest` | AVAILABLE LIVE ONLY (verified: no history request) | No |
| B | OI change | from collected snapshots | DERIVABLE, forward only | No |
| C | OI acceleration | from collected snapshots | DERIVABLE, forward only | No |
| D | Funding level | `fundingHistory.fundingRate` | AVAILABLE HISTORICALLY | **Yes** (all three sources) |
| D2 | Premium level | `fundingHistory.premium` | AVAILABLE HISTORICALLY | Partly: same mechanism, finer than V1's floor-clamped funding |
| E | Funding change | from D | DERIVABLE | No (V1 is point-in-time) |
| F | Funding extreme vs own baseline | from D/D2 | DERIVABLE | No (V1 uses a fixed saturation scale) |
| G | Price change | `candleSnapshot` | AVAILABLE HISTORICALLY (most recent 5,000 candles) | Yes, elsewhere (technical score) |
| H | Volume acceleration | `candleSnapshot.v` | DERIVABLE | No |
| I | OI/price divergence | A with G | DERIVABLE, forward only | No |
| J | Liquidations | none aggregate | NOT AVAILABLE (verified: no aggregate Info request) | No |

## Incremental information test (measured on the 575 V1 observations)

| Measure | Value |
|---|---|
| Coverage (point-in-time features available) | 575 / 575 (100 %), all with a 7-day baseline; 188 at weekends |
| BTC funding sitting at the 0.00125 %/h floor | 72.2 % of observations |
| V1 `funding` = 50 agrees with "Hyperliquid at the floor" | 84.8 % |
| Spearman, Hyperliquid premium vs V1 `funding` | 0.68 |
| Share of observations flagged abnormal (own 7-day 5th-95th pct.) | premium low 7.7 %, premium high 7.8 %, funding low 7.3 %, funding high 2.8 %, 6h return low 5.0 % / high 9.9 %, volume ≥ 2x median 18.6 % |

The premium carries the same information as V1 `funding` (ρ 0.68) and stays readable while funding is clamped
at the floor, which is where V1 is blind.

| V1 dimension | Hyperliquid dimension | Overlap | New information | Verdict |
|---|---|---|---|---|
| funding level (BTC/ETH, 5-asset, HYPE) | D funding level | Same field | None | **Not incremental** |
| funding level | D2 premium, F extreme vs baseline | Same mechanism (positioning pressure) | Resolution while funding sits at the floor | Better implementation, not new |
| funding level | E funding change | Derived from the same field | Dynamics V1 does not keep | Weakly incremental |
| none | A/B/C/I open interest | None | Leverage build-up / unwind | **Genuinely new**, but **no free history** |
| none | H volume acceleration | None (V1 has no volume source) | Activity bursts | New, historical |
| none | J liquidations | n/a | n/a | Not available |

The only clearly new leverage dimension (open interest) is the one with no free historical backfill
available from the Info API (verified in the docs). It would have to be collected going forward, which needs a human-approved
collection plan, outside this read-only build.

## Event #15 (V1 side)

- BTC 84,429 → 83,480 (-1.1 %) in the 24 h to 2026-09-28 03:01 UTC (`btc_data`). Most of the fall came
  after 00:01 UTC (84,441 → 83,480). The GDELT runner's event label "-4.1 %" is wrong; GDELT is frozen,
  so this is noted rather than changed.
- The cluster started with the first failed V1 UP prediction at **2026-09-27 12:01 UTC** (p_up 0.73, at
  the 84,925 high; `predictions.id` 1125, ts 1790510487481). The first version of this note said 15:01; the
  code always used the correct timestamp. Timing classes: PRE-EVENT = flag **on** at 12:01 on 27 Sep, when
  that prediction was issued; CONTEMPORANEOUS = first switched on between 12:01 and 03:01; POST-EVENT = after
  03:01; UNUSABLE = never flagged. An episode that switched on and then cleared before 12:01 does not count as
  PRE-EVENT: it could not have warned that prediction. Every episode is listed in the artifact.
- V1 funding / longshort / hypefunding: 50/50/50 at 18:59, 49/49/50 at 20:19, 48/49/50 at 20:41 (a
  contemporaneous drift), then 42/46/50 at 04:23 after the event (post-event).

### Event #15, Hyperliquid side (live data, hourly from -48 h to +24 h)

| Flag | Episodes (UTC) | Class |
|---|---|---|
| premium low / funding low | 26 Sep 20:01-22:01 (3 h, cleared 14 h before the boundary); 28 Sep 02:01-06:01 (5 h); 28 Sep 22:01-23:01 | **CONTEMPORANEOUS** |
| 6h return low | 28 Sep 04:01, 06:01 | POST-EVENT |
| 6h return high | 28 Sep 17:01-18:01 (rebound) | POST-EVENT |
| volume ≥ 2x median, premium high, funding high | none | UNUSABLE |

At the boundary (27 Sep 12:01) every flag was off: funding at the floor, premium -2.1 bp (44th percentile of
its 7 days), volume 0.62x median. Premium fell below its 5th percentile only at 02:01 on 28 Sep, one hour
before the event window closed, as the price was already falling. **Hyperliquid gave no pre-event warning for
Event #15.** The 26 Sep episode is the kind of isolated reading that occurs on ~7.7 % of observations.

## Recommendation

**Historical Hyperliquid dimensions (funding, premium, price, volume): do not pursue as a Risk Regime Shock
input.** They gave no pre-event signal for Event #15 (n = 1). The premium is a better-resolved version of
what V1 `funding` already measures, not new information. Improving how V1 reads funding at the floor is a
separate V1 methodology question; it is not proposed here.

**Open interest: DATA_COLLECTION_REQUIRED.** It is the only genuinely new leverage dimension, and the docs
confirm it has no free history. Testing it means collecting `metaAndAssetCtxs` snapshots going forward. That
needs a human-approved collection plan and is not started by this research.

Candidate #1 stays **NEW_SIGNAL / Risk Regime Shock / REGIME_MODIFIER / DATA_COLLECTION_REQUIRED**.

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

- 1h candles only reach back 5,000 hours; longer backtests would need daily candles.
- One venue; BTC only for the event test.
- Open-interest history cannot be backfilled for free (no Info request exists); no third-party archive is used.
- No causality is claimed; n = 1 event of interest.
