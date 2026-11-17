# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `ba4c7989f07e92569463b4dba23f0350102bb3053b8d120acc1a2b4ac98cc8b2`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 9/60 events, UP 2/25, DOWN 7/25)
- Post-seal price hours: 915; eligible decision hours: 915
- Labelled hours: 890 (VALID 890); burn-in complete: True; episodes: 9 (UP 2, DOWN 7); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 915 | 1791583200000 |
| F2 | 909 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 384 of 915 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 915 | 1791583200000 |
| F3_HL | 140 | 1793577600000 |
| F4_HL | 140 | 1793577600000 |
| F3_OKX | 507 | 1792234800000 |
| F4_OKX | 506 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 1021 | 1794945600077 | 1794945676449 | 3.3 | 0 | 775 |
| OKX | 276 | 1794931200000 | 1794931279649 | 354.7 | 0 | 408 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 775 of 915 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 3.3 h after settlement
- U_OKX: not evaluable at 408 of 915 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 354.7 h after settlement
