# Spike S4: Unraid and a real Pixel

Spike S4 of the [single-container design](../design/single-container.md) (section 12, tracking issue #284, spike issue #311). It is meant to check, on the owner's Unraid server and an original Pixel, what no sandbox can: Syncthing's local discovery under host, `br0` and bridge networking, the ports a Pixel needs, the template's extra parameters and the seccomp file on the host, `TZ`, appdata on the user-share layer, and the Syncthing app on the Pixel pairing with the release Darkroom pins. It needs the owner's hardware, and this page was written from the record alone (#387): nothing was run for it.

## Result in one paragraph

No result is recorded. #311 was closed as completed on 2026-11-16 with no comment, no linked pull request and no write-up, and nothing in the repository holds one. The pull requests that rest on S4 and were written around that date say it had not been run: #372 (the pairing API and screens, its section "Resting on S4 (#311): NOT VERIFIED") and #379 (C8, merged after #311 was closed: "S4 (#311) has not been run"), and #386 still lists S4 as coming before the first deploy. Every item below is therefore NOT VERIFIED, and what the design, the deploy page and the runbook say about Unraid stays UNVERIFIED. The second half of this page is what the owner has to run and record; once it is filled in, the "Result in one paragraph" above is replaced with the result.

## What other work checked, which is not this spike

None of these ran on Unraid or with a Pixel. They narrow what S4 has to settle; they do not settle it.

| Where | What it showed | What it leaves to S4 |
| --- | --- | --- |
| [S1](merged-image.md) | The merged image runs as one unprivileged user under `deploy/browser/seccomp.json`, `--cap-drop=ALL` and `no-new-privileges`, in a cloud container with cgroup v1 | The seccomp file on Unraid's flash drive, the template's flags, cgroup v2 |
| [S3](syncthing-api.md) | Syncthing v2.1.7 against a second desktop instance: the pending-device list, a paused start, the API on a Unix socket, Send Only against a hostile peer, the network options written before the first start | Discovery and the ports on a real LAN, the app on an original Pixel |
| #340 (A5), in a throwaway Docker daemon | The Unraid template's parameters turned into the `docker run` Unraid would issue: the container healthy as 99:100 with 1 GiB of shared memory and every capability dropped; a missing profile stops the container (`opening seccomp profile ... no such file or directory`) | The template imported on Unraid, `TZ`, appdata on the user-share layer |
| C7a and C8 image CI (`tests/browser/host_network.sh`) | With host networking and Syncthing on, the container's listeners are only 8000, 22000 and 21027, and 5900, 6080 and 8384 are refused | The same on Unraid's host network, and whether a Pixel needs anything else |
| C4 image CI (`tests/browser/syncthing_peer.sh`) | Against a second pinned Syncthing as the Pixel, the share is unchanged while the peer edits, deletes, renames and adds | The Syncthing app on an original Pixel |
| #372, with the fake Syncthing | The pairing routes, the waiting list and approval as built | Every behaviour #372 lists as NOT VERIFIED (below) |

Since #311 was written, C7a moved every socket into `/run/darkroom` inside the container, so appdata holds no Unix socket any more (design section 9, "As built (C8)"). The socket half of the user-share item is moot for the one container; SQLite locking is what is left of it. S2's fallback item, the pairing folder in appdata, is moot too: the two-container fallback is gone (D1, #327).

## Versions

To be recorded by the owner. Placeholders in angle brackets.

| Piece | Version |
| --- | --- |
| Unraid | `<Unraid OS version>`, `<amd64>` |
| Docker on Unraid | `<docker version>`, cgroup `<v1 or v2>` (`stat -fc %T /sys/fs/cgroup`) |
| Darkroom image | `ghcr.io/anthonypluth/darkroom-single:<release tag>`, its digest |
| Seccomp profile | `deploy/browser/seccomp.json` of the same release, at `/boot/config/darkroom/seccomp.json` |
| Darkroom's Syncthing | the version `/rest/system/version` reports (v2.1.7 is pinned) |
| Pixel | original Pixel, Android `<version>` |
| Syncthing app on the Pixel | `<app name and version>`, where it came from (Play Store, F-Droid, an APK) |

## What the owner runs, and what to record

Before starting: no other Syncthing may serve `_pixels/` or hold 22000 or 21027 on the server while this runs (design section 9). Use made-up photos only. Write down no address, device ID, host name or file name: say "the server's LAN address", "Darkroom's device ID", "a made-up photo".

| # | Item (from #311) | Run | Record |
| --- | --- | --- | --- |
| 1 | The seccomp file and the template's extra parameters | Copy the release's `deploy/browser/seccomp.json` to `/boot/config/darkroom/seccomp.json`. Import `deploy/unraid/darkroom.xml`, set **Repository** to the release tag, fill the variables, Apply. Then `docker inspect darkroom --format '{{.HostConfig.ShmSize}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.Config.User}}'` and `docker top darkroom` | That the container starts and `/healthz` answers; shm 1 GiB, `[ALL]`, both security options, user 99:100; every process uid 99. Once with the profile missing: the error Unraid shows |
| 2 | The browser under the profile on Unraid | Open the Remote browser page; run a Takeout job or a sign-in for a low-value test account | That Chrome starts with its sandbox (no `--no-sandbox` in `docker top`); any `seccomp` or `user namespace` error in the container log |
| 3 | `TZ` | `docker exec darkroom sh -c 'echo "$TZ"; date'` and `python run.py check-config` in the container | Whether Unraid set `TZ` for the container without the template naming it, and the value's form (an IANA name or not). If it is empty, the design's section 8 row falls back to the wizard |
| 4 | Appdata on the user-share layer, SQLite locking | Run as deployed, at `/mnt/user/appdata/darkroom`. Start a Takeout import or the Immich reconcile while browsing the People and Status pages for a few minutes; then stop, map appdata at the direct pool path (`/mnt/<pool>/appdata/darkroom`) and repeat | Any `database is locked` or I/O error in the log on either path; which path the template should default to |
| 5 | Listeners on the host | `sudo ss -Hltunp` on the server with the container running | Only 8000, 22000 (TCP and UDP) and 21027 (UDP) for the container; nothing on 5900, 6080 or 8384 |
| 6 | Pairing by QR with the pinned release | Bind the share (`python run.py bind-share` in the container's console). Add a Pixel on the People page; on the Pixel, scan the QR code in the Syncthing app | That the app reads Darkroom's device ID from the QR code and accepts it; the app's version; any version warning on either side |
| 7 | The pending device, and pairing only after approval | Before approving, look at the waiting list on the People page (it reads `/rest/cluster/pending/devices`). Then approve the device whose ID matches the one the Pixel shows | That the Pixel shows up as waiting; that no folder reaches the Pixel and the Pixel is not connected before approval; that after approval the Pixel is offered its folder, accepts it, receives the made-up photos, and the waiting entry clears. Whether the name the app presents is the one Darkroom lists |
| 8 | Local discovery with host networking | With the template as shipped (`<Network>host</Network>`), the Pixel on the same LAN, no address typed on either side | That the Pixel connects; how long after approval; the Status page's Syncthing card and the People page's state |
| 9 | Local discovery with `br0` | Switch the container to `br0` with its own LAN address, restart, wait | Whether the Pixel finds Darkroom without an address typed. Expected to work; unchecked |
| 10 | Local discovery with bridge networking | Switch to `bridge`, publishing 8000/tcp, 22000/tcp, 22000/udp and 21027/udp, restart, wait | Whether the Pixel finds Darkroom; expected not to, since broadcasts are believed not to cross Docker's bridge. If not, whether typing `tcp://<server's LAN address>:22000` into the Pixel's app connects |
| 11 | The ports a Pixel needs | On the host network, block each port in turn on the server's firewall (or stop the container and listen with `nc` to see what the Pixel sends) | That 22000 TCP alone carries sync, 22000 UDP is QUIC only, 21027 UDP is discovery only, and nothing else is needed |
| 12 | The Pixel's behaviour after a release, and a reset Pixel (from #372) | Remove the folder (make it not Send Only by hand, let the reconciler remove it), then Share again on the People page. Then reset the Syncthing app on the Pixel and pair again | What the Pixel does with the share it already had; that a reset Pixel appears waiting with its new ID and receives the copies still marked placed |

Afterwards: switch the container back to the host network and stop the test Pixel's folder or unpair it if it is not one the household keeps.

## What this changes in the design

Nothing yet. Until the table above is filled in:

- Section 9's "Unraid" stays UNVERIFIED, and so does every S4 mention in sections 3 (the ports), 7.1 (an original Pixel's app and the pinned release) and 8 (`TZ`).
- C8's check is not met: S4 is one of the things listed as coming before the first deploy, in the design's plan row and in [docs/deploy/one-container.md](../deploy/one-container.md#before-the-first-deploy).
- Appdata stays at `/mnt/user/appdata/darkroom`; item 4 decides whether the template should default to the direct pool path instead.

If item 6 or 7 fails because the Pixel's app is too old for v2.1.7, that is a design decision for the owner (the pinned range of section 7.1), not something the write-up settles.

## Not verified, and what would settle each

| Item | Why not | What would settle it |
| --- | --- | --- |
| Every item in the table above | No result recorded on #311 or anywhere else; it needs the owner's Unraid server and an original Pixel | The owner's run of the table, recorded on this page |
| Override against the real Pixel (S3's open item) | Needs the Pixel | After item 7, "make the Pixel match" once with a made-up extra file on the Pixel, recording that the file is deleted (S3 section 7) |
| aarch64 | `darkroom-single` is published for amd64 only (#340) | Not needed unless the server is ever arm64 |
