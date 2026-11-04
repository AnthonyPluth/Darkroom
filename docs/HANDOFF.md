# Darkroom — hand-off plan for Opus coding agents

Covers the 42 GitHub issues listed in `docs/backlog/issues.json`. Each issue body starts with its key. Every child belongs to one epic; `manual`, `decision`, `spike` and `design-signoff` issues are the owner's unless the issue says an agent writes a script or document first. Phase 8 (E8 (#8)) is a pointer epic: the Waypoint integration is tracked as issues in `AnthonyPluth/Waypoint`.

## Ground rules that apply to every wave

- One issue, one agent, one session, one PR. An agent reads `AGENTS.md`, `docs/SPEC.md` and its issue, nothing else is assumed.
- No frontend design ships without the owner's sign-off. The web screens are designed in P2-08 (#16) and the iPhone screens in P5-05 (#34) (Claude produces mockups as design artifacts; the owner signs off in the issue; approved designs are exported under `docs/design/`). Every issue that builds a web page or an app screen depends on the matching design issue and carries the criterion "UI matches the signed-off design …; include screenshots in the PR". P2-04 (#15) and P5-02 (#33) are scaffolds and proceed with placeholders; P2-05 (#17) replaces the theme tokens from the signed-off design before it merges.
- Within a wave, issues touch different trees and can run at the same time. Across waves, a wave starts only when every issue it depends on is merged (not just open as a PR).
- Two PRs that both regenerate the same generated files (`docs/feature-map.json`, `docs/openapi.json`, `frontend/src/lib/api-types.ts`) land one at a time: the second rebases on `main`, re-runs `make feature-map api-contract`, then `make check`.
- Two PRs that both add an Alembic migration never run in the same wave. P2-02 (#13) defines the whole model up front, so a PR that wants a migration says so in its PR.

## Waves

| Wave | Issues (parallel) | Owner steps before the wave | Notes |
| --- | --- | --- | --- |
| 0 | P1-01 (#9), P1-02 (#10), P1-03 (#11) (owner) | — | Runs alongside waves 1–4. P1-02 (#10) item 5 (name) before wave 1 merges; items 1, 10, 11 before wave 5. |
| 1 | P2-01 (#12) | Name decided | Solo: it defines the layout, Makefile, CI and AGENTS.md every later issue relies on. |
| 2 | P2-02 (#13) · P5-02 (#33) · P3-01 (#20) (agent part) · P3-02 (#21) (agent part) | — | Four separate trees: `darkroom/storage/`, `ios/`, `tools/spikes/` + `docs/spikes/` (two different files each). |
| 3 | P2-03 (#14) · P2-06 (#18) | — | Both on P2-02 (#13). `oidc.py`/`server/api/auth` vs `providers/homeassistant.py`, `domain/alerts.py`, `server/jobs.py`. |
| 4 | P2-04 (#15) · P2-07 (#19) · P4-01 (#28) | P1-01 (#9) far enough that the owner can run P3-01 (#20)/P3-02 (#21) scripts on real data | `frontend/` vs `domain/media.py` + `domain/index.py` + `providers/exiftool.py` vs `darkroom_browser/` + `server/api/browser*.py`. P4-01 (#28) and P2-07 (#19) both register scheduler jobs in `server/jobs.py`: append-only, rebase. |
| 4½ | P2-08 (#16) (design: web UI screens; Claude + owner, not a coding agent) | P2-04 (#15) merged (mockups use the real component library) | Owner gate for every web page issue (P2-05 (#17), P3-08 (#27), P4-02 (#29), P4-03 (#30), P4-04 (#31), P6-01 (#37)). Start the design session as soon as P2-04 (#15) merges; sign-off must land before wave 5 merges. |
| 5 | P2-05 (#17) · P3-03 (#22) · P4-02 (#29) | **P2-08 (#16) signed off**; P1-03 (#11) snapshots committed (else P4-02 (#29) works on synthetic ones and tags the tests `snapshot_pending`); P1-02 (#10) item 1 to seed people/Pixels | P2-05 (#17) and P4-02 (#29) both add pages and routes: separate page files, nav edit conflicts are trivial; regenerate contract on rebase. P2-05 (#17) replaces the placeholder theme tokens from `docs/design/web/`. |
| 6 | P5-01 (#32) · P3-04 (#23) · P4-03 (#30) · P5-05 (#34) (design: iPhone screens; Claude + owner) | P3-01 (#20) results pasted (Möbius answer feeds P5-03 (#35)'s README; Live Photo answer → P1-02 (#10) item 14) | P5-01 (#32) and P4-03 (#30) both add routes (device vs exports); P3-04 (#23) is pure domain code. P5-05 (#34) is the owner gate for P5-03 (#35) and P6-02 (#38). |
| 7 | P3-05 (#24) · P4-04 (#31) · P5-03 (#35) | **P5-05 (#34) signed off** (before P5-03 (#35) starts); P1-02 (#10) items 4, 9, 13 confirmed or defaults accepted | Nothing in this wave migrates. P3-05 (#24) adds the `confirmed_in_google` Semgrep rule; P4-04 (#31) must not assign that column. P4-04 (#31) and P5-03 (#35) build UI: screenshots in the PR against the signed-off designs. |
| 8 | P3-06 (#25) · (after P5-03 (#35) verified on each phone: P5-04 (#36), owner) | P3-02 (#21) result (sidecar name form) or accept the default; owner verifies P5-03 (#35) on a Mac/iPhone | P3-06 (#25) adds `providers/exiftool.write_xmp` and two Semgrep rules; nothing else touches `exiftool.py` this wave. |
| 9 | P3-07 (#26) | Deploy `darkroom` + `darkroom-browser` on Unraid; connect one account through noVNC (P4-02 (#29) live); P5-04 (#36) switch under way | Solo: it stitches the stages, adds the unmatched alert and cleanup; `server/jobs.py` and `domain/ingest/run.py` are its own this wave. Afterwards the owner runs `run.py import-takeout` on the hand-downloaded export → Phase 3 "done when". |
| 10 | P3-08 (#27) · P7-02 (#40) | Import from wave 9 reviewed; P7-01 (#39) observation starts whenever a renewal is due (no blocker) | P3-08 (#27) (review/imports UI + reassign, against P2-08 (#16)'s Review and Imports screens) vs P7-02 (#40) (renew job, incremental semantics, runbook). Both touch `domain/ingest/run.py` only if P7-02 (#40)'s incremental changes land first; P3-08 (#27) should not need it. If both do, P7-02 (#40) merges first. |
| 11 | P6-01 (#37) | P1-02 (#10) items 2 and 16 answered; P5-04 (#36) done on at least one phone | Solo and safety-critical: review the invariant tests before merging, not just CI. Its People-page switch follows P2-08 (#16). |
| 12 | P6-02 (#38) | Owner enables cleanup per device only after confirming iCloud Photos is off on it | P6-02 (#38) is iOS only (against P5-05 (#34)'s cleanup screens). E8 (#8) points at the Waypoint repo; nothing to run here. |

Critical path: `P2-01 (#12) → P2-02 (#13) → P2-06 (#18) → P2-07 (#19) → P3-03 (#22) → P3-04 (#23) → P3-05 (#24) → P3-06 (#25) → P3-07 (#26) → P3-08 (#27) → P6-01 (#37) → P6-02 (#38)`, with the two design sign-offs (P2-08 (#16) before wave 5, P5-05 (#34) before wave 7) as owner gates on it. The browser worker (`P4-*`) and the upload side (`P5-*`) hang off wave 3/4 and can trail or lead without blocking it.

## Model per issue

Opus is the default. Fable takes anything security-related (sign-in, device tokens, the worker token, OAuth). Sonnet only does mechanical work. Each issue's "Agent task" template names its model; `CLAUDE.md` (from P2-01 (#12)) carries the same routing.

| Model | Issues |
| --- | --- |
| Fable | P2-03 (#14) (OIDC, sessions, every-route guard) · P4-01 (#28) (worker token, internal API, credential policy) · P5-01 (#32) (device sign-in and upload API) |
| Sonnet | Creating and updating the GitHub issues from `issues.json` · P3-02 (#21) agent part (sample generator and method doc) |
| Opus | Everything else, including P3-05 (#24) (dedupe), P6-01 (#37) (eligibility), P6-02 (#38) (iOS cleanup) |

Owner-only issues (`P1-*`, P2-08 (#16), P5-05 (#34), P5-04 (#36), P7-01 (#39)) have no coding model; P2-08 (#16) and P5-05 (#34) are design sessions with Claude producing the mockups.

## Files that must not be edited by two agents at once

| Files | Issues that write them | Rule |
| --- | --- | --- |
| `darkroom/storage/models.py`, `migrations/` | P2-02 (#13) (all) | One migration PR open at a time; anything else proposes a model change in its PR description and waits. |
| `darkroom/server/routes.py`, `contract.py`, generated contract/feature-map files | every API issue (P2-03 (#14), P2-05 (#17), P3-07 (#26), P3-08 (#27), P4-01 (#28)–P4-04 (#31), P5-01 (#32), P6-01 (#37)) | Append-only; rebase, regenerate, re-check; merge serially within a wave. |
| `darkroom/server/jobs.py` registrations | P2-06 (#18) (creates), P2-07 (#19), P3-07 (#26), P4-03 (#30), P4-04 (#31), P7-02 (#40) | Append-only; never reorder. |
| `darkroom_browser/selectors.py`, `pages/takeout.py`, `live.py` | P4-01 (#28) (creates), P4-02 (#29), P4-03 (#30), P4-04 (#31), P7-02 (#40) | Browser-worker job issues are in different waves on purpose; keep it that way. |
| `darkroom/providers/exiftool.py` | P2-07 (#19) (creates), P3-06 (#25) (adds `write_xmp`) | Different waves. |
| `darkroom/domain/ingest/run.py` | P3-03 (#22) (CLI stub), P3-07 (#26), P7-02 (#40) | Different waves; P3-08 (#27) must not edit it. |
| `darkroom/domain/dedupe.py` and `confirmed_in_google` writers | P3-05 (#24) only; P3-08 (#27) calls `dedupe.reverse`/resolve, never assign the flag | Enforced by the Semgrep rule P3-05 (#24) adds. |
| `frontend/src/App.svelte` / nav | P2-04 (#15) (creates), P2-05 (#17), P3-08 (#27), P4-02 (#29), P4-03 (#30) | Each page in its own file; nav edits are one line each. |
| `frontend/src/app.css` (theme tokens) | P2-04 (#15) (placeholders), P2-05 (#17) (from `docs/design/web/`) | Nobody else edits tokens; a token change is a design change and goes back to P2-08 (#16). |
| `docs/design/` | P2-08 (#16), P5-05 (#34) (owner/Claude design sessions) | Agents read, never write; a needed deviation is raised in the PR and signed off before merge. |
| `AGENTS.md` promises section | P2-01 (#12) (writes), P3-05 (#24), P3-06 (#25), P4-01 (#28), P6-01 (#37) (each fills in "checked by") | One-line edits; rebase. |
| `.semgrep/darkroom.yml` + examples | P2-01 (#12), P2-03 (#14), P3-05 (#24), P3-06 (#25), P6-01 (#37) | Append a rule and its example pair; never edit another issue's rule. |
| `ios/` | P5-02 (#33), P5-03 (#35), P6-02 (#38) | Serial by dependency already. |

## What the owner does between waves (summary)

1. Before wave 1: decide the name (P1-02 (#10) item 5).
2. During waves 1–4: finish P1-01 (#9) (Immich, folders, Syncthing join, keys, Pocket ID client, Traefik route) and P1-03 (#11) (hand-downloaded export, scrubbed Takeout snapshots, sample files). Answer P1-02 (#10) items 1, 10, 11.
3. After wave 2: run `tools/spikes/compare_hashes.py` and the Immich XMP steps; paste results into P3-01 (#20) and P3-02 (#21). If Takeout lacks Live Photo video halves, answer P1-02 (#10) item 14 before P6-01 (#37) starts.
4. After wave 4 (P2-04 (#15) merged): run the web design session (P2-08 (#16)) with Claude, review every screen at phone and desktop widths, sign off in the issue; the approved designs and tokens land under `docs/design/web/`. Wave 5 does not merge before this.
5. Before wave 5: commit the snapshots; seed people, accounts and Pixels once P2-05 (#17) is deployed.
6. During wave 6: run the iPhone design session (P5-05 (#34)), sign off; P5-03 (#35) does not start before this.
7. After wave 7: build P5-03 (#35) on a Mac, check the screens against `docs/design/ios/`, verify on each iPhone, then do P5-04 (#36) phone by phone.
8. After wave 9: deploy both containers, connect each account through noVNC, run the manual import, review the counts and the review queue; confirm the unmatched-originals alert is quiet (or points at a Pixel folder that isn't backed up).
9. Before wave 11: confirm iCloud Photos is off per iPhone (P1-02 (#10) item 2); decide item 16.
10. After wave 12: enable cleanup per device, verify P6-02 (#38) against the signed-off cleanup screens; when a renewal comes due, record P7-01 (#39).
11. Any UI PR whose screenshots deviate from the signed-off design: sign off the change in P2-08 (#16)/P5-05 (#34) (or a follow-up `design-signoff` issue) before it merges, or ask for it to be reverted.

Decisions with proposed defaults (P1-02 (#10) items 3, 4, 9, 10, 11, 12, 13, 15) can wait: an agent uses the default and writes it into `docs/DECISIONS.md`. Items 2, 5, 14 and 16 have no default an agent may assume.

## Standard brief for each Opus agent

Paste this at the start of every session, with the issue number filled in:

> You are working in `AnthonyPluth/Darkroom` on issue #N and nothing else. Read `AGENTS.md`, then `docs/SPEC.md` in full, then the issue; the issue plus the spec is everything you need, and the sections it links are the ones that matter. Keep to the modules the issue names; if you believe something outside them must change, say so in the PR description instead of changing it. Start from `main`, branch `issue/N-<short-slug>`, commit in focused steps with a `Co-Authored-By: Claude <model> <noreply@anthropic.com>` trailer. No comments or docstrings in code; name things so the code says what it does and put the why in the PR. Add or update tests with the change; a safety-critical issue is not done until every numbered invariant has a positive and a negative test. Never put real names, emails, archive ids, photo paths or anything from the owner's instance into code, fixtures, tests, issues or the PR; fixtures are synthetic. If the issue leaves a user-visible decision open and `docs/DECISIONS.md` doesn't settle it, ask in the PR rather than choosing. If the issue builds a web page or an app screen, match the signed-off design under `docs/design/` exactly, put screenshots in the PR, and treat any change to layout, flow or visual style as needing the owner's sign-off before merge; never adjust theme tokens on your own. Before pushing run `make check` and paste its last lines into the PR. Open one PR titled `feat: …` or `fix: …` whose body has a few bullets on what changed, how it was tested, and `Closes #N`; regenerate the feature map and API contract if you added or changed a route, and commit the result. Watch the PR for review comments and CI, fix what's correct, reply to what isn't, and stop when the merge gate is green.

For iOS issues add: "You cannot build or run Xcode. Write the Swift, keep logic in `DarkroomCore` with `swift test` coverage, and end the PR with the owner's verification steps from the issue as a checklist. Read the `iOS` check's annotations on the PR (file:line compile errors from the unsigned simulator build): a red `iOS` check blocks the merge."

For `spike` issues with an agent part add: "Deliver the script and the write-up with an empty Results table; the owner runs it and closes the issue."
