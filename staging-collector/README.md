# Staging collector (STAGING only)

`.github/workflows/staging-collector.yml` collects one real BTC price and one CryptoPulse composite per run into the
**staging** D1 database (`pulseworker-v2-staging` / `5458d504-2778-49ae-bd25-7751f1c49d50`) for Experiment 5. It never
touches production, Stage 7, Experiment 5 itself, predictions or weights.

The collector code is **not** on this branch by design: the workflow checks out the reviewed commit
`ddf48cad109f306fe76f2c701ce8736369247f92` (collector, browser harness, relay shims, migration 0020 and their tests,
reviewed in quiquandon-oss/PulseWorkerV2#84) and verifies every file by SHA-256 before running it. The CryptoPulse page
is pinned to `0a1dfb8c` and verified by SHA-256.

Activation is two-locked: the hourly schedule (`7 * * * *`) only exists once this file is on the default branch, and
every run is skipped unless the repository variable `STAGING_COLLECTION_ENABLED` is `'true'`. A tick also requires an
OPENED collection period (opened/closed only by manual dispatch with `mode=open-period|close-period`).

`test_staging_collector_workflow.py` guards the workflow (schedule, pins, hashes, target, environment-scoped secret,
production sinkholes, no Stage 7 / Experiment 5 / other write path) and runs in the Test workflow.
