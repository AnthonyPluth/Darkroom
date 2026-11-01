# Darkroom — hand-off plan for Opus coding agents

Covers the 41 GitHub issues (8 epics, 33 children). Each issue body starts with its key, e.g. P2-01 (#12). Every child belongs to one epic; `manual`, `decision` and `spike` issues are the owner's unless the issue says an agent writes a script or document first.

## Ground rules that apply to every wave

- One issue, one agent, one session, one PR. An agent reads `AGENTS.md`, `docs/SPEC.md` and its issue, nothing else is assumed.
- Within a wave, issues touch different trees and can run at the same time. Across waves, a wave starts only when every issue it depends on is merged (not just open as a PR).
- Two PRs that both regenerate the same generated files (`docs/feature-map.json`, `docs/openapi.json`, `frontend/src/lib/api-types.ts`) land one at a time: the second rebases on `main`, re-runs `make feature-map api-contract`, then `make check`.
- Two PRs that both add an Alembic migration never run in the same wave. P2-02 (#13) defines the whole model up front so only P7-03 (#39) should need one; anything else that wants a migration says so in its PR.

## Waves

| Wave | Issues (parallel) | Owner steps before the wave | Notes |
| --- | --- | --- | --- |
| 0 | P1-01 (#9), P1-02 (#10), P1-03 (#11) (owner) | — | Runs alongside waves 1–4. P1-02 (#10) item 5 (name) before wave 1 merges; items 1, 10, 11 before wave 5. |
| 1 | P2-01 (#12) | Name decided | Solo: it defines the layout, Makefile, CI and AGENTS.md every later issue relies on. |
| 2 | P2-02 (#13) · P5-02 (#32) · P3-01 (#19) (agent part) · P3-02 (#20) (agent part) | — | Four separate trees: `darkroom/storage/`, `ios/`, `tools/spikes/` + `docs/spikes/` (two different files each). |
| 3 | P2-03 (#14) · P2-06 (#17) | — | Both on P2-02 (#13). `oidc.py`/`server/api/auth` vs `providers/homeassistant.py`, `domain/alerts.py`, `server/jobs.py`. |
| 4 | P2-04 (#15) · P2-07 (#18) · P4-01 (#27) | P1-01 (#9) far enough that the owner can run P3-01 (#19)/P3-02 (#20) scripts on real data | `frontend/` vs `domain/media.py` + `domain/index.py` + `providers/exiftool.py` vs `darkroom_browser/` + `server/api/browser*.py`. P4-01 (#27) and P2-07 (#18) both register scheduler jobs in `server/jobs.py`: append-only, rebase. |
| 5 | P2-05 (#16) · P3-03 (#21) · P4-02 (#28) | P1-03 (#11) snapshots committed (else P4-02 (#28) works on synthetic ones and tags the tests `snapshot_pending`); P1-02 (#10) item 1 to seed people/Pixels | P2-05 (#16) and P4-02 (#28) both add pages and routes: separate page files, nav edit conflicts are trivial; regenerate contract on rebase. |
| 6 | P5-01 (#31) · P3-04 (#22) · P4-03 (#29) | P3-01 (#19) results pasted (Möbius answer feeds P5-03 (#33)'s README; Live Photo answer → P1-02 (#10) item 14) | P5-01 (#31) and P4-03 (#29) both add routes (device vs exports); P3-04 (#22) is pure domain code. |
| 7 | P3-05 (#23) · P4-04 (#30) · P5-03 (#33) · P7-03 (#39) | P1-02 (#10) items 4, 9, 13 confirmed or defaults accepted | P7-03 (#39) adds the only post-baseline migration; nothing else in this wave migrates. P3-05 (#23) adds the `confirmed_in_google` Semgrep rule; P4-04 (#30) and P7-03 (#39) must not assign that column. |
| 8 | P3-06 (#24) · (after P5-03 (#33) verified on each phone: P5-04 (#34), owner) | P3-02 (#20) result (sidecar name form) or accept the default; owner verifies P5-03 (#33) on a Mac/iPhone | P3-06 (#24) adds `providers/exiftool.write_xmp` and two Semgrep rules; nothing else touches `exiftool.py` this wave. |
| 9 | P3-07 (#25) | Deploy `darkroom` + `darkroom-browser` on Unraid; connect one account through noVNC (P4-02 (#28) live); P5-04 (#34) switch under way | Solo: it stitches the stages, adds the unmatched alert and cleanup; `server/jobs.py` and `domain/ingest/run.py` are its own this wave. Afterwards the owner runs `run.py import-takeout` on the hand-downloaded export → Phase 3 "done when". |
| 10 | P3-08 (#26) · P7-02 (#38) | Import from wave 9 reviewed; P7-01 (#37) observation starts whenever a renewal is due (no blocker) | P3-08 (#26) (review/imports UI + reassign) vs P7-02 (#38) (renew job, incremental semantics, runbook). Both touch `domain/ingest/run.py` only if P7-02 (#38)'s incremental changes land first; P3-08 (#26) should not need it. If both do, P7-02 (#38) merges first. |
| 11 | P6-01 (#35) | P1-02 (#10) items 2 and 16 answered; P5-04 (#34) done on at least one phone | Solo and safety-critical: review the invariant tests before merging, not just CI. |
| 12 | P6-02 (#36) · P7-04 (#40) · P8-01 (#41) | Owner enables cleanup per device only after confirming iCloud Photos is off on it | P6-02 (#36) is iOS only; P7-04 (#40) is `server/mcp_server.py` tools; P8-01 (#41) is a document. |

Critical path: `P2-01 (#12) → P2-02 (#13) → P2-06 (#17) → P2-07 (#18) → P3-03 (#21) → P3-04 (#22) → P3-05 (#23) → P3-06 (#24) → P3-07 (#25) → P3-08 (#26) → P6-01 (#35) → P6-02 (#36)`. The browser worker (`P4-*`) and the upload side (`P5-*`) hang off wave 3/4 and can trail or lead without blocking it.

## Files that must not be edited by two agents at once

| Files | Issues that write them | Rule |
| --- | --- | --- |
| `darkroom/storage/models.py`, `migrations/` | P2-02 (#13) (all), P7-03 (#39) (MCP tables) | One migration PR open at a time; anything else proposes a model change in its PR description and waits. |
| `darkroom/server/routes.py`, `contract.py`, generated contract/feature-map files | every API issue (P2-03 (#14), P2-05 (#16), P3-07 (#25), P3-08 (#26), P4-01 (#27)–P4-04 (#30), P5-01 (#31), P6-01 (#35), P7-03 (#39)) | Append-only; rebase, regenerate, re-check; merge serially within a wave. |
| `darkroom/server/jobs.py` registrations | P2-06 (#17) (creates), P2-07 (#18), P3-07 (#25), P4-03 (#29), P4-04 (#30), P7-02 (#38) | Append-only; never reorder. |
| `darkroom_browser/selectors.py`, `pages/takeout.py`, `live.py` | P4-01 (#27) (creates), P4-02 (#28), P4-03 (#29), P4-04 (#30), P7-02 (#38) | Browser-worker job issues are in different waves on purpose; keep it that way. |
| `darkroom/providers/exiftool.py` | P2-07 (#18) (creates), P3-06 (#24) (adds `write_xmp`) | Different waves. |
| `darkroom/domain/ingest/run.py` | P3-03 (#21) (CLI stub), P3-07 (#25), P7-02 (#38) | Different waves; P3-08 (#26) must not edit it. |
| `darkroom/domain/dedupe.py` and `confirmed_in_google` writers | P3-05 (#23) only; P3-08 (#26) and P7-04 (#40) call `dedupe.reverse`/resolve, never assign the flag | Enforced by the Semgrep rule P3-05 (#23) adds. |
| `frontend/src/App.svelte` / nav | P2-04 (#15) (creates), P2-05 (#16), P3-08 (#26), P4-02 (#28), P4-03 (#29), P7-03 (#39) | Each page in its own file; nav edits are one line each. |
| `AGENTS.md` promises section | P2-01 (#12) (writes), P3-05 (#23), P3-06 (#24), P4-01 (#27), P6-01 (#35) (each fills in "checked by") | One-line edits; rebase. |
| `.semgrep/darkroom.yml` + examples | P2-01 (#12), P2-03 (#14), P3-05 (#23), P3-06 (#24), P6-01 (#35) | Append a rule and its example pair; never edit another issue's rule. |
| `ios/` | P5-02 (#32), P5-03 (#33), P6-02 (#36) | Serial by dependency already. |

## What the owner does between waves (summary)

1. Before wave 1: decide the name (P1-02 (#10) item 5).
2. During waves 1–4: finish P1-01 (#9) (Immich, folders, Syncthing join, keys, Pocket ID client, Traefik route) and P1-03 (#11) (hand-downloaded export, scrubbed Takeout snapshots, sample files). Answer P1-02 (#10) items 1, 10, 11.
3. After wave 2: run `tools/spikes/compare_hashes.py` and the Immich XMP steps; paste results into P3-01 (#19) and P3-02 (#20). If Takeout lacks Live Photo video halves, answer P1-02 (#10) item 14 before P6-01 (#35) starts.
4. Before wave 5: commit the snapshots; seed people, accounts and Pixels once P2-05 (#16) is deployed.
5. After wave 7: build P5-03 (#33) on a Mac, verify on each iPhone, then do P5-04 (#34) phone by phone.
6. After wave 9: deploy both containers, connect each account through noVNC, run the manual import, review the counts and the review queue; confirm the unmatched-originals alert is quiet (or points at a Pixel folder that isn't backed up).
7. Before wave 11: confirm iCloud Photos is off per iPhone (P1-02 (#10) item 2); decide item 16.
8. After wave 12: enable cleanup per device, verify P6-02 (#36); when a renewal comes due, record P7-01 (#37).

Decisions with proposed defaults (P1-02 (#10) items 3, 4, 9, 10, 11, 12, 13, 15) can wait: an agent uses the default and writes it into `docs/DECISIONS.md`. Items 2, 5, 14 and 16 have no default an agent may assume.

## Standard brief for each Opus agent

Paste this at the start of every session, with the issue number filled in:

> You are working in `AnthonyPluth/Darkroom` on issue #N and nothing else. Read `AGENTS.md`, then `docs/SPEC.md` in full, then the issue; the issue plus the spec is everything you need, and the sections it links are the ones that matter. Keep to the modules the issue names; if you believe something outside them must change, say so in the PR description instead of changing it. Start from `main`, branch `issue/N-<short-slug>`, commit in focused steps with a `Co-Authored-By: Claude <model> <noreply@anthropic.com>` trailer. No comments or docstrings in code; name things so the code says what it does and put the why in the PR. Add or update tests with the change; a safety-critical issue is not done until every numbered invariant has a positive and a negative test. Never put real names, emails, archive ids, photo paths or anything from the owner's instance into code, fixtures, tests, issues or the PR; fixtures are synthetic. If the issue leaves a user-visible decision open and `docs/DECISIONS.md` doesn't settle it, ask in the PR rather than choosing. Before pushing run `make check` and paste its last lines into the PR. Open one PR titled `feat: …` or `fix: …` whose body has a few bullets on what changed, how it was tested, and `Closes #N`; regenerate the feature map and API contract if you added or changed a route, and commit the result. Watch the PR for review comments and CI, fix what's correct, reply to what isn't, and stop when the merge gate is green.

For iOS issues add: "You cannot build or run Xcode. Write the Swift, keep logic in `DarkroomCore` with `swift test` coverage, and end the PR with the owner's verification steps from the issue as a checklist."

For `spike` issues with an agent part add: "Deliver the script and the write-up with an empty Results table; the owner runs it and closes the issue."
