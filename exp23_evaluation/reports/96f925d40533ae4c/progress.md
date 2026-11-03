# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `96f925d40533ae4cef74cdd7a36bcd6fd8932a2aacdf3ffc1f251a8df34c5467`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 4/60 events, UP 0/25, DOWN 4/25)
- Post-seal price hours: 579; eligible decision hours: 579
- Labelled hours: 554 (VALID 554); burn-in complete: True; episodes: 4 (UP 0, DOWN 4); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 579 | 1791583200000 |
| F2 | 573 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 48 of 579 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 579 | 1791583200000 |
| F3_HL | 4 | 1793577600000 |
| F4_HL | 4 | 1793577600000 |
| F3_OKX | 262 | 1792234800000 |
| F4_OKX | 261 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 680 | 1793718000049 | 1793718466995 | 4.3 | 0 | 575 |
| OKX | 233 | 1793692800000 | 1793718467151 | 522.7 | 0 | 317 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 575 of 579 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.3 h after settlement
- U_OKX: not evaluable at 317 of 579 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 522.7 h after settlement
