# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `ce6c0a5d608bbe21d92d0cb458b4734e43ca1ee290be91b17c630eecc3d3bbf0`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 8/60 events, UP 1/25, DOWN 7/25)
- Post-seal price hours: 819; eligible decision hours: 819
- Labelled hours: 794 (VALID 794); burn-in complete: True; episodes: 8 (UP 1, DOWN 7); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 819 | 1791583200000 |
| F2 | 813 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 288 of 819 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 819 | 1791583200000 |
| F3_HL | 44 | 1793577600000 |
| F4_HL | 44 | 1793577600000 |
| F3_OKX | 411 | 1792234800000 |
| F4_OKX | 410 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 924 | 1794596400016 | 1794596467527 | 3.9 | 0 | 775 |
| OKX | 264 | 1794585600000 | 1794585669640 | 402.7 | 0 | 408 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 775 of 819 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 3.9 h after settlement
- U_OKX: not evaluable at 408 of 819 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 402.7 h after settlement
