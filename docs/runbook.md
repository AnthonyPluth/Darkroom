# Runbook

What the owner sets up by hand around the repository, and what to do when the Takeout import needs a person. The deployment of the browser worker is in [deploy/browser.md](deploy/browser.md).

## Branch protection

`main` is protected by a ruleset (Settings, Rules, Rulesets, a branch ruleset targeting the default branch; managed in Terraform in the Infrastructure repository):

- Require a pull request before merging, with a review from a Code Owner (`.github/CODEOWNERS`: the workflows, `.claude/`, `AGENTS.md`, `CLAUDE.md`, `SECURITY.md`, `.semgrep/` and `docs/design/` need the owner).
- Require one status check to pass: **Merge gate**, and nothing else.
- Block force pushes and deletions.
- No bypass for apps or bots: Dependabot's pull requests go through the gate too.

Why only "Merge gate" is required: the gate (`.github/workflows/merge-gate.yml`, which calls fleet's reusable merge gate) sets that one commit status on a pull request's head once the newest run of every workflow the pull request started has succeeded (`CI`, `Docker image`, `Security scans` and `CodeQL` must have run at all), every other app's check run and every commit status has passed, the "Second review" status is set and passing (fleet's agent review; a pull request that isn't an agent's or isn't risky gets a passing one), Codex has reviewed the head commit, and no review thread opened by Codex with a P1 or P2 badge is unresolved. It is pending while anything is still running and failing otherwise. Requiring the individual checks again in the ruleset would keep a pull request whose path filters skip a workflow from ever merging, and the list would go stale each time a workflow is added or renamed; the gate's lists live in the workflow, next to the workflows they name. The repository is public (the owner's decision, #90), so code scanning works. CodeQL's default setup runs (its runs have the event `dynamic`, which `merge-gate.yml` accepts for a run tied to a pull request) and re-runs the gate when it completes; the ruleset's `code_scanning` rule enforces CodeQL's results. Semgrep and Trivy keep failing their job on findings, which fails "Security scans" and so "Merge gate"; their SARIF upload stays off (`sarif-upload: false` in `.github/workflows/security.yml`) until the ruleset (Terraform, Infrastructure repository) also lists those tools in its code scanning rule, or fleet is confirmed to keep failing the jobs on findings when it uploads. CodeQL's default setup does not scan pull requests from forks, so for a pull request whose head repository is not this one (known from the `workflow_run` event) `merge-gate.yml` lists `Security scans` a second time instead of `CodeQL`, which requires nothing more; a run started by hand (`workflow_dispatch`) or by a check suite cannot tell and still requires `CodeQL`, so a fork's pull request is gated by a `workflow_run` evaluation only. If CodeQL's default setup is turned off, take `CodeQL` out of `required-workflows` and the `workflow_run` list in `merge-gate.yml`, or every pull request waits for it.

## Who reviews a pull request

The owner's decision (Nov 3, recorded in #76): Codex reviews every pull request, and the Claude review runs only for security-sensitive ones. Since fleet v2.0.0 this is fleet's default, so the callers carry only what is Darkroom's own.

- **Codex** reviews every pull request: the agent comments `@codex review` on the draft right after opening it and again after a substantive push. `.github/workflows/merge-gate.yml` passes `require-codex-review: true`, so "Merge gate" stays pending ("Codex review of this commit") until Codex has reviewed the head commit, by a review on it or its own comment naming it; a review of an older commit doesn't count, and Dependabot's pull requests are exempt. A thread Codex opens with a P1 or P2 badge fails "Merge gate" until it is resolved. `.github/workflows/merge-gate-threads.yml` starts the gate when a review or review comment arrives (job `decide`) and when Codex comments on a pull request (job `codex-comment`, on `issue_comment`, since a review that finds nothing arrives as a comment). Resolving a thread starts no workflow (GitHub Actions has no event for it), so after resolving one, run "Merge gate" by hand (Actions, Merge gate, Run workflow, with the pull request's number) or push. The threads caller must not name `pull_request_review_thread`, which GitHub Actions doesn't have (`fleet checks` fails it).
- **Claude** reviews only a risky pull request (labelled `safety-critical` or `security`, or changing `.github/**`, `.claude/**` or a path listed in `.github/workflows/agent-review.yml`), as the "Second review". fleet's `first-review` defaults to `false`, so `agent-review.yml` runs no first review: no "Agent review" status and no comment on any pull request. A pull request that isn't risky gets a passing "Second review" ("Not a risky pull request"), and fleet's merge gate requires `Second review` alone by default (`required-statuses`), failing on a blocking one.
- **Switching the Claude first review back on** takes two edits in one pull request: pass `first-review: true` in `.github/workflows/agent-review.yml` and pass `required-statuses` listing `Agent review` and `Second review` in `.github/workflows/merge-gate.yml`. Do both together: requiring "Agent review" while the first review is off leaves every pull request waiting for a status nothing sets, and turning the review on without requiring it lets a blocking finding through.

What else the workflows need, set once:

- Secrets `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (the reviews), the variable `FLEET_BOT_APP_ID` and the secret `FLEET_BOT_PRIVATE_KEY` (the content sync's fleet-bot App, installed on this repository); optionally the variables `AGENT_REVIEW_BUDGET_USD` and `AGENT_REVIEW_MAX_TURNS`.
- Labels `safety-critical` and `security` (they make a pull request risky for the second review), `fleet` and `review-mining` (the "Repeated review findings" issue).

A push straight to `main` is refused by the ruleset; every change lands through a pull request, and merging one releases (`.github/workflows/docker.yml` on `main` works out the next version from the pull request's title, builds the images and publishes the release).

## Takeout import: when it needs a person

The browser worker is the fragile part; everything after the inbox works without it. Each alert below names the account or export it is about, and the Exports page shows the same jobs.

### A job is waiting for sign-in

Alert "Google sign-in needed" (or "Wrong Google account"). The worker never types a password or a 2FA code; it stops and waits.

1. Open the link in the alert (the `VNC_URL` browser view, behind the same sign-in as Darkroom).
2. Sign in to the Google account the alert names, finish any 2FA prompt, and leave the browser on a Google page.
3. Do nothing else: the waiting job notices the signed-in page and carries on, and the alert closes. If the job already timed out, press Connect on the Exports page.

"Wrong Google account" means the browser is signed in as someone else: sign out there and sign in as the named account.

### An export expired

Takeout keeps an export for about a week. The alert "A Takeout export is about to expire" means Darkroom has not finished downloading it; an export marked expired on the Exports page is gone.

- If it is only about to expire, check that the worker is running and has not hit a failure alert (below). The daily check re-queues a download that failed, up to its limits.
- If it expired, download it by hand while it is listed or ask Takeout for a new export, then hand-drop the parts (below). The scheduled exports continue every 2 months regardless.

### Google changed a page

Alert "Darkroom needs a look" with a link to the capture of the failed job. The capture is a screenshot and a scrubbed copy of the page (the screenshot is the link, else the page), at `/api/browser/failures/<job>/screenshot.png` and `/api/browser/failures/<job>/page.html`, and on the photo share under `_browser/<account>/failures/<job>/`.

1. Open the capture and compare it with what the worker expected (the job's message names the step, for example `UnexpectedPage` or a missing control).
2. Every selector and label the worker looks for is in `darkroom_browser/selectors.py`. Change the one that moved, update the matching page fixture and test under `tests/browser/`, and deploy a new worker image.
3. Run the step again from the Exports page (Connect, Create schedule or Check exports now; downloads queue themselves). Until the fix is deployed, use the fallback: download the parts by hand and hand-drop them.

### Hand-dropping an export

Put the Takeout `.zip` parts, all of one export, in `<photos>/_inbox/<person>/manual/` and run, on the Darkroom host:

```
python run.py import-takeout --person <slug> [--account <google email>] [--dir <other folder>]
```

It registers the parts as a manual export for that person's Google account and runs the same pipeline as a downloaded one: unpack, dedupe, place, sidecars, Immich rescan, summary alert. `--unpack-only` stops after unpacking. Importing the same export twice is refused.

### The schedule is ending, and a failed renewal

About 3 weeks before the schedule ends, Darkroom opens "Takeout schedule ending" and queues one renew job, which creates next year's schedule. When it succeeds the alert closes and "Takeout schedule renewed" confirms the new end date. The first export after a renewal is a full export again; keep disk headroom for about twice your largest library while it is imported.

A failed renewal is not retried (the owner's decision, #40). The "Takeout schedule ending" alert closes and "Takeout schedule not renewed" replaces it: it says the renewal did not happen and the date the schedule ends, and repeats at 24 hours and 4 days. The failure alert links the capture, and the Exports page shows "Renew schedule: failed" with the same capture links. Fix the cause as above, then create the schedule by hand in Takeout (Google Photos only, `.zip`, 50 GB parts, every 2 months for 1 year) before it ends. Darkroom has no action yet to renew on request or to record a hand-made schedule, so its end date and the "not renewed" alert stay as they are; decide whether to add a "renew now" action if this happens. If the worker could not confirm the renewal (Takeout did not confirm it, or the worker was lost), the alert is "Takeout schedule renewal not confirmed" instead: open Takeout's export list, and create a schedule by hand only if there is no new one; Darkroom does not retry, so it will not create a second. While a renew job is open (queued, running or waiting for a sign-in), the Google account's email cannot be changed.

### Reading an import summary

"Export N: A added, S skipped as duplicates, R in review, G missing GPS" counts this export's decisions. An item that matches a phone original again, one an earlier export already matched, records no new decision and is not counted as skipped; that is why later exports show small numbers. An item whose bytes Darkroom already holds is skipped, and if its description or location changed in Google the sidecar is refreshed (a `metadata_refreshed` event on the status page), unless you edited that sidecar since.

### "Some originals are not in Google Photos"

The alert says how many of a person's originals still have no match in Google a week after they were uploaded, for that person's Pixel. Photos only count once Google has them, so the usual cause is that the folder is not backed up:

1. On the Pixel that serves that person, open Google Photos, then Settings, Backup, Device folders (the exact menu names vary by version), and turn on backup for the folder the iPhone uploads land in.
2. Wait for the backup to finish and for the next scheduled export; the alert closes by itself when the count reaches zero after an import.

If the folder is on and the count stays, the items may differ from Google's copy (an edit, or a format Google converts); they stay in the library and nothing is deleted from the iPhone until they match.

### Rotating the worker token

`DARKROOM_BROWSER_TOKEN` is a shared secret of at least 32 printable ASCII characters. Make a new one with `openssl rand -hex 32`, put it in `.env`, and restart both the `darkroom` and `darkroom-browser` containers. Until both run with the new value the worker gets 401 and does no jobs; queued jobs wait.
