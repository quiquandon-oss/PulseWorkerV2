# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `7a4f10d16882c754d4b0ede7bebeea872e8e83d8f762cc1b26fbed817573916a`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 1/60 events, UP 0/25, DOWN 1/25)
- Post-seal price hours: 99; eligible decision hours: 99
- Labelled hours: 74 (VALID 74); burn-in complete: True; episodes: 1 (UP 0, DOWN 1); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 99 | 1791583200000 |
| F2 | 99 | 1791583200000 |
| F3 | 0 | None |
| F4 | 0 | None |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- F3: no eligible decision hour has all F3 features; F3 vs F1 not evaluable yet
- F4: no eligible decision hour has all F4 features; F4 vs F1 not evaluable yet
- funding: last settlement available_at 1790784000002; stale (> 8h15m) at 99 of 99 eligible hours (Binance monthly archive not yet published)
