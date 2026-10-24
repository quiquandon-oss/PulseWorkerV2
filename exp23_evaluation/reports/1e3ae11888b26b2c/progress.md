# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `1e3ae11888b26b2ccdb3f56ea7215a7fbcbca113af165eb89875533a16cd108b`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 3/60 events, UP 0/25, DOWN 3/25)
- Post-seal price hours: 339; eligible decision hours: 339
- Labelled hours: 314 (VALID 314); burn-in complete: True; episodes: 3 (UP 0, DOWN 3); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 339 | 1791583200000 |
| F2 | 333 | 1791583200000 |
| F3 | 0 | None |
| F4 | 0 | None |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- F3: no eligible decision hour has all F3 features; F3 vs F1 not evaluable yet
- F4: no eligible decision hour has all F4 features; F4 vs F1 not evaluable yet
- funding: last settlement available_at 1790784000002; stale (> 8h15m) at 339 of 339 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 339 | 1791583200000 |
| F3_HL | 0 | None |
| F4_HL | 0 | None |
| F3_OKX | 108 | 1792234800000 |
| F4_OKX | 107 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 439 | 1792850400061 | 1792852391489 | 19.5 | 0 | 339 |
| OKX | 203 | 1792828800000 | 1792839614035 | 642.7 | 0 | 231 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 339 of 339 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 19.5 h after settlement
- U_OKX: not evaluable at 231 of 339 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 642.7 h after settlement
- F3_HL: no eligible decision hour has all F3_HL features; F3_HL vs F1 not evaluable yet
- F4_HL: no eligible decision hour has all F4_HL features; F4_HL vs F1 not evaluable yet
