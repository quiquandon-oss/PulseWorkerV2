# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `7f41aa8ab13437f17494d2f4d976eb58905b9254d1bafc8cc92267da0b0a8a34`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 7/60 events, UP 1/25, DOWN 6/25)
- Post-seal price hours: 795; eligible decision hours: 795
- Labelled hours: 770 (VALID 770); burn-in complete: True; episodes: 7 (UP 1, DOWN 6); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 795 | 1791583200000 |
| F2 | 789 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 264 of 795 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 795 | 1791583200000 |
| F3_HL | 36 | 1793577600000 |
| F4_HL | 36 | 1793577600000 |
| F3_OKX | 394 | 1792234800000 |
| F4_OKX | 393 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 900 | 1794510000035 | 1794510068425 | 4.0 | 0 | 759 |
| OKX | 261 | 1794499200000 | 1794506470082 | 410.7 | 0 | 401 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 759 of 795 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.0 h after settlement
- U_OKX: not evaluable at 401 of 795 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 410.7 h after settlement
