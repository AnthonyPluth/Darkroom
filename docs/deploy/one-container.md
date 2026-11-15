# Deploying Darkroom: the one container

The default deployment is one container, `ghcr.io/anthonypluth/darkroom-single`, built from `Dockerfile.single`. Its init (`darkroom_init/`) starts the app, then the browser worker and its display stack (Xvfb, x11vnc, websockify with noVNC), all as one unprivileged user (uid 99, gid 100 by default), restarts any of them that exits, and hands the browser token it generates at each start to the app and the worker on a pipe. Nothing in it runs as root. The design is [docs/design/single-container.md](../design/single-container.md) (sections 3 to 6 and 9).

The two-container layout (`darkroom` and `darkroom-browser`) is the fallback. It is configured exactly as before ([browser.md](browser.md)) and kept for one minor release after this one became the default; then it is removed (#327). Never run both layouts against the same appdata folder or share at once.

| | One container | Two containers (fallback) |
| --- | --- | --- |
| Images | `darkroom-single` (amd64) | `darkroom` and `darkroom-browser` |
| Compose | `docker compose --profile single up -d darkroom-single` | `docker compose --profile browser up -d` |
| Unraid | [deploy/unraid/darkroom.xml](../../deploy/unraid/darkroom.xml) | Two containers set up by hand |
| Browser token, `DARKROOM_URL`, `VNC_URL`, `DARKROOM_VNC_SECRET` | None: generated, on loopback, or served by the app | Set by hand in both containers |
| Remote browser view | The app's own Remote browser page, behind Darkroom's sign-in | A second Traefik route to `darkroom-browser:6080` |
| Browser profiles and failure captures | `browser/` in appdata (`/data/browser`) | `_browser/` on the photo share |
| Published ports | 8000 only | 8000 (the worker publishes none) |

## Before the first deploy

Two things are the owner's and come first:

- **The scoped re-scan of #54.** Section 5 of the design lists what the browser phase changes in the trust boundary (the init and its per-child environments and token handoff, every process sharing one user, `Dockerfile.single`, the seccomp profile and capability set applied to every process, the viewer relay, the worker's authentication, the browser profiles' new home). That re-scan is recorded on #54, with no HIGH or CRITICAL finding open, before the one container is deployed.
- **Spike S5a (#312).** The backup, restore and rollback below are rehearsed on a fresh one container and written up in `docs/spikes/restore-app-browser.md`. Until then they are the documented procedure, not a rehearsed one.

## The one manual host step: the seccomp profile

Chrome's sandbox needs user namespaces, which Docker's default seccomp profile refuses to an unprivileged process, so the container runs under `deploy/browser/seccomp.json` instead of with `--no-sandbox` ([browser.md](browser.md#chromium-sandbox-seccomp-instead-of---no-sandbox)). In one container that profile covers every process. Docker reads it from the host when the container is created, which the container cannot do for itself, so it is put on the host by hand:

- **Compose:** nothing to do. The service points at `./deploy/browser/seccomp.json` in the checkout, which matches the checked-out release.
- **Unraid:** copy `deploy/browser/seccomp.json` of the release you run to `/boot/config/darkroom/seccomp.json`, the path the template's Extra Parameters name:

  ```sh
  mkdir -p /boot/config/darkroom
  wget -O /boot/config/darkroom/seccomp.json https://raw.githubusercontent.com/AnthonyPluth/Darkroom/<release>/deploy/browser/seccomp.json
  ```

  `<release>` is the tag of the image you pull (for example `v1.2.3`), so the profile and the image match. Keep the file outside every folder mapped into a container (appdata and the photo share included): the container can write there, and a profile it could edit would no longer limit it. `/boot/config` is on the flash drive, survives a reboot and is mapped into nothing.

Before each upgrade, compare `deploy/browser/seccomp.json` of the new release with the copy on the host (`git diff <old release> <new release> -- deploy/browser/seccomp.json` in a checkout); when it changed, copy the new file before recreating the container. If the file is missing, Docker refuses to start the container (`opening seccomp profile (...) failed: open ...: no such file or directory`); it never runs without it.

## Unraid

1. Put the seccomp profile in place (above).
2. Create the appdata folder and give it to Darkroom's user, so the app can write its database and secret key there (Docker would otherwise create it as root): `mkdir -p /mnt/user/appdata/darkroom && chown 99:100 /mnt/user/appdata/darkroom`. Prepare the photo share as the runbook says ([runbook](../runbook.md#darkroom-could-not-create-the-persons-folders)).
3. Add the template: copy [deploy/unraid/darkroom.xml](../../deploy/unraid/darkroom.xml) to `/boot/config/plugins/dockerMan/templates-user/my-darkroom.xml`, then Docker, Add Container, and choose `darkroom` under Template.
4. Set **Photo share** to the share's path (for example `/mnt/user/photos`), and the sign-in and Immich variables (or leave them empty and use the first-run setup, [runbook](../runbook.md#the-first-run-setup-api)). Leave `DARKROOM_SECRET_KEY` empty: the key is generated into appdata on the first start. Don't change Extra Parameters: they hold the user, the shared memory size, `--cap-drop=ALL`, `no-new-privileges` and the seccomp profile.
5. Apply, then check from another machine on the LAN: `curl -fsS http://<server>:8000/healthz` answers `{"ok": true, "version": ...}`, and the container's log (Docker, the container's icon, Logs) shows `"event": "init started"`.

The template publishes port 8000 on the server's LAN address only; never forward it from the router. Unraid is believed to set `TZ` for every container (UNVERIFIED until spike S4, #311); if the times in Darkroom's log are UTC, add a `TZ` variable. Appdata on Unraid's user-share layer is believed to be unreliable for SQLite's locking on some setups; if the log shows `database is locked`, point **Appdata** at the pool's direct path (for example `/mnt/cache/appdata/darkroom`).

## Docker Compose

From a checkout of the release you run, with the photo share at `/path/to/the/photo/share`:

```sh
git checkout <release>
mkdir -p data && sudo chown 99:100 data
export PHOTOS_ROOT=/path/to/the/photo/share
docker compose --profile single pull darkroom-single
docker compose --profile single up -d darkroom-single
curl -fsS http://127.0.0.1:8000/healthz
```

Always name the service: `--profile single up -d` without `darkroom-single` also starts the two-container `darkroom` service, which has no profile. The port is published on the host's loopback only, for a reverse proxy on the same host (Traefik) to reach; `docker compose --profile single up -d --build darkroom-single` builds the image from the checkout instead of pulling it, for custom `PUID` and `PGID` (both are build arguments, as in [browser.md](browser.md#users-and-the-shared-group)).

Settings go in `.env` next to `docker-compose.yml`, which compose reads for `PHOTOS_ROOT` and hands to the container whole. Copy only what you set from `.env.example`; in particular leave out `DARKROOM_HOST`, `DARKROOM_PORT`, `DARKROOM_PHOTOS_ROOT`, `DARKROOM_DATA_DIR` and `DARKROOM_BROWSER_ROOT`, which the image sets: `DARKROOM_HOST=127.0.0.1` from the example would make the app listen on the container's own loopback, where the published port cannot reach it.

In the one container, leave `DARKROOM_BROWSER_TOKEN`, `VNC_URL` and `DARKROOM_VNC_SECRET` empty: the init generates the token and the viewer secret and hands them on pipes, and the Remote browser page and the sign-in alert show the app's own viewer, at `/viewer/` under `DARKROOM_PUBLIC_URL` (so set that address, or the page has no viewer). `DARKROOM_URL` is ignored there: the init gives the worker the app's loopback address. `VNC_URL`, when set, still wins and points the page at that address instead, which in one container is a route to nothing; `python run.py check-config` says so. The Traefik route needs only `darkroom-single:8000`, behind the same sign-in as before; the separate `browser.<your-domain>` route and its viewer-secret label go.

## Syncthing: in the image, off by default

The image carries Darkroom's own Syncthing (v2.1.7, the official release, checked against its signed checksum when the image is built). It stays off until `DARKROOM_SYNCTHING=1` is set. With it on, the init starts Syncthing with its home in `/data/syncthing` (a fresh identity on the first start; an existing Syncthing's keys are never imported), always paused, with automatic upgrades, usage and crash reporting, global and local discovery, relays and NAT traversal off, and its API on a socket inside the container only. Nothing syncs yet: no folder or device is configured, and a later release adds the pairing and resumes what Darkroom approves. Don't publish 22000 or 21027 yet. `check-config` says whether it is on. The way back is to unset the variable and recreate the container; `/data/syncthing` stays, and is backed up with appdata like the secret key, since it holds Syncthing's device key.

## Moving from the two containers

The database and the generated secret key stay where they are, directly in appdata, and the one container uses the same appdata folder. The browser profiles move from `_browser/` on the share into `browser/` in appdata with a copy that is checked before it is used and never removes the source (design section 9). With Compose:

1. Keep the two containers' settings for the way back: `cp .env .env.two-containers`.
2. Stop both containers: `docker compose --profile browser down`.
3. Back up `./data` and `$PHOTOS_ROOT/_browser`, and with `DATABASE_URL` set, a dump of that database taken now ([postgres.md](postgres.md)).
4. Copy and verify the profiles, as Darkroom's user under the same sandbox:

   ```sh
   docker compose --profile single run --rm --no-deps -w /app darkroom-single python run.py copy-browser-state --source /photos/_browser --target /data/browser
   ```

   What it checks, and what to do when it stops, is in [browser.md](browser.md#moving-existing-profiles-into-appdata).
5. In `.env`, empty `DARKROOM_BROWSER_TOKEN`, `VNC_URL` and `DARKROOM_VNC_SECRET`, and move the Traefik route from `darkroom:8000` to `darkroom-single:8000`.
6. Start the one container: `docker compose --profile single up -d darkroom-single`, then `curl -fsS http://127.0.0.1:8000/healthz`. Within a minute the Remote browser page shows the worker as last seen.

On Unraid, stop both containers, back up as in step 3, put the seccomp profile in place, and run step 4 with the template's flags and paths (the share and appdata paths are yours):

```sh
docker run --rm --user 99:100 --cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=/boot/config/darkroom/seccomp.json \
  -v /mnt/user/appdata/darkroom:/data -v /mnt/user/photos:/photos -w /app ghcr.io/anthonypluth/darkroom-single:<release> \
  python run.py copy-browser-state --source /photos/_browser --target /data/browser
```

Then add the container from the template with the same appdata folder (Unraid, above) and leave the two old containers stopped, with their autostart turned off on the Docker tab.

## Going back to the two containers

Go back to the two containers of the same release: their images are published with every release until #327, and they start on the database the one container used. An older release than the one you moved with cannot start on a database a newer one has migrated; for that, restore the backup from step 3 (design section 9).

- **Before the one container has done any browser work** (no sign-in, no account added, no failure capture): stop it (`docker compose --profile single stop darkroom-single`), `cp .env.two-containers .env`, and `docker compose --profile browser up -d`. The profiles on the share are as you left them.
- **Afterwards**, the share's copy is stale: copy appdata back first, with the container stopped, then start the two containers:

  ```sh
  docker compose --profile single stop darkroom-single
  docker compose --profile single run --rm --no-deps -w /app darkroom-single python run.py copy-browser-state --source /data/browser --target /photos/_browser --set-aside
  cp .env.two-containers .env
  docker compose --profile browser up -d
  ```

  `--set-aside` renames the old `_browser/` to `_browser.before-<time>` only after the copy is verified; nothing is deleted in either direction.

Remove the stopped one container (`docker compose --profile single rm darkroom-single`) so it is not started again next to the two. `_browser/` on the share, and any `_browser.before-<time>`, are removed by hand once you no longer need the way back; no Darkroom code deletes them. Until then they still hold every account's signed-in session, including accounts deleted later, and so does an older `browser/` left in appdata after going back ([browser.md](browser.md#going-back-to-two-containers) says what to remove before using one again). `copy-browser-state` only ever copies between the share's own `_browser/` and a folder outside the share: any other folder on the share, or one that holds it, is refused before anything is copied or renamed.

## Backup and restore

Back up the appdata folder with the container stopped (or with Unraid's appdata backup, which stops it): the database, the `secret-key` file and the browser profiles are consistent only together. The backup holds the key that unseals the stored secrets, so keep it as carefully as those secrets. With `DARKROOM_SECRET_KEY` set, the key is not in appdata: keep it with the backup, matched to the same time, and a restore starts only once the same key is supplied again ([runbook](../runbook.md#the-secret-key)). With `DATABASE_URL` set, the database is not in appdata either: take a dump at the same stopped point, and restore both together.

To restore, stop the container, put the appdata folder back (owner 99:100), and start it. The photo share is not part of this backup: `_inbox/` holds imports that are not resolved yet, and `originals/` and `google-only/` are backed up as today.

## Upgrading

Pull and recreate: `docker compose --profile single pull darkroom-single && docker compose --profile single up -d darkroom-single`, or on Unraid, Check for Updates and Apply. Before that, check whether the seccomp profile changed, and copy it first (above). A release that migrates the database cannot be undone by starting the older image; take a backup before upgrading and restore it to go back (design section 9).
