# Risk Regime research dataset

"What did the external world look like when V1 made this call?" One row per real V1 observation, plus a
reusable index of real failure episodes. Raw values only: no score, no weights, no improvement measured.

## Files

| File | Content |
|---|---|
| `results/risk_regime_history.json` | the 575 V1 observations with V1 score, V1 call, the 10 V1 sources, the 24h BTC outcome and every research series as available at that timestamp; the failure index |
| `results/risk_regime_episodes.json` | the 123 inspectable episodes (research_events, prediction failures, consecutive-failure runs), each with a synchronized timeline (−24h … +24h) and, for research_events, the top GDELT conflict events |
| `results/risk_regime_event15.json` | the Event #15 timeline at the requested offsets (see `RISK_REGIME_EVENT15.md`) |
| `results/risk_regime_source_inventory.json` | sources, statuses, coverage, quality |
| `results/risk_regime_raw/observations.jsonl.gz` | every raw observation, provider fields included |
| `results/risk_regime_event_research.html` | Event Research page (Advanced / Research tooling; see below) |

## V1 rows (`risk_regime_history.json → rows`)

| Field | Source |
|---|---|
| `ts`, `v1_score` | read-only `history` ∪ `research_sentiment_archive` extract (same 575 timestamps as the GDELT and Hyperliquid studies; column sums checked against D1) |
| `v1_direction` | EXP-005 V1 baseline call, reused: `experiment5_agent._v1_baseline_direction` (score ≥ 50 → UP) |
| `v1_sources` | geopolitics, macrogeo, oil, yield10y, nasdaq, sp500, usd, funding, longshort, hypefunding as stored |
| `btc_outcome_24h` | `outcome_engine.compute_forward_returns_from_history`, **unchanged**, over an in-memory copy of the read-only `btc_data` extract (759 rows) |
| `research` | `{series: [value, age_minutes]}`: the latest observation of each research series whose `available_at` ≤ `ts`; `null` = nothing available |

27 research series are attached today (Hyperliquid 9 instruments × close/volume, Hyperliquid BTC
funding/premium, GDELT 6 batch counts, geo_shock). Blocked sources appear as soon as they are collected.

## Failure / event index (`failure_index`)

Existing definitions only:

| Id | Type | Definition (reused) | Count |
|---|---|---|---|
| `RE-<id>` | LARGE_MOVE, REGIME_REVERSAL, VOLATILITY_EXPANSION, V2_FAILURE_CLUSTER | `research_events` rows, `pr3-v1`, unchanged (|24h| ≥ 4 %; 7-day regime change; 7-day realised vol ≥ 1.5× frozen baseline; ≥ 5 consecutive incorrect predictions) | 6 / 2 / 3 / 4 |
| `PRED-<id>` | PRED_UP_BTC_DOWN / PRED_DOWN_BTC_UP | `predictions` (knn-core-v1, the model `research_events` calls "V2"): p_up > 0.5 vs the resolver's `realized_up`; p_up = 0.5 excluded | 53 / 33 |
| `PREDRUN-<h>-<first id>` | PRED_CONSECUTIVE_FAILURES | consecutive incorrect predictions of one horizon (same metric as V2_FAILURE_CLUSTER; listed from length 2, with the length) | 22 |
| `V1OBS-<ts>` | V1_UP_BTC_DOWN / V1_DOWN_BTC_UP | V1 baseline call vs `outcome_engine` 24h realised direction (FLAT excluded) | 194 / 113 |

**Event #15** is `RE-15` (V2_FAILURE_CLUSTER, BTC_24h, anchored at the event, 2026-09-28 03:01). Its first
failing prediction is `PRED-1125` (2026-09-27 12:01, the prediction boundary).

Every `RE-`, `PRED-` and `PREDRUN-` episode (123) has a synchronized raw timeline; every `V1OBS-` episode
(307) is a row of the V1 dataset. `RE-1` to `RE-3` (20-22 Aug) fall before the GDELT batch series starts
(24 Aug), so their timelines show GDELT counts as not available; their raw GDELT event rows were fetched
(MD5-verified) and are included. Hyperliquid starts 18 Aug, so their −24h points are partly missing.

## No look-ahead

- Each research value carries `available_at`: bar close (+1h for hourly OI/DVOL candles, +1 ms after a
  Hyperliquid candle's close), settlement time (funding), day end (DeFiLlama), batch + 15 min (GDELT), first-seen
  + 15 min (GDELT DOC), retrieval time (live snapshots).
- `get_observations_available_at(ts)` returns only `available_at ≤ ts`, with age.
- Outcomes are computed only for the outcome field and never attached as research values.
- GDELT event rows keep three times: `event_date` (SQLDATE, day only), `first_seen` (publishing batch),
  `retrieved_at`.

## Event Research page

`results/risk_regime_event_research.html` lives with the research tooling and is not part of the production
Worker UI (adding it to the Worker's Research Lab would change `worker.js`, which deploys on merge; that is
left as a human decision). To open it: `cd research/results && python3 -m http.server`, then visit
`/risk_regime_event_research.html`. Select Event #15 or any indexed episode to see the synchronized raw
timeline, source statuses, and GDELT conflict events. It shows no score.

## Reproducing

```
python3 research/risk_regime_reconstruction.py --v1 <v1_full.json> --predictions <predictions.json> \
  --events <research_events.json> --btc <btc_data.json> --hl-cache <asset-universe cache> \
  --hl-funding-cache <leverage-study cache> --gdelt-cache <GDELT export cache> --out-dir research/results --live
```

The four inputs are read-only `SELECT` extracts (sha256 recorded in `risk_regime_history.json`). Without
`--live` the third-party APIs are not called and are reported `NOT_RUN`.
