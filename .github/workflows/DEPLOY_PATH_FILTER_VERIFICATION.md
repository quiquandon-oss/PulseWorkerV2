# Deploy Path Filter — Verification Method

Status: **Implemented, NOT live-tested.** Documenting why, per explicit instruction not to
pretend a test happened when it didn't.

## What changed

`.github/workflows/deploy.yml`'s `push` trigger now has a `paths:` filter. Previously:
any push to `main`, regardless of content, ran `Deploy Worker`. Now: only pushes that
touch `worker.js`, `wrangler.toml`, `package.json`, `package-lock.json`, or the workflow
file itself do.

## Why this isn't live-tested

Proving the negative case (an Obsidian/markdown-only push does **not** trigger a deploy)
and the positive case (a `worker.js` push **does**) both require actually pushing commits
to `main` and observing whether `Deploy Worker` fires. Doing that here — to test a
production deployment guard — would itself be a push to `main`, which I'm not doing
without explicit authorization, especially right after the exact incident this fix exists
to prevent.

## What I did verify instead

- **`paths:` is documented, standard GitHub Actions behavior**, not a custom or
  speculative mechanism: https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflows-run/triggering-a-workflow#example-including-paths
  A `push` trigger with a `paths:` list only fires when at least one changed file in the
  push matches the list. This is the same mechanism GitHub itself recommends for exactly
  this situation.
- **YAML validity**: parsed and confirmed the exact resulting trigger structure (see the
  commit) — `paths` contains exactly the 5 intended entries, `branches: [main]` and
  `workflow_dispatch: {}` unchanged.
- **The path list itself was checked against the actual repo contents** (not assumed):
  `worker.js`, `wrangler.toml`, `package.json`, `package-lock.json` are the only files in
  this repo that affect what `wrangler deploy` actually deploys. `.obsidian/`,
  `Untitled.md`, `README.md`, and everything under `tests/` and `learning/` were
  deliberately excluded — confirmed by listing the repo root before writing the filter.

## How to actually verify it live, once merged

The next real-world push to `main` will do this naturally:
- If it's another accidental notes/docs-only push: `Deploy Worker` should **not** appear
  in the Actions run list for that commit. `Test` (which has no path filter) still will.
- If it's a real `worker.js` change (e.g. the Gemini canary restoration in a later phase):
  `Deploy Worker` should appear and run normally.

I'd recommend checking this after the fix merges and the next couple of pushes happen,
rather than treating it as proven now.
