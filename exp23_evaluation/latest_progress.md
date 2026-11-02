# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `ab4ee877d98408b47f972f2863fd5b8782441d0c3dcd9eb38129537c0ff68dfb`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 4/60 events, UP 0/25, DOWN 4/25)
- Post-seal price hours: 555; eligible decision hours: 555
- Labelled hours: 530 (VALID 530); burn-in complete: True; episodes: 4 (UP 0, DOWN 4); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 555 | 1791583200000 |
| F2 | 549 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 24 of 555 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 555 | 1791583200000 |
| F3_HL | 1 | 1793577600000 |
| F4_HL | 1 | 1793577600000 |
| F3_OKX | 258 | 1792234800000 |
| F4_OKX | 257 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 656 | 1793631600052 | 1793634746174 | 4.3 | 0 | 554 |
| OKX | 230 | 1793606400000 | 1793634746482 | 538.7 | 0 | 297 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 554 of 555 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.3 h after settlement
- U_OKX: not evaluable at 297 of 555 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 538.7 h after settlement
