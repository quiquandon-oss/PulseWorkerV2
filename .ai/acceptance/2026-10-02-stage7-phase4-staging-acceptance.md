# Stage 7 Phase 4 -- staging acceptance record

**Status: FORMALLY ACCEPTED (staging only)** -- accepted by the project owner on 2026-10-02.

This record closes Phase 4 (Stage 7 staging acceptance). It records evidence; it does not change any test result.
Production was not modified at any point during acceptance.

## Scope

| Item | Value |
|---|---|
| Reviewed code | PR #82 `claude/stage7-human-controlled-workflow` @ `b3077dd3fe2770b2fc129f5fb3d22111cbb5ddbb` |
| Staging Worker | `pulseworker-v2-staging` -- overview reports `git_commit_sha = b3077dd…`, `stage7_enabled = true` |
| Staging D1 | `pulseworker-v2-staging` / `5458d504-2778-49ae-bd25-7751f1c49d50`, account `f58e761fbc8e62dc404d8684290af264` |
| Production | Worker, D1 `sentiment-history` (`f91ca980-…`), secrets and workflows: untouched. Last `deploy.yml` run 2026-09-28 (before acceptance). |
| PRs | #82 (`b3077dd`) and #83 (`0910d13`) open and unmerged throughout |

Preconditions completed earlier in the same remediation (2026-10-01): obsolete `stage7-staging-dispatcher.yml`
disabled (`disabled_manually`); migration 0018 applied to staging D1 only; PR #82 deployed to staging
(deploy run #4, later redeployed from the same reviewed SHA after an accidental `main` deploy, run #5 -> run #6).

## Results

| Test | Result | Evidence |
|---|---|---|
| A1 Schema readiness | PASS | `activated:true`; write-route preflight passes the schema probe |
| A2 Deployed SHA | PASS | overview `deployment.git_commit_sha = b3077dd…` |
| A3 Preview writes nothing | PASS | counts/timestamps unchanged after overview, source preview and UI loads |
| A4 UI quality | PASS | desktop 1366px and mobile 390px: no JS errors, no horizontal overflow (benign `/favicon.ico` 404 only) |
| B1 Missing / wrong token rejected | PASS | missing token: live 401 `{"ok":false,"error":"Unauthorized"}`, no schema details. Wrong token: run by the owner from Windows PowerShell with `curl.exe -q` and a deliberately wrong token (no real token used) -> `HTTP/1.1 401 Unauthorized`, body exactly `{"ok":false,"error":"Unauthorized"}`; staging D1 unchanged afterwards. Unit tests also cover it. |
| B2 Failed auth creates no rows | PASS | request count unchanged on every rejected call |
| B3 One request per authorized create | PASS | `stage7-req-999001-1`, `stage7-req-999002-1`; candidates CONVERTED |
| B4 Create retry is idempotent | PASS | repeat -> `skipped: candidate is already CONVERTED` |
| C1 Copied prompt == stored prompt | PASS | byte-identical (2,340 and 3,016 chars) |
| C2 No external AI call | PASS | code inspection; browser network log shows only the staging host |
| C3 Malformed input | PASS | truncated JSON / prose / JSON array -> clear messages; server `400 INVALID_JSON`; no crash |
| C4 Manual correction | PASS | fields editable after failed parse; edits kept |
| D1 Source cases | PASS | valid / non-http / duplicate / post-cutoff / undated / same-claim each classified correctly |
| D2 Truthful wording | PASS | "FORMAT OK" + "URL not opened, claim not verified"; "verified" never used for sources |
| D3 No confirmation without explicit human action | PASS | UI and API refuse validated-without-confirmation (400/422); nothing written |
| E1 Registration keeps raw response + provenance | PASS | raw text byte-identical; provider, note, per-source verdicts stored |
| E2 Registration does not recalculate | PASS | `recalculation_status` null after PENDING and VALIDATED registration |
| E3 Identical re-registration | PASS | `already_registered:true`, nothing changed |
| E4 Conflicting registration | PASS | 409; stored response unchanged |
| E5 Exactly one REQUESTED | PASS | double click -> one POST; repeat -> `already_requested`, timestamp unchanged |
| E6 UI distinguishes REQUESTED from completed | PASS | "REQUESTED -- NOT CALCULATED YET" / "No calculation has happened" |
| E7 Old event 999001 | PASS (expected failure) | [run #11](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/36905353250): `FAILED: EVENT_NOT_ELIGIBLE …(6 days)`, 0 rows, red by design |
| E8 Re-run after failure | PASS | [run #12](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/36905541022): green, 0 rows, FAILED request not auto-retried |
| E9 Successful recalculation | PASS | see below |
| Recalculation idempotency | PASS | post-COMPLETED re-request -> 409; [run #15](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/36907153733) wrote 0 rows |
| F Write boundary | PASS | row counts matched the authorized plan at every step |

### E9 -- successful recalculation (owner-authorized synthetic fixture)

Fixture (staging only; preserved as the audit trail): `btc_data` ids 7/8 (60000.0 @ `1790640300000`, 63000.0 @
`1790726700000`, +5%); `research_events` `999002` (`fp-synthetic-e9-fixture-999002`, LARGE_MOVE UP,
`event_ts` 2026-09-30 00:05Z); `research_event_evidence` `999201` (PRE_EVENT, labelled synthetic). No `history` or
`predictions` rows; no eligibility rule changed.

- [Run #13](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/36906515682): candidate `stage7-cand-999002` proposed; baseline sentiment row 6 (label null, `INSUFFICIENT_EVIDENCE`).
- UI: request `stage7-req-999002-1` created; response registered VALIDATED with human confirmation (2 sources valid, 3 excluded); recalculation REQUESTED.
- [Run #14](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/36906865954): sentiment row 7 -- POSITIVE / 100, `SUFFICIENT`, `stage7-v1`, cutoff `1790726700000`, `previous_sentiment_id = 6`; contributing `[999201, stage7-source-…-0, stage7-source-…-1]`; excluded `-3` post-cutoff, `-4` undated, `-2` duplicate of `-0`; `ai_research_response_id = stage7-resp-stage7-req-999002-1`. Request COMPLETED / `INTEGRATION_REVIEW`, 1 attempt. UI correct on desktop and mobile.
- [Run #15](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/36907153733): 0 rows written (idempotent).

All five pipeline runs: 1,025 tests passed, 0 production-identifier hits in logs, no git commit or push.

## Staging D1 row counts

| | requests | responses | candidates | sentiment | events | evidence | btc_data | history / predictions |
|---|---|---|---|---|---|---|---|---|
| Start of Phase 4 | 0 | 0 | 1 | 0 | 1 | 0 | 0 | 0 / 0 |
| Accepted state | 2 | 2 | 2 | 2 | 2 | 1 | 2 | 0 / 0 |

Synthetic fixtures (events 999001 and 999002 and every row derived from them) are retained as the audit trail.
Cleanup is not authorized.

## Observations (not failures)

- An `EVENT_NOT_ELIGIBLE` failure records `recalculation_attempts = 0` (the recalculation never started).
- If the pipeline runs while a request is `PENDING_RESEARCH`, the publisher commits a request file to PR #82's branch -- by design; the dispatcher must know this.
- The test session's egress proxy injected the admin token on every request to the staging host, which is why B1's wrong-token case was run from the owner's terminal.

## What acceptance authorizes next

Migration 0019 on staging and Phase 6 (staging-safe EXP-005 runner) were authorized separately by the owner on
2026-10-02. Production deployment, production D1 changes and PR merges remain unauthorized.
