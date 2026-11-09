# Runbook

What the owner sets up by hand around the repository, and what to do when the Takeout import needs a person. The deployment of the browser worker is in [deploy/browser.md](deploy/browser.md).

## Branch protection

`main` is protected by a ruleset (Settings, Rules, Rulesets, a branch ruleset targeting the default branch; managed in Terraform in the Infrastructure repository):

- Require a pull request before merging, squash merges only. No approving review is required (`required_approving_review_count = 0`, Code Owner review off): the gate merges Dependabot's pull requests itself, and a pull request merges on the checks below alone. `.github/CODEOWNERS` still asks the owner to review changes to the workflows, `.claude/`, `AGENTS.md`, `CLAUDE.md`, `SECURITY.md`, `.semgrep/` and `docs/design/`, but nothing enforces it.
- Require one status check to pass: **Merge gate**, and nothing else.
- Require CodeQL code scanning results (alerts at `high_or_higher` security severity or `errors`).
- Block force pushes and deletions.
- The owner can bypass the ruleset; apps and bots cannot: Dependabot's pull requests go through the gate too.

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
2. Sign in to the Google account the alert names, finish any 2FA prompt, and leave the browser on a Google page (a page the worker does not recognise for more than 5 minutes fails the job).
3. Do nothing else: the waiting job notices the signed-in page and carries on, and the alert closes.

A job waits 48 hours for the sign-in and then fails ("Darkroom needs a look" names it). Signing in afterwards does not resume it, and Darkroom does not start it again: Connect only queues a separate connect job. The account also stays marked as needing sign-in, so "Check exports now" and the daily check skip it until a connect succeeds. To recover:

1. Press Connect on the Exports page, sign in as above, and wait for the connect job to finish.
2. Start again what failed:
   - **A check of Takeout**: press "Check exports now".
   - **Create schedule**: press Create schedule. If the failed attempt may have created one, the page first asks you to look in Takeout and answer ("Takeout has the schedule" or "I checked Takeout, there is no schedule").
   - **A download**: nothing to press. The next check (the daily one, or "Check exports now") queues a new download job. An export gives up after 3 download jobs that failed once a part had started (a job that only timed out waiting for sign-in does not use one); it is then marked failed on the Exports page and has to be hand-dropped (below). An export with an "about to expire" alert is on a clock (the alert opens 4 days before), so sign in promptly.
   - **A renewal**: it is not retried, whatever you press. If it failed before the worker clicked Create export, the alert is "Takeout schedule not renewed"; if the worker stopped reporting or Takeout did not confirm, it is "Takeout schedule renewal not confirmed". Follow the matching section below: answer the buttons for "not confirmed", or press Create schedule once the schedule has ended for "not renewed".

"Wrong Google account" means the browser is signed in as someone else: sign out there and sign in as the named account.

### An export expired

Takeout keeps an export for about a week. The alert "A Takeout export is about to expire" means Darkroom has not finished downloading it; an export marked expired on the Exports page is gone.

- If it is only about to expire, check that the worker is running and has not hit a failure alert (above and below). The daily check (or "Check exports now") re-queues a download that failed, up to 3 download jobs that failed after starting a part, per export; it only runs for an account that is signed in. If Darkroom will not get there in time, download the parts by hand from Takeout's export list while the alert is still open, then hand-drop them (below).
- If it expired, Takeout no longer has the files, so nothing can be downloaded, by Darkroom or by hand. The only recovery is a new export: request one in Takeout (Darkroom itself only creates the schedule), download it, and hand-drop the parts. The scheduled exports keep coming every 2 months until the schedule ends.

### Google changed a page

Alert "Darkroom needs a look" with a link to the capture of the failed job. The capture is a screenshot and a scrubbed copy of the page (the screenshot is the link, else the page), at `/api/browser/failures/<job>/screenshot.png` and `/api/browser/failures/<job>/page.html`, and on the photo share under `_browser/<account>/failures/<job>/`.

