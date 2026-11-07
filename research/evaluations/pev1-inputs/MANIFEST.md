# PEV-1 pinned inputs

The 2026-11-01 run can be regenerated from these files plus the pre-seal candles on the `research-data/market-moves`
branch.

| File | sha256 | Origin |
|---|---|---|
| `postseal-candles/BTC/observations.jsonl` | `09524705b11d1602953348225b2b49d855f890edd8385e32b26e4f492fd3cc9b` | `prediction_eval.py fetch`, Hyperliquid public `candleSnapshot`, retrieved 2026-11-01 |
| `postseal-candles/ETH/observations.jsonl` | `387b53aac7cd6d2d3e51feb7c49ab6e0842ef3734749d37d27cff993c44c806b` | same |
| `postseal-candles/LINK/observations.jsonl` | `a2f00c193ea95bc5a373b5a5e92428ed4d813061cc6434791519195d3fd1d706` | same |
| `experiment5-sentiment-postseal.json` | `e18b71e1db305c2f247c2454d06f15a7802ebfbd773b725e47fccc3b017605c8` | `/api/research-lab/experiment5-sentiment`, retrieved 2026-11-01. It contains post-seal rows only (206). The 101 sealed-period readings in the original retrieval (sha256 `eca076b3…5c848`) were removed before commit. The harness drops them at load, so the metrics are unaffected. |

Pre-seal candles: `research-data/market-moves`, `market_moves/hourly/<COIN>/observations.jsonl`.
- BTC: `e0141555…501d`
- ETH: `c15ea7f3…6fc9`
- LINK: `9937e4e8…aad2`

The full hashes are in the report's `inputs_sha256`.

## Regenerate

```
git worktree add ../mm-data origin/research-data/market-moves
python3 research/prediction_eval.py run --candles ../mm-data/market_moves/hourly research/evaluations/pev1-inputs/postseal-candles \
  --sentiment research/evaluations/pev1-inputs/experiment5-sentiment-postseal.json --out /tmp/pev1.json
```

Every metric is identical to `2026-11-01-pev1-report.json`; only `generated_at` and the input paths differ (verified
2026-11-01).
