# Runbook

What the owner sets up by hand around the repository. The deployment of the browser worker is in [deploy/browser.md](deploy/browser.md).

## Branch protection

`main` is protected by a ruleset (Settings, Rules, Rulesets, a branch ruleset targeting the default branch; managed in Terraform in the Infrastructure repository):

- Require a pull request before merging, with a review from a Code Owner (`.github/CODEOWNERS`: the workflows, `.claude/`, `AGENTS.md`, `CLAUDE.md`, `SECURITY.md`, `.semgrep/` and `docs/design/` need the owner).
- Require one status check to pass: **Merge gate**, and nothing else.
- Block force pushes and deletions.
- No bypass for apps or bots: Dependabot's pull requests go through the gate too.

Why only "Merge gate" is required: the gate (`.github/workflows/merge-gate.yml`, which calls fleet's reusable merge gate) sets that one commit status on a pull request's head once the newest run of every workflow the pull request started has succeeded (`CI`, `Docker image` and `Security scans` must have run at all), every other app's check run and every commit status has passed, the "Agent review" and "Second review" statuses are set and passing (fleet's agent review; a pull request that isn't an agent's or isn't risky gets a passing one), and no review thread opened by Codex with a P1 or P2 badge is unresolved. It is pending while anything is still running and failing otherwise. Requiring the individual checks again in the ruleset would keep a pull request whose path filters skip a workflow from ever merging, and the list would go stale each time a workflow is added or renamed; the gate's lists live in the workflow, next to the workflows they name. CodeQL isn't required: a private repository on a personal account has no code scanning, so the security scans fail their job on findings instead of uploading them.

What else the workflows need, set once:

- Secrets `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (the reviews), the variable `FLEET_BOT_APP_ID` and the secret `FLEET_BOT_PRIVATE_KEY` (the content sync's fleet-bot App, installed on this repository); optionally the variables `AGENT_REVIEW_BUDGET_USD` and `AGENT_REVIEW_MAX_TURNS`.
- Labels `safety-critical` and `security` (they make a pull request risky for the second review), `fleet` and `review-mining` (the "Repeated review findings" issue).

A push straight to `main` is refused by the ruleset; every change lands through a pull request, and merging one releases (`.github/workflows/docker.yml` on `main` works out the next version from the pull request's title, builds the images and publishes the release).
