# Fleet: shared agent build safeguards for Darkroom, Waypoint and Runway

Draft only. Nothing has been created on GitHub: no repository, issue, label, branch, commit or push. `AnthonyPluth/fleet` doesn't exist yet; `FLEET-SETUP` is the owner creating it.

## Files

| File | What |
| --- | --- |
| `fleet/issues.json` | 13 issues that build fleet v1, in creation order |
| `waypoint/issues.json` | 4 issues: decision, adopt workflows, adopt content, deep-scan runbook |
| `waypoint/edits.json` | 2 edits to the photos issues, keyed `PHOTOS-DEMO` and `PHOTOS-UI-SETTINGS` |
| `runway/issues.json` | 4 issues: adopt workflows, adopt content, provider fakes, deep-scan runbook |
| `darkroom/issues.json` | 7 new issues (keys continue each phase's numbering) |
| `darkroom/edits.json` | 8 edits to existing issues #12, #24, #26, #27, #33, #35, #37, #38 |

Conventions in the files:

- `{{KEY}}` in a body is a new issue in the same repository; replace it with the issue number when creating. Darkroom's existing issues are `#N`. Waypoint's photos issues are referred to by key (they exist on GitHub as #482–#499, but the bodies don't use the numbers).
- Cross-repository references are written as text with the key in backticks (`AnthonyPluth/fleet` v1.0.0 (fleet issue `FLEET-RELEASE`)); turn them into `AnthonyPluth/fleet#N` links when creating.
- `depends_on`: same-repo keys plain (`P2-10`), Darkroom's existing issues as `#N`, other repos as `AnthonyPluth/fleet:FLEET-RELEASE`.
- `darkroom/edits.json`: `append_section` goes before the `**Acceptance criteria**` line; every `replace.old` is an exact single line of the body in `docs/backlog/issues.json` (checked: each occurs exactly once; #12's acceptance line was updated in `docs/backlog/issues.json` to match the live issue). #12's live body may have gained an **AGENTS.md outline** section; the edit doesn't touch it, and its append section says how the outline and the fleet blocks fit.
- `waypoint/edits.json`: same shape keyed by `key`; those bodies use `## ` headings, so `append_section` goes before the `## Acceptance criteria` line. Both `replace.old` lines are checked against `docs/waypoint/issues.json`.
- Every issue body has Context, Scope, Out of scope, Acceptance criteria (`- [ ]`), Tests, Depends on and Model. Fable is named for everything touching the review workflows, CI secrets, untrusted pull request content or security scanning; Opus otherwise; no issue needed Sonnet. No `CLAUDE.md` routing is changed anywhere.

## Key decisions

- **fleet first, then adoptions.** fleet v1 is the union of Waypoint and Runway, with each drift resolved in `docs/drift.md` (table below). App repos call fleet's reusable workflows pinned by SHA with a `# vX.Y.Z` comment and pass secrets by name.
- **Darkroom #12: fold in, plus one new issue.** #12 (Opus) now consumes fleet instead of copying: the `tools/fleet` shim and `[tool.fleet]` replace `no_comments.py`, `coverage_floor.py` and `fleet_checks.py`; it vendors `.claude/` and syncs the `AGENTS.md` blocks. That makes #12 smaller, not bigger. The parts that hold secrets, read untrusted PR content or decide merges (agent review with the second review, merge gate, security scans, CODEOWNERS, ruleset) move to the new **P2-09** (Fable, owner rule), which merges right after #12 and before wave 2.
- **Plugin: published, vendored, not enabled.** Claude Code's docs ("What carries over from your setup" in cloud environments) say cloud sessions don't install plugins a repository enables under `enabledPlugins`/`extraKnownMarketplaces`, while `.claude/skills`, `.claude/agents` and settings hooks in the clone do load. The agents run in cloud sessions, so `fleet sync` vendors the plugin's skills, reviewer agent and session-start hook into each repo. Each repo declares the marketplace pinned to the tag but doesn't enable the plugin, which would load every skill twice in a local session. This departs from the plan you sent.
- **Two pins per consumer, two bots.** Dependabot bumps the workflow pins (`fleet` group). A consumer-side `sync.yml` then opens the matching "fleet content" PR (AGENTS blocks, vendored files, `.fleet/version`) with a `fleet-bot` GitHub App token, because PRs opened with `GITHUB_TOKEN` start no CI. The App has no Workflows permission. `fleet drift` lets content trail the pins within one major version.
- **FLEET_REF stamping.** A called workflow can't know its own ref (`github.workflow_ref`/`workflow_sha` are the caller's), so each reusable workflow carries `FLEET_REF: vX.Y.Z`, stamped by the release PR, and fetches its scripts at that tag. Tags are immutable by ruleset.
- **Second review.** A separate job and status, "Second review", on `claude-fable-5-1`. It runs when a PR has the label `safety-critical` or `security`, or touches the repo's listed paths (`.github/**` and `.claude/**` always count). Otherwise it passes as "not risky", so the merge gate can require it on every PR. Label events rerun only this job. Removing a label counts only when the repo owner does it. The first review stays Sonnet.
- **Deep scans can't be automated.** The claude-security plugin (0.11.0) asks a cost confirmation that, by design, only the person's own request can answer, never repository text or a scheduled task. It refuses in non-interactive sessions and needs a live session. So the scans are `manual` runbooks. Darkroom gets two on-demand scans (P2-12 before the first deploy, P6-03 before iPhone cleanup). Waypoint and Runway get a quarterly reminder issue from fleet's `deep-scan-due.yml`. On the public repos, findings go to draft security advisories, never public issues.
- **Repeated mistakes become checks.** fleet's shared "Mistakes that keep coming back" block opens with the rule. Review findings now carry a `kind` (the AGENTS item they fall under) in a hidden comment line. `fleet review-mine` runs monthly and deterministically (no model), and rewrites one open "Repeated review findings" issue per repo, listing kinds seen on 2+ PRs in 90 days with the check each should become.

## Order to apply

1. **Owner:** `FLEET-SETUP` (create the repo and its rulesets, create the `fleet-bot` App and install it on the three app repos, add secrets, create labels).
2. **fleet v1** (create `fleet/issues.json` in order). Waves: `FLEET-SCAFFOLD` → `FLEET-TOOLS`, `FLEET-MERGE-GATE`, `FLEET-SECURITY` → `FLEET-REVIEW`, `FLEET-OPTIONAL`, `FLEET-AGENTS-MD` → `FLEET-SECOND-REVIEW`, `FLEET-PLUGIN` → `FLEET-SYNC`, `FLEET-MINING` → `FLEET-RELEASE` (owner merges; v1.0.0 tagged).
3. **Now, independent of fleet:** apply `waypoint/edits.json` (PHOTOS-DEMO is wave 4 of the photos plan, so it may already be under way; check first). Create `RW-PROVIDER-FAKES`. Create `WP-REVIEW-DECISION` so the owner can answer early.
4. **Darkroom:** apply `darkroom/edits.json` and create `darkroom/issues.json` before #12 starts. #12 waits for fleet v1.0.0. Wave placement in `docs/HANDOFF.md` terms:
   - **P2-09:** wave 1½, alone, after #12 and before wave 2.
   - **P3-09:** wave 2 (only `tests/fakes/`).
   - **P2-10:** wave 5 (needs #13, #14, #18 and #19).
   - **P2-11:** wave 6 (needs #17 and P2-10).
   - **P3-10:** wave 11, beside #37 (it touches only the demo and verify files).
   - **P2-12:** owner, before the first deploy after #17.
   - **P6-03:** owner, after wave 12 and before cleanup is enabled on any phone.

   #33 now also depends on P2-09, which is still before wave 2. Update the HANDOFF iOS brief as #33's edit says.
5. **Runway adoption:** `RW-FLEET-WORKFLOWS` → `RW-FLEET-CONTENT`, then `RW-DEEP-SCAN`. Runway goes first because its review already runs, so adopting fleet changes the least there.
6. **Waypoint adoption:** `WP-REVIEW-DECISION` answered → `WP-FLEET-WORKFLOWS` → `WP-FLEET-CONTENT`, then `WP-DEEP-SCAN`. `WP-FLEET-CONTENT` and PHOTOS-PROXY both edit `AGENTS.md` (different sections). Whichever lands second merges `main` first.

Each adoption PR changes `agent-review.yml`, but `pull_request_target` runs main's copy, so the new review first runs on the next PR. The owner reviews the adoption PRs by hand (CODEOWNERS).

## Issues and edits mapped to items 1–5

Items: 1 shared Claude setup and independent review gate, 2 verification against fakes, 3 iOS build feedback, 4 second review and deep scans, 5 fix mistakes with checks.

### fleet (AnthonyPluth/fleet)

| Key | Title | Items | Model |
| --- | --- | --- | --- |
| FLEET-SETUP | Owner setup: repo, rulesets, fleet-bot App, secrets | 1 | Owner |
| FLEET-SCAFFOLD | Scaffold, fleet's CI and self-test, FLEET_REF convention | 1 | Opus |
| FLEET-TOOLS | Shared Python tools (fleet checks union, no-comments, coverage floor, next version) | 1, 5 | Opus |
| FLEET-REVIEW | Reusable first review; findings carry a `kind` | 1, 5 | Fable |
| FLEET-SECOND-REVIEW | Fable second review as its own required status | 4 | Fable |
| FLEET-MERGE-GATE | Reusable merge gate | 1, 4 | Fable |
| FLEET-SECURITY | Reusable security scans; deep-scan runbook and reminder | 4 | Fable |
| FLEET-OPTIONAL | Opt-in verify (control smoke / verify flows), screenshot diff, cancel on close, tidy releases | 1, 2 | Fable |
| FLEET-AGENTS-MD | Shared AGENTS.md blocks, the repeated-mistake rule, sync and drift | 1, 5 | Opus |
| FLEET-PLUGIN | Marketplace and plugin (skills, reviewer agent, hook), vendored for cloud sessions | 1, 5 | Opus |
| FLEET-SYNC | Reusable `checks.yml` and `sync.yml` (fleet-bot PRs) | 1 | Fable |
| FLEET-MINING | Monthly "Repeated review findings" report | 5 | Fable |
| FLEET-RELEASE | Release process, adoption guide, v1.0.0 | 1 | Opus |

### Waypoint

| Key / edit | Title | Items | Model |
| --- | --- | --- | --- |
| WP-REVIEW-DECISION | Turn the Claude review back on beside Codex; opt-ins | 1, 4 | Owner |
| WP-FLEET-WORKFLOWS | Adopt fleet workflows (reviews, merge gate, security, control smoke, mining, deep-scan reminder, sync) | 1, 4, 5 | Fable |
| WP-FLEET-CONTENT | Adopt fleet tools, vendored `.claude/`, AGENTS blocks with the rule | 1, 5 | Opus |
| WP-DEEP-SCAN | Owner runbook: first deep scan, then quarterly | 4 | Owner |
| edit PHOTOS-DEMO | Demo `original`, Test and Sync answer locally; verify-waypoint gotcha | 2 | (unchanged) |
| edit PHOTOS-UI-SETTINGS | `fake_immich` flow step and `photos-connect` flows | 2 | (unchanged) |

Photos gap analysis: PHOTOS-DEMO already makes `make verify` show photos with no Immich (a `demo` source, placeholder thumbnails and previews, sync skipped). What was missing: the `original` route for a demo source (it would try a connection, which shows up as a 5xx in a session), Test and Sync now on a demo library, any way to drive Connect → Test in a flow, and the verify-waypoint skill's notes. Nothing else needed changing.

### Runway

| Key | Title | Items | Model |
| --- | --- | --- | --- |
| RW-FLEET-WORKFLOWS | Adopt fleet workflows (reviews, merge gate, security, verify flows, screenshot diff, mining, deep-scan reminder, sync) | 1, 4, 5 | Fable |
| RW-FLEET-CONTENT | Adopt fleet tools, vendored `.claude/`, AGENTS blocks with the rule | 1, 5 | Opus |
| RW-PROVIDER-FAKES | Loopback fakes for SimpleFIN and price quotes in make verify and tools/control | 2 | Fable |
| RW-DEEP-SCAN | Owner runbook: first deep scan, then quarterly | 4 | Owner |

Runway gap analysis: `make verify` runs demo data behind a dead proxy and fakes no provider. Connect, Sync now and sync errors can only be tested by patching the provider in unit tests. Plaid stays out (its Link widget loads from Plaid's CDN, and Plaid's sandbox covers it manually).

### Darkroom

| Key / edit | Title | Items | Model |
| --- | --- | --- | --- |
| edit #12 | Consume fleet (shim, `[tool.fleet]`, vendored `.claude/`, AGENTS blocks with the rule); security workflows move to P2-09; no CodeQL | 1, 3, 5 | Opus (unchanged) |
| P2-09 | Review and merge gates through fleet; second-review paths and `invariants.md`; ruleset | 1, 4, 5 | Fable |
| P2-10 | Demo dataset and demo server on a frozen clock | 2 | Fable (demo-only sign-in) |
| P2-11 | `tools/control`, `make verify`, verify-darkroom skill | 2 | Opus |
| P3-09 | Fake Immich (serves libraries, records rescans) | 2 | Opus |
| P3-10 | Demo import journey: fixture export → Imports and review queue screenshots | 2 | Opus |
| P2-12 | Owner runbook: deep scan before the first deploy | 4 | Owner |
| P6-03 | Owner runbook: deep scan before iPhone cleanup is turned on | 4 | Owner |
| edit #33 | `iOS` workflow (unsigned simulator build + `swift test`) is a required merge-gate workflow | 3 | Opus (unchanged) |
| edit #35 | Acceptance: required `iOS` check green | 3 | (unchanged) |
| edit #38 | Second review; required `iOS` check | 3, 4 | (unchanged) |
| edit #24, #37 | Second review; invariants into `invariants.md` | 4 | (unchanged) |
| edit #27 | Second review (it's `safety-critical` too); screenshots from `make verify` | 2, 4 | (unchanged) |
| edit #26 | Tests use the fake Immich from P3-09 | 2 | (unchanged) |

What #12 and #33 say for item 3: #12's workflows line names `ios.yml` as #33's. #33 makes `ios.yml` mandatory. It runs a `changes` job and, only when `ios/` changed, a pinned `macos-15` build with XcodeGen, `swift test` and an unsigned `xcodebuild` for the simulator. Errors appear as file:line annotations through `xcbeautify`. In the same PR, #33 adds `iOS` to the merge gate's required workflows. Requiring it any earlier would hold every PR waiting for a workflow that doesn't exist.

## New labels

| Repo | New | Existing ones used |
| --- | --- | --- |
| AnthonyPluth/fleet | `manual`, `security`, `safety-critical`, `workflows`, `plugin`, `tools`, `release`, `review-mining` | (new repo) |
| Waypoint | `fleet`, `safety-critical`, `review-mining` | `decision`, `manual`, `security` |
| Runway | `fleet`, `security`, `safety-critical`, `manual`, `review-mining` | `enhancement` |
| Darkroom | `security`, `fleet`, `review-mining` | `infra`, `backend`, `frontend`, `ingest`, `manual`, `safety-critical` |

`safety-critical` and `security` are also the second review's trigger labels, so they must exist in every repo. In Darkroom, consider adding `security` to #14, #28, #32 and #41. Their modules are on P2-09's path list anyway.

## Drift resolved in fleet

| Piece | Waypoint | Runway | fleet takes | Why |
| --- | --- | --- | --- | --- |
| Agent review | Disabled; Codex comments; no required status | On, required | On, required, plus the second review | An independent review gate is the point; Codex comments don't gate merges. Waypoint decides in WP-REVIEW-DECISION (`first-review: false` is available) |
| `ready_for_review` | Not a trigger | Triggers a review | Triggers one only if the head has no review yet | Runway's coverage without paying twice for a reviewed draft |
| `agent_review.py` | Never omits fixtures/demo/sample data; more image types | Omits the forecast golden fixture | Waypoint's rules, plus the caller's `generated` list, where an explicit entry wins | The privacy check is the review's job; Runway's golden fixture stays out by listing it |
| Review prompt | Travel wording | Finance wording | One generic prompt plus each app's `context.md` | One prompt to maintain |
| Review model | Sonnet | Sonnet ("never Opus", owner decision) | Sonnet first; Fable second on risky PRs | The owner's new rule; recorded in the workflow header |
| `merge-gate.sh` | Requires no status | Requires Agent review; ignores Preview and Screenshot diff | One script; required and ignored lists are inputs; triggers stay in the caller | Called workflows can't declare triggers |
| `security.yml` | `vars.RUNS_ON` option | Hosted only; adds `extension/` | Inputs for paths and the runner; `sarif-upload`/`scorecard` switches | Darkroom uploads SARIF since #90 |
| `fleet_checks.py` | Test-removal trailers, `# hosted:` rule, parser fixtures | `EARLY_ENV` rule, a11y baseline, flow inheritance | The union of shared checks; app-only checks via `extra-checks` | Each gains the other's general checks |
| `no_comments.py`, `coverage_floor.py` | Present | Absent | Opt-in | Runway's convention allows comments |
| Session-start hook | Venv-version guard, `npm install --no-save` | `node_modules` completeness check | Union, with `--no-save` | A session must not rewrite a lockfile |
| AGENTS "Git and PRs" | Open a PR for every change | Open a PR only when asked | Every change | Every push to `main` releases, so all work lands through PRs |
| control-smoke / verify / screenshot-diff | control-smoke | verify flows, screenshot-diff | Opt-in reusable workflows | Each repo keeps what it runs today |
| `preview.yml` | — | Present | Stays in Runway | Runway-specific deploy; nothing to share yet |
| `docker.yml`, `tools/control`, verify-* skills | Own | Own | Stay per app (v2 candidates) | Hundreds of lines of app-specific drift |

## Surprising findings

1. **Waypoint has no review gate today.** `agent-review.yml`'s trigger is commented out ("Codex reviews pull requests now"), `merge-gate.sh` has `REQUIRED_STATUSES=()`, and AGENTS.md tells agents to watch Codex comments.
2. **Cloud sessions ignore repo-declared plugins**, so the marketplace plan alone wouldn't reach the agents. The vendoring decision above follows from that.
3. **claude-security refuses unattended runs** by design.
4. **Darkroom had no CodeQL or SARIF uploads** while it was a private repo on a personal account. It is public now and both are back (#90).
5. **Runway records an owner decision against non-Sonnet reviews** ("never Opus, security-sensitive changes included"). The Fable second review supersedes it, and the adoption issue says so.
6. **Waypoint's `CLAUDE.md` defaults to Sonnet** for everything, security work included. Runway's says cheapest-first. Both differ from the owner's Opus/Fable rule these issues follow. `CLAUDE.md` is left untouched as asked.
7. **The photos issues already exist** (Waypoint #482–#499), so the PHOTOS edits apply to live issues.
