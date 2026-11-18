# The browser worker

The browser worker runs inside the one container ([one-container.md](one-container.md)), started by its init next to the app. The separate browser container, its image and its settings (`DARKROOM_BROWSER_TOKEN`, `DARKROOM_URL`, `VNC_URL`, `DARKROOM_VNC_SECRET`) were removed (#327): when one of those is still set, `python run.py check-config` and the start-up log name it as set and ignored, and never show its value. To go back to the two containers, pin the previous release ([one-container.md](one-container.md#going-back-to-the-two-containers)).

The worker is a stateless executor: the app owns the job queue in its database and the worker claims jobs over the internal API with a token the init generates at every start and hands to both on a pipe, runs them in Chromium on a virtual display, and reports back. Its state is the per-account browser profiles and failure captures in appdata (`/data/browser`), backed up with the rest of appdata.

## What runs inside

The init (`darkroom_init/`) starts, in order: Darkroom's Syncthing when `DARKROOM_SYNCTHING=1`, its API only on `/run/darkroom/syncthing.sock`; the app; Xvfb on `DISPLAY` (`:99`, with `-nolisten tcp -nolisten local`: no TCP port and no abstract socket, only its file socket in the container's own `/tmp/.X11-unix`); x11vnc serving VNC only on the Unix socket `/run/darkroom/vnc.sock` (`-unixsock`, with `-rfbport 0` and `-rfbportv6 0`, so no TCP port on IPv4 or IPv6; no VNC password); websockify with noVNC only on `/run/darkroom/viewer.sock`, which reaches x11vnc through its socket and answers only with the viewer secret the init generates and hands it on a pipe; and then `python -m darkroom_browser`, which reaches the app only on `/run/darkroom/worker-api.sock` with the per-start token. The app serves the worker's routes (`/internal/browser/...`) only on that socket, mode `0600`, and answers 404 to them on its web port, so nothing on the host's loopback can answer the worker; the init counts the app as started when that socket accepts a connection, not when something listens on the web port. `/run/darkroom` is made in the image, owned by the container's user, mode `0700`. The two display listeners start through `darkroom_init/sockets.py`, which refuses to start them when that folder is not private to the container's user or the socket would be anywhere but directly in it, and clears a stale socket left by a previous run; the app applies the same two refusals before connecting to any of the sockets and before serving the worker's, and the worker applies them before it sends its token. The Remote browser page reaches the viewer through the app's relay at `/viewer/`, behind Darkroom's sign-in, which connects to the viewer's socket ([one-container.md](one-container.md)). Nothing listens on 5900, 6080 or 8384, so under the host networking the one container runs on, only 8000 (and, with Syncthing on, 22000 and 21027) listens: image CI checks it with `tests/browser/host_network.sh`. Chromium is launched by Playwright only while a job runs and exits with it, so the worker idles between jobs. The worker has its own virtualenv, `/opt/worker`, on the image's Python, with `playwright` (and `greenlet`) installed from `deploy/single/requirements.txt` with `--require-hashes`; the build imports it and checks its version, and `tests/browser/test_image.py` holds the pin. To move to a new Playwright release, change the pin and `pyproject.toml`'s `browser` extra together, refresh the hashes from PyPI, and re-run the noop job: its result carries the installed `playwright` version. Everything runs as the one unprivileged user (uid 99, gid 100 by default), with every capability dropped and `no-new-privileges` set.

## Google Chrome, and what it does and does not fix

The Remote browser page gives the viewer the available window height. Use **Full screen** for the largest embedded view, or **Open in new tab** to use the viewer directly. Full-screen entry and exit keep the same embedded viewer and signed-in session. Browsers without full-screen support still offer the separate-tab link.

**Connect** checks that Takeout is signed in to the expected Google account and saves that session; it does not create an export or a schedule. Once Connect finishes, use **Create schedule** on the Exports page. A download or schedule job waiting for sign-in resumes its own work after the account is confirmed. Account-switch links and sign-in URLs in Takeout's scripts do not count as a sign-in form; the worker still verifies the signed-in account's email before finishing Connect.

