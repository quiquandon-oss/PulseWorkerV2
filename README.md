# research-data/risk-regime-forward

**Research data only.** This orphan branch shares no history with `main`. It contains no code, no workflow
definitions and no Worker files, and it is never merged.

It holds the CryptoPulseV2 Risk Regime **forward collection store** (`risk_regime_forward/`), written by
`research/risk_regime_forward.py` from branch `claude/sweet-meitner-66ntx8`. The design and the rules are in that
branch's `research/RISK_REGIME_FORWARD.md`:
- append-only, idempotent partitions with content fingerprints;
- file and raw sha256 checksums;
- missing slots listed, never filled;
- conflicts kept, never overwritten;
- one run record per run in `risk_regime_forward/runs/`.

**Seed.** The first commit is a byte-identical copy of `research/results/risk_regime_forward/` at research commit
`8307af6`: Hyperliquid partitions for 2026-10-06 08:00 → 2026-10-08, plus the first three run records. From then on,
forward collection writes **only** here.

Not here: Bybit and liquidations (no permitted source). The frozen historical datasets stay on the research branch.