1. Open the capture and compare it with what the worker expected (the job's message names the step, for example `UnexpectedPage` or a missing control).
2. The controls the worker clicks and the elements it looks for are in `darkroom_browser/selectors.py`; the phrases and patterns it uses to recognise a page (signed in, sign-in needed, "verify it's you", export created, the export list) are in `darkroom_browser/pages/takeout.py`. Change the one that moved, update the matching page fixture and test under `tests/browser/`, and deploy a new worker image.
3. Run the step again from the Exports page (Connect, Create schedule or Check exports now); a failed job is not started again by itself, and a download is queued again by the next check (see "A job is waiting for sign-in" for the limit). Until the fix is deployed, use the fallback: download the parts by hand (while Takeout still lists them) and hand-drop them.

### Hand-dropping an export

Put the Takeout `.zip` parts, all of one export, in `<photos>/_inbox/<person>/manual/` and run it inside the `darkroom` container, which holds the photo share (`/photos`), the data folder (`/data`) and the database:

```
docker compose exec darkroom python run.py import-takeout --person <slug> [--account <google email>] [--dir <other folder>]
```

`--dir` is a path inside the container, so it must be under a mount `docker-compose.yml` gives `darkroom` (`/photos` or `/data`); a folder elsewhere on the host is not visible to it. It registers the parts as a manual export for that person's Google account and runs the same pipeline as a downloaded one: unpack, dedupe, place, sidecars, Immich rescan, summary alert. `--unpack-only` stops after unpacking. Running it again for an export that was imported with no failed items says "already imported" and changes nothing. If the summary said some items "could not be imported", running it again retries those items, as long as the export's staging folder still exists; once it is gone the command says so and the items stay failed. The person needs a Google account in Darkroom to attach the export to; with several, say which with `--account`.

### Importing a person's legacy library

Once a person's Takeout is imported, their old Google Photos sync folder can be merged in, once. It must be a folder inside the `darkroom` container that does not overlap `/photos` (nor another person's legacy folder), for example a read-only mount of the old folder:

```
docker compose exec darkroom python run.py import-legacy --person <slug> [--dir <legacy folder>]
docker compose exec darkroom python run.py legacy-report
```

Darkroom only reads that folder. Each photo or video is matched against that person's own Takeout: a match proves Google has it and nothing is copied. Any other file is copied once into the person's `originals/legacy/` and waits for their Pixel folder. A folder that cannot be listed, or a file that cannot be read or copied, counts as failed and is tried again on the next run. `legacy-report` prints, per person, the files seen, how many are matched in Google, and how many are not backed up (unmatched plus failed), with how many are already in the Pixel folder and how many wait for it. "Everything is backed up" is true when the not-backed-up count is 0. A second run changes nothing for files already seen.

### The schedule renewal

