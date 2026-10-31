# EXP-008: Amazon Chronos-2 BTC challenger (research only)

**Question:** can a pretrained time-series foundation model (Amazon Chronos-2, Apache-2.0) forecast BTC
direction at 12h and 24h from price history alone? Is it better than TimesFM (EXP-004), production
k-NN, always-UP and last-tick persistence?

**Status:** built and tested; collection starts once this folder and `.github/workflows/exp008-chronos.yml`
are on `main`. The registry entry (`EXP-008`, PROPOSED, in production D1) is not changed by this code. It
needs the owner's approval and a separate registry update. Collection does not imply success: the
evaluation plan in `EVALUATION_PLAN.md` is **PROPOSED** until the owner freezes it.

## How a forecast is made

1. **Origin:** the newest EXP-004 TimesFM forecast's `input_end_ts`, per horizon, read from
   `experiment_4_timesfm`. Chronos-2 never picks its own origin, so every forecast pairs with a TimesFM
   forecast.
   - If TimesFM did not run, there is no new origin and nothing is forecast.
   - Origins older than 48 h are not backfilled.
2. **Inputs:** `btc_data` up to and including the origin (`ts <= input_end_ts`, so no look-ahead).
   - Deduplication, the 128-point context and the cadence rule are copied unchanged from EXP-004, and a
     test checks they behave identically.
   - The run **refuses** to forecast if its last context point differs from TimesFM's
     `price_at_prediction`.
3. **Model:** `amazon/chronos-2` on CPU via `chronos-forecasting==2.3.2`.
   - The point forecast is the median of the last forecast step; q10 and q90 are kept.
   - Direction is UP if the median return is above 0, the same threshold as EXP-004.
4. **Resolution:** the first `btc_data` price at or after `target_ts = input_end_ts + horizon`, compared
   with `price_at_prediction`, exactly as EXP-004 does. So both challengers share ground truth.
5. **Persistence:** each forecast records the last tick's return and direction. The resolution records
   whether "same direction as the last tick" was right. This rule is **provisional**: the registry names
   "last-tick persistence" but never defined it.

## Storage

Branch `research-data/exp008-chronos`, folder `exp008_chronos/`. Every file is written once and never
rewritten.

| Path | Content |
|---|---|
| `forecasts/BTC/{12,24}h/<origin>.json` | forecast, quantiles, model id and revision, runtime versions, context hash, anchor |
| `resolutions/BTC/{12,24}h/<origin>.json` | actual price and return, direction, correct, persistence_correct, errors |
| `runs/<time>.json` | per-run record: resolved, pending, forecasts made, notes, errors |

**Duplicate prevention:** an origin that already has a forecast file is skipped, and the model isn't loaded.

**Failure handling:** a model or forecast failure writes no forecast, records the error and fails the job.
A resolution with no price yet stays pending.

## Resources

- **GitHub Actions:** one CPU job after each TimesFM run, plus a 21:40 UTC fallback. About 5–10 min,
  dominated by installing torch; the model weights are cached.
- **D1 reads per run:**
  - one `btc_data` read up to the origin, about 2.7k rows;
  - 2 rows from `experiment_4_timesfm`;
  - one row per due resolution.
- **D1 writes:** none.

## Evaluate

```bash
curl -s "https://pulseworker-v2.quiquandon.workers.dev/research/timesfm-recent?horizon=12&limit=N" > tfm12.json
curl -s "https://pulseworker-v2.quiquandon.workers.dev/research/timesfm-recent?horizon=24&limit=N" > tfm24.json
python3 exp008-chronos/evaluate.py --store <data checkout>/exp008_chronos \
  --timesfm tfm12.json tfm24.json --knn-csv data-exports/btc_predictions.csv
```

- Choose N so the oldest returned TimesFM row is after 2026-10-09T21:00Z (the sealed period's end).
- The output is descriptive (counts, hit rates, Wilson intervals, exact McNemar tests) until the plan is
  frozen.

## Tests

```bash
python3 -m pytest -q exp008-chronos/
```

The suite runs in CI, in `test.yml` and again in the workflow before each run. It covers:
- anchoring and no look-ahead;
- duplicate and overwrite prevention;
- refusal of unpaired forecasts;
- stale and missing anchors;
- model-load retry and failure;
- resolution;
- evaluation pairing;
- the real Chronos-2 pipeline code on a tiny randomly initialised model. The real weights are only
  downloaded in Actions.
