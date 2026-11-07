# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `fcd4162136a2222a46f93f424ae0af0140fb380decef295e2532539eb78672fb`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 6/60 events, UP 1/25, DOWN 5/25)
- Post-seal price hours: 675; eligible decision hours: 675
- Labelled hours: 650 (VALID 650); burn-in complete: True; episodes: 6 (UP 1, DOWN 5); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 675 | 1791583200000 |
| F2 | 669 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 144 of 675 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 675 | 1791583200000 |
| F3_HL | 18 | 1793577600000 |
| F4_HL | 18 | 1793577600000 |
| F3_OKX | 324 | 1792234800000 |
| F4_OKX | 323 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 777 | 1794067200001 | 1794068786769 | 4.1 | 0 | 657 |
| OKX | 246 | 1794067200000 | 1794068786900 | 474.7 | 0 | 351 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 657 of 675 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.1 h after settlement
- U_OKX: not evaluable at 351 of 675 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 474.7 h after settlement
