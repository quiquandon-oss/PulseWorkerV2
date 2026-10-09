# Market-moves research data (research-data/market-moves)

Research only. This branch holds data, no code and no workflows. Nothing here is read by production.

## What it is
Hourly candles for BTC, ETH and LINK from Hyperliquid's free `candleSnapshot` endpoint (perpetuals), collected by
`research/market_moves/collect.py` on branch `claude/epic-planck-uyapsw-market-moves`.

**Retrospective reference.** The candles were retrieved after the fact. They describe what the market did. They are
not a record of what CryptoPulse knew at the time.

## Layout
- `market_moves/hourly/<ASSET>/observations.jsonl`
  - First-seen accepted candles, one JSON object per line, sorted by `open_ts`.
  - Append-only: an accepted line is never changed.
- `market_moves/hourly/<ASSET>/revisions.jsonl`
  - A later retrieval whose candle differs from the accepted one.
  - Both the original (`accepted_*`) and the revised candle are kept, each with its retrieval time.
- `market_moves/runs/<run_id>.json`: one manifest per collection run. It holds the requests, retrieval times, counts,
  excluded forming candles and file sha256s.
- `market_moves/quality_report.json`: coverage and quality counts only, with no returns and no performance.

## Record fields
| Field | Meaning |
|---|---|
| `record_id` | `<ASSET>:1h:<open_ts>`, a stable identifier |
| `open_ts`, `close_ts` | Candle start and end in UTC epoch milliseconds; `close_ts` = `open_ts` + 1 h − 1 ms |
| `available_at` | `close_ts` + 1 ms. A close price is not known before this time |
| `o`, `h`, `l`, `c` | Provider decimal strings, never converted to float in storage |
| `v` | Traded volume of the Hyperliquid perpetual during the candle, in base-asset units (e.g. BTC). Venue-specific |
| `n` | Number of trades |
| `retrieved_at` | When the response was received |
| `raw` | The provider's candle object as received |
| `raw_sha256` | sha256 of the canonical JSON of `raw` |
| `market`, `endpoint`, `request`, `collector_version` | Provenance |

## Rules
- **Forming candles:** a candle whose `close_ts` is not before the retrieval time is still forming. It is excluded and
  counted in the run manifest, never stored.
- **Revisions:** a re-collection that returns a different candle is recorded in `revisions.jsonl`. The accepted
  candle is never overwritten. Downstream research uses the first-seen candle.
- **No gap filling:** missing hours stay missing, with no interpolation.
