# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `7696c8e3561beb65393775519e0650c7d20f3325ccfc8284a7c7a8b93fb74afd`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 0/60 events, UP 0/25, DOWN 0/25)
- Post-seal price hours: 27; eligible decision hours: 27
- Labelled hours: 2 (VALID 2); burn-in complete: False; episodes: 0 (UP 0, DOWN 0); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 27 | 1791583200000 |
| F2 | 27 | 1791583200000 |
| F3 | 0 | None |
| F4 | 0 | None |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- F3: no eligible decision hour has all F3 features; F3 vs F1 not evaluable yet
- F4: no eligible decision hour has all F4 features; F4 vs F1 not evaluable yet
- funding: last settlement available_at 1790784000002; stale (> 8h15m) at 27 of 27 eligible hours (Binance monthly archive not yet published)
