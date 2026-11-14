# Deploying the browser worker

The `darkroom-browser` container is a stateless executor: `darkroom` owns the job queue in its database and the worker claims jobs over the internal API with a shared token, runs them in Chromium on a virtual display, and reports back. Nothing in it needs to be backed up; the per-account browser profiles and failure captures live on the photo share under `_browser/`.

## What runs inside

`deploy/browser/entrypoint.sh` starts, in order: Xvfb on `DISPLAY` (`:99`), x11vnc on `VNC_PORT` (5900) on the container's loopback only (`-localhost`, no VNC password), websockify + noVNC on `NOVNC_PORT` (6080), which answers every request, the noVNC page and the WebSocket alike, only with the viewer secret (below), then `python -m darkroom_browser`. Chromium is launched by Playwright only while a job runs and exits with it, so the container idles between jobs. The image is `mcr.microsoft.com/playwright/python` pinned to one Playwright release; that image carries the browsers and their system libraries, not the Python package, so the build installs `playwright` (with `greenlet` and `pyee`) from `deploy/browser/requirements.txt`, pinned to the same release with `--require-hashes` (x86_64 and aarch64 wheels), and then imports it and checks the version. `tests/browser/test_image.py` fails when the image tag and the pip pin drift apart. To move to a new Playwright release, change both, refresh the hashes from PyPI, and re-run the noop job: its result carries the installed `playwright` version. The container runs as the non-root user `darkroom-browser` (uid 99, gid 100 by default), drops every capability and sets `no-new-privileges`.

## Google Chrome, and what it does and does not fix

The Remote browser page gives the viewer the available window height. Use **Full screen** for the largest embedded view, or **Open in new tab** to use the viewer directly. Full-screen entry and exit keep the same embedded viewer and signed-in session. Browsers without full-screen support still offer the separate-tab link.

**Connect** checks that Takeout is signed in to the expected Google account and saves that session; it does not create an export or a schedule. Once Connect finishes, use **Create schedule** on the Exports page. A download or schedule job waiting for sign-in resumes its own work after the account is confirmed. Account-switch links and sign-in URLs in Takeout's scripts do not count as a sign-in form; the worker still verifies the signed-in account's email before finishing Connect.