Google can refuse a sign-in in the remote viewer with "This browser or app may not be secure". Playwright's defaults mark the browser as automated (the `--enable-automation` flag, `navigator.webdriver`) and its bundled Chromium is Chromium for Testing, not Google Chrome. The image installs Google Chrome stable at build time (`python -m playwright install --with-deps chrome`, the pinned Playwright's own installer, as root; Chrome for Linux is amd64 only, which is the platform the image is published for) and `darkroom_browser/launch.py` starts it with `channel="chrome"`, without `--enable-automation` and with `--disable-blink-features=AutomationControlled`. Nothing else changes: no stealth plugin, no user-agent or fingerprint patch, and the worker still never types a password or a 2FA code (`selectors.TYPEABLE`); you sign in by hand in the viewer. The worker passes `chromium_sandbox=True` (Playwright's default is off, and then it adds `--no-sandbox` itself), so the browser's sandbox runs and depends on the user namespaces `seccomp.json` allows; `seccomp.json` allows namespace creation and the `chroot` call Chromium uses to isolate the sandbox; `cap_drop: [ALL]` stays in place. If Chrome does not start, the worker logs a `chrome_launch_failed` event (the error's type and a fixed reason code, never its raw output) to the container's log (`docker compose logs darkroom`) and falls back to Chromium; look for that line when sign-in is still refused, since Chromium is the browser Google refuses. If Chromium also fails, a `chromium_launch_failed` event carries its reason, and the failed job shows a fixed troubleshooting message. A profile that Chrome has opened may not open cleanly in a later Chromium fallback; back up the profile before trying a fresh one.

`BROWSER_CHANNEL` (default `chrome`) chooses the browser: `chromium` (or empty) uses the bundled Chromium. When Chrome is not in the image (a build on another platform skips it) or fails to launch, the worker falls back to the bundled Chromium instead of failing the job.

This is not a guarantee. Google changes how it detects browsers, and it may still refuse the sign-in. If it does: try again (a second attempt often passes), try from a fresh profile (stop the container, remove `browser/<account id>/profile/` in appdata, start it and press Connect again), and if it still refuses, use the manual fallback: download the Takeout parts yourself and put them in `_inbox/<slug>/manual/` (see "Hand-dropping an export" in the [runbook](../runbook.md)).

Chrome makes the image larger (a few hundred MB; not measured here, so check the image size after the first build) and Trivy's scan of the image sees its packages. Playwright's installer downloads Chrome stable from Google at build time, so Chrome is not pinned to a version, it is whatever stable is on the day the image is built, and a rebuild brings security fixes.

## Configuration

| Variable | Meaning |
| --- | --- |
| `PUID` / `PGID` | Container UID and GID, default `99` / `100`. Compose uses them for `user` and the image's build arguments; changing them does not remap users in an already published image (Users, below). |
| `DARKROOM_BROWSER_ROOT` | Where the browser profiles and failure captures live: `/data/browser`, set by the image. The init gives the worker the same folder as its `BROWSER_ROOT`. |
| `POLL_SECONDS` | How often an idle worker asks for a job (15). |
| `BROWSER_CHANNEL` | `chrome` (default): Google Chrome when the image has it; `chromium`: always the bundled Chromium. |
| `DISPLAY` | The Xvfb display (`:99`). |

The init sets the worker's own `PHOTOS_ROOT`, `BROWSER_ROOT` and the pipe its token arrives on (`DARKROOM_BROWSER_TOKEN_FD`); nobody sets those by hand. The worker takes no address or port: it reaches the app only on `/run/darkroom/worker-api.sock`.

## Chromium sandbox: seccomp instead of `--no-sandbox`

Chromium's own sandbox needs user namespaces (`clone`/`unshare` with `CLONE_NEWUSER`), which Docker's default seccomp profile only allows to a process with `CAP_SYS_ADMIN`. Rather than start Chromium with `--no-sandbox`, the compose service applies `deploy/browser/seccomp.json`: an allowlist profile that permits the syscalls Chromium and Python need, including `clone`, `unshare`, `setns` and `chroot`, and denies everything else with `EPERM`. Keep `cap_drop: [ALL]`; the sandbox does not need any capability once namespaces are allowed. `shm_size: 1g` is set because Chromium uses `/dev/shm` for its renderers and the Docker default of 64 MB makes tabs crash.

With the sandbox on, Chrome and Chromium both fail to start when the host blocks unprivileged user namespaces; with Chrome that shows up as a `chrome_launch_failed` event in the container's log before the worker falls back to Chromium (which then fails the same way).

The earlier allowlist omitted `chroot`, which Chromium uses after entering its user namespace: namespace creation alone was not enough. The image CI now launches a real page in both browsers with the deployment seccomp profile, all capabilities dropped, and `no-new-privileges` enabled. Run the same check locally after a build:

```sh
docker run --rm --cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json --shm-size=1g -w /opt/worker/app -e PATH=/opt/worker/bin:/usr/local/bin:/usr/bin:/bin ghcr.io/anthonypluth/darkroom:latest bash /opt/worker/app/smoke.sh
```

For `sandbox_unavailable`, apply the current seccomp profile when recreating the container (on Unraid, the template's Extra Parameters name `/boot/config/darkroom/seccomp.json`; copy the release's profile there) and check the host's `kernel.unprivileged_userns_clone` (where present) and `user.max_user_namespaces` (must be above 0). The worker does not disable the browser sandbox to work around a host restriction.

Other fixed launch reasons are `permission_denied` (check the configured UID/GID and writable browser home and share), `display_unavailable` (check Xvfb and `DISPLAY`), `browser_missing` (rebuild or pull the image), `profile_in_use` (another browser or a stale profile lock), and `browser_exited` (unrecognised startup exit; check memory limits as well). `TargetClosedError` by itself does not identify which cause applies.

For `profile_in_use`, stop every worker that can open that account's profile. Back up `browser/<account id>/profile/` in appdata, then remove only stale `SingletonLock`, `SingletonSocket` and `SingletonCookie` entries before restarting. A recreated Docker container has a different hostname, so Chrome can mistake an old lock for a browser running on another machine. Never remove locks while another worker is using the profile; do not delete the profile to diagnose a launch error.

## Volumes

| Host | Container | Mode | Why |
| --- | --- | --- | --- |
| The photo share | `/photos` | rw | The working root. The download job writes each Takeout part to `_inbox/<person>/export-<export id>/.partial/`, checks its size and zip integrity, then moves it to `_inbox/<person>/export-<export id>/`. Chromium keeps a download in progress under `_inbox/.downloads/<account id>/`, on the same mount, so a finished part moves without a copy. The worker empties that folder's files when it opens a browser, and refuses to start the job if `_inbox`, `.downloads` or the account's folder is a link; deleting the account removes the folder. |
| Appdata | `/data` | rw | `browser/<account id>/profile/` holds the persistent Chromium profile (the signed-in session), `browser/<account id>/failures/<job id>/` the screenshot and scrubbed page snapshot of a failed job. |

### Users and the shared group

The image defaults to uid 99 and gid 100, matching Unraid's `nobody:users`; compose uses `user: "${PUID:-99}:${PGID:-100}"` and the Unraid template `--user=99:100`. The image reuses an existing group with that GID, rather than failing when `users` already exists. Custom IDs are build arguments (`PUID`, `PGID`): build the image with `docker compose up -d --build` and use a matching runtime user. The folders remain private to their owner and group, but other host processes belonging to Unraid's `users` group can access group-writable files.

- Darkroom makes `_inbox`, `_inbox/.downloads`, each person's `_inbox/<person>/` and `manual/`, and each export's `_inbox/<person>/export-<id>/` (when the worker asks for its download plan) with mode `2770`: owner and group can read, write and enter, and nobody else can even list them, so what the inbox holds is never readable outside the group. The setgid bit keeps every folder made under them in the configured primary group (100 by default). The mode is set with `chmod` after `mkdir`, so it doesn't depend on the umask. When the app starts it gives the folders it owns under `_inbox` (`_inbox`, `.downloads` and each person's inbox) that group and mode, so a share set up before this change is fixed on the next start.
- The worker runs with umask `007` and makes `.downloads/<account id>/`, any missing export folder and `.partial/` with mode `2770` too, and sets each finished part to `0660`, so the app can read the parts, unpack them and remove the export folder, and deleting an account can remove `.downloads/<account id>/`.
- A folder in the chain that is a link is refused on both sides.

Chromium makes some folders inside its profile (`browser/<id>/profile/`) with mode `0700`, whatever the umask. When a job closes its browser, the worker adds the group's read, write and search bits (`g+rwx`, never following a link and never touching others' bits) to each folder under `browser/<id>/`, and leaves every file as Chromium made it (a folder's bits are all removing a file needs), so deleting the account can remove the folder. If the worker was killed or Chromium crashed before that ran, the folder keeps its `0700` folders: the delete then answers 500 and changes nothing ("the account's browser folder could not be removed"). Open the account's browser once more (any job closes it the same way), or remove the folder on the host, and delete the account again.

A download that stops receiving data is cancelled after five minutes: the worker watches the size of `_inbox/.downloads/<account id>/` while it waits, and when nothing has grown for that long it cancels the download, empties that folder and reports the part as `interrupted`, which counts as one of its attempts.

When clicking a part's link starts no download within 30 seconds, the worker reads every open tab (its own and any the click opened). If any of them is Google's sign-in or password prompt (a page on `accounts.google.com`, a password field, or Google's re-authentication wording), the job waits for you ("waiting for you", reason `verify`) and types nothing; sign in in the Remote browser page. Once the prompt is gone the part is reported as `asked_to_verify`, which does not count as one of its 3 attempts but still counts as one of Google's 5 downloads, and the link is clicked again. If no tab shows a prompt, the job waits 3 minutes before reporting the part as `not_started` (one attempt) and trying again, and its message says what the click led to in fixed words: which tab, the page's host category (`takeout`, `accounts`, `other` or `none`), whether a password field was there, the page's known headings and the number of tabs, never the page's text or address. The worker listens for downloads on every tab: a download that starts late (while it waits for you, during those 3 minutes, or before it clicks again) is kept instead of clicking the link a second time, but only if the job's last click was this part's checked link, the download started in the job's own tab or a tab opened from it, and it is this part's: its final address (after Google's redirect) is plain `https` on one of Google's hosts (`google.com`, `googleusercontent.com` or a host under either, never `accounts.google.com`, and on `takeout.google.com` only the `/takeout/download` path) and carries the same export `j` and part `i` as the checked link. A download whose address carries no ids is not kept, whatever its file name: it cannot be told apart from another export's part with the same number, so the part is clicked again instead. Any other download is cancelled and the folder emptied, and the part is reported `interrupted`. The file's name, size and zip checks still apply after that. A download Chromium reports as failed also empties `_inbox/.downloads/<account id>/`.

## Reading Google's verification window from the event log

How long Google lets a download go without asking you to verify it's you again is not documented (spike #432). Every download job records what it sees, so the export's own history answers it. Each part click is an `export_part_clicked` event, and each verification prompt that a click leads to is an `export_part_prompt` event, both in the event log (Status page, or `GET /api/status`) with the export's number as subject and fixed words as detail, never the page's text, an address, an account or a file name:

- `part 3: clicked with 0 other parts downloading, 52 minutes after the previous click, 53 minutes after the last answered prompt`
- `part 4: Google asked to verify after 2 clicks since the last answered prompt, with 0 other parts downloading, 52 minutes after the previous click, 105 minutes after the last answered prompt`

"The previous click" and "the last answered prompt" are within the same job (a restarted job starts counting again). An answer is timed when the prompt is gone and Takeout is signed in again: a sign-in the job waited for at its start, or one Google asked for right after a prompt, counts as the answer at the moment it finished. The minutes in a prompt's event are those of the click that led to it. Parts download one at a time today, so the number of other parts downloading is always 0 and the summary says `at most 1 part downloading at once`; the field is there for downloads that overlap, which a later change adds. The job's message shows the same words while it runs (`downloading part 3 of 25; 52 minutes after the previous click, ...`, and after a prompt `verified; resuming part 4 (prompt 2 of this job, after 2 part clicks since the last answered prompt)`), and its last message sums it up, for example `downloaded 25 of 25 parts; 31 part clicks and 6 verification prompts in 21 hours 4 minutes; at most 9 part clicks in a row without a prompt; at most 1 part downloading at once`. The same counts are in the job's result under `telemetry`.

How to read them:

- A prompt on almost every click, each "N minutes after the previous click" with N about one part's download time, means Google's window is shorter than one part's download, or that Google asks per download whatever the time (the spike's H2 or H3).
- Clicks that start without a prompt when they come within some number of minutes of the last answered prompt, and prompts once that gap is longer, put the window between the longest gap that passed and the shortest that prompted. "N clicks since the last answered prompt" is how many parts one answer started.
- A click that comes soon after the previous click but long after the last answer, and does not prompt, means activity refreshes the window, not only an answer.