About 3 weeks before an account's export schedule ends, Darkroom opens "Takeout schedule ending" and queues one renew job, which creates next year's schedule. When it succeeds the alert closes and "Takeout schedule renewed" confirms the new end date. Darkroom never retries a renewal on its own (#40); you can ask for one with Renew now (below). The first export after a renewal is a full export again; keep disk headroom for about twice your largest library while it is imported.

#### "Takeout schedule renewal not confirmed"

The worker clicked Create export but Takeout did not confirm it, or the worker stopped reporting. Takeout may have the new schedule.

1. Open Takeout's export list for the account the alert names (in the remote browser, or signed in as that account anywhere) and look for a new scheduled export (every 2 months, created around the time of the renew job).
2. On the Exports page, press "Takeout has the new schedule" if it is there. Darkroom records it, ending a year after the renew job started, closes the alert and confirms the renewal; the next renewal is queued 3 weeks before that date.
3. Press "Takeout has no new schedule" if it is not. The alert becomes "Takeout schedule not renewed" (below). Don't create one by hand in Takeout: Darkroom would not know about it, and next year's renewal would not happen.

#### "Takeout schedule not renewed"

The renewal did not happen and the current schedule still ends on the date the alert gives; its scheduled exports keep coming until then.

1. Fix what made the job fail: the failure alert and the Exports page link the capture (see "Google changed a page" when that is the cause).
2. Press Renew now on the Exports page to try again (it works inside the window and after the end, with no schedule or renew job open); once the schedule has ended, Create schedule makes a new one as well. When that job is done, the "not renewed" alert closes and next year's renewal follows as usual.
3. Exports between the end and the new schedule are not made. If that gap matters, press Renew now instead of waiting for the end (see "Renew now").

#### Renew now

`POST /api/accounts/{id}/schedule/renew` queues one renew job on request, through the same path as the daily watch: the same job, the same email pin and the same failure alerts. It works from 3 weeks before the schedule ends, so inside the window after a "not renewed" failure, and after the end. It answers 409 and queues nothing before the window opens (this also stops it stacking a second recurring schedule on one that was just renewed), while a schedule or renew job is queued, running or waiting for a sign-in, while an earlier attempt is waiting for your Takeout check answer, when the account has no schedule, and when the account needs to sign in. Queuing the job closes the "not renewed" alert; if the job fails the alert opens again, and the daily watch shows "Takeout schedule ending" while it is queued, as for the watch's own renewal. On the Exports page it is the Renew now button, last in the account's button row. It is drawn once the account has a schedule and is enabled in the cases above; a sentence under the buttons says why when it is not, and a refusal from the route (the page was out of date) appears in the account's card.

The account's Google email cannot be changed while a renew job is open (queued, running or waiting for a sign-in), or while an unconfirmed attempt is waiting for your answer.

### Reading an import summary

"Export N: A added, S skipped as duplicates, E already imported earlier, R in review, G missing GPS" (", E already imported earlier" only when there are some, and ", F could not be imported" when some items failed) counts this export's decisions. An item that matches an original again, one an earlier export already matched, records no new decision and is not counted as skipped; it is counted as already imported earlier instead, which is why later exports show small skipped numbers. An item whose bytes Darkroom already holds is skipped, and if its description or location changed in Google the sidecar is refreshed (a "Metadata refreshed" line among the latest events on the status page), unless you edited that sidecar since; a refreshed location also takes the photo off Missing GPS.

### "Some originals are not in Google Photos"

The alert says how many of a person's originals still have no match in Google a week after Darkroom copied them, for that person's Pixel. Photos only count once Google has them, so the usual cause is that the Pixel's folder is not backed up:

1. On the Pixel that serves that person, open Google Photos, then Settings, Backup, Device folders (the exact menu names vary by version), and turn on backup for the folder Syncthing delivers that Pixel's photos to.
2. Wait for the backup to finish and for the next scheduled export; the alert closes by itself when the count reaches zero after an import.

If the folder is on and the count stays, the items may differ from Google's copy (an edit, or a format Google converts); they stay in the library, and nothing is lost: the permanent copy is in the person's `originals/`.

### Where alerts arrive

Every alert in this runbook is recorded as an event and shown on the status page. When Resend is set up ([docs/deploy/alerts-email.md](deploy/alerts-email.md)) each one is also emailed to the owner's address, with only its title and a link back to Darkroom; without it, nothing is sent and the status page is the only place. Either way, look at the status page after a scheduled export is due, a sign-in session expires or a schedule nears its end. `python run.py check-config` says which of the two is in force.

### Pixel folders are pruned after a Takeout match

A Pixel's transient folder is emptied by Darkroom deleting the per-Pixel copy, which Syncthing carries to the Pixel; it may do so only once a Takeout match proves Google has the photo (#199, question 6), which can take up to about two months after the photo was copied. The hourly `pixels.tend` job prunes confirmed copies, then copies queued photos oldest first while the folder stays under `PIXEL_FOLDER_CAP_GB` (default 50); its log line counts what was placed, pruned, skipped, deferred, failed and refused. A refused prune is recorded as a `pixel_prune_refused` event and leaves the copy where it is: look for a link, a mount or a changed file in `_pixels/<pixel>/<person>/`. The folder layout is `_pixels/<pixel id>/<person>/`, and Syncthing shares `_pixels/<pixel id>/` with that Pixel as Send Only; it has to be able to read files Darkroom writes (0644, folders 0755). A mount at or below `_pixels/` stops placing and pruning. The permanent copies in `originals/` are never part of that folder.

### The iPhones and Immich's app

Each iPhone backs up and clears its photos with Immich's own iOS app; Darkroom does not gate it and has no app on the phone. Before a phone is allowed to clear backed-up photos, check that iCloud Photos is off on it (with it on, clearing removes the photo from iCloud and every other device) and that the phone reaches Immich over the LAN, Tailscale or an unproxied hostname: a Cloudflare-proxied hostname caps request bodies at 100 MB on its Free and Pro plans, so large videos fail. Clearing in Immich's app can happen at any time and Darkroom plays no part in it (#199); the gap before Google has the photo (via the Pixel) should be an hour or two at most, so Immich's library and the Unraid share need redundant storage and a backup. What Immich's clearing removes (RAW+JPEG pairs, Live Photos) is an assumption until the spike (#202) checks it.

### Rotating the worker token

`DARKROOM_BROWSER_TOKEN` is a shared secret of at least 32 printable ASCII characters. Make a new one with `openssl rand -hex 32`, put it in `.env`, and recreate both containers with `docker compose up -d --force-recreate darkroom darkroom-browser` (`docker compose restart` keeps the old environment). Until both run with the new value the worker gets 401 and does no jobs; queued jobs wait.

## Immich reconcile

The `immich.reconcile` job (every 15 minutes, or `python run.py reconcile-immich [--person SLUG]`) reads each person's phone uploads from Immich with that person's own API key (`POST /api/search/metadata` on `updatedAt`, the cursor being an SQL offset, so a window is re-read with a 5 minute overlap and every step is idempotent) and runs only after that person's Takeout and, when a legacy folder is set, their legacy import. A phone upload is an asset with the `mobile-app` metadata key, no library, owned by the person's Immich user; a Live Photo's hidden video is stored with its still, and a video or still whose other half has not arrived waits and is retried. Each new original is downloaded once, checked against Immich's base64 SHA-1 and written once to `<person>/originals/immich/` (the file is finished under a temporary name and linked into place, so a failed download leaves nothing), then queued for every Pixel that serves the person with `always_send`, even if a Takeout already matches. A photo whose SHA-1 is already in the person's originals is not written again. Darkroom only reads Immich through its API and never changes anything there or on a phone. Known gap: a Takeout copy already in `google-only/` stays next to the new original until #234 resolves such duplicates. A failing run is recorded as `immich_reconcile_failed` and opens one alert until a run succeeds; `immich_reconciled` events and the log line count what was listed, stored, ignored, waiting and failed.
