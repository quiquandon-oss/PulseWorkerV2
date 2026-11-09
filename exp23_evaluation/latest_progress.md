# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `5142345f1e1ba9c9206c3c14c1f5e458c47250ff5e188fd6f51c503f514841b7`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 6/60 events, UP 1/25, DOWN 5/25)
- Post-seal price hours: 723; eligible decision hours: 723
- Labelled hours: 698 (VALID 698); burn-in complete: True; episodes: 6 (UP 1, DOWN 5); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 723 | 1791583200000 |
| F2 | 717 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 192 of 723 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 723 | 1791583200000 |
| F3_HL | 27 | 1793577600000 |
| F4_HL | 27 | 1793577600000 |
| F3_OKX | 358 | 1792234800000 |
| F4_OKX | 357 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 826 | 1794243600052 | 1794245353683 | 4.1 | 0 | 696 |
| OKX | 252 | 1794240000000 | 1794245353921 | 450.7 | 0 | 365 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 696 of 723 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.1 h after settlement
- U_OKX: not evaluable at 365 of 723 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 450.7 h after settlement
