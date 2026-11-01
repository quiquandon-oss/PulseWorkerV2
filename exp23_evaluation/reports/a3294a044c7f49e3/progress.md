# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `a3294a044c7f49e30492deec2e531fc99a33b598ccea9064faf1d9d498d03824`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 4/60 events, UP 0/25, DOWN 4/25)
- Post-seal price hours: 531; eligible decision hours: 531
- Labelled hours: 506 (VALID 506); burn-in complete: True; episodes: 4 (UP 0, DOWN 4); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 531 | 1791583200000 |
| F2 | 525 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 531 | 1791583200000 |
| F3_HL | 0 | None |
| F4_HL | 0 | None |
| F3_OKX | 245 | 1792234800000 |
| F4_OKX | 244 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 632 | 1793545200063 | 1793545737565 | 4.5 | 0 | 531 |
| OKX | 227 | 1793520000000 | 1793545737747 | 546.7 | 0 | 286 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 531 of 531 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.5 h after settlement
- U_OKX: not evaluable at 286 of 531 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 546.7 h after settlement
- F3_HL: no eligible decision hour has all F3_HL features; F3_HL vs F1 not evaluable yet
- F4_HL: no eligible decision hour has all F4_HL features; F4_HL vs F1 not evaluable yet
