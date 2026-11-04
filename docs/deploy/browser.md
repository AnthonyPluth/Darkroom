# Deploying the browser worker

The `darkroom-browser` container is a stateless executor: `darkroom` owns the job queue in its database and the worker claims jobs over the internal API with a shared token, runs them in Chromium on a virtual display, and reports back. Nothing in it needs to be backed up; the per-account browser profiles and failure captures live on the photo share under `_browser/`.

## What runs inside

`deploy/browser/entrypoint.sh` starts, in order: Xvfb on `DISPLAY` (`:99`), x11vnc on `VNC_PORT` (5900) with no password, websockify + noVNC on `NOVNC_PORT` (6080), then `python -m darkroom_browser`. Chromium is launched by Playwright only while a job runs and exits with it, so the container idles between jobs. The image is `mcr.microsoft.com/playwright/python` pinned to one Playwright release; that image carries the browsers and their system libraries, not the Python package, so the build installs `playwright` (with `greenlet` and `pyee`) from `deploy/browser/requirements.txt`, pinned to the same release with `--require-hashes` (x86_64 and aarch64 wheels), and then imports it and checks the version. `tests/browser/test_image.py` fails when the image tag and the pip pin drift apart. To move to a new Playwright release, change both, refresh the hashes from PyPI, and re-run the noop job: its result carries the installed `playwright` version. The container runs as the non-root user `darkroom-browser` (uid 10002), drops every capability and sets `no-new-privileges`.

## Configuration

| Variable | Service | Meaning |
| --- | --- | --- |
| `DARKROOM_BROWSER_TOKEN` | both | Shared secret, at least 32 printable ASCII characters (`openssl rand -hex 32`). The worker refuses to start without it; `darkroom` answers 401 to every worker request while it is unset or short. Rotate it by changing `.env` and restarting both containers. |
| `DARKROOM_URL` | browser | Where the worker reaches `darkroom`: `http://darkroom:8000` on the compose network. |
| `PHOTOS_ROOT` | browser | `/photos` inside the container: `_inbox/` and `_browser/` are mounted there read-write. |
| `POLL_SECONDS` | browser | How often an idle worker asks for a job (15). |
| `DISPLAY` | browser | The Xvfb display (`:99`). |
| `VNC_URL` | darkroom | The address a person opens to see the browser (the Traefik route below). It goes into the "Google sign-in needed" alert. |

## Chromium sandbox: seccomp instead of `--no-sandbox`

Chromium's own sandbox needs user namespaces (`clone`/`unshare` with `CLONE_NEWUSER`), which Docker's default seccomp profile only allows to a process with `CAP_SYS_ADMIN`. Rather than start Chromium with `--no-sandbox`, the compose service applies `deploy/browser/seccomp.json`: an allowlist profile that permits the syscalls Chromium and Python need, including namespace creation, and denies everything else with `EPERM`. Keep `cap_drop: [ALL]`; the sandbox does not need any capability once namespaces are allowed. `shm_size: 1g` is set because Chromium uses `/dev/shm` for its renderers and the Docker default of 64 MB makes tabs crash.

If Chromium still fails to start on Unraid, check `docker logs darkroom-browser` for `clone` or `unshare` errors and compare the kernel's `kernel.unprivileged_userns_clone` (Debian-derived) or `user.max_user_namespaces` (must be above 0).

## Network

Neither 5900 nor 6080 is published to the host. The worker and the VNC endpoints are reachable only on the compose network (`darkroom`, Traefik). Point a Traefik router at `darkroom-browser:6080` behind the same Pocket ID (OIDC) forward-auth middleware the `darkroom` service uses, on a hostname such as `browser.<your-domain>`, and set `VNC_URL=https://browser.<your-domain>/vnc.html?autoconnect=1&resize=scale`. As a Docker label on the browser service:

```yaml
labels:
  traefik.enable: "true"
  traefik.http.routers.darkroom-browser.rule: Host(`browser.example.internal`)
  traefik.http.routers.darkroom-browser.entrypoints: websecure
  traefik.http.routers.darkroom-browser.tls: "true"
  traefik.http.routers.darkroom-browser.middlewares: pocket-id-auth@file
  traefik.http.services.darkroom-browser.loadbalancer.server.port: "6080"
```

The route is internal-only (reached over the LAN or Tailscale, like `darkroom` itself). Anyone who gets through the middleware can drive the browser, which is signed in to the household's Google accounts: do not relax the middleware for this route, and do not expose the VNC port.

## Volumes

| Host | Container | Mode | Why |
| --- | --- | --- | --- |
| `${PHOTOS_ROOT}/_inbox` | `/photos/_inbox` | rw | Downloaded Takeout parts land in the person's inbox (#31). |
| `${PHOTOS_ROOT}/_browser` | `/photos/_browser` | rw | `<account id>/profile/` holds the persistent Chromium profile (the signed-in session), `<account id>/failures/<job id>/` the screenshot and scrubbed page snapshot of a failed job. |

Nothing else of the photo share is mounted: the worker cannot see or touch `originals/`.

## Running a `noop` job end to end

1. Put `DARKROOM_BROWSER_TOKEN` in `.env`, then `docker compose --profile browser up -d`.
2. `docker compose logs -f darkroom-browser` should show `browser worker ready`.
3. Queue a job while signed in to Darkroom: `POST /api/browser/jobs` with `{"account_id": <id>, "kind": "noop"}` (or `GET /api/browser/jobs` to watch it).
4. Within `POLL_SECONDS` the job goes `queued → running → done`, its result reads `{"ok": true, "kind": "noop", "playwright": "1.49.1"}` (a `null` means the package is missing from the image) and `GET /api/status` shows `worker_last_seen`.
5. A job whose worker disappears is marked `failed` after the lease (`LEASE_SECONDS`, 3 minutes without a heartbeat) and a "Darkroom needs a look" alert opens. The worker gives up first: when Darkroom has acknowledged no heartbeat for 2 minutes (`GIVE_UP_SECONDS`), or Chromium fails to close, the worker logs it and exits with code 3, taking Chromium with it, and Docker restarts the container (`restart: unless-stopped`). It never reports a job finished while its browser may still be open, so deleting the account (which removes `_browser/<id>`) waits for that job.

Failure captures are served to signed-in users only, at `/api/browser/failures/<job>/screenshot.png` and `/api/browser/failures/<job>/page.html`. The worker reports which of the two it managed to write; the service keeps only names it knows that exist in the job's own failure folder, and the alert links to the screenshot, else the page snapshot, else nothing. The HTML is scrubbed before it is written (`darkroom_browser/scrub.py`; `tools/scrub_snapshot.py` applies the same rules to a saved page): scripts, form values, token-like attributes and `<meta>` tags, cookie pairs, JWTs, long opaque strings and query strings are removed.
