# Runbook

What the owner sets up by hand around the repository. The deployment of the browser worker is in [deploy/browser.md](deploy/browser.md).

## Branch protection

`main` is protected by a ruleset (Settings, Rules, Rulesets, a branch ruleset targeting the default branch; managed in Terraform in the Infrastructure repository):

- Require a pull request before merging, with a review from a Code Owner (`.github/CODEOWNERS`: the workflows, `.claude/`, `AGENTS.md`, `CLAUDE.md`, `SECURITY.md`, `.semgrep/` and `docs/design/` need the owner).
- Require one status check to pass: **Merge gate**, and nothing else.
- Block force pushes and deletions.
- No bypass for apps or bots: Dependabot's pull requests go through the gate too.

Why only "Merge gate" is required: the gate (`.github/workflows/merge-gate.yml`, which calls fleet's reusable merge gate) sets that one commit status on a pull request's head once the newest run of every workflow the pull request started has succeeded (`CI`, `Docker image`, `Security scans` and `CodeQL` must have run at all), every other app's check run and every commit status has passed, the "Second review" status is set and passing (fleet's agent review; a pull request that isn't an agent's or isn't risky gets a passing one), Codex has reviewed the head commit, and no review thread opened by Codex with a P1 or P2 badge is unresolved. It is pending while anything is still running and failing otherwise. Requiring the individual checks again in the ruleset would keep a pull request whose path filters skip a workflow from ever merging, and the list would go stale each time a workflow is added or renamed; the gate's lists live in the workflow, next to the workflows they name. The repository is public (the owner's decision, #90), so code scanning works: CodeQL's default setup runs, `merge-gate.yml` also re-runs the gate when the `CodeQL` workflow completes, and Semgrep and Trivy upload SARIF (`sarif-upload: true` in `.github/workflows/security.yml`). If CodeQL's default setup is turned off, take `CodeQL` out of `required-workflows` and the `workflow_run` list in `merge-gate.yml`, or every pull request waits for it.

How Semgrep and Trivy findings gate a merge: before #90 (`sarif-upload: false`) their jobs failed on findings, so "Security scans" failed and "Merge gate" went red. The ruleset's `code_scanning` rule names only the CodeQL tool, so it does not hold Semgrep or Trivy results. With `sarif-upload: true` the findings go to the Security tab; whether those jobs still fail on a finding is decided by fleet's reusable `security.yml` at the pinned commit, which could not be read when this was written, so it is not verified that blocking is unchanged. Check it on the first pull request with a finding, and if the jobs no longer fail, add Semgrep and Trivy to the ruleset's code scanning tools.

## Who reviews a pull request

The owner's decision (Nov 3, recorded in #76): Codex reviews every pull request, and the Claude review runs only for security-sensitive ones. Since fleet v2.0.0 this is fleet's default, so the callers carry only what is Darkroom's own.

- **Codex** reviews every pull request: the agent comments `@codex review` on the draft right after opening it and again after a substantive push. `.github/workflows/merge-gate.yml` passes `require-codex-review: true`, so "Merge gate" stays pending ("Codex review of this commit") until Codex has reviewed the head commit, by a review on it or its own comment naming it; a review of an older commit doesn't count, and Dependabot's pull requests are exempt. A thread Codex opens with a P1 or P2 badge fails "Merge gate" until it is resolved. `.github/workflows/merge-gate-threads.yml` starts the gate when a review or review comment arrives (job `decide`) and when Codex comments on a pull request (job `codex-comment`, on `issue_comment`, since a review that finds nothing arrives as a comment). Resolving a thread starts no workflow (GitHub Actions has no event for it), so after resolving one, run "Merge gate" by hand (Actions, Merge gate, Run workflow, with the pull request's number) or push. The threads caller must not name `pull_request_review_thread`, which GitHub Actions doesn't have (`fleet checks` fails it).
- **Claude** reviews only a risky pull request (labelled `safety-critical` or `security`, or changing `.github/**`, `.claude/**` or a path listed in `.github/workflows/agent-review.yml`), as the "Second review". fleet's `first-review` defaults to `false`, so `agent-review.yml` runs no first review: no "Agent review" status and no comment on any pull request. A pull request that isn't risky gets a passing "Second review" ("Not a risky pull request"), and fleet's merge gate requires `Second review` alone by default (`required-statuses`), failing on a blocking one.
- **Switching the Claude first review back on** takes two edits in one pull request: pass `first-review: true` in `.github/workflows/agent-review.yml` and pass `required-statuses` listing `Agent review` and `Second review` in `.github/workflows/merge-gate.yml`. Do both together: requiring "Agent review" while the first review is off leaves every pull request waiting for a status nothing sets, and turning the review on without requiring it lets a blocking finding through.

What else the workflows need, set once:

- Secrets `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (the reviews), the variable `FLEET_BOT_APP_ID` and the secret `FLEET_BOT_PRIVATE_KEY` (the content sync's fleet-bot App, installed on this repository); optionally the variables `AGENT_REVIEW_BUDGET_USD` and `AGENT_REVIEW_MAX_TURNS`.
- Labels `safety-critical` and `security` (they make a pull request risky for the second review), `fleet` and `review-mining` (the "Repeated review findings" issue).

A push straight to `main` is refused by the ruleset; every change lands through a pull request, and merging one releases (`.github/workflows/docker.yml` on `main` works out the next version from the pull request's title, builds the images and publishes the release).
