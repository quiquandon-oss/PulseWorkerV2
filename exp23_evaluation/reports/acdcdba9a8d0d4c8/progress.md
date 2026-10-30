# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `acdcdba9a8d0d4c88d9ea0a7be7fb158c3d0a30113222b49c71a58eb082c1cd5`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 4/60 events, UP 0/25, DOWN 4/25)
- Post-seal price hours: 483; eligible decision hours: 483
- Labelled hours: 458 (VALID 458); burn-in complete: True; episodes: 4 (UP 0, DOWN 4); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 483 | 1791583200000 |
| F2 | 477 | 1791583200000 |
| F3 | 0 | None |
| F4 | 0 | None |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- F3: no eligible decision hour has all F3 features; F3 vs F1 not evaluable yet
- F4: no eligible decision hour has all F4 features; F4 vs F1 not evaluable yet
- funding: last settlement available_at 1790784000002; stale (> 8h15m) at 483 of 483 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 483 | 1791583200000 |
| F3_HL | 0 | None |
| F4_HL | 0 | None |
| F3_OKX | 216 | 1792234800000 |
| F4_OKX | 215 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 583 | 1793368800025 | 1793369314678 | 5.1 | 0 | 483 |
| OKX | 221 | 1793347200000 | 1793369314893 | 570.7 | 0 | 267 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 483 of 483 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 5.1 h after settlement
- U_OKX: not evaluable at 267 of 483 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 570.7 h after settlement
- F3_HL: no eligible decision hour has all F3_HL features; F3_HL vs F1 not evaluable yet
- F4_HL: no eligible decision hour has all F4_HL features; F4_HL vs F1 not evaluable yet
