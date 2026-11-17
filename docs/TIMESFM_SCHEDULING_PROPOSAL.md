# EXP-004 / EXP-008 scheduling: measured delay and proposal (PROPOSED, not implemented)

Status: proposal only, 2026-11-17. No workflow, Worker or schedule is changed by this document.

## Measured: GitHub Actions run history, exp004-timesfm.yml (`cron: '30 7 * * *'`)

| Measure | Value |
|---|---|
| Scheduled runs, 2026-10-08 to 2026-11-16 | 40 on 40 days: none skipped; 1 failed (2026-11-08) |
| Run created (= event released by GitHub's scheduler) | 11:42 to 17:43 UTC, i.e. **4 h 12 m to 10 h 13 m after 07:30**; typically 12:00–15:45. Late-October runs mostly started around 12:00–14:30, November runs around 14:20–17:45. |
| Queue: run created → job started | **1 s** (run 43916853382: created 17:17:42, job started 17:17:44) |
| Execution | **60–100 s** (install about 22 s, forecast + resolve about 23 s) |
| Concurrency | no `concurrency:` group; never overlapping; the free standard-runner allowance is nowhere near |

**Cause:** the whole delay is before the run exists. GitHub releases `schedule` events late under load: no queue,
runner shortage or concurrency limit is involved. The same lag shows on other daily workflows in this repository
(`export-learning-data.yml`, 08:00, lands about 15:00–17:50). EXP-008's 21:40 fallback started at 04:09 on 2026-11-17
(6 h 29 m late). The repository has measured this before: the hourly funding collector fired 118 times in 624 h, which
is why worker.js already starts two workflows from the Worker's own cron (`dispatchResearchCollection`,
`RESEARCH_DISPATCH_TOKEN`).

**Effect on the experiment:** resolution happens only in each run, so a due outcome waits up to about 24 h + 10 h. The
time of day of the origin drifts with GitHub's lag: origins have landed on the 12:01, 15:01 or 18:01 tick.

## Proposal A (recommended): start EXP-004 from the Worker's existing hourly cron
- In the existing non-tick hourly Cloudflare cron, at the 07:00 UTC firing (hour 7, already a dispatch hour for the
  staleness alert), call the same `dispatchResearchCollection` with the `exp004-timesfm.yml` dispatch URL.
- **Uses no new cron trigger** (it stays within the 5 free Cloudflare cron triggers), **no new secret** (the existing
  `RESEARCH_DISPATCH_TOKEN`), one extra subrequest a day, and about 1.5 standard-runner minutes a day on a public
  repository: no paid resource.
- The GitHub schedule stays as a fallback.
- **Required guard (protocol):** the plan's replication needs non-overlapping origins. Before forecasting,
  run_experiment.py would skip the forecast step (resolution still runs) when a forecast was already issued less than
  20 h earlier. A late fallback run on the same day then only resolves. Tests: dispatch at hour 7 only; a second run
  within 20 h makes no forecast and still resolves; the GitHub schedule unchanged.
- **Owner decision needed:** origins would move to the 06:01 tick, a fixed time of day, instead of the drifting 12:01,
  15:01 or 18:01. The frozen plan fixes no origin hour, but a cadence change mid-collection should be recorded as a
  versioned operational note before the checkpoint.
- EXP-008 follows automatically (`workflow_run` on EXP-004 completion), on time as well.

## Proposal B (smaller): resolution-only dispatch
Keep forecast timing as it is. Also dispatch a resolve-only EXP-004 run (no forecast) from the Worker at 07:00 and
19:00. Outcomes resolve within about 4 h of their tick. Origins and cadence are unchanged, so there is no protocol
question. This needs a `resolve_only` workflow_dispatch input. Same cost profile as A.

## Not recommended
- Changing the cron minute (for example off :30): the lag is account- and repository-wide, and the evidence shows
  every minute delayed.
- More frequent GitHub schedules: they add load and events that may be skipped, and do not fix the start time.
- Self-hosted or paid runners: excluded by the operating rules, and the runner is not the bottleneck.