The events cost nothing to record and change no part's state, Google's download count or the attempts; if Darkroom cannot record one, the download carries on.

## The browser profiles in appdata (the one container)

The browser's state and the working folders are two separate roots, configured apart and never worked out from each other (docs/design/single-container.md section 3):

| Root | Setting | Value |
| --- | --- | --- |
| Browser state: `<account id>/profile/` and `<account id>/failures/<job id>/` | `DARKROOM_BROWSER_ROOT` (the worker's `BROWSER_ROOT`, set by the init from it) | `/data/browser`, in appdata |
| Working folders: `_inbox/` (with `.downloads/<account id>/`) and `_pixels/` | `DARKROOM_PHOTOS_ROOT` (the worker's `PHOTOS_ROOT`) | `/photos` |

Chromium's download folder stays under the working root's `_inbox/.downloads/<account id>/`, next to the export folders, so a finished part still moves into place with a rename on one filesystem, and a part never lands in appdata. Darkroom serves a failed job's capture from the browser-state root, and deleting an account removes its folder there, the copy of that account's folder still on the share from before the move to appdata (`_browser/<account id>/`, so going back to an older release never brings back a deleted account's Google session), and its `_inbox/.downloads/<account id>/`, with the same link and containment refusals as before. `DARKROOM_BROWSER_ROOT` may not be the photo share, a folder inside it other than its `_browser` folder, or a folder that holds it, links followed: `check-config`, `serve` and the worker refuse to start with one, and the remover refuses such a root itself, so an account's deletion can never reach the photo tree. The job's reported `failure_dir` keeps its `_browser/<account id>/failures/<job id>` form: it names the job's folder and is never used as a path.

### Copying browser profiles: `copy-browser-state`

Profiles left on the share by the two containers (`_browser/`) are moved into appdata on the release before the one that removed those containers ([one-container.md](one-container.md#moving-from-the-two-containers)). The command that copies them stays, for that move and the way back. It runs inside the image, as Darkroom's user under the same sandbox, with the container stopped:

```sh
docker compose run --rm --no-deps -w /app darkroom python run.py copy-browser-state --source /photos/_browser --target /data/browser
```

and back to the share, setting the old `_browser/` aside:

```sh
docker compose run --rm --no-deps -w /app darkroom python run.py copy-browser-state --source /data/browser --target /photos/_browser --set-aside
```

On Unraid, run it with the template's flags and paths (the share and appdata paths are yours), with the container stopped:

```sh
docker run --rm --user 99:100 --cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=/boot/config/darkroom/seccomp.json \
  -v /mnt/user/appdata/darkroom:/data -v /mnt/user/photos:/photos -w /app ghcr.io/anthonypluth/darkroom:<release> \
  python run.py copy-browser-state --source /photos/_browser --target /data/browser
```

It copies the source into a new hidden folder next to the target (`.browser.copying-<random>`), leaving out Chromium's `SingletonLock`, `SingletonSocket` and `SingletonCookie` links, then compares every entry of the copy with the source (kind, mode, size and SHA-256), lists the source again to make sure nothing changed it meanwhile, and only then renames the copy into place. It copies only between the photo share's own `_browser/` and a folder outside the photo share (appdata), with links resolved and the photo share taken from `DARKROOM_PHOTOS_ROOT`: any other folder on the share (a person's folder, `originals/`, `_inbox/`, `_pixels/`, the share itself) or a folder that holds the share is refused, and nothing is copied or renamed. It also refuses a source or target that is a link, any other link or special file in the profiles, any folder or file in them it cannot read (it stops with "part of the browser profiles could not be read" rather than install an incomplete profile; fix the ownership, which should be the container's user, and run it again), and a target that already exists, unless `--set-aside` is given, which renames the old target to `<name>.before-<time>` only after the copy has been verified. It deletes nothing: a copy that was interrupted or did not match stays as the hidden folder, is reported on the next run, and can be removed by hand before trying again.

`cp -a`, `shutil.copy2`, `shutil.copytree` and a `shutil.move` across mounts all fail inside this container: `deploy/browser/seccomp.json` answers the extended-attribute calls they make with `EPERM` (spike S1). `copy-browser-state` reads and writes each file and sets its mode and times, which the profile allows, so the profile stays as it is. The image CI runs it under the profile, beside a `cp -a` that fails there (`tests/browser/copy_under_profile.sh`).

`_browser/` on the share is left as it was. Deleting an account removes its folder in the live browser-state root and in `_browser/` on the share, and nothing else; no other Darkroom code deletes anything there. The copies a move leaves behind still hold every account's signed-in session, including accounts deleted later: `_browser/` itself, a set-aside `_browser.before-<time>/` or `browser.before-<time>/`, and an unfinished `.browser.copying-<random>/`. Remove them by hand once you no longer need them, and before starting the container on an older `/data/browser/`, remove the folders of accounts deleted since, because SQLite can give a new account a deleted account's id.

## Running a `noop` job end to end

1. Start the one container: `docker compose up -d`. The init generates the browser token; nothing is set by hand.
2. `docker compose logs -f darkroom` should show `browser worker ready`.
3. Queue a job while signed in to Darkroom: `POST /api/browser/jobs` with `{"account_id": <id>, "kind": "noop"}` (or `GET /api/browser/jobs` to watch it).
4. Within `POLL_SECONDS` the job goes `queued → running → done`, its result reads `{"ok": true, "kind": "noop", "playwright": "1.49.1"}` (a `null` means the package is missing from the image) and `GET /api/status` shows `worker_last_seen`.
5. A job whose worker disappears is marked `failed` after the lease (`LEASE_SECONDS`, 3 minutes without a heartbeat) and a "Darkroom needs a look" alert opens. The worker gives up first: when Darkroom has acknowledged no heartbeat for 2 minutes (`GIVE_UP_SECONDS`), or Chromium fails to close, the worker logs it and exits with code 3, taking Chromium with it, and the init restarts it. It never reports a job finished while its browser may still be open, so deleting the account (which removes `browser/<id>`) waits for that job.

Failure captures are served to signed-in users only, at `/api/browser/failures/<job>/screenshot.png` and `/api/browser/failures/<job>/page.html`. The worker reports which of the two it managed to write; the service keeps only names it knows that exist in the job's own failure folder, and the alert links to the screenshot, else the page snapshot, else nothing. The HTML is scrubbed before it is written (`darkroom_browser/scrub.py`; `tools/scrub_snapshot.py` applies the same rules to a saved page): scripts, form values, token-like attributes and `<meta>` tags, cookie pairs, JWTs, long opaque strings and query strings are removed.
