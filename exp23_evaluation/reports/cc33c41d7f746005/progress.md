# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `cc33c41d7f746005143e638a460c072bb804163416099ae5cd8d82ff28b8bde5`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 6/60 events, UP 1/25, DOWN 5/25)
- Post-seal price hours: 747; eligible decision hours: 747
- Labelled hours: 722 (VALID 722); burn-in complete: True; episodes: 6 (UP 1, DOWN 5); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 747 | 1791583200000 |
| F2 | 741 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 216 of 747 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 747 | 1791583200000 |
| F3_HL | 29 | 1793577600000 |
| F4_HL | 29 | 1793577600000 |
| F3_OKX | 367 | 1792234800000 |
| F4_OKX | 366 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 850 | 1794330000051 | 1794333060755 | 4.1 | 0 | 718 |
| OKX | 255 | 1794326400000 | 1794333061027 | 434.7 | 0 | 380 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 718 of 747 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.1 h after settlement
- U_OKX: not evaluable at 380 of 747 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 434.7 h after settlement
