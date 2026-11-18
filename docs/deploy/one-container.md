# Deploying Darkroom: the one container

Darkroom runs as one container, `ghcr.io/anthonypluth/darkroom`, built from `Dockerfile`. Its init (`darkroom_init/`) starts the app, then the browser worker and its display stack (Xvfb, x11vnc, websockify with noVNC), all as one unprivileged user (uid 99, gid 100 by default), restarts any of them that exits, and hands the browser token it generates at each start to the app and the worker on a pipe. Nothing in it runs as root. The design is [docs/design/single-container.md](../design/single-container.md) (sections 3 to 6 and 9).

The separate browser container and its settings are gone (#327, a breaking release): its image is no longer published and the compose file has no `browser` profile. Rolling back to it is pinning the previous release (Going back to the two containers, below). There is no app-only image or compose service any more (#400): the `darkroom` image is the one container, and the compose file's one service, `darkroom`, runs it. The app runs alone only from a source checkout (`python run.py serve`), for development and the tests.

| | |
| --- | --- |
| Image | `darkroom` (amd64) |
| Compose | `docker compose up -d` |
| Unraid | [deploy/unraid/darkroom.xml](../../deploy/unraid/darkroom.xml) |
| Browser token and the viewer's secret | None to set: generated at every start and handed on pipes |
| Remote browser view | The app's own Remote browser page, behind Darkroom's sign-in |
| Browser profiles and failure captures | `browser/` in appdata (`/data/browser`) |
| Network | The host's network (Syncthing's local discovery needs it) |
| Ports it listens on | 8000 (web UI; on the host's loopback with compose, on every address on Unraid), 22000 TCP and UDP and 21027 UDP (Darkroom's Syncthing, on every address) |
| Syncthing | Always on, with no switch ([Syncthing: always on](#syncthing-always-on)) |

## Before the first deploy

These are the owner's and come first:

- **The scoped re-scan of #54.** Section 5 of the design lists what the browser phase changes in the trust boundary (the init and its per-child environments and token handoff, every process sharing one user, the merged `Dockerfile`, the seccomp profile and capability set applied to every process, the viewer relay, the worker's authentication, the browser profiles' new home). That re-scan is recorded on #54, with no HIGH or CRITICAL finding open, before the one container is deployed.
- **Spike S5a (#312).** The backup, restore and rollback below are rehearsed on a fresh one container and the result recorded in [docs/spikes/restore-app-browser.md](../spikes/restore-app-browser.md). #312 was closed with no result recorded; that page lists what to run and record. Until then they are the documented procedure, not a rehearsed one.
- **The scoped re-scan of the Syncthing phase on #54.** Section 5 of the design lists it: the Syncthing provider and reconciler, the pairing routes, the bundled binary and how it is pinned, its listeners (22000, 21027 and the API's socket) and the folder-path and Send Only invariants. It is recorded on #54, with no HIGH or CRITICAL finding open, before Darkroom's Syncthing is deployed.
- **Spike S5b (#313).** The transport's backup and restore (a lost share and a bound replacement, the placed copies found missing and placed again before a folder is shared, nothing deleted from a Pixel, an unbound or unmounted share refused, a database older than the share, the stale restore) is rehearsed with Darkroom's Syncthing and the result recorded in [docs/spikes/restore-transport.md](../spikes/restore-transport.md) before Darkroom's Syncthing is deployed. #313 was closed with no result recorded; that page lists what to run and record. Until then the restore below covers the transport only as the documented procedure, not a rehearsed one.
- **Spike S4 (#311).** What this page says about Unraid's networking, the ports a Pixel needs, `TZ`, appdata on the user-share layer and an original Pixel pairing with the pinned Syncthing is UNVERIFIED until S4's result is recorded in [docs/spikes/unraid-pixel.md](../spikes/unraid-pixel.md). #311 was closed with no result recorded; that page lists what to run and record on the server and the Pixel.

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
3. Add the template: copy [deploy/unraid/darkroom.xml](../../deploy/unraid/darkroom.xml) to `/boot/config/plugins/dockerMan/templates-user/my-darkroom.xml`, then Docker, Add Container, and choose `darkroom` under Template. Change **Repository** from `ghcr.io/anthonypluth/darkroom:latest` to the release whose profile you copied (`ghcr.io/anthonypluth/darkroom:v1.2.3`): `latest` moves with every release, and the profile on the flash drive does not.
4. Set **Photo share** to the share's path (for example `/mnt/user/photos`), and the sign-in variables (`OIDC_*`, `DARKROOM_PUBLIC_URL`) and `IMMICH_URL`. The template does not turn on the first-run setup: its routes answer 401 unless `DARKROOM_WIZARD=1` is set, you open it at `http://<server's address>:8000/setup` (directly, not through Traefik), and it may be on only while port 8000 cannot be reached from outside the LAN, IPv6 included ([runbook](../runbook.md#the-first-run-setup-api), `SECURITY.md`). If you use it anyway, add `DARKROOM_WIZARD=1` as a variable yourself, under those conditions, and remove it once the setup is done. Leave `DARKROOM_SECRET_KEY` empty: the key is generated into appdata on the first start. The template has no Syncthing switch: Darkroom's own Syncthing always runs ([Syncthing](#syncthing-always-on), below). Don't change Extra Parameters (they hold the user, the shared memory size, `--cap-drop=ALL`, `no-new-privileges` and the seccomp profile) or **Network Type**, which is `host`.
5. Apply, then check from another machine on the LAN: `curl -fsS http://<server>:8000/healthz` answers `{"ok": true, "version": ...}`, and the container's log (Docker, the container's icon, Logs) shows `"event": "init started"`. To see which release runs, look for the log's `"event": "darkroom started"` line, which names the release and the build's commit (`"version": "v1.2.3"`, `"commit": "abc1234"`), or the Version row in the About card at the bottom of the Status page. `/healthz`'s `version` is a fixed package number, not the release. An image built from a checkout (compose's `build`, as for a custom `PUID` or `PGID`) has no release build args and shows `dev`.

The template runs the container on the host's network, for Syncthing's local discovery (decision 7 of the design): its port entries say what listens and publish nothing. Port 8000 listens on every address the server has, as Docker's bridge already published it, so host networking does not widen who can reach the web UI. It is IPv4 only (`0.0.0.0`), where the bridge's proxy also relayed IPv6 connections. Syncthing's 22000 (TCP and UDP) and 21027 (UDP) listen on every address as well. Anything that can reach the server on any address can reach them: never forward them from the router, don't give the server a public IPv6 address that the firewall lets through, and treat Tailscale or VPN addresses as part of who can reach them. Syncthing accepts only devices Darkroom has configured, and global discovery, relays and NAT traversal are off. VNC, noVNC and Syncthing's API listen on no port at all, only on sockets in `/run/darkroom` inside the container (C7a, design section 9). Port 8000 already in use on the server stops the web UI from starting; 22000 or 21027 already in use (an existing Syncthing, for example) fails silently ([Ports already in use](#ports-already-in-use), below): stop or retire whatever holds them first. Unraid is believed to set `TZ` for every container (UNVERIFIED until spike S4, #311); if the times in Darkroom's log are UTC, add a `TZ` variable. **Appdata** stays on the user share (`/mnt/user/appdata/darkroom`), which works on every server. Syncthing's socket is in `/run/darkroom`, never in appdata, so only the database's locking (Darkroom's, and Syncthing's index in `/data/syncthing`) depends on that layer. Whether the user-share layer is reliable for that is UNVERIFIED until S4 (#311); if the log shows `database is locked`, point **Appdata** at the pool's direct path (for example `/mnt/cache/appdata/darkroom`) with the container stopped, moving the folder as it is.

## Docker Compose

From a checkout of the release you run (a release tag such as `v1.2.3`, not `main`), with the photo share at `/path/to/the/photo/share`:

```sh
git checkout v1.2.3
printf 'DARKROOM_TAG=v1.2.3\nPHOTOS_ROOT=/path/to/the/photo/share\n' >> .env
mkdir -p data && sudo chown 99:100 data
docker compose pull
docker compose up -d
curl -fsS http://127.0.0.1:8000/healthz
```

`DARKROOM_TAG` in `.env` picks the image tag, so the image and the checkout's seccomp profile come from the same release for every later `docker compose` command, in any shell; left unset, compose pulls `latest`, which matches only a checkout of the newest release. Keep it in `.env` rather than exporting it in one shell, and change it only together with the checkout (Upgrading, below). The service runs on the host's network (`network_mode: host`), for Syncthing's local discovery, and compose sets `DARKROOM_HOST=127.0.0.1`, so the web port listens on the host's loopback only, exactly where the published port was before; Syncthing listens on 22000 (TCP and UDP) and 21027 (UDP) of every address of the host, so keep those off the internet. A reverse proxy reaches the web port only from the host itself ([Traefik](#traefik), below). `docker compose up -d --build` builds the image from the checkout instead of pulling it, for custom `PUID` and `PGID` (both are build arguments, as in [browser.md](browser.md#users-and-the-shared-group)).

Settings go in `.env` next to `docker-compose.yml`, which compose reads for `PHOTOS_ROOT` and hands to the container whole. Copy only what you set from `.env.example`; in particular leave out `DARKROOM_PORT`, `DARKROOM_PHOTOS_ROOT`, `DARKROOM_DATA_DIR` and `DARKROOM_BROWSER_ROOT`, which the image sets, and `DARKROOM_HOST`, which the compose service sets to `127.0.0.1` whatever `.env` says. Port 8000 must be free on the host's loopback.

The init generates the browser token and the viewer secret at every start and hands them on pipes, and the Remote browser page and the sign-in alert show the app's own viewer, at `/viewer/` under `DARKROOM_PUBLIC_URL` (so set that address, or the page has no viewer). The worker reaches the app only on a private socket inside the container, `/run/darkroom/worker-api.sock`, never on the web port: the app answers 404 to the worker's routes on 8000, so a process on the host's loopback can neither receive the worker's token nor hand it a job (`SECURITY.md`, "The worker's API is on a private socket"). `DARKROOM_BROWSER_TOKEN`, `DARKROOM_URL`, `VNC_URL` and `DARKROOM_VNC_SECRET`, the separate browser container's settings, are ignored: when one is still in `.env`, `python run.py check-config` and the start-up log name it as set and ignored, never with its value, and it can be removed. A `browser.<your-domain>` Traefik route and its viewer-secret label from the two containers go.

### Traefik

On the host's network the service is on no Docker network, so a Traefik container on a bridge network can no longer reach `darkroom:8000`, and the web port listens only on the host's loopback. Traefik reaches it at `http://127.0.0.1:8000` when Traefik itself runs on the host's network (`network_mode: host` in its own compose file) or directly on the host. Route Darkroom's host name to that address with a file-provider service, for example:

```yaml
http:
  routers:
    darkroom:
      rule: Host(`darkroom.example.lan`)
      service: darkroom
  services:
    darkroom:
      loadBalancer:
        servers:
          - url: http://127.0.0.1:8000
```

Keep Darkroom behind the same sign-in as before. Don't make the web port reachable from a bridge network by setting `DARKROOM_HOST` to another address: that would put it on every address of the host. Set `DARKROOM_PUBLIC_URL` to the address Traefik serves.

## Syncthing: always on

The image carries Darkroom's own Syncthing (v2.1.7, the official release, checked against its signed checksum when the image is built). It is always on: there is no switch, and the `DARKROOM_SYNCTHING` variable of earlier releases is ignored ([Upgrading from a release with the Syncthing switch](#upgrading-from-a-release-with-the-syncthing-switch)). The init starts Syncthing with its home in `/data/syncthing` (a fresh identity on the first start; an existing Syncthing's keys are never imported), always paused, with automatic upgrades, usage and crash reporting, global and local discovery, relays and NAT traversal off, and its API on a socket inside the container only. Darkroom then configures Syncthing itself and photos start going to the Pixels. Every Pixel paired on the People page (below) gets that device and one Send Only folder, its own `_pixels/<pixel id>/` (the copies of the originals of the people it serves, never `originals/` itself), shared with that device alone, with no versioning and `*.xmp` ignored. Darkroom removes any other folder or device it finds in Syncthing, reads the whole configuration back, and only then resumes the folder and its device, which happens after the first hourly tend has checked the Pixel's copies; it also turns local discovery on. A Pixel without a device ID gets nothing, and so does one whose device ID is invalid or used by another Pixel, or whose `_pixels/<pixel id>/` (or `_pixels/` itself) is a link. A failed share check ([share.md](share.md)) or any Syncthing error pauses every folder and device. Global discovery, relays and NAT traversal stay off, so a Pixel reaches Syncthing only on the local network: through 22000 (TCP and UDP) and, for local discovery, 21027/udp, which host networking puts on the LAN directly; local discovery then announces the server to every device there. That a Pixel finds Darkroom by local discovery this way, and needs no other port, is UNVERIFIED until S4 (#311). Unraid's custom `br0` network, a container address of its own on the LAN, is believed to make discovery work too (UNVERIFIED, S4); the template uses the host's network, as the owner chose. With bridge networking, discovery broadcasts are believed not to cross the bridge, so neither the compose service nor the template uses it.

After the first start:

1. Bind the working share once ([share.md](share.md)): `docker compose exec darkroom python run.py bind-share` (or the container's console on Unraid). Until then every folder and device stays paused. Darkroom's own Syncthing is the only one serving `_pixels/`, so it answers `paused` truthfully: never run a second Syncthing on the same `_pixels/`.
2. Pair each Pixel on the People page: Add Pixel saves it and goes to the pairing step, and an existing Pixel's row (or its Edit form) has Pair. The step shows a QR code of Darkroom's device ID: scan it with the Syncthing app on the Pixel to add Darkroom's device, then, back in Darkroom, approve the waiting device whose ID matches the one the Pixel shows ("This is {name}", then Approve). When Darkroom's Syncthing is not answering, or the waiting list can't be read, type the Pixel's device ID instead. Then, on the Pixel, accept the folder Darkroom shares. A Pixel whose device ID is not valid or is used by another Pixel gets Pair again in its row (the old ID stays until a new one is approved or saved), and one whose folder Darkroom removed gets Share again ([runbook](../runbook.md#syncthing-events-syncthing_folder_removed-and-syncthing_reconcile_failed)). That the Syncthing app on an original Pixel pairs with the pinned release, by QR or otherwise, is UNVERIFIED until S4 (#311).
3. On the Pixel, in Google Photos, turn on backup for that folder ([runbook](../runbook.md#some-originals-are-not-in-google-photos)).

`check-config` says Syncthing is on. There is no way to turn it off: Darkroom's own Syncthing is the only one that may serve `_pixels/`. If an existing Syncthing on the server (Unraid's Syncthing app, or one on the host) holds 22000 or 21027, retire it, or move it off those ports and stop it serving `_pixels/`, rather than looking for a switch in Darkroom ([Ports already in use](#ports-already-in-use)). `/data/syncthing` is backed up with appdata like the secret key, since it holds Syncthing's device key: losing it means pairing every Pixel again.

### Ports already in use

Darkroom's Syncthing needs 22000 (TCP and UDP) and 21027 (UDP) on the host. When something else already holds one (most often an existing Syncthing on the server, such as Unraid's Syncthing app or one on the host network), the container still starts: Syncthing runs without the listener it could not bind, its own log is not kept, and Darkroom talks to it over its socket, so the Status page shows Syncthing as running and `check-config` as on. Nothing alerts. What you see is a Pixel that never shows up in the pairing step's waiting list (with 22000 taken, the Pixel's connection reaches whatever holds the port, not Darkroom), or Pixels that are paired but never connect (People and Status keep them waiting, then not connected or not seen).

To spot it, list what holds the ports on the host and compare with the container's processes:

```sh
sudo ss -Hltunp '( sport = :22000 or sport = :21027 )'
docker compose top darkroom
```

(on Unraid, `docker top darkroom -eo pid,comm`, the container's name as the template has it). Every process `ss` shows on those ports must be one of the container's PIDs. If another one holds them, stop it or move it off those ports (for an existing Syncthing, retire it: no other Syncthing may serve `_pixels/`), then restart Darkroom's container so its Syncthing binds them, and check with `ss` again.

## Moving from the two containers

The two containers are not in this release. Move from them on the newest `v3` release, the last one that publishes their images and has the `browser` profile, following that release's copy of this page (`git checkout` its tag and read `docs/deploy/one-container.md`; its `DARKROOM_TAG=v1.2.3` steps name that tag). That keeps the two containers of the same release as the way back while you need one. Once the one container runs there, upgrade it to this release (Upgrading, below) and remove `DARKROOM_BROWSER_TOKEN`, `DARKROOM_URL`, `VNC_URL` and `DARKROOM_VNC_SECRET` from `.env`. The profiles are copied into appdata with `copy-browser-state` ([browser.md](browser.md#copying-browser-profiles-copy-browser-state)), which this release keeps.

## Going back to the two containers

Rollback is pinning the previous release. The release that removed the two containers (#327) added no migration, so the newest `v3` release starts on the database it left. Stop the one container, check out that release's tag, set `DARKROOM_TAG` in `.env` to it, and follow its copy of this page, "Going back to the two containers", which also copies the profiles back to the share (`copy-browser-state --set-aside`). On Unraid, give the containers that release's tag as **Repository**. A release cannot start on a database a newer one has migrated; after a later release that migrates it, restore the backup taken before that upgrade (design section 9).

`_browser/` on the share, and any `_browser.before-<time>`, are removed by hand once you no longer need the way back; no Darkroom code deletes them. Until then they still hold every account's signed-in session, including accounts deleted later ([browser.md](browser.md#the-browser-profiles-in-appdata-the-one-container) says what to remove before using one again). `copy-browser-state` only ever copies between the share's own `_browser/` and a folder outside the share: any other folder on the share, or one that holds it, is refused before anything is copied or renamed.

## Backup and restore

Back up the appdata folder with the container stopped (or with Unraid's appdata backup, which stops it): the database, the `secret-key` file and the browser profiles are consistent only together. The backup holds the key that unseals the stored secrets, so keep it as carefully as those secrets. With `DARKROOM_SECRET_KEY` set, the key is not in appdata: keep it with the backup, matched to the same time, and a restore starts only once the same key is supplied again ([runbook](../runbook.md#the-secret-key)). With `DATABASE_URL` set, the database is not in appdata either: take a dump at the same stopped point, and restore both together.

To restore, stop the container, put the appdata folder back (owner 99:100), and start it. The photo share is not part of this backup: `_inbox/` holds imports that are not resolved yet, and `originals/` and `google-only/` are backed up as today.

## Upgrading

With Compose, check out the new release tag and change the `DARKROOM_TAG` line in `.env` to it, then pull and recreate: `docker compose pull && docker compose up -d`; the checkout brings the release's seccomp profile. On Unraid, copy the new release's seccomp profile first when it changed (above), then change **Repository** to the new tag and Apply. A release that migrates the database cannot be undone by starting the older image; take a backup before upgrading and restore it to go back (design section 9).

### Upgrading from a release before host networking

The release that put Darkroom's Syncthing on the host's network (C8, #379) breaks an existing compose or Unraid setup; Darkroom has no users yet, so no migration was written (the owner's decision on #326). What changes and what to do:

- **Compose:** the one container's service now has `network_mode: host` and publishes no ports. A changed host port in an old `ports:` entry (for example `8080:8000`) no longer applies: the web UI is on the host's loopback port 8000, so that port must be free. A Traefik container on a bridge network can no longer reach the service by name on port 8000, so a Traefik label route stops working; route to `http://127.0.0.1:8000` from Traefik on the host network instead ([Traefik](#traefik)). A `DATABASE_URL` that names the compose service `postgres` must name `127.0.0.1` instead ([postgres.md](postgres.md)). Check out the new release first, so the service definition and `DARKROOM_TAG` move together.
- **Unraid:** an existing container keeps the template it was created from (`my-darkroom.xml` in `templates-user`), with bridge networking and only port 8000. Copy the new [deploy/unraid/darkroom.xml](../../deploy/unraid/darkroom.xml) over it (or edit the container: **Network Type** `host` and the three Syncthing port entries), check that the variables you set are still there, and Apply. On the host network the Web UI port entry only describes the port: the container listens on 8000 itself, whatever host port the bridge mapped before, and a reverse proxy container that reached it by name on a Docker network no longer can; point the proxy at the server's address and port 8000.
- **Both:** 22000 (TCP and UDP) and 21027 (UDP) must be free on the host ([Ports already in use](#ports-already-in-use)), and they listen on every address of the host, so check that the router and firewall forward none of them.

### Upgrading from a release with two images

The release that made `darkroom` the one container's image (#400) breaks an existing setup of the release before it, which published the one container as `ghcr.io/anthonypluth/darkroom-single` and the app alone as `ghcr.io/anthonypluth/darkroom`; no migration was written. This is a breaking change for anyone on `ghcr.io/anthonypluth/darkroom:latest`, `darkroom:N` or `darkroom:N.M`: since #400 those tags mean the one container (host network, Syncthing, browser worker), not the app alone, and `darkroom-single` is no longer published. Pull one of them and you get the one container; to stay on the app alone, pin `DARKROOM_TAG` to the last app-only release (the last release before #400). What changes and what to do:

- **Compose:** the one container's service is `darkroom`, with no profile, so `docker compose up -d` starts it. Check out the new release, set `DARKROOM_TAG` to it, then `docker compose pull && docker compose up -d --remove-orphans`: the old service's container is no longer in the compose file, and `--remove-orphans` stops and removes it. The appdata folder (`./data`) and the share stay as they are. Anything that named the old service or container (a `docker compose exec`, a backup script) names `darkroom` instead. To go back, stop the new service first, before you check out the earlier tag: `docker compose down` (or `docker compose stop darkroom && docker compose rm -f darkroom`), then check out that release's tag, set `DARKROOM_TAG` to it and follow that release's own steps for `darkroom-single`. The new `darkroom` service (host network, Darkroom's Syncthing always on) left running next to the old `darkroom-single` on the same `./data` and `_pixels/` would run two Syncthings on one `_pixels/`, which the design forbids (section 9 of [the design](../design/single-container.md)).
- **Unraid:** change **Repository** to `ghcr.io/anthonypluth/darkroom:<release>` and Apply; nothing else in the template changes.
- **The app alone:** the app-only image and compose service are gone, and `ghcr.io/anthonypluth/darkroom` now runs the one container. Pulling it changes what runs: besides the app, the init starts the browser worker and its display stack and Darkroom's Syncthing, on the host's network. Syncthing's 22000 (TCP and UDP) and 21027 (UDP) then listen on every address of the host: check that the router and firewall forward none of them and keep them off the internet ([Ports already in use](#ports-already-in-use) says what must be free). Do the one manual host step first (the seccomp profile, above): without it the container does not start. It runs on the same appdata folder and share. To go back, check out the last release that published the app alone, set `DARKROOM_TAG` in `.env` to it (on Unraid, give **Repository** that tag) and run it as before: this release adds no migration, so that release starts on the same database. Nothing is migrated either way.

### Upgrading from a release with the Syncthing switch

The release that removed the `DARKROOM_SYNCTHING` switch (#430) breaks a setup that turned Darkroom's Syncthing off with it; no migration was written. Darkroom's own Syncthing now always starts in the one container, and nothing turns it off. What changes and what to do:

- **Both:** the variable is ignored. When it is still set to anything but `1` (`0` used to turn Syncthing off), the init logs one warning at every start, `DARKROOM_SYNCTHING is no longer supported and is ignored: Darkroom's Syncthing is always on in the one container, so remove it (docs/deploy/one-container.md)`, and `check-config` prints the same line as a warning; neither shows its value, and the container starts anyway. Remove it.
- **With Syncthing turned off before:** Syncthing now listens on 22000 (TCP and UDP) and 21027 (UDP) on every address of the host, so check that the router and firewall forward none of them. If you turned it off because another Syncthing served the Pixels (Unraid's Syncthing app, or one on the host), retire that one before you start this release: stop it, or move it off those ports and stop it serving `_pixels/` ([Ports already in use](#ports-already-in-use)); two Syncthings on one `_pixels/` are what the design forbids. Then bind the share and pair each Pixel as "After the first start" in [Syncthing: always on](#syncthing-always-on) says; until the share is bound every folder and device stays paused.
- **Compose:** the service no longer sets `DARKROOM_SYNCTHING`. Remove its line from `.env`: compose hands `.env` to the container whole, so a line left there still reaches it, and is warned about.
- **Unraid:** the template no longer has the variable, but an existing container keeps it from `my-darkroom.xml`. Edit the container, remove **DARKROOM_SYNCTHING**, and Apply.
- **To go back:** pin the last release with the switch (the release before #430; on Unraid, give **Repository** that tag) and set `DARKROOM_SYNCTHING=0` as before. This release adds no migration, so that release starts on the same database, and `/data/syncthing` and `_pixels/` stay as they are.
