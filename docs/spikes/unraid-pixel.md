# Spike S4: Unraid and a real Pixel

Spike S4 of the [single-container design](../design/single-container.md) (section 12, tracking issue #284, spike issue #311). It is meant to check, on the owner's Unraid server and an original Pixel, what no sandbox can: Syncthing's local discovery under host, `br0` and bridge networking, the ports a Pixel needs, the template's extra parameters and the seccomp file on the host, `TZ`, appdata on the user-share layer, and the Syncthing app on the Pixel pairing with the release Darkroom pins. It needs the owner's hardware, and this page was written from the record alone (#387): nothing was run for it.

## How to report back

Run an item's commands from [Commands for each item](#commands-for-each-item) (or follow its steps), fill in its `Result:` block and paste the block into a comment on #387, and we will fold it in. Several items can go in one comment, and an item that was not run can say so. Before pasting, replace every address, device ID, host name, account and file name with the words this page uses ("the server's LAN address", "Darkroom's device ID", "a made-up photo"): the repository is public.

```text
Spike S4 (docs/spikes/unraid-pixel.md), item <number>
Version, date, who ran it: <release tag>, <date>, <who>
Pass or fail: <pass / fail / not run, and why>
Result:
<the output the item asks for, pasted, with the replacements above>
```

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
| Darkroom image | `ghcr.io/anthonypluth/darkroom:<release tag>`, its digest |
| Seccomp profile | `deploy/browser/seccomp.json` of the same release, at `/boot/config/darkroom/seccomp.json` |
| Darkroom's Syncthing | the version `/rest/system/version` reports (v2.1.7 is pinned) |
| Pixel | original Pixel, Android `<version>` |
| Syncthing app on the Pixel | `<app name and version>`, where it came from (Play Store, F-Droid, an APK) |

In the Unraid terminal, with the variables of [Before you start](#before-you-start) set:

```sh
cat /etc/unraid-version; uname -m
docker version --format '{{.Server.Version}}'; stat -fc %T /sys/fs/cgroup
docker inspect --format '{{.Config.Image}}' "$C"
docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE"
curl -fsS http://127.0.0.1:8000/healthz; echo
sha256sum /boot/config/darkroom/seccomp.json
wget -qO- "https://raw.githubusercontent.com/AnthonyPluth/Darkroom/$TAG/deploy/browser/seccomp.json" | sha256sum
syncthing_version
```

`stat` prints `cgroup2fs` for cgroup v2 and `tmpfs` for v1. `/healthz` answers `{"ok": true, "version": ...}` with the release. The two `sha256sum` lines must show the same hash (the profile on the flash drive is the release's). `syncthing_version` (defined in Before you start) asks Darkroom's Syncthing on its socket and prints `v2.1.7 release`. On the Pixel, the app's version is at the bottom of its App info page (Android's Settings, Apps, the Syncthing app).

```text
Result:
Version, date, who ran it:
```

## What the owner runs, and what to record

Before starting: no other Syncthing may serve `_pixels/` or hold 22000 or 21027 on the server while this runs (design section 9). Use made-up photos only. Write down no address, device ID, host name or file name: say "the server's LAN address", "Darkroom's device ID", "a made-up photo".

| # | Item (from #311) | Run | Record |
| --- | --- | --- | --- |
| 1 | The seccomp file and the template's extra parameters | Copy the release's `deploy/browser/seccomp.json` to `/boot/config/darkroom/seccomp.json`. Import `deploy/unraid/darkroom.xml`, set **Repository** to the release tag, fill the variables, Apply. Then `docker inspect darkroom --format '{{.HostConfig.ShmSize}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.Config.User}}'` and `docker top darkroom` | That the container starts and `/healthz` answers; shm 1 GiB, `[ALL]`, both security options, user 99:100; every process uid 99. Once with the profile missing: the error Unraid shows |
| 2 | The browser under the profile on Unraid | Open the Remote browser page; run a Takeout job or a sign-in for a low-value test account | That Chrome starts with its sandbox (no `--no-sandbox` in `docker top`); any `seccomp` or `user namespace` error in the container log |
| 3 | `TZ` | `docker exec darkroom sh -c 'echo "$TZ"; date'` | Whether Unraid set `TZ` for the container without the template naming it (the template has no `TZ` variable), and the value's form (an IANA name or not). Darkroom today reads `TZ` from the environment (the init passes it to the app and the worker) and uses UTC when it is unset; there is no wizard step for it, although section 8 says "otherwise confirmed in the wizard". If Unraid does not set it, that row needs a template variable or the wizard step |
| 4 | Appdata on the user-share layer, SQLite locking | On SQLite (no `DATABASE_URL`; with PostgreSQL the database is not in appdata and this item does not apply). Run as deployed, at `/mnt/user/appdata/darkroom`. Start a Takeout import or the Immich reconcile while browsing the People and Status pages for a few minutes; then stop, map appdata at the direct pool path (`/mnt/<pool>/appdata/darkroom`) and repeat | Any `database is locked` or I/O error in the log on either path; which path the template should default to |
| 5 | Listeners on the host | `sudo ss -Hltunp` on the server with the container running | Only 8000, 22000 (TCP and UDP) and 21027 (UDP) for the container; nothing on 5900, 6080 or 8384 |
| 6 | Pairing by QR with the pinned release | Bind the share (`python run.py bind-share` in the container's console). Add a Pixel on the People page; on the Pixel, scan the QR code in the Syncthing app | That the app reads Darkroom's device ID from the QR code and accepts it; the app's version; any version warning on either side. Darkroom's Syncthing starts with local discovery off and the reconciler turns it on at the end of its first run that gets as far as resuming, so note whether that run had happened (the share bound and checked) before the Pixel looked for Darkroom |
| 7 | The pending device, and pairing only after approval | Before approving, look at the waiting list on the People page (it reads `/rest/cluster/pending/devices`). Then approve the device whose ID matches the one the Pixel shows | That the Pixel shows up as waiting; that no folder reaches the Pixel and the Pixel is not connected before approval; that after approval the Pixel is offered its folder, accepts it, receives the made-up photos, and the waiting entry clears. Whether the name the app presents is the one Darkroom lists |
| 8 | Local discovery with host networking | With the template as shipped (`<Network>host</Network>`), the Pixel on the same LAN, no address typed on either side. Global discovery, relays and NAT traversal are off, so only local discovery can find it | That the Pixel connects; how long after approval; the Status page's Syncthing card and the People page's state |
| 9 | Local discovery with `br0` | Switch the container to `br0` with its own LAN address, restart, wait | Whether the Pixel finds Darkroom without an address typed. Expected to work; unchecked |
| 10 | Local discovery with bridge networking | Switch to `bridge`, publishing 8000/tcp, 22000/tcp, 22000/udp and 21027/udp, restart, wait | Whether the Pixel finds Darkroom; expected not to, since broadcasts are believed not to cross Docker's bridge. If not, whether typing `tcp://<server's LAN address>:22000` into the Pixel's app connects |
| 11 | The ports a Pixel needs | On the host network, block each port in turn on the server's firewall (or stop the container and listen with `nc` to see what the Pixel sends) | That 22000 TCP alone carries sync, 22000 UDP is QUIC only, 21027 UDP is discovery only, and nothing else is needed |
| 12 | The Pixel's behaviour after a release, and a reset Pixel (from #372) | Make the folder wrong the way image CI does: stop the container, change that folder's `type` to `sendreceive` in `/data/syncthing/config.xml`, start (Syncthing's GUI and API are only on a socket inside the container, so there is no GUI to do it in). The reconciler removes it (`syncthing_folder_removed`, the People page shows Folder removed); then Share again on the People page. Then reset the Syncthing app on the Pixel and pair again (Pair again) | What the Pixel does with the share it already had; that a reset Pixel appears waiting with its new ID and receives the copies still marked placed |

Afterwards: switch the container back to the host network and stop the test Pixel's folder or unpair it if it is not one the household keeps.

## Commands for each item

Each item below has what to run (in the Unraid terminal: the `>_` icon at the top right of the web UI, as root, so no `sudo`), what a pass and a fail look like, and an empty block to fill in and paste ([How to report back](#how-to-report-back)). What a pass looks like is taken from the code and the image CI, not from a run on Unraid. Every log line Darkroom writes is one JSON object with its keys sorted, so the commands grep for `"event": "<name>"`.

### Before you start

Set these in every new terminal (the values in angle brackets are yours; the examples are made up):

```sh
C='<container>'                      # the container's name on the Docker tab
TAG='<release tag>'                  # the tag in the template's Repository, for example v1.2.3
IMAGE="ghcr.io/anthonypluth/darkroom:$TAG"
APPDATA=/mnt/user/appdata/darkroom   # the template's Appdata path
SHARE='<photo share path>'           # the template's Photo share path, for example /mnt/user/photos

events() {  # the newest events of the given types, from Darkroom's SQLite database (read only)
  docker exec -i "$C" python - "$@" <<'PY'
import datetime, sqlite3, sys
types = sys.argv[1:]
db = sqlite3.connect("file:/data/darkroom.db?mode=ro", uri=True)
where = f" where type in ({','.join('?' * len(types))})" if types else ""
for at, kind, subject, detail in db.execute(f"select time, type, subject, coalesce(detail, '') from events{where} order by id desc limit 30", types):
    print(datetime.datetime.fromtimestamp(at, datetime.UTC).strftime("%Y-%m-%d %H:%M:%SZ"), kind, subject, detail)
PY
}

syncthing_version() {  # asks Darkroom's Syncthing on its socket inside the container
  docker exec -i "$C" python - <<'PY'
import http.client, json, socket
from xml.etree import ElementTree
class OnSocket(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/run/darkroom/syncthing.sock")
key = ElementTree.parse("/data/syncthing/config.xml").getroot().findtext("gui/apikey")
connection = OnSocket("localhost", timeout=10)
connection.request("GET", "/rest/system/version", headers={"X-API-Key": key})
found = json.loads(connection.getresponse().read())
print(found["version"], "release" if found["isRelease"] else "not a release")
PY
}
```

`events` reads the `events` table that the Status page also reads; its subjects are ids (`pixel 3`, `share`), never names. With `DATABASE_URL` set it does not apply: run `psql "$DATABASE_URL" -c "select to_timestamp(time), type, subject, detail from events order by id desc limit 30"` wherever `psql` reaches the database instead. Never paste `docker inspect` output whole or the whole of `config.xml`: they hold the OIDC secret, the Immich key and Syncthing's API key. The commands below print only the fields they need.

### Item 1: the seccomp file and the template's extra parameters

Put the profile in place and add the container as [one-container.md](../deploy/one-container.md#unraid) says (steps 1 to 5), then:

```sh
docker inspect --format '{{.HostConfig.SecurityOpt}} {{.HostConfig.CapDrop}} {{.HostConfig.ShmSize}}' "$C" | cut -c1-80
docker inspect --format '{{.HostConfig.CapDrop}} {{.HostConfig.ShmSize}} {{.Config.User}} {{.HostConfig.NetworkMode}}' "$C"
docker inspect --format '{{range .HostConfig.SecurityOpt}}{{printf "%.40s" .}}{{println}}{{end}}' "$C"
docker top "$C" -eo uid,pid,comm
curl -fsS http://127.0.0.1:8000/healthz; echo
docker logs "$C" 2>&1 | grep -F '"event": "init started"'
```

Pass: the second line prints `[ALL] 1073741824 99:100 host`. The third prints `no-new-privileges:true` and a line starting `seccomp={` followed by the profile's own text (`"defaultAction":"SCMP_ACT_ERRNO"`): the Docker client reads the file when the container is created and stores its content, which is also why the first command's output is long and cut here. Every row of `docker top` has uid 99. `/healthz` answers `{"ok": true, "version": ...}` and the log has `{"event": "init started", "orphans_adopted": 0}` (or 1). Fail: `[]` for CapDrop, `67108864` (Docker's 64 MB default) for the shared memory, no `seccomp=` entry, a uid 0 process.

Then once with the profile missing, and put it back:

```sh
mv /boot/config/darkroom/seccomp.json /boot/config/darkroom/seccomp.json.away
# Docker tab: the container's icon, Edit, Apply (Apply recreates the container and reads the profile again)
mv /boot/config/darkroom/seccomp.json.away /boot/config/darkroom/seccomp.json
# Edit, Apply again
```

Pass: Apply fails and the dialog shows `opening seccomp profile (/boot/config/darkroom/seccomp.json) failed: open /boot/config/darkroom/seccomp.json: no such file or directory` (or close to it), and the container does not run; after the second Apply it starts again. Fail: the container starts without the file.

```text
Result:
Version, date, who ran it:
```

### Item 2: the browser under the profile on Unraid

On the host, then the image's smoke test under the same flags as the template (a throwaway container, so it does not touch the running one's display):

```sh
cat /proc/sys/user/max_user_namespaces
cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo "unprivileged_userns_clone: not present"
docker run --rm --user=99:100 --cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=/boot/config/darkroom/seccomp.json \
  --shm-size=1g -w /opt/worker/app -e PATH=/opt/worker/bin:/usr/local/bin:/usr/bin:/bin "$IMAGE" bash /opt/worker/app/smoke.sh; echo "smoke exit $?"
```

Pass: `max_user_namespaces` above 0; `unprivileged_userns_clone` 1 or not present; `smoke exit 0` with no `chrome_launch_failed` in the output (the script opens a page in Chrome, then in Chromium, and fails on either). Fail: `smoke exit 1` and a line such as `{"channel": "chrome", "error": "...", "event": "chrome_launch_failed", "reason": "sandbox_unavailable"}`; [browser.md](../deploy/browser.md#chromium-sandbox-seccomp-instead-of---no-sandbox) lists the reasons.

Then in the running container, with a low-value test account:

1. In Darkroom, open **People**. Under **Google accounts**, **Add account** and add the test account (it needs a person first: **Add person**).
2. Open **Exports**; on the test account's row press **Connect**, then **Open remote browser** (or open **Browser** in the navigation).
3. While the remote browser shows Google's page, in the Unraid terminal:

```sh
docker top "$C" -eo comm | grep -c chrome
docker top "$C" -eo args | grep -c -- '--no-sandbox'
docker logs "$C" 2>&1 | grep -E '"event": "(chrome_launch_failed|chromium_launch_failed)"' | tail -n 5
docker logs "$C" 2>&1 | grep -iE 'seccomp|user namespace' | tail -n 5
```

4. Sign in to the test account in the remote browser by hand (Darkroom never types it), or stop at Google's page.

Pass: the first count is above 0 (Chrome is running), the second is `0`, and the two greps print nothing. Fail: `--no-sandbox` found, or a `chrome_launch_failed` line (the worker then falls back to Chromium, which Google refuses for sign-in).

```text
Result:
Version, date, who ran it:
```

### Item 3: `TZ`

```sh
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$C" | grep '^TZ='
docker exec "$C" sh -c 'echo "TZ=$TZ"; date'
docker exec "$C" python -c 'import os, zoneinfo; print(zoneinfo.ZoneInfo(os.environ.get("TZ", "UTC")))'
```

Keep the `grep` on the first line: without it the command prints every variable, secrets included. Darkroom reads `TZ` (`darkroom/config.py`, default `UTC`), the init passes it to the app and the worker, and the app hands it to `zoneinfo`. Pass: the first line shows `TZ=` with an IANA name (for example `TZ=Europe/Berlin`), `date` prints local time, and the last line prints the same name. Fail, either: no `TZ=` line (Unraid did not set it; Darkroom runs in UTC, and the template needs a `TZ` variable or the wizard a step), or the last line raises `ZoneInfoNotFoundError` (the value is not a name Darkroom can use).

```text
Result:
Version, date, who ran it:
```

### Item 4: appdata on the user-share layer, SQLite locking

On SQLite only (no `DATABASE_URL`). First where appdata is, and what the container sees there:

```sh
docker inspect --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' "$C"
docker exec "$C" stat -f -c %T /data
docker exec "$C" python -c 'import sqlite3; print(sqlite3.connect("file:/data/darkroom.db?mode=ro", uri=True).execute("pragma journal_mode").fetchone()[0])'
ls -d /mnt/*/appdata/darkroom
```

Expected: `/mnt/user/appdata/darkroom -> /data`; a FUSE type (`fuseblk` or `fuse`) for the user share; `wal` (Darkroom opens SQLite in WAL mode, which needs shared-memory locking to work on that file system); `ls` shows the pool the folder really lives on (for example `/mnt/cache/appdata/darkroom`) besides `/mnt/user/...`.

Then load it for a few minutes: in one terminal run a write-heavy command while you click through **People** and **Status** in the browser, and in another a second writer:

```sh
docker exec "$C" python run.py index
docker exec "$C" python run.py reconcile-immich
```

then:

```sh
docker logs --since 30m "$C" 2>&1 | grep -E 'the database was busy|"error": "OperationalError"|database is locked|disk I/O' | tail -n 20
```

Darkroom never logs SQLite's own message: a lock it waited out for 30 seconds shows as `job postponed: the database was busy` or `request postponed: the database was busy` (the page gets a 503), and any other database error as `"event": "job failed"` with `"error": "OperationalError"`. The two commands print `database is locked` or an I/O error themselves if they hit one. Syncthing's own log is not kept, so a problem with its index in `/data/syncthing` shows only as Syncthing down on the Status page. Pass: the grep prints nothing and both commands finish. Fail: any line, with how often.

Then repeat on the direct pool path: stop the container, change **Appdata** on the Edit page from `/mnt/user/appdata/darkroom` to the path `ls -d` showed (the files are already there; nothing moves), Apply, and run the same commands. Change it back afterwards unless the result says the template should default to it.

```text
Result (user share):
Result (pool path):
Version, date, who ran it:
```

### Item 5: listeners on the host

```sh
ss -ltnup | grep -E ':(8000|22000|21027|5900|6080|8384)[[:space:]]'
pids=$(docker top "$C" -eo pid | tail -n +2 | tr -s ' \n' '|' | sed 's/^|//; s/|$//')
ss -Hltunp | grep -E "pid=($pids),"
docker exec "$C" grep -o '<localAnnounceEnabled>[a-z]*</localAnnounceEnabled>' /data/syncthing/config.xml
```

Compare with the table in [one-container.md](../deploy/one-container.md) ("Ports it listens on"): 8000 on every address on Unraid, 22000 TCP and UDP and 21027 UDP for Darkroom's Syncthing. Pass: the second `ss` (only the container's processes) shows `0.0.0.0:8000` (TCP, a Python process), `0.0.0.0:22000` TCP and UDP (`syncthing`), and 21027 UDP (`syncthing`, one line or one per address family), and nothing else; nothing at all on 5900, 6080 or 8384 in the first. 21027 is there only once the reconciler has turned local discovery on (the last line `true`), which happens after the share is bound (item 6); before that, `false` and no 21027. Fail: another port of the container's, any of 5900, 6080 or 8384, or 22000 or 21027 held by a process that is not the container's ([Ports already in use](../deploy/one-container.md#ports-already-in-use)).

```text
Result:
Version, date, who ran it:
```

### Item 6: pairing by QR with the pinned release

Bind the share (it asks two questions; type `paused`, then `bind`):

```sh
docker exec -it "$C" python run.py bind-share
docker restart "$C"
sleep 90; docker logs --since 5m "$C" 2>&1 | grep -E '"event": "(syncthing started paused; its API key was rotated|syncthing is a tested release; the transport may start|syncthing transport kept off|syncthing reconciled)"' | tail -n 5
docker exec "$C" grep -o '<localAnnounceEnabled>[a-z]*</localAnnounceEnabled>' /data/syncthing/config.xml
```

`bind-share` prints the share root, what it is mounted from, the counts, then `Syncthing is Darkroom's own (DARKROOM_SYNCTHING=1) ...` and, after `bind`, `Bound. ...`. The service tends once as it starts and then hourly, so the restart runs the placed-copy check straight away. Expected log: `syncthing started paused; its API key was rotated`, `syncthing is a tested release; the transport may start` (not `syncthing transport kept off`), and `syncthing reconciled` with `"failed": false`; local discovery `true` once a reconcile has resumed something.

Then pair, click by click:

1. In Darkroom, **People**. Under **Pixels**, **Add Pixel** (it needs a Google account: item 2). Name it (for example "Test Pixel"), choose the test account under **Google account**, tick one person under **People served**, and press **Add Pixel**. The pairing step opens: **1. Scan Darkroom's code** shows a QR code and Darkroom's device ID.
2. On the Pixel, open the Syncthing app, add a device (in most versions the **Devices** tab, then **+**), tap the QR icon beside the device ID field and scan the code on the screen. Give the device a name (for example "Darkroom"), share no folder, and save.
3. Note what the app says: whether it read the ID, any warning about the other side's version.

Pass: the app fills in Darkroom's device ID from the code and saves the device. Fail: the app does not read the code, refuses the ID, or warns that the versions do not match (record the exact words).

```text
Result:
Syncthing app and version:
Had the reconciler run (local discovery true) before the Pixel looked for Darkroom?
Version, date, who ran it:
```

### Item 7: the pending device, and pairing only after approval

Continue from item 6, with the pairing step still open (or **Pair** on the Pixel's row):

1. Under **2. Approve the Pixel**, wait for the Pixel to appear ("Waiting for Test Pixel to connect. Darkroom checks again every few seconds."; **Check now** asks at once).
2. Before approving, on the Pixel: the Darkroom device shows as not connected, and no folder is offered. On the server:

   ```sh
   ss -Htn state established '( sport = :22000 )' | wc -l
   ls -A "$SHARE/_pixels/"
   ```

3. On the Pixel, find its own device ID (the app's menu, an entry such as "Show device ID"). In Darkroom, compare it with the waiting entry, press **This is Test Pixel**, then **Approve** in "Approve this device for Test Pixel?".
4. On the Pixel, accept the folder Darkroom offers (a notification or a banner in the app) and choose where it goes on the Pixel.
5. Put a few made-up photos in that person's `originals/` if there are none, then queue and place them (the folder is resumed by the service's own tend, so restart rather than only running the job):

   ```sh
   docker exec "$C" python run.py index
   docker restart "$C"
   sleep 120; docker logs --since 5m "$C" 2>&1 | grep -E '"event": "(placed copies checked|pixel folders tended|syncthing reconciled)"' | tail -n 6
   ```

Pass: before approval the Pixel is listed as waiting, the established count is `0` and the Pixel has no folder from Darkroom; after it, the `syncthing reconciled` lines show `"approved": 1` (the Pixel's folder read back as Darkroom set it) and `"failed": false`, the Pixel accepts the folder and receives the made-up photos, `pixel folders tended` shows `"held": 0`, the waiting entry is gone, and the Pixel's row on **People** reaches **Connected** ("Up to date"). Record whether the name in the waiting list is the one the app presents. Fail: a folder or a connection before approval, or the Pixel never appears.

```text
Result:
Version, date, who ran it:
```

### Item 8: local discovery with host networking

With the template as shipped and the Pixel on the same Wi-Fi, no address typed anywhere (in the app, the Darkroom device's addresses stay `dynamic`):

```sh
docker inspect --format '{{.HostConfig.NetworkMode}}' "$C"
date -u; docker restart "$C"
# watch People until the Pixel's row shows Connected, then:
date -u
ss -Htn state established '( sport = :22000 )' | wc -l
```

Pass: `host`; the Pixel reconnects without help, the established count is 1 or more, and **People** and the **Status** page's Syncthing card show it connected (record how long it took between the two `date` lines). Fail: it stays "Waiting for the Pixel" or "Not connected".

```text
Result:
Version, date, who ran it:
```

### Item 9: local discovery with `br0`

On the container's Edit page, **Network Type** Custom: `br0`, a fixed IP of its own on the LAN, Apply. Then as in item 8:

```sh
docker inspect --format '{{.HostConfig.NetworkMode}}' "$C"
docker exec "$C" grep -o '<localAnnounceEnabled>[a-z]*</localAnnounceEnabled>' /data/syncthing/config.xml
```

Pass: `br0`, and the Pixel connects with no address typed (record how long). Fail: it never connects.

```text
Result:
Version, date, who ran it:
```

### Item 10: local discovery with bridge networking

**Network Type** Bridge; on the Edit page the four port entries then publish 8000/tcp, 22000/tcp, 22000/udp and 21027/udp; Apply.

```sh
docker inspect --format '{{.HostConfig.NetworkMode}}' "$C"
docker port "$C"
```

Pass for the setup: `bridge` and the four ports listed by `docker port`. Record whether the Pixel connects with no address typed (expected not to). If not, in the app edit the Darkroom device's addresses to `tcp://<server's LAN address>:22000` (in place of `dynamic`), save, and record whether it connects then. Put the address back to `dynamic` and the network back to `host` afterwards.

```text
Result:
Version, date, who ran it:
```

### Item 11: the ports a Pixel needs

On the host network, with the Pixel connected. Block one port at a time with a firewall rule on the server, restart the Syncthing app on the Pixel (or toggle Wi-Fi) and watch whether it reconnects and syncs a new made-up photo, then remove the rule. The rules are not saved and go at a reboot.

```sh
iptables -I INPUT -p udp --dport 22000 -j DROP    # QUIC blocked: sync should carry on over TCP
ss -Htn state established '( sport = :22000 )' | wc -l
iptables -D INPUT -p udp --dport 22000 -j DROP

iptables -I INPUT -p udp --dport 21027 -j DROP    # discovery blocked: a connected Pixel should stay connected; a new one should not find Darkroom
iptables -D INPUT -p udp --dport 21027 -j DROP

iptables -I INPUT -p tcp --dport 22000 -j DROP    # TCP blocked: record whether QUIC alone connects
iptables -D INPUT -p tcp --dport 22000 -j DROP
iptables -S INPUT | grep -E 'dport (22000|21027)' || echo "no test rule left"
```

Pass: with UDP 22000 blocked the Pixel still syncs over TCP (established count 1); with 21027 blocked only discovery stops; nothing else is needed. Record what happened with TCP 22000 blocked. The last line must print `no test rule left`.

```text
Result:
Version, date, who ran it:
```

### Item 12: the Pixel after a release, and a reset Pixel

Make the folder wrong the way image CI does, on the host, with the container stopped (Syncthing's GUI and API are only on a socket inside it):

```sh
docker stop "$C"
grep -c 'type="sendonly"' "$APPDATA/syncthing/config.xml"
sed -i 's/type="sendonly"/type="sendreceive"/' "$APPDATA/syncthing/config.xml"
chown 99:100 "$APPDATA/syncthing/config.xml"
docker start "$C"
sleep 90; events syncthing_folder_removed syncthing_folder_released syncthing_folder_added | head -n 5
```

The first `grep` counts the Pixels' folders (one per paired Pixel; the edit makes every one of them Send and Receive). Pass: a `syncthing_folder_removed` event for `pixel <id>`, the Pixel's row on **People** shows **Folder removed** with a **Share again** button, and no file on the share or the Pixel is deleted. Then press **Share again** and confirm (**Share folder again**): `syncthing_folder_released`, and within a minute the folder is back, Send Only. Record what the Pixel did with the folder it already had while it was removed and when it came back.

Then reset the app on the Pixel (Android's Settings, Apps, the Syncthing app, Storage, Clear storage; this gives it a new device ID) and pair again: on **People**, the Pixel's **Edit**, **Pair again**, and items 6 and 7 from the scan on.

Pass: the reset Pixel appears in the waiting list with its new ID, and after approval it receives the copies still marked placed. Fail: the old ID is still shared, or the reset Pixel receives nothing.

```text
Result:
Version, date, who ran it:
```

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
| Override against the real Pixel (S3's open item) | Needs the Pixel, and Darkroom has no override: section 7.5's "make the Pixel match" is not built (no override call in `darkroom/providers/syncthing.py`) | Only if it is built: after item 7, run it once with a made-up extra file on the Pixel and record that the file is deleted (S3 section 7) |
| aarch64 | `darkroom` is published for amd64 only (#340) | Not needed unless the server is ever arm64 |
