# PulseWorkerV2

Cloudflare Worker backend for **CryptoPulseV2** — a standalone BTC prediction-model
tool, built alongside the original [CryptoPulse](https://github.com/quiquandon-oss/CryptoPulse) /
[PulseWorker](https://github.com/quiquandon-oss/PulseWorker) rather than inside them,
so model experimentation can't destabilize the working V1 app.

## Why a separate Worker, shared database

This Worker is deliberately its own Cloudflare Worker (`pulseworker-v2`), isolated from
the original `sentiment-ff75` Worker. But it binds to the **same D1 database**
(`sentiment-history`) as PulseWorker — see `wrangler.toml` — which gives it immediate
read access to weeks of real sentiment/technical/regime history instead of starting
from an empty dataset, and a place to write new V2-only tables (predictions log,
calibration results) without touching anything V1 depends on.

CORS on the original PulseWorker is open (`Access-Control-Allow-Origin: *`), so the
CryptoPulseV2 frontend can also call PulseWorker's existing routes directly
(e.g. `/history` for price series, `/gemini-outlook` for narration) instead of
duplicating that plumbing here.

## Design principle

Same as V1: this Worker **computes deterministically** (the prediction model itself)
and/or calls an LLM (Gemini, free tier) purely for **narration** — catalysts, regime
context, plain-language explanation. It never asks an LLM to invent a price or a
probability; those numbers come from the model.

## Current state

Scaffold only:
- `GET /` — health check
- `GET /db-check` — confirms the shared D1 binding works, reports how much history exists
- `GET /predict` — stub, not yet implemented

Model logic (k-NN historical analog matching on BTC, per the agreed V2 plan — see
CryptoPulseV2 README) is the next build, not this scaffold.

## Research endpoints

Read-only, GET-only endpoints that expose evidence already logged by the
learning system for independent inspection (e.g. by ChatGPT as auditor —
see `.ai/ARCHITECTURE.md`). None of these write to D1, recompute a
production number differently, or influence prediction/selection/calibration
in any way. Optional `coin=BTC|ETH|LINK` and `horizon=12|24` filters: if both
are given and valid, the response is scoped to that single combination;
otherwise (missing, or either invalid) the response covers all 3 coins × 2
horizons — no default coin/horizon is ever guessed.

- `GET /research/regime-directional` — accuracy split by regime-anomaly bucket.
- `GET /research/anomaly-conditioned-audit` — Learning Roadmap §3 Experiment 1:
  anomaly-conditioned directional-accuracy audit, episode-level (not
  row-level) significance.
- `GET /research/anomaly-gate` — Learning Roadmap §3 Experiment 2: reads
  `selection_decisions_anomaly`, the logged-only alternative decision the
  softened Bonferroni gate would have made on high-anomaly cycles, and lines
  it up against the actual production decision (`selection_decisions`) and
  the eventual resolved outcome for the same cycle. Per row: both decisions,
  the alternative's LCA score / `n_matched` / margin info already persisted
  by the experiment, and resolution status. Also returns an episode-grouped
  aggregate (consecutive high-anomaly cycles counted as one episode, not
  independent observations) with an explicit `insufficient_sample` flag —
  never a fabricated conclusion from too little data. The anomaly-gate choice
  in this response is research/audit only: it is logged for comparison and
  never replaces `chosen_variant`, which selection continues to serve
  unchanged.

## Deploy

Push to `main` → GitHub Actions (`cloudflare/wrangler-action@v3`) deploys automatically.
Requires a `CLOUDFLARE_API_TOKEN` repo secret (Settings → Secrets and variables →
Actions), same as PulseWorker.
