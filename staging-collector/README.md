# Staging collector (STAGING only)

Real, prospective input for Experiment 5 on the **staging** database only:

```
public data ──> btc-tick ─────────────────────────> staging btc_data  ─┐
public data ──> history_harness.py (pinned page) ──> history-ingest ──> staging history ─┴─> staging_ingest_ledger
```

No path to production: the target is fixed to `pulseworker-v2-staging` / `5458d504-2778-49ae-bd25-7751f1c49d50`
(confirmed with Cloudflare before any SQL), the request allowlist is Hyperliquid + Coinbase + that one database, and
the only SQL is four INSERT templates plus read-only SELECTs. The browser harness runs the **unmodified** CryptoPulse
`index.html` at `0a1dfb8c…` with a deny-by-default policy: the `/history` POST is captured locally, never sent;
every other production Worker path, any `*.workers.dev` host and `script.google.com` are aborted.

| File | Role |
|---|---|
| `collector.py` | target validation, network/SQL allowlists, `btc-tick`, `history-ingest`, `open-period`/`close-period`, `verify`, `status`, `check-delta` |
| `history_harness.py` | runs the pinned page, captures the composite payload + full request log (writes only the capture file) |
| `harness_policy.py` | pure deny-by-default request classification (CI-tested without a browser) |
| `relay_shims.py` | local ports of the stateless V1 relays `/macro-proxy`, `/news-proxy`, `/strc-proxy` |
| `harness_fixtures.py` | offline self-test responses; captures made with them are refused by the collector |
| `run-local.ps1` | PC runner: `-Mode DryRun` (G1-A/G1-B, no writes) and `-Mode SingleTick` (controlled staging test) |
| `../.ai/migrations/0020_staging_ingest_ledger.sql` | provenance ledger + collection periods (staging only) |
| `../.github/workflows/staging-collector.yml` | manual-only workflow, kill switch `STAGING_COLLECTION_ENABLED` |

**Rules enforced in code.**
- **Idempotency:** every data INSERT is guarded by NOT EXISTS on its own table for the current slot, computed from the
  database clock: 30 min for BTC, 60 min for history.
- **Provenance:** every attempt is appended to the ledger as WRITTEN, SKIPPED_DUPLICATE, NO_OBSERVATION or REJECTED.
  A `btc_data`/`history` row without a WRITTEN entry was not collected; the E9 fixture rows 7 and 8 are such rows.
- **Ordering:** a history row is refused until a collected BTC row exists. This is checked in Python and inside the
  INSERT, so history can never be priced off the E9 fixture.
- **Schema drift:** collection stops if `stale_refresh_claim` or `btc_data.technical_score` appear. Either one would
  re-arm the staging Worker's public `/predict` / `/btc-backfill` writers.
- **Validation:** invalid observations are recorded as REJECTED, never inserted. BTC is rejected when its cross-check
  differs by more than 1%; history is rejected when the score or any source is outside [0, 100], an unknown source
  appears, the capture is stale or in the future, the page is not the pinned one, or the capture came from fixtures.

**Known input coverage.** 19 of 21 composite sources. `ninemag` needs the `/stock-proxy` relay, which is not shimmed.
`foufi` reads production D1 and is blocked. The page renormalises over the sources that resolved, exactly as it does
in production when a source fails. `globalMcap` has no staging column, so it is kept in the ledger payload only.

**Readiness** (`collector.py status`, read-only) takes one of four operational states:
- COLLECTING: fewer than 269 hourly observations, a span shorter than 14 days, or no BTC row in the last 2 h.
- READY: enough data, and Experiment 5 has not run on it.
- RUNNING: Experiment 5 has run, with no evaluated decision yet.
- EVALUATING: at least one evaluated decision.

CONCLUSION is never reached automatically: no success criterion was pre-registered for Experiment 5, so that step is a
human decision. The Experiment 5 staging runner now exits 5 with `RESULT: NO_USABLE_INPUT` when the agent had nothing
to process. Before this change, such a run exited 0 with `status=OK` and looked like a successful run.
