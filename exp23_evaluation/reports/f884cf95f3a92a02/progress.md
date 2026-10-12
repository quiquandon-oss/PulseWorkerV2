# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `f884cf95f3a92a02bf4f45c1c79854d94e043190f2938ebf2c186ec89b864eec`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 0/60 events, UP 0/25, DOWN 0/25)
- Post-seal price hours: 51; eligible decision hours: 51
- Labelled hours: 26 (VALID 26); burn-in complete: True; episodes: 0 (UP 0, DOWN 0); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 51 | 1791583200000 |
| F2 | 51 | 1791583200000 |
| F3 | 0 | None |
| F4 | 0 | None |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- F3: no eligible decision hour has all F3 features; F3 vs F1 not evaluable yet
- F4: no eligible decision hour has all F4 features; F4 vs F1 not evaluable yet
- funding: last settlement available_at 1790784000002; stale (> 8h15m) at 51 of 51 eligible hours (Binance monthly archive not yet published)