Google can refuse a sign-in in the remote viewer with "This browser or app may not be secure". Playwright's defaults mark the browser as automated (the `--enable-automation` flag, `navigator.webdriver`) and its bundled Chromium is Chromium for Testing, not Google Chrome. The image installs Google Chrome stable at build time (`python -m playwright install --with-deps chrome`, the pinned Playwright's own installer, as root; Chrome for Linux is amd64 only, so on aarch64 the step is skipped and the worker keeps using the bundled Chromium) and `darkroom_browser/launch.py` starts it with `channel="chrome"`, without `--enable-automation` and with `--disable-blink-features=AutomationControlled`. Nothing else changes: no stealth plugin, no user-agent or fingerprint patch, and the worker still never types a password or a 2FA code (`selectors.TYPEABLE`); you sign in by hand in the viewer. The worker passes `chromium_sandbox=True` (Playwright's default is off, and then it adds `--no-sandbox` itself), so the browser's sandbox runs and depends on the user namespaces `seccomp.json` allows; `seccomp.json` allows namespace creation and the `chroot` call Chromium uses to isolate the sandbox; `cap_drop: [ALL]` stays in place. If Chrome does not start, the worker logs a `chrome_launch_failed` event (the error's type and a fixed reason code, never its raw output) to `docker logs darkroom-browser` and falls back to Chromium; look for that line when sign-in is still refused, since Chromium is the browser Google refuses. If Chromium also fails, a `chromium_launch_failed` event carries its reason, and the failed job shows a fixed troubleshooting message. A profile that Chrome has opened may not open cleanly in a later Chromium fallback; back up the profile before trying a fresh one.

`BROWSER_CHANNEL` (default `chrome`) chooses the browser: `chromium` (or empty) uses the bundled Chromium. When Chrome is not in the image (aarch64, or the build skipped it) or fails to launch, the worker falls back to the bundled Chromium instead of failing the job.

This is not a guarantee. Google changes how it detects browsers, and it may still refuse the sign-in. If it does: try again (a second attempt often passes), try from a fresh profile (stop the worker, remove `_browser/<account id>/profile/` on the host, press Connect again), and if it still refuses, use the manual fallback: download the Takeout parts yourself and put them in `_inbox/<slug>/manual/` (see "Hand-dropping an export" in the [runbook](../runbook.md)).

Chrome makes the amd64 image larger (a few hundred MB; not measured here, so check the image size after the first build) and Trivy's scan of the image sees its packages. Playwright's installer downloads Chrome stable from Google at build time, so Chrome is not pinned to a version, it is whatever stable is on the day the image is built, and a rebuild brings security fixes.

## Configuration

| Variable | Service | Meaning |
| --- | --- | --- |
| `PUID` / `PGID` | both | Container UID and GID, default `99` / `100`. Compose uses them for `user` and image build arguments. For custom IDs, build both images with `docker compose --profile browser up -d --build`; changing these variables does not remap users in an already published image. |
| `DARKROOM_BROWSER_TOKEN` | both | Shared secret, at least 32 printable ASCII characters (`openssl rand -hex 32`). The worker refuses to start without it; `darkroom` answers 401 to every worker request while it is unset or short. Rotate it by changing `.env` and recreating both containers (`docker compose up -d --force-recreate darkroom darkroom-browser`; `docker compose restart` keeps the old environment). |
| `DARKROOM_URL` | browser | Where the worker reaches `darkroom`: `http://darkroom:8000` on the compose network. |
| `PHOTOS_ROOT` | browser | `/photos` inside the container: `_inbox/` and `_browser/` are mounted there read-write. |
| `POLL_SECONDS` | browser | How often an idle worker asks for a job (15). |
| `BROWSER_CHANNEL` | browser | `chrome` (default): Google Chrome when the image has it; `chromium`: always the bundled Chromium. |
| `DISPLAY` | browser | The Xvfb display (`:99`). |
| `DARKROOM_VNC_SECRET` | browser | The viewer secret, at least 32 printable ASCII characters (`openssl rand -hex 32`). websockify on 6080 accepts a request only with `Authorization: Basic` for `darkroom:<secret>`, which the Traefik route adds (Network, below); the person signing in never types it. Unset, short or not printable, the worker logs `viewer locked` and the viewer refuses everyone with a secret generated at each start and kept nowhere, while jobs still run. The worker itself never sees it (the entrypoint unsets it before starting Python). Rotate it like the browser token: change `.env` and the Traefik label, then recreate the container. |
| `VNC_URL` | darkroom | The address a person opens to see the browser (the Traefik route below). It goes into the "Google sign-in needed" alert. |

## Chromium sandbox: seccomp instead of `--no-sandbox`

Chromium's own sandbox needs user namespaces (`clone`/`unshare` with `CLONE_NEWUSER`), which Docker's default seccomp profile only allows to a process with `CAP_SYS_ADMIN`. Rather than start Chromium with `--no-sandbox`, the compose service applies `deploy/browser/seccomp.json`: an allowlist profile that permits the syscalls Chromium and Python need, including `clone`, `unshare`, `setns` and `chroot`, and denies everything else with `EPERM`. Keep `cap_drop: [ALL]`; the sandbox does not need any capability once namespaces are allowed. `shm_size: 1g` is set because Chromium uses `/dev/shm` for its renderers and the Docker default of 64 MB makes tabs crash.

With the sandbox on, Chrome and Chromium both fail to start when the host blocks unprivileged user namespaces; with Chrome that shows up as a `chrome_launch_failed` event in `docker logs darkroom-browser` before the worker falls back to Chromium (which then fails the same way).

The earlier allowlist omitted `chroot`, which Chromium uses after entering its user namespace: namespace creation alone was not enough. The image CI now launches a real page in both browsers with the deployment seccomp profile, all capabilities dropped, and `no-new-privileges` enabled. Run the same check locally after a build:

```sh
docker run --rm --cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json --shm-size=1g --entrypoint bash ghcr.io/anthonypluth/darkroom-browser:latest /app/smoke.sh
```

For `sandbox_unavailable`, apply the current seccomp profile when recreating the container (on Unraid, add `--security-opt seccomp=/absolute/host/path/seccomp.json` to Extra Parameters) and check the host's `kernel.unprivileged_userns_clone` (where present) and `user.max_user_namespaces` (must be above 0). The worker does not disable the browser sandbox to work around a host restriction.

Other fixed launch reasons are `permission_denied` (check the configured UID/GID and writable browser home and share), `display_unavailable` (check Xvfb and `DISPLAY`), `browser_missing` (rebuild or pull the image), `profile_in_use` (another browser or a stale profile lock), and `browser_exited` (unrecognised startup exit; check memory limits as well). `TargetClosedError` by itself does not identify which cause applies.

For `profile_in_use`, stop every worker that can open that account's profile. Back up `_browser/<account id>/profile/`, then remove only stale `SingletonLock`, `SingletonSocket` and `SingletonCookie` entries before restarting. A recreated Docker container has a different hostname, so Chrome can mistake an old lock for a browser running on another machine. Never remove locks while another worker is using the profile; do not delete the profile to diagnose a launch error.

## Network

Neither 5900 nor 6080 is published to the host. x11vnc listens on the browser container's own loopback, so 5900 is reachable only from inside that container (websockify reaches it at `127.0.0.1:5900`); the image and compose expose only 6080. 6080 is on the compose network (`darkroom`, Traefik), and any container there can connect to it, so websockify refuses a request without the viewer secret: only the Traefik route, which adds it after the forward-auth sign-in, gets through. A container that reaches `darkroom-browser:6080` directly, without going through the sign-in, gets a 401.

Point a Traefik router at `darkroom-browser:6080` behind the same Pocket ID (OIDC) forward-auth middleware the `darkroom` service uses, on a hostname such as `browser.<your-domain>`, followed by a `headers` middleware that adds the viewer secret, and set `VNC_URL=https://browser.<your-domain>/vnc.html?autoconnect=1&resize=scale`. Traefik runs the middlewares in the order listed, so the secret is added only to a request that has passed the sign-in. Work out the header's value once with `printf 'darkroom:%s' "$DARKROOM_VNC_SECRET" | base64 -w0` and put it after `Basic `. As Docker labels on the browser service:

```yaml
labels:
  traefik.enable: "true"
  traefik.http.routers.darkroom-browser.rule: Host(`browser.example.internal`)
  traefik.http.routers.darkroom-browser.entrypoints: websecure
  traefik.http.routers.darkroom-browser.tls: "true"
  traefik.http.routers.darkroom-browser.middlewares: pocket-id-auth@file,darkroom-viewer-secret
  traefik.http.middlewares.darkroom-viewer-secret.headers.customrequestheaders.Authorization: "Basic <base64 of darkroom:secret>"
  traefik.http.services.darkroom-browser.loadbalancer.server.port: "6080"
```

The label holds the secret, so anyone who can read the container's labels can reach the viewer; that is anyone with access to the Docker socket, who can already control both containers. If the browser asks for a username and password on the viewer, the `headers` middleware is missing or its value is wrong: fix the label rather than typing the secret.

The route is internal-only (reached over the LAN or Tailscale, like `darkroom` itself). Anyone who gets through the middleware can drive the browser, which is signed in to the household's Google accounts: do not relax the middleware for this route, do not expose either port, and do not put the secret in `VNC_URL` (it goes into alerts).

Why a secret the route sends rather than a socket: in the two-container layout Traefik reaches the viewer over the compose network, so 6080 cannot be loopback-only or a Unix socket, and a secret generated at each start could not be given to Traefik. x11vnc, which only websockify needs, is loopback-only already. The proposed single-container design (#285, step C7a) moves these listeners to Unix sockets behind the app's own viewer relay; nothing here stands in its way (websockify can already connect to x11vnc over a Unix socket with `--unix-target`).

## Volumes

| Host | Container | Mode | Why |
| --- | --- | --- | --- |
| `${PHOTOS_ROOT}/_inbox` | `/photos/_inbox` | rw | The download job writes each Takeout part to `<person>/export-<export id>/.partial/`, checks its size and zip integrity, then moves it to `<person>/export-<export id>/`. Chromium keeps a download in progress under `.downloads/<account id>/`, on the same mount, so a finished part moves without a copy. The worker empties that folder's files when it opens a browser, and refuses to start the job if `_inbox`, `.downloads` or the account's folder is a link; deleting the account removes the folder. |
| `${PHOTOS_ROOT}/_browser` | `/photos/_browser` | rw | `<account id>/profile/` holds the persistent Chromium profile (the signed-in session), `<account id>/failures/<job id>/` the screenshot and scrubbed page snapshot of a failed job. |

Nothing else of the photo share is mounted: the worker cannot see or touch `originals/`.

### Users and the shared group

Both images default to uid 99 and gid 100, matching Unraid's `nobody:users`. Compose uses `user: "${PUID:-99}:${PGID:-100}"` for both services. The images reuse an existing group with that GID, rather than failing when `users` already exists. On Unraid, use `--user=99:100` in both containers' Extra Parameters and remove an old `--user=10001:10010` or `--user=10002:10010` override. Custom IDs are build arguments (`PUID`, `PGID`); rebuild both images and use matching runtime users. The folders remain private to their owner and group, but other host processes belonging to Unraid's `users` group can access group-writable files.

- Darkroom makes `_inbox`, `_inbox/.downloads`, each person's `_inbox/<person>/` and `manual/`, and each export's `_inbox/<person>/export-<id>/` (when the worker asks for its download plan) with mode `2770`: owner and group can read, write and enter, and nobody else can even list them, so what the inbox holds is never readable outside the group. The setgid bit keeps every folder made under them in the configured primary group (100 by default). The mode is set with `chmod` after `mkdir`, so it doesn't depend on the umask. When `darkroom` starts it gives the folders it owns under `_inbox` (`_inbox`, `.downloads` and each person's inbox) that group and mode, so a share set up before this change is fixed on the next start.
- The worker runs with umask `007` and makes `.downloads/<account id>/`, any missing export folder and `.partial/` with mode `2770` too, and sets each finished part to `0660`, so Darkroom can read the parts, unpack them and remove the export folder, and deleting an account can remove `.downloads/<account id>/`.
- A folder in the chain that is a link is refused on both sides, as before.

When upgrading from uid 10001/10002 and gid 10010, stop both services and back up the database, the `secret-key` file beside it in the data directory and the browser profiles first. On the host, change the ownership of Darkroom's data directory and its working folders, using the actual host data path in place of `./data`:

```sh
docker compose --profile browser down
chown -hR 99:100 ./data "$PHOTOS_ROOT/_inbox" "$PHOTOS_ROOT/_browser"
chgrp 100 "$PHOTOS_ROOT"
chmod g+rwx "$PHOTOS_ROOT"
find "$PHOTOS_ROOT/_inbox" -type d -exec chmod 2770 {} +
docker compose --profile browser up -d --build
```

Create these host folders before starting if they do not exist. Use your chosen IDs instead of `99:100` when overriding the defaults. Do not run a recursive ownership change over the entire photo share or Immich's storage. Browser profile files often have mode `0600`; changing only their group leaves them unreadable by uid 99, so their owner must be migrated too. Keep the profile to preserve its signed-in session.

Also check existing per-person folders and `_pixels` created by Darkroom: any entries still owned by the former service UID 10001 need ownership updated to the configured IDs so later imports, sidecars and Pixel copies remain writable. Limit that change to Darkroom-owned entries in those configured folders; leave files belonging to other services alone. To keep an existing installation on its former IDs temporarily, build both images with `PUID=10001 PGID=10010` and matching runtime users; browser profiles formerly owned by 10002 still need their owner changed to the chosen UID.

Chromium makes some folders inside its profile (`_browser/<id>/profile/`) with mode `0700`, whatever the umask. When a job closes its browser, the worker adds the group's read, write and search bits (`g+rwx`, never following a link and never touching others' bits) to each folder under `_browser/<id>/`, and leaves every file as Chromium made it (a folder's bits are all removing a file needs), so deleting the account can remove the folder. If the worker was killed or Chromium crashed before that ran, or the profile was last used by an earlier release, the folder keeps its `0700` folders: the delete then answers 500 and changes nothing ("the account's browser folder could not be removed"). Open the account's browser once more (any job closes it the same way), or remove the folder on the host, and delete the account again.

A download that stops receiving data is cancelled after five minutes: the worker watches the size of `_inbox/.downloads/<account id>/` while it waits, and when nothing has grown for that long it cancels the download, empties that folder and reports the part as `interrupted`, which counts as one of its attempts.

## Running a `noop` job end to end

1. Put `DARKROOM_BROWSER_TOKEN` in `.env`, then `docker compose --profile browser up -d`.
2. `docker compose logs -f darkroom-browser` should show `browser worker ready`.
3. Queue a job while signed in to Darkroom: `POST /api/browser/jobs` with `{"account_id": <id>, "kind": "noop"}` (or `GET /api/browser/jobs` to watch it).
4. Within `POLL_SECONDS` the job goes `queued → running → done`, its result reads `{"ok": true, "kind": "noop", "playwright": "1.49.1"}` (a `null` means the package is missing from the image) and `GET /api/status` shows `worker_last_seen`.
5. A job whose worker disappears is marked `failed` after the lease (`LEASE_SECONDS`, 3 minutes without a heartbeat) and a "Darkroom needs a look" alert opens. The worker gives up first: when Darkroom has acknowledged no heartbeat for 2 minutes (`GIVE_UP_SECONDS`), or Chromium fails to close, the worker logs it and exits with code 3, taking Chromium with it, and Docker restarts the container (`restart: unless-stopped`). It never reports a job finished while its browser may still be open, so deleting the account (which removes `_browser/<id>`) waits for that job.

Failure captures are served to signed-in users only, at `/api/browser/failures/<job>/screenshot.png` and `/api/browser/failures/<job>/page.html`. The worker reports which of the two it managed to write; the service keeps only names it knows that exist in the job's own failure folder, and the alert links to the screenshot, else the page snapshot, else nothing. The HTML is scrubbed before it is written (`darkroom_browser/scrub.py`; `tools/scrub_snapshot.py` applies the same rules to a saved page): scripts, form values, token-like attributes and `<meta>` tags, cookie pairs, JWTs, long opaque strings and query strings are removed.
