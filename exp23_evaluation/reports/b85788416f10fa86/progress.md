# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `b85788416f10fa86e9103060eea483da5738058200d2dee58f62cfec5197257f`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 8/60 events, UP 1/25, DOWN 7/25)
- Post-seal price hours: 891; eligible decision hours: 891
- Labelled hours: 866 (VALID 866); burn-in complete: True; episodes: 8 (UP 1, DOWN 7); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 891 | 1791583200000 |
| F2 | 885 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 360 of 891 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 891 | 1791583200000 |
| F3_HL | 116 | 1793577600000 |
| F4_HL | 116 | 1793577600000 |
| F3_OKX | 483 | 1792234800000 |
| F4_OKX | 482 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 998 | 1794862800033 | 1794862868998 | 3.4 | 0 | 775 |
| OKX | 273 | 1794844800000 | 1794844869571 | 362.7 | 0 | 408 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 775 of 891 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 3.4 h after settlement
- U_OKX: not evaluable at 408 of 891 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 362.7 h after settlement
