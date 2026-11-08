# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `7733dd1b39fba850b5a3daedf975fdb20af7760f1e1c08930ca234c40510122a`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 6/60 events, UP 1/25, DOWN 5/25)
- Post-seal price hours: 699; eligible decision hours: 699
- Labelled hours: 674 (VALID 674); burn-in complete: True; episodes: 6 (UP 1, DOWN 5); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 699 | 1791583200000 |
| F2 | 693 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 168 of 699 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 699 | 1791583200000 |
| F3_HL | 22 | 1793577600000 |
| F4_HL | 22 | 1793577600000 |
| F3_OKX | 344 | 1792234800000 |
| F4_OKX | 343 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 800 | 1794150000016 | 1794150774379 | 4.1 | 0 | 677 |
| OKX | 248 | 1794124800000 | 1794150774673 | 466.7 | 0 | 355 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 677 of 699 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.1 h after settlement
- U_OKX: not evaluable at 355 of 699 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 466.7 h after settlement
