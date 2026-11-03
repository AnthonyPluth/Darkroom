# Runbook

What the owner sets up by hand around the repository. The deployment of the browser worker is in [deploy/browser.md](deploy/browser.md).

## Branch protection

`main` is protected by a ruleset (Settings, Rules, Rulesets, a branch ruleset targeting the default branch; managed in Terraform in the Infrastructure repository):

- Require a pull request before merging, with a review from a Code Owner (`.github/CODEOWNERS`: the workflows, `.claude/`, `AGENTS.md`, `CLAUDE.md`, `SECURITY.md`, `.semgrep/` and `docs/design/` need the owner).
- Require one status check to pass: **Merge gate**, and nothing else.
- Block force pushes and deletions.
- No bypass for apps or bots: Dependabot's pull requests go through the gate too.

Why only "Merge gate" is required: the gate (`.github/workflows/merge-gate.yml`, which calls fleet's reusable merge gate) sets that one commit status on a pull request's head once the newest run of every workflow the pull request started has succeeded (`CI`, `Docker image` and `Security scans` must have run at all), every other app's check run and every commit status has passed, the "Second review" status is set and passing (fleet's agent review; a pull request that isn't an agent's or isn't risky gets a passing one), and no review thread opened by Codex with a P1 or P2 badge is unresolved. It is pending while anything is still running and failing otherwise. Requiring the individual checks again in the ruleset would keep a pull request whose path filters skip a workflow from ever merging, and the list would go stale each time a workflow is added or renamed; the gate's lists live in the workflow, next to the workflows they name. CodeQL isn't required: a private repository on a personal account has no code scanning, so the security scans fail their job on findings instead of uploading them.

## Who reviews a pull request

The owner's decision (Nov 3): Codex reviews every pull request, and the Claude review runs only for security-sensitive ones.

- **Codex** reviews every pull request: the agent comments `@codex review` on the draft right after opening it and again after a substantive push. A thread it opens with a P1 or P2 badge fails "Merge gate" until it is resolved. Resolving a thread starts no workflow (GitHub Actions has no event for it), so after resolving one, run "Merge gate" by hand (Actions, Merge gate, Run workflow, with the pull request's number) or push. `.github/workflows/merge-gate-threads.yml` must stay free of the `pull_request_review_thread` event (fixed in 743bd92): it reacts to a new review or review comment only.
- **Claude** reviews only a risky pull request (labelled `safety-critical` or `security`, or changing `.github/**`, `.claude/**` or a path listed in `.github/workflows/agent-review.yml`), as the "Second review". `agent-review.yml` passes `first-review: false`, which makes fleet's workflow run neither job of the first review: no "Agent review" status and no comment on any pull request. A pull request that isn't risky gets a passing "Second review" ("Not a risky pull request"), so the gate requires `Second review` alone in `required-statuses` in `.github/workflows/merge-gate.yml`.
- **Switching the Claude first review back on** takes two edits in one pull request: delete `first-review: false` from `.github/workflows/agent-review.yml` and add `Agent review` above `Second review` in `required-statuses` of `.github/workflows/merge-gate.yml`. Do both together: requiring "Agent review" while the first review is off leaves every pull request waiting for a status nothing sets, and turning the review on without requiring it lets a blocking finding through.

What else the workflows need, set once:

- Secrets `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (the reviews), the variable `FLEET_BOT_APP_ID` and the secret `FLEET_BOT_PRIVATE_KEY` (the content sync's fleet-bot App, installed on this repository); optionally the variables `AGENT_REVIEW_BUDGET_USD` and `AGENT_REVIEW_MAX_TURNS`.
- Labels `safety-critical` and `security` (they make a pull request risky for the second review), `fleet` and `review-mining` (the "Repeated review findings" issue).

A push straight to `main` is refused by the ruleset; every change lands through a pull request, and merging one releases (`.github/workflows/docker.yml` on `main` works out the next version from the pull request's title, builds the images and publishes the release).
