# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `71f2842cd21c5d833e8b27415f8a936c3d88d15a92c585d5eb004d2d19290cb2`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 2/60 events, UP 0/25, DOWN 2/25)
- Post-seal price hours: 243; eligible decision hours: 243
- Labelled hours: 218 (VALID 218); burn-in complete: True; episodes: 2 (UP 0, DOWN 2); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 243 | 1791583200000 |
| F2 | 237 | 1791583200000 |
| F3 | 0 | None |
| F4 | 0 | None |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- F3: no eligible decision hour has all F3 features; F3 vs F1 not evaluable yet
- F4: no eligible decision hour has all F4 features; F4 vs F1 not evaluable yet
- funding: last settlement available_at 1790784000002; stale (> 8h15m) at 243 of 243 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 243 | 1791583200000 |
| F3_HL | 0 | None |
| F4_HL | 0 | None |
| F3_OKX | 42 | 1792234800000 |
| F4_OKX | 41 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 340 | 1792494000005 | 1792496216825 | 25.3 | 0 | 243 |
| OKX | 191 | 1792483200000 | 1792496217058 | 690.7 | 0 | 201 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 243 of 243 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 25.3 h after settlement
- U_OKX: not evaluable at 201 of 243 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 690.7 h after settlement
- F3_HL: no eligible decision hour has all F3_HL features; F3_HL vs F1 not evaluable yet
- F4_HL: no eligible decision hour has all F4_HL features; F4_HL vs F1 not evaluable yet
