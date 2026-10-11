# Market-move detection results (plan v1.0.0)

`development_report.json` — development period only (scored 2026-04-24 to 2026-07-22 UTC), produced by

    python3 research/market_moves/evaluate.py --store <research-data/market-moves @ 0eb5be0>/market_moves \
      --period development --out research/market_moves/results/development_report.json

Only observations with `available_at < 2026-07-22T00:00:00Z` were loaded. Output sha256
`477a2588b5b86dc0c84038b2bceb8cdf88bec9611fa05791175eccd974701c68`; re-running reproduces it byte for byte.

The evaluation period (2026-07-22 to 2026-10-09) is sealed: the evaluator refuses to compute it without
`--authorize-evaluation <plan sha256>` in an explicit, separately authorised run. Per the plan's claims rule, no
detector is called better from the development period alone. B0 (production data replay) is NOT_RUN: it needs a
read-only extract of production `btc_data`.
