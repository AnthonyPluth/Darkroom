# Deploying Darkroom: the one container

The default deployment is one container, `ghcr.io/anthonypluth/darkroom-single`, built from `Dockerfile.single`. Its init (`darkroom_init/`) starts the app, then the browser worker and its display stack (Xvfb, x11vnc, websockify with noVNC), all as one unprivileged user (uid 99, gid 100 by default), restarts any of them that exits, and hands the browser token it generates at each start to the app and the worker on a pipe. Nothing in it runs as root. The design is [docs/design/single-container.md](../design/single-container.md) (sections 3 to 6 and 9).

The separate browser container (`darkroom-browser`) and its settings are gone (#327, a breaking release): its image is no longer published and the compose file has no `browser` profile. Rolling back to it is pinning the previous release (Going back to the two containers, below). The `darkroom` image and compose service stay as the app alone, without the browser worker, so with no Google sign-in, Takeout download or remote browser view; never run it against the same appdata folder or share as the one container.

| | |
| --- | --- |
| Image | `darkroom-single` (amd64) |
| Compose | `docker compose --profile single up -d darkroom-single` |
| Unraid | [deploy/unraid/darkroom.xml](../../deploy/unraid/darkroom.xml) |
| Browser token and the viewer's secret | None to set: generated at every start and handed on pipes |
| Remote browser view | The app's own Remote browser page, behind Darkroom's sign-in |
| Browser profiles and failure captures | `browser/` in appdata (`/data/browser`) |
| Published ports | 8000 only |

## Before the first deploy

Two things are the owner's and come first:

- **The scoped re-scan of #54.** Section 5 of the design lists what the browser phase changes in the trust boundary (the init and its per-child environments and token handoff, every process sharing one user, `Dockerfile.single`, the seccomp profile and capability set applied to every process, the viewer relay, the worker's authentication, the browser profiles' new home). That re-scan is recorded on #54, with no HIGH or CRITICAL finding open, before the one container is deployed.
- **Spike S5a (#312).** The backup, restore and rollback below are rehearsed on a fresh one container and written up in `docs/spikes/restore-app-browser.md`. Until then they are the documented procedure, not a rehearsed one.

## The one manual host step: the seccomp profile

Chrome's sandbox needs user namespaces, which Docker's default seccomp profile refuses to an unprivileged process, so the container runs under `deploy/browser/seccomp.json` instead of with `--no-sandbox` ([browser.md](browser.md#chromium-sandbox-seccomp-instead-of---no-sandbox)). In one container that profile covers every process. Docker reads it from the host when the container is created, which the container cannot do for itself, so it is put on the host by hand:

- **Compose:** nothing to copy. The service points at `./deploy/browser/seccomp.json` in the checkout; set `DARKROOM_TAG` to the release tag the checkout is at, so the image compose pulls comes from the same release (below).
- **Unraid:** copy `deploy/browser/seccomp.json` of the release you run to `/boot/config/darkroom/seccomp.json`, the path the template's Extra Parameters name:

  ```sh
  mkdir -p /boot/config/darkroom
  wget -O /boot/config/darkroom/seccomp.json https://raw.githubusercontent.com/AnthonyPluth/Darkroom/<release>/deploy/browser/seccomp.json
  ```

  `<release>` is a release tag (for example `v1.2.3`), and the template's **Repository** names the same tag (below), so the profile and the image come from one release. Keep the file outside every folder mapped into a container (appdata and the photo share included): the container can write there, and a profile it could edit would no longer limit it. `/boot/config` is on the flash drive, survives a reboot and is mapped into nothing.

Before each upgrade, compare `deploy/browser/seccomp.json` of the new release with the copy on the host (`git diff <old release> <new release> -- deploy/browser/seccomp.json` in a checkout); when it changed, copy the new file before recreating the container. If the file is missing, Docker refuses to start the container (`opening seccomp profile (...) failed: open ...: no such file or directory`); it never runs without it.

## Unraid

1. Put the seccomp profile in place (above).
2. Create the appdata folder and give it to Darkroom's user, so the app can write its database and secret key there (Docker would otherwise create it as root): `mkdir -p /mnt/user/appdata/darkroom && chown 99:100 /mnt/user/appdata/darkroom`. Prepare the photo share as the runbook says ([runbook](../runbook.md#darkroom-could-not-create-the-persons-folders)).
3. Add the template: copy [deploy/unraid/darkroom.xml](../../deploy/unraid/darkroom.xml) to `/boot/config/plugins/dockerMan/templates-user/my-darkroom.xml`, then Docker, Add Container, and choose `darkroom` under Template. Change **Repository** from `ghcr.io/anthonypluth/darkroom-single:latest` to the release whose profile you copied (`ghcr.io/anthonypluth/darkroom-single:v1.2.3`): `latest` moves with every release, and the profile on the flash drive does not.
4. Set **Photo share** to the share's path (for example `/mnt/user/photos`), and the sign-in variables (`OIDC_*`, `DARKROOM_PUBLIC_URL`) and `IMMICH_URL`. The template does not turn on the first-run setup: its routes answer 401 unless `DARKROOM_WIZARD=1` is set, it has no screens yet (#305), and it may be on only while port 8000 cannot be reached from outside the LAN, IPv6 included ([runbook](../runbook.md#the-first-run-setup-api), `SECURITY.md`). If you use it anyway, add `DARKROOM_WIZARD=1` as a variable yourself, under those conditions, and remove it once the setup is done. Leave `DARKROOM_SECRET_KEY` empty: the key is generated into appdata on the first start. Don't change Extra Parameters: they hold the user, the shared memory size, `--cap-drop=ALL`, `no-new-privileges` and the seccomp profile.
5. Apply, then check from another machine on the LAN: `curl -fsS http://<server>:8000/healthz` answers `{"ok": true, "version": ...}`, and the container's log (Docker, the container's icon, Logs) shows `"event": "init started"`.

In bridge mode Docker publishes port 8000 on every address the server has, not only its LAN address, and its proxy relays IPv6 connections to it too: anything that can reach the server on any address can reach Darkroom. Keep it so: never forward the port from the router, don't give the server a public IPv6 address that the firewall lets through, and treat Tailscale or VPN addresses as part of who can reach it. Unraid is believed to set `TZ` for every container (UNVERIFIED until spike S4, #311); if the times in Darkroom's log are UTC, add a `TZ` variable. Appdata on Unraid's user-share layer is believed to be unreliable for SQLite's locking on some setups; if the log shows `database is locked`, point **Appdata** at the pool's direct path (for example `/mnt/cache/appdata/darkroom`).

## Docker Compose

From a checkout of the release you run (a release tag such as `v1.2.3`, not `main`), with the photo share at `/path/to/the/photo/share`:

```sh
git checkout v1.2.3
printf 'DARKROOM_TAG=v1.2.3\nPHOTOS_ROOT=/path/to/the/photo/share\n' >> .env
mkdir -p data && sudo chown 99:100 data
docker compose --profile single pull darkroom-single
docker compose --profile single up -d darkroom-single
curl -fsS http://127.0.0.1:8000/healthz
```

`DARKROOM_TAG` in `.env` picks the image tag of every service, so the image and the checkout's seccomp profile come from the same release for every later `docker compose` command, in any shell; left unset, compose pulls `latest`, which matches only a checkout of the newest release. Keep it in `.env` rather than exporting it in one shell, and change it only together with the checkout (Upgrading, below). Always name the service: `--profile single up -d` without `darkroom-single` also starts the app-only `darkroom` service, which has no profile. The port is published on the host's loopback only, for a reverse proxy on the same host (Traefik) to reach; `docker compose --profile single up -d --build darkroom-single` builds the image from the checkout instead of pulling it, for custom `PUID` and `PGID` (both are build arguments, as in [browser.md](browser.md#users-and-the-shared-group)).

Settings go in `.env` next to `docker-compose.yml`, which compose reads for `PHOTOS_ROOT` and hands to the container whole. Copy only what you set from `.env.example`; in particular leave out `DARKROOM_HOST`, `DARKROOM_PORT`, `DARKROOM_PHOTOS_ROOT`, `DARKROOM_DATA_DIR` and `DARKROOM_BROWSER_ROOT`, which the image sets: `DARKROOM_HOST=127.0.0.1` from the example would make the app listen on the container's own loopback, where the published port cannot reach it.

The init generates the browser token and the viewer secret at every start and hands them on pipes, and the Remote browser page and the sign-in alert show the app's own viewer, at `/viewer/` under `DARKROOM_PUBLIC_URL` (so set that address, or the page has no viewer). The worker reaches the app on the container's loopback. `DARKROOM_BROWSER_TOKEN`, `DARKROOM_URL`, `VNC_URL` and `DARKROOM_VNC_SECRET`, the separate browser container's settings, are ignored: when one is still in `.env`, `python run.py check-config` and the start-up log name it as set and ignored, never with its value, and it can be removed. The Traefik route needs only `darkroom-single:8000`, behind the same sign-in as before; a `browser.<your-domain>` route and its viewer-secret label from the two containers go.

## Syncthing: in the image, off by default

The image carries Darkroom's own Syncthing (v2.1.7, the official release, checked against its signed checksum when the image is built). It stays off until `DARKROOM_SYNCTHING=1` is set. With it on, the init starts Syncthing with its home in `/data/syncthing` (a fresh identity on the first start; an existing Syncthing's keys are never imported), always paused, with automatic upgrades, usage and crash reporting, global and local discovery, relays and NAT traversal off, and its API on a socket inside the container only. With it on, Darkroom configures Syncthing itself and photos start going to the Pixels. Every Pixel whose Syncthing device ID is filled in on the People page's Pixel form gets that device and one Send Only folder, its own `_pixels/<pixel id>/` (the copies of the originals of the people it serves, never `originals/` itself), shared with that device alone, with no versioning and `*.xmp` ignored. Darkroom removes any other folder or device it finds in Syncthing, reads the whole configuration back, and only then resumes the folder and its device, which happens after the first hourly tend has checked the Pixel's copies; it also turns local discovery on. A Pixel without a device ID gets nothing, and so does one whose device ID is invalid or used by another Pixel, or whose `_pixels/<pixel id>/` (or `_pixels/` itself) is a link. A failed share check ([share.md](share.md)) or any Syncthing error pauses every folder and device. Global discovery, relays and NAT traversal stay off, so a Pixel reaches Syncthing only on the local network: through 22000 (TCP and UDP) and, for local discovery, 21027/udp. With the bridge network compose uses, publish those ports only when the Pixels should sync, and know that discovery broadcasts don't cross the bridge; with host or Unraid's br0 networking, Syncthing listens on the LAN directly and local discovery announces the server to every device there. Leave `DARKROOM_SYNCTHING` unset, or remove the device IDs, until you mean photos to leave the server. `check-config` says whether it is on. The way back is to unset the variable and recreate the container; `/data/syncthing` stays, and is backed up with appdata like the secret key, since it holds Syncthing's device key.

## Moving from the two containers

The two containers are not in this release. Move from them on the newest `v3` release, the last one that publishes their images and has the `browser` profile, following that release's copy of this page (`git checkout` its tag and read `docs/deploy/one-container.md`; its `DARKROOM_TAG=v1.2.3` steps name that tag). That keeps the two containers of the same release as the way back while you need one. Once the one container runs there, upgrade it to this release (Upgrading, below) and remove `DARKROOM_BROWSER_TOKEN`, `DARKROOM_URL`, `VNC_URL` and `DARKROOM_VNC_SECRET` from `.env`. The profiles are copied into appdata with `copy-browser-state` ([browser.md](browser.md#copying-browser-profiles-copy-browser-state)), which this release keeps.

## Going back to the two containers

Rollback is pinning the previous release. The release that removed the two containers (#327) added no migration, so the newest `v3` release starts on the database it left. Stop the one container, check out that release's tag, set `DARKROOM_TAG` in `.env` to it, and follow its copy of this page, "Going back to the two containers", which also copies the profiles back to the share (`copy-browser-state --set-aside`). On Unraid, give the containers that release's tag as **Repository**. A release cannot start on a database a newer one has migrated; after a later release that migrates it, restore the backup taken before that upgrade (design section 9).

`_browser/` on the share, and any `_browser.before-<time>`, are removed by hand once you no longer need the way back; no Darkroom code deletes them. Until then they still hold every account's signed-in session, including accounts deleted later ([browser.md](browser.md#the-browser-profiles-in-appdata-the-one-container) says what to remove before using one again). `copy-browser-state` only ever copies between the share's own `_browser/` and a folder outside the share: any other folder on the share, or one that holds it, is refused before anything is copied or renamed.

## Backup and restore

Back up the appdata folder with the container stopped (or with Unraid's appdata backup, which stops it): the database, the `secret-key` file and the browser profiles are consistent only together. The backup holds the key that unseals the stored secrets, so keep it as carefully as those secrets. With `DARKROOM_SECRET_KEY` set, the key is not in appdata: keep it with the backup, matched to the same time, and a restore starts only once the same key is supplied again ([runbook](../runbook.md#the-secret-key)). With `DATABASE_URL` set, the database is not in appdata either: take a dump at the same stopped point, and restore both together.

To restore, stop the container, put the appdata folder back (owner 99:100), and start it. The photo share is not part of this backup: `_inbox/` holds imports that are not resolved yet, and `originals/` and `google-only/` are backed up as today.

## Upgrading

With Compose, check out the new release tag and change the `DARKROOM_TAG` line in `.env` to it, then pull and recreate: `docker compose --profile single pull darkroom-single && docker compose --profile single up -d darkroom-single`; the checkout brings the release's seccomp profile. On Unraid, copy the new release's seccomp profile first when it changed (above), then change **Repository** to the new tag and Apply. A release that migrates the database cannot be undone by starting the older image; take a backup before upgrading and restore it to go back (design section 9).
