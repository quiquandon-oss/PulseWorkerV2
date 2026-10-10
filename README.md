# research-data/exp23-evaluation

**Research data only.** Orphan branch: no code, no workflows, no Worker files; never merged.

Holds the EXP-23 BTC 12-hour early-warning forward evaluation results written by
`research/exp23_forward_eval.py` (PulseWorkerV2, branch `claude/epic-planck-uyapsw-exp23-eval`), under
`exp23_evaluation/` only:

- `state.json`: last status, last evaluated dataset fingerprint, history
- `reports/<fingerprint16>/report.json` and `progress.md`: deterministic report per dataset version
- `runs/<evaluated_at>_<run_id>.json`: one record per run that evaluated or failed (NO_NEW_DATA writes nothing)
- `latest_progress.md`: the latest progress report

Spec `btc-12h-early-warning` v0.4.0; events per market-moves v1.0.0. Counts only until the checkpoint
(60 evaluable events, 25 per direction); no performance claim is made before it.
