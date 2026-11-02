# research-data/pev1p-candles: post-seal Hyperliquid hourly candles (append-only)

**Data branch only.** No code and no workflows live here. Nothing in production reads it.

**Why it exists.** Hyperliquid's public `candleSnapshot` serves only the most recent ~5,000 hourly candles (measured
2026-11-02: 208.5 days, oldest 2026-04-07T23:00Z). The PEV-1P analog pool starts at the seal end; those candles stop
being servable around 2027-05-05. This archive keeps them, so the frozen PEV-1P analysis and any later re-run or audit
stay reproducible.

**Writer.** `research/candle_archive.py` (PulseWorkerV2 PR #114), tested in `research/test_candle_archive.py`.

## Layout

The layout follows `research-data/market-moves`.

- **`candles_post_seal/<COIN>/observations.jsonl`**
  - First-seen closed candles, one per `open_ts`.
  - Append-only: an accepted line is never changed.
  - Only candles whose close became available after 2026-10-09T21:00Z (the seal end) are stored; nothing sealed and
    nothing still open.
- **`candles_post_seal/<COIN>/revisions.jsonl`:** a later retrieval that differs from the accepted candle. The
  original and the revision are both kept, each with its retrieval time.
- **`candles_post_seal/runs/<run_id>.json`:** one manifest per run, with requested ranges, counts, gaps and the sha256
  of every file after the run.

## Seed (2026-11-02)

- **Contents:** 565 candles per coin (BTC, ETH, LINK), 2026-10-09T21:00Z → 2026-11-02T10:00Z, 0 gaps.
- **Cross-check:** against the independent 2026-11-01 retrieval pinned in PEV-1 (`research/evaluations/pev1-inputs`),
  all 534 overlapping candles per coin are identical, so no revisions so far.

**Raw prices only.** No label, prediction or metric is computed or stored here.
