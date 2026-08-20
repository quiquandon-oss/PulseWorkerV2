# Experiment Registry

Tracks every experiment against CryptoPulseV2's models, per
`.ai/EXPERIMENT_PROTOCOL.md` and `.ai/AI_COLLABORATION.md`.

## Lifecycle

```
PROPOSED → IMPLEMENTED → BACKTESTING → OUT_OF_SAMPLE → REVIEW → PASSED/FAILED → PROMOTED/REJECTED
```

## Directories

- `experiments/` — ChatGPT's experiment specifications (one file per ID). Never edited by Claude except to append a status update.
- `claude_tasks/` — Implementation tasks handed to Claude, derived from an experiment spec.
- `results/` — Claude's result reports after implementation, tests, backtest, and out-of-sample validation.

## ID Rule

IDs are `EXP-NNN`, permanent, sequential, never reused — even if an experiment is rejected or abandoned.

## Log

| ID | Status | Proposed | Coin/Horizon | One-line hypothesis | Result file |
|---|---|---|---|---|---|
| — | — | — | — | *No experiments proposed yet. This log is updated as each EXP-NNN moves through the lifecycle.* | — |

## Templates

See `experiments/TEMPLATE.md`, `claude_tasks/TEMPLATE.md`, `results/TEMPLATE.md`.
