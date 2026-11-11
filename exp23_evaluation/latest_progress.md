# EXP-23 BTC 12-hour early warning: forward progress

- Dataset fingerprint: `52a2b63de6a4a43e2b2754224b8301117a4c8fc7f105d45243239e038e4824f8`
- Spec: v0.4.0 `8231568780906804…`; definition v1.0.0 `c3ef65830c3bf729…`
- Status: **INSUFFICIENT_SAMPLE** (checkpoint 7/60 events, UP 1/25, DOWN 6/25)
- Post-seal price hours: 771; eligible decision hours: 771
- Labelled hours: 746 (VALID 746); burn-in complete: True; episodes: 7 (UP 1, DOWN 6); burn-in crossings: 0

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 771 | 1791583200000 |
| F2 | 765 | 1791583200000 |
| F3 | 531 | 1791583200000 |
| F4 | 525 | 1791583200000 |

No performance metric is reported below the checkpoint (counts only).

Blockers:
- funding: last settlement available_at 1793462400000; stale (> 8h15m) at 240 of 771 eligible hours (Binance monthly archive not yet published)

## v0.5.0 source variants (separate from the v0.4.0 table above)

| Feature set | Evaluable hours | First evaluable (ms) |
|---|---|---|
| F1 | 771 | 1791583200000 |
| F3_HL | 33 | 1793577600000 |
| F4_HL | 33 | 1793577600000 |
| F3_OKX | 388 | 1792234800000 |
| F4_OKX | 387 | 1792234800000 |

| Source | Settlements | Last settlement (ms) | Last causal availability (ms) | Median observation lag (h) | Non-causal rows | Not evaluable hours |
|---|---|---|---|---|---|---|
| HL | 872 | 1794409200023 | 1794409931842 | 4.1 | 0 | 738 |
| OKX | 257 | 1794384000000 | 1794409932093 | 426.7 | 0 | 383 |

Availability: a forward funding row is used only from max(settlement time, first observation by the collector). Historical/backfilled funding is not read. No metric below the checkpoint.

Source-variant blockers:
- U_HL: not evaluable at 738 of 771 eligible hours (latest causally available settlement older than 75 min, or windows incomplete); median first-observation lag 4.1 h after settlement
- U_OKX: not evaluable at 383 of 771 eligible hours (latest causally available settlement older than 495 min, or windows incomplete); median first-observation lag 426.7 h after settlement
