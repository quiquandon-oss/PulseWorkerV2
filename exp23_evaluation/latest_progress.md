# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `65789f8d2bf9d0c325a37dc3d43ec815ee16184225361c6ccf59d850b80079f2`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 5/60 events, UP 1/25, DOWN 4/25)
- Post-seal price hours: 627; eligible decision hours: 627
- Labelled hours: 602 (VALID 602); burn-in complete: True; episodes: 5 (UP 1, DOWN 4); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 627 | 1791583200000 |
| F2 | 621 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 96 of 627 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 627 | 1791583200000 |
| F3_HL | 11 | 1793577600000 |
| F4_HL | 11 | 1793577600000 |
| F3_OKX | 290 | 1792234800000 |
| F4_OKX | 289 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 725 | 1793880000083 | 1793881272596 | 4.2 | 0 | 616 |
| OKX | 239 | 1793865600000 | 1793881272897 | 498.7 | 0 | 337 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 616 of 627 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.2 h after settlement
- U_OKX: not evaluable at 337 of 627 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 498.7 h after settlement
