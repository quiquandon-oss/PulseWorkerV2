# cryptopulse-research-dispatcher

This is a dedicated Cloudflare Worker that lets the owner start allowlisted PulseWorkerV2 research workflows from
CryptoPulse V3's Research Controls. It is isolated from `pulseworker-v2`:
- no D1, KV or service bindings;
- no prediction code;
- no generic admin or database-write route.

Its only outbound calls go to `api.github.com` for `quiquandon-oss/PulseWorkerV2`.

## API

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | none | `{ok, service, version, dispatch_enabled, jobs}`; no secrets |
| `POST /dispatch` | `Authorization: Bearer <owner key>` | Body exactly `{"job": "<allowlisted id>", "confirm": true, "request_id"?: "<8-64 chars>"}` |
| `GET /audit` | owner key | Last 50 audit entries |

**Only the job id comes from the client.** The repository, workflow ID, workflow path, branch (`main`) and inputs
(none) are fixed server-side, and any other field is rejected with 400 `field_not_allowed`.

**A 202 `accepted` means GitHub accepted the dispatch, not that a run started.** V3 reports a run as started
only after GitHub lists it.

**Errors:**

| HTTP status | Code(s) | Meaning |
|---|---|---|
| 401 | `unauthorized` | Owner key missing or wrong |
| 403 | `origin_not_allowed`, `job_not_allowed` | Request from another origin, or a job that isn't allowlisted |
| 400 / 413 / 415 | (validation codes) | Malformed request |
| 409 | `conflict` (`dispatch_in_flight` or `run_active` with `run_id`) | A dispatch or run is already active |
| 429 | `rate_limited` (with `Retry-After`) | Too many requests |
| 502 | `upstream_auth`, `upstream_not_found`, `upstream_rejected`, `upstream_error`, `workflow_mismatch` | GitHub refused or the workflow changed |
| 503 | `upstream_rate_limited`, `dispatch_disabled` | GitHub rate limit, or secrets not provisioned |
| 504 | `upstream_timeout` | GitHub didn't respond in time |

## Allowlist

| Job id | Workflow | Ref | Inputs | Why it is safe |
|---|---|---|---|---|
| `prediction-freshness-check` | ID 381092876, `.github/workflows/prediction-freshness-check.yml` | `main` | none | `contents: read`, no secrets, only GETs to public endpoints |

**Not allowlisted, and what each would need first:**
- **`export-learning-data`, `exp004-timesfm`, `exp005-*`, `exp009-*`, `exp010-*`, `live-evidence-collection`:**
  they read production D1 with `CLOUDFLARE_API_TOKEN` (export also pushes to `main`). Each needs the owner's explicit
  authorization for production data access on demand, plus a per-job review of inputs and side effects.
- **`exp23-forward-evaluation`:** schedule-only governance, with no `workflow_dispatch`. It needs an owner-approved
  governance change and a reviewed workflow change.
- **Market-move sealed evaluation:** no workflow exists. It needs a reviewed `workflow_dispatch` workflow that takes
  the plan sha256, with `contents: write` limited to a research-data branch, and the protected-evaluation gate in V3.
- **Deploy, canary and production audits, stage7, test:** these have production or staging write access and are
  never exposed.

## Controls

- **Authentication:** only the SHA-256 of the owner key is stored (`OWNER_KEY_SHA256`), and comparison is
  constant-time. Failed attempts are limited to 10 per hour per client, keyed by a hashed IP.
- **GitHub credential:** the token (`GITHUB_DISPATCH_TOKEN`) is sent only to `api.github.com`. GitHub response
  bodies are never forwarded, and the token never appears in responses, logs or the audit.
- **Duplicates:** the Durable Object `DispatchGuard` holds an atomic per-job lock. Before dispatching, the Worker
  also checks GitHub's queued and running runs. After an accepted dispatch the lock is held for 3 minutes.
- **Rate limits:** one dispatch per job per 2 minutes, and 12 per hour overall. A failed dispatch is not counted.
- **Audit:** append-only (insert-only SQL). It records time, outcome, code, job, HTTP and upstream status,
  request id, dispatch id, run id, origin and a hashed IP. It never stores credentials, headers or request bodies.
- **CORS:** only `https://quiquandon-oss.github.io` (V3) is allowed.

## Secret provisioning (owner)

1. **Create the GitHub token.** In GitHub, go to Settings > Developer settings > Fine-grained tokens:
   - resource owner `quiquandon-oss`;
   - **Only select repositories**: `PulseWorkerV2`;
   - Repository permissions: **Actions: Read and write** (Metadata: Read is added automatically), nothing else;
   - expiry of 90 days or less.
2. **Generate an owner key** on your own machine, for example `openssl rand -base64 36`. Keep it in your password
   manager.
3. **Add both as GitHub Actions secrets** in PulseWorkerV2 (Settings > Secrets and variables > Actions):
   `RESEARCH_DISPATCH_GITHUB_TOKEN` and `RESEARCH_DISPATCH_OWNER_KEY`.
4. **Run the deploy workflow** (Actions > *Deploy research dispatcher* > Run workflow). It:
   - pipes the token to `wrangler secret put GITHUB_DISPATCH_TOKEN`;
   - pipes the SHA-256 of the owner key to `OWNER_KEY_SHA256`;
   - checks access control: 401 without and with a wrong key, 403 for other origins.

   It prints status codes only, never secret values.
5. **Enter the owner key in V3** under Lab > Experiments > Research controls > Owner dispatch key. It is kept in
   that browser tab's session storage, or in local storage only if you choose "remember on this device".

**Rotation:** replace either GitHub secret and re-run step 4. The token expires on its own.

**Revocation:** revoke the PAT on GitHub (it stops working at once), delete either GitHub secret, then re-run the
workflow. The Worker secrets are overwritten with inert placeholders and dispatch becomes disabled.
