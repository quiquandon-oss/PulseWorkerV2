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

## Deploy

Push to `main` → GitHub Actions (`cloudflare/wrangler-action@v3`) deploys automatically.
Requires a `CLOUDFLARE_API_TOKEN` repo secret (Settings → Secrets and variables →
Actions), same as PulseWorker.
