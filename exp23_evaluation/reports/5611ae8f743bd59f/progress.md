# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `5611ae8f743bd59f072782ba2057e5fefc7123c038eafc0c368c195e2396751d`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 6/60 events, UP 1/25, DOWN 5/25)
- Post-seal price hours: 651; eligible decision hours: 651
- Labelled hours: 626 (VALID 626); burn-in complete: True; episodes: 6 (UP 1, DOWN 5); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 651 | 1791583200000 |
| F2 | 645 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 120 of 651 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 651 | 1791583200000 |
| F3_HL | 14 | 1793577600000 |
| F4_HL | 14 | 1793577600000 |
| F3_OKX | 303 | 1792234800000 |
| F4_OKX | 302 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 754 | 1793984400027 | 1793986175113 | 4.2 | 0 | 637 |
| OKX | 243 | 1793980800000 | 1793986175350 | 482.7 | 0 | 348 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 637 of 651 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.2 h after settlement
- U_OKX: not evaluable at 348 of 651 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 482.7 h after settlement
