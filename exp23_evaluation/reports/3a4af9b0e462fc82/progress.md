# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `3a4af9b0e462fc826e06bc439bed58aed7976c9a4b8e57e874786f3b6d283acd`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 4/60 events, UP 0/25, DOWN 4/25)
- Post-seal price hours: 603; eligible decision hours: 603
- Labelled hours: 578 (VALID 578); burn-in complete: True; episodes: 4 (UP 0, DOWN 4); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 603 | 1791583200000 |
| F2 | 597 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 72 of 603 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 603 | 1791583200000 |
| F3_HL | 7 | 1793577600000 |
| F4_HL | 7 | 1793577600000 |
| F3_OKX | 269 | 1792234800000 |
| F4_OKX | 268 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 706 | 1793811600000 | 1793812525893 | 4.3 | 0 | 596 |
| OKX | 237 | 1793808000000 | 1793812526169 | 506.7 | 0 | 334 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 596 of 603 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.3 h after settlement
- U_OKX: not evaluable at 334 of 603 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 506.7 h after settlement
