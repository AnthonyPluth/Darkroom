# Spike S3: Syncthing's API and behaviour

Spike S3 of the [single-container design](../design/single-container.md) (section 12, tracking issue #284, spike issue #310). It checks every belief about Syncthing in sections 7.2 to 7.5 against a pinned official release, with a second instance standing in for a Pixel, and recommends how the init hands Syncthing's API key over (for #318). It was run once, on amd64, mostly directly on the host and, for the sandbox flags, in a throwaway Docker daemon. No repository file changed except this write-up: every prototype lived in a scratch folder outside the repository and was deleted afterwards. Folder names, device names and file names were made up, and the device IDs below are placeholders, not the ones the throwaway instances generated.

## Result in one paragraph

Syncthing v2.1.7, the current stable release, downloaded from the project's GitHub release with a signed checksum, behaves as the design hopes in nearly every respect, with four findings that change the design. What holds: every endpoint section 7.2 names exists and does what it says, every configuration change tried took effect without a restart, a Send Only folder kept its whole tree byte-for-byte and metadata-for-metadata identical while the peer edited, deleted, added and renamed files, deletions made while Syncthing was stopped reached the peer within seconds of the restart, removing a folder or a device deleted no photo on either side, the folder marker put a folder on an unmounted or empty mount into an error state with no deletion announced (also across a restart), the REST API serves on a Unix socket whose mode can be set to `0600` through the configuration, `--paused` exists, persists into `config.xml`, and lets folders and devices be resumed one at a time, and Syncthing ran with TCP and QUIC sync, the file watcher and scans as uid 99 under `deploy/browser/seccomp.json`, `--cap-drop=ALL` and `no-new-privileges` with no warning and no syscall beyond the profile. The four findings: an unknown folder `type` (for example `SendOnly` or `send-only`) is accepted with 200 and silently stored as `sendreceive`, so the reconciler's read-back is essential; a paused start pauses folders and devices only, so local discovery keeps broadcasting the device ID and, with default options, a first start reaches out to relays, global discovery and NAT gateways within 0.2 s, before any REST call can turn them off, so the init must write the network options into the generated `config.xml` before the first `serve`; removing a folder from the configuration deletes Syncthing's own folder marker (only that), and re-adding a folder whose path is an empty mount creates a new marker and runs it with an empty index; and "override" on a Send Only folder makes the peer match exactly, which restores what the peer changed or deleted but also deletes, with no versioning, every file the peer added. For the API key, both override mechanisms (`--gui-apikey` and `STGUIAPIKEY`) put the key in the child's environment, the flag also on two command lines, and neither replaces the key in `config.xml`, which keeps working; the recommendation is to rotate the persisted key through the API at every start and hand the new one to the app on a pipe (section 12).

## Versions

| Piece | Version |
| --- | --- |
| Host | Ubuntu 24.04.5, Linux 6.18, x86_64, a cloud container with no Yama LSM (not Unraid) |
| Syncthing (both instances, and the sandboxed one) | v2.1.7 "Hafnium Hornet", go1.27.3 linux-amd64, built 2026-10-20 (`isRelease: true`, not a candidate or beta) |
| Release tarball | `syncthing-linux-amd64-v2.1.7.tar.gz`, SHA-256 `2edc416e7ed136b7c858ed5f3f02a3b8316589605e54c380228ceea7acbf0142` |
| `syncthing` binary inside it | SHA-256 `f5c12679f2bad1b5eff619ba7b04ead1a457e439a1e55dc640d4d8995d7f4dbd`, statically linked |
| Docker Engine (own scratch daemon, `--bridge=none`) | 29.9.2, overlay2 |
| Tools | curl 8.5.0, jq 1.7, strace 6.8, the host's Python 3 for the checks |

### Where the release came from

`https://upgrades.syncthing.net/meta.json` (the feed Syncthing's own upgrade check reads) named v2.1.7 as the newest non-prerelease. The tarball and `sha256sum.txt.asc` came from `https://github.com/syncthing/syncthing/releases/download/v2.1.7/` through this sandbox's TLS-re-signing proxy, with verification on. The GitHub API was refused by the proxy, the release download URLs were not. `sha256sum.txt.asc` is a clear-signed file carrying two signatures. `gpg --verify` reported a good signature from "Syncthing Release Management" for both keys: `FBA2 E162 F2F4 4657 B38F 0309 E566 5F9B D597 0C47` (from `https://syncthing.net/release-key.txt`) and `37C8 4554 E7E0 A261 E4F7 6E1E D26E 6ED0 0065 4A3E` (from `keyserver.ubuntu.com`). Both keys were fetched through the same proxy and not cross-checked against a second channel, so gpg's "not certified with a trusted signature" stands. The tarball's SHA-256 matched the signed line. No distro package was used.

The same signed file lists the other platforms' checksums; only `linux-amd64` was downloaded and run.

## Setup

Two instances on the host, as root, each with its own home and its API on a Unix socket in a `0700` folder:

```text
syncthing generate --home=<home> --no-port-probing
syncthing serve --home=<home> --no-browser --no-upgrade --no-restart --gui-address=unix://<run dir>/gui.sock [--paused]
```

Instance "darkroom-test" held Send Only folders under a scratch photo folder and listened on `tcp://127.0.0.1:22001`. Instance "pixel-test" was the peer, with Send & Receive folders, on `tcp://127.0.0.1:22002`. Both had global discovery, local discovery, relays and NAT traversal off, set through the API as the first step. Each folder held made-up `IMG_1000.jpg`…`IMG_1019.jpg` (random bytes, 1 kB to 300 kB) in `alice/` and `bob/` sub-folders and one `IMG_1001.xmp`. Every REST call was:

```text
curl --unix-socket <run dir>/gui.sock -H "X-API-Key: <key>" -X <METHOD> http://localhost<path> [-d <json>]
```

with the key read from the instance's `config.xml` by the script (never on a command line of its own or printed).

"Whole-tree SHA-1" below is the SHA-1 of the sorted `sha1sum` list of every regular file under the folder, taken three ways: every file; every file except Syncthing's marker; and a metadata list (path, type, mode, size, mtime) of every entry below the root except the marker. "Unchanged" means all three were equal before and after.

## 1. The beliefs of section 7.2

| Belief | Result |
| --- | --- |
| Requests carry an `X-API-Key` header | Holds. `Authorization: Bearer <key>` is also accepted. No key or a wrong one gets `403` with the body `CSRF Error`. `/rest/noauth/health` answers without a key |
| `/rest/system/status` returns the own device ID | Holds: `myID` |
| `/rest/system/version` | Holds: `version` (`v2.1.7`), `longVersion`, `isRelease`, `isBeta`, `isCandidate`, `os`, `arch`, `container`, `date` |
| `/rest/config`, `/rest/config/folders`, `/rest/config/devices`, `/rest/config/options` read and change configuration | Holds for what was used: `GET /rest/config`; `GET`/`POST /rest/config/folders`; `GET`/`PATCH`/`PUT`/`DELETE /rest/config/folders/<id>`; `POST /rest/config/devices`; `PATCH`/`DELETE /rest/config/devices/<id>`; `PATCH /rest/config/options`; `GET`/`PATCH /rest/config/gui`. `PUT /rest/config` (the whole configuration at once) was not tried. `GET /rest/config` and `GET /rest/config/gui` include `gui.apiKey` in the reply |
| A folder's `type` value `sendonly`, its `path` and device list | Holds: `{"id":…,"path":…,"type":"sendonly","devices":[{"deviceID":…},…],"paused":…}`. **Finding:** an unknown `type` is not refused. `PATCH` with `"SendOnly"`, `"send-only"` or `"bogus"` answered `200` and stored `sendreceive` (in the reply and in `config.xml`) |
| `/rest/db/ignores` reads and sets ignore patterns | Holds. `POST /rest/db/ignores?folder=<id>` with `{"ignore":["*.xmp"]}` wrote `.stignore` (mode `0600`) and the reply's `expanded` list showed `*.xmp`, `**/*.xmp`, `*.xmp/**`, `**/*.xmp/**`. It works on a paused folder. The `.xmp` was never sent to the peer |
| `/rest/db/scan` | Holds: `POST /rest/db/scan?folder=<id>` answers `200`. On a folder in an error state it answers `500` with the error text |
| `/rest/db/completion` | Holds: `?folder=<id>&device=<id>` gives `completion`, `globalBytes`, `globalItems`, `needBytes`, `needDeletes`, `needItems`, `remoteState`, `sequence` |
| `/rest/cluster/pending/devices` | Holds. A device that tried to connect and is not configured is listed under its ID with `name`, `address` and `time`. The peer side has `/rest/cluster/pending/folders` for a folder offered to it |
| `/rest/system/connections` and `/rest/stats/device` | Hold: `connections.<id>.connected` and `type` (`tcp-client`, `quic-client`, …); `lastSeen` and `lastConnectionDurationS` per device |
| Device ID format | Holds: 56 characters of the base32 alphabet in eight dash-separated groups of seven. Split into four chunks of 14, each is 13 data characters and one Luhn mod 32 check character; the 52 data characters are the unpadded base32 of the SHA-256 of the device certificate's DER (checked with an independent script against `cert.pem`). `/rest/svc/deviceid?id=` accepts lower case without dashes and returns the canonical form. One character changed: `/rest/svc/deviceid` answers `check digit incorrect`, and `POST /rest/config/devices` answers `400` |
| Automatic upgrades, usage reporting, crash reporting, global discovery, relays and NAT traversal can each be turned off | Holds: `autoUpgradeIntervalH: 0` (and `--no-upgrade`, logged as "No automatic upgrades"), `urAccepted: -1`, `crashReportingEnabled: false`, `globalAnnounceEnabled: false`, `relaysEnabled: false`, `natEnabled: false`. Observed off in section 11. The defaults in a generated `config.xml` are all on, except usage reporting, which waits for consent (`urAccepted: 0`); crash reporting defaults to on |

Two more facts the provider needs. `serve` always runs as two processes, a monitor and the real Syncthing as its child, even with `--no-restart`; `SIGTERM` to the monitor and `POST /rest/system/shutdown` each stopped both. No "Default Folder" was created at first start, after `generate` or in a fresh `serve`.

## 2. Configuration changes without a restart

Every change tried took effect at once, and `GET /rest/config/restart-required` stayed `{"requiresRestart":false}` after each:

- options: listen addresses (`ss -ltn` showed the old listener gone and the new one up within 3 s), local discovery on and off, global discovery, relays, NAT, usage and crash reporting;
- `gui.unixSocketPermissions` (the socket was re-created with the new mode) and `gui.apiKey` (the old key got `403` at once);
- folders: add, remove, pause, resume, type changed both ways, `PUT` of an identical folder;
- devices: add, remove, pause, resume, addresses;
- ignore patterns.

One loose end: after global discovery, relays and NAT were turned off at runtime, the process still held three UDP sockets on ephemeral ports. Whether they sent anything afterwards was not traced. The run with the options off from the first start (section 11) had none of this traffic.

## 3. A Send Only folder against a hostile peer

With the folder synced, the peer did all of this in its copy: appended to one file, overwrote another with new content, deleted three, added one file and one file in a new sub-folder, renamed one, added an `.xmp` and replaced its own `.stignore`. Both sides were rescanned and the Send Only side was then restarted.

- The Send Only tree was unchanged (all three whole-tree hashes), after the rescan and after the restart.
- `/rest/db/status` on the Send Only side reported `needFiles: 5`, `needDeletes: 4`, state `idle`, and `/rest/db/need` listed the peer's changes. It pulled none of them.
- The same was repeated under the sandbox flags (section 10, one edit, one deletion, one addition): unchanged.

## 4. Deletions made while Syncthing was down

The Send Only instance was stopped, two files were deleted from its folder, and it was started again. Both were gone from the peer at the first check, 5 s after the start (the initial scan carries them). Under the sandbox flags, a new file and a deletion made while Syncthing was running reached the peer through the file watcher alone, with no scan requested.

## 5. Removing a folder or a device

`DELETE /rest/config/folders/<id>` and then `DELETE /rest/config/devices/<id>` on the Send Only side, then the same on the peer. On both sides the tree without the marker was unchanged, and so was its metadata. **The one thing removed is Syncthing's own folder marker:** removing a folder from the configuration deletes its `.stfolder` directory (on each side where the folder is removed). No photo, no `.stignore`, no sub-folder went.

In v2.1.7 the marker is a directory, `.stfolder/`, holding one file `syncthing-folder-<six hex>.txt` with the folder ID and its creation time. It is created when a folder first runs (a folder added paused gets none until it is resumed), and again whenever a folder is added to the configuration while its path exists.

## 6. The folder marker and an unmounted folder

A Send Only folder on a tmpfs mount was synced to the peer, then:

| Case | Send Only side | Peer |
| --- | --- | --- |
| Lazily unmounted while running, then a scan requested | `error`: "folder marker missing (this indicates potential data loss, …)"; the scan answered `500` | unchanged |
| Restarted while unmounted | the same error from the start | unchanged |
| An empty tmpfs mounted in its place (a blank replacement share), scan requested | the same error | unchanged |
| Paused and resumed, and `PUT` of its identical configuration | still the error; no marker created | unchanged |
| Removed from the configuration and added again with the same ID and path, on the empty mount | a new marker created, folder `idle`, `localFiles: 0`, `globalFiles: 6`, `needFiles: 6` | unchanged (checked again 30 s later) |
| Contrast: marker present, every photo deleted | deletions announced | all deleted within 12 s |

So the marker holds while the folder stays in the configuration, and a missing mount is never read as "everything was deleted". Removing and re-adding the folder defeats it: Syncthing drops the folder's index on removal, so the new folder announces no deletion, but it now runs on the empty mount as the lost-index case of section 7, where an "override" would delete everything on the Pixel. The reconciler must therefore never remove and re-add a folder to repair it, and must never add or re-add one before the share check (C0) and the placed-copy check (C3a) have passed. The last row is the case section 9 already describes: the marker does not help when the share is mounted but emptied.

How quickly a running instance notices an unmount by itself (without a requested scan) was not measured.

## 7. A lost index, and "override"

**Lost index.** With a folder of five files synced, the Send Only instance was stopped, its database (`index-v2/` in its home) was deleted, two files were deleted from its folder, and it was started. It rescanned three files. The peer kept all five, with no conflict copies for the three that matched. The Send Only side reported `needFiles: 2` and `/rest/db/need` named the two. So a lost index leaves the Pixel holding what Darkroom has since pruned, as section 7.5 expects, and nothing on the Unraid side changes.

**Override** (`POST /rest/db/override?folder=<id>` on the Send Only side) makes the peer match the Send Only side exactly:

- After the lost index, the peer deleted the two pruned files within 15 s.
- After the hostile peer of section 3, the peer got back the original content of the file it had appended to and overwritten, and the three it had deleted. It also **deleted** the two files it had added, the renamed file's new name and the new sub-folder. The peer had no versioning, so those were gone for good.
- Files the Send Only side ignores (the peer's own `.xmp`) were left alone.

So the manual "make the Pixel match" of section 7.5 is this call, and its confirmation text has to say that it also deletes anything a person put into that folder on the Pixel. The peer here was a Send & Receive folder; what override does to a Pixel whose folder is Receive Only is NOT VERIFIED.

## 8. The REST API on a Unix socket

- `--gui-address=unix:///<path>` (flag) serves the GUI and the API on that socket only (no TCP listener). In `config.xml`, the same is written as a bare path, `<address>/<path></address>`. Written as `unix:///<path>` in `config.xml` it was parsed as a TCP address, the API failed to start ("lookup tcp////…: unknown port") and Syncthing exited, which at least fails closed. The flag's address is not written to `config.xml`.
- The socket's mode follows the process's umask by default (`srwxr-xr-x` under umask `022`). `gui.unixSocketPermissions` sets it: `"0600"` gave `srw-------`, applied at once through `PATCH /rest/config/gui` and kept across restarts. Under the sandbox flags, with the setting in `config.xml` from the start, the socket was `srw-------` owned by uid 99 in a `0700` folder.

## 9. A paused start

`serve --paused` exists ("Start with all devices and folders paused"; `--unpaused` is its opposite).

- It **writes** `paused` into `config.xml` for every folder and device, the instance's own device included. A later start without the flag stays paused, so a crash after a paused start cannot resume anything.
- While paused for 70 s, the instance logged no scan, the file added while it was down was not indexed (`localFiles` unchanged), and it made no connection to the peer (no `connect` to the peer's port in an `strace` of the whole process tree). The peer showed it disconnected.
- **It still announced itself.** With local discovery on and a non-loopback listen address, the paused instance sent its local discovery broadcast (device ID and listen addresses) every 30 s, three in the 70 s. Pausing is folders and devices only. A fresh home started with `--paused` and default options made 542 connection attempts to relays on port 22067, HTTPS to the global discovery servers and a NAT-PMP request within 20 s, the first 0.12 s after start (section 11).
- Folders and devices added after a paused start are added **unpaused** unless the request says `"paused": true` (which works, also for setting ignores before the first scan).
- Resuming one folder (`PATCH /rest/config/folders/<id>` with `{"paused":false}`) scanned it (`localFiles` went up) and still sent nothing, because the device was paused. Resuming the device then connected within 15 s and the new file reached the peer. The other folder stayed paused throughout.

So "paused" does what step C3 needs for folders and devices, one by one. Discovery is not part of it: the reconciler should keep `localAnnounceEnabled` off in the configuration until it has approved the configuration, then turn it on (a runtime change, section 2).

## 10. Under S1's sandbox flags

The image for this was made without a pull: a root filesystem holding only the release's static `syncthing`, an `/etc/passwd` with uid 99 and a `/tmp`, imported with `docker import` (34 MB). It ran on the scratch daemon with:

```text
docker run --user 99:100 --cap-drop=ALL --security-opt no-new-privileges:true \
  --security-opt seccomp=deploy/browser/seccomp.json --network host \
  -v <home>:/data/syncthing -v <run dir>:/run/st -v <photos>:/photos \
  <image> syncthing serve --home=/data/syncthing --no-browser --no-upgrade --no-restart --paused
```

`/proc/<pid>/status` showed `Uid: 99`, `CapEff: 0000000000000000`, `NoNewPrivs: 1`, `Seccomp: 2`. `generate` ran under the same flags. The instance served the API on its socket, synced a Send Only folder to the host's peer over TCP and then over QUIC, picked up a new file and a deletion with the file watcher alone, rescanned, kept its tree against the peer's edits (section 3), and started paused. Its log had no warning and no error, with `setLowPriority` on.

S1 saw Debian's 1.29.5 log "Failed to lower process priority: set I/O priority: operation not permitted" under this profile. v2.1.7 did not log it, here or on the host. An `strace -f -c` of a host run with `setLowPriority` on (start, scan, TCP and QUIC listeners, sync, deletion, REST calls, shutdown) showed `setpriority` and no `ioprio_set`. That run used 73 distinct syscalls, and every one is in the profile's unconditional allow list. So for v2.1.7 the profile needs no addition for what was exercised. Paths not exercised under the profile: relays, NAT traversal and global discovery (off by design), extended attributes (`syncXattrs` and `sendXattrs` are off by default; the profile refuses the xattr calls, as S1 found), versioning and very large files. Setting `setLowPriority: false` costs nothing and removes the question.

## 11. The first start reaches out before the API can stop it

Every option section 7.2 wants off is on in the `config.xml` that `generate` writes, and a REST call can only change them once `serve` is up. A fresh home started with `--paused` made its first outbound connection 0.12 s after start and reached about 265 distinct addresses in 20 s (relays, global discovery over HTTPS, DNS, NAT-PMP). It also used the `HTTPS_PROXY` in its environment for the HTTPS lookups.

What worked: `generate`, then a rewrite of the generated `config.xml` before the first `serve`, setting in `<options>` `globalAnnounceEnabled`, `relaysEnabled`, `natEnabled` and `crashReportingEnabled` to `false`, `urAccepted` to `-1`, `autoUpgradeIntervalH` to `0`, `startBrowser` and `setLowPriority` to `false`, `listenAddress` to the wanted address, and in `<gui>` the socket path and `<unixSocketPermissions>0600</unixSocketPermissions>`. Started paused from that file, the instance made no outbound connection at all in 75 s except its local discovery broadcasts (local discovery was left on in that file to see it). This is the one write of `config.xml` Darkroom would make, on first start only, before Syncthing has ever run. Section 7.3 says Darkroom never edits these files, so the design should name this exception. The init's allowlisted environment for Syncthing should also leave out any proxy variables.

## 12. Handing the API key over (for #318)

Observed with a freshly generated key on the peer instance (`secrets.token_urlsafe(32)`), checking `/proc/<pid>/environ`, `/proc/<pid>/cmdline` and `ps` for the monitor and the child, the log and `config.xml`:

| Way | Where the key showed | Does `config.xml`'s own key still work? |
| --- | --- | --- |
| `STGUIAPIKEY` in the environment | both processes' `environ` | yes, both keys were accepted |
| `--gui-apikey=<key>` | both processes' `cmdline` (so `ps` for every user of the host), and the child's `environ` (the monitor passes it on) | yes |
| `<apikey>` in `config.xml` (what `generate` writes, 32 characters) | only `config.xml`, mode `0600`, next to `key.pem` | it is that key |
| `PATCH /rest/config/gui` with `{"apiKey":"<new>"}` | written to `config.xml`; the old key got `403` at once; no restart | the new one only |

No run logged a key. `GET /rest/config` and `GET /rest/config/gui` return it.

**Recommendation.** Do not hand the key in at all: neither override hides the key from another process, and neither removes the key that `config.xml` already holds, so both add an exposure and take none away. Instead:

1. On first start only, the init runs `generate` and writes the options of section 11 into the generated `config.xml`, then starts Syncthing paused.
2. On every start, once Syncthing answers on its socket (`0600`, in a `0700` folder only the container's user can enter), the init reads the current `<apikey>` from `config.xml`, generates a new key with `secrets`, sets it with `PATCH /rest/config/gui` and checks that the old key now gets `403` and the new one `200`.
3. The init keeps the new key in memory for the container's life and hands it to the app on a pipe of its own, exactly as S2 hands the browser token (`pass_fds`, the fd number only on the command line, read to the end and closed before anything else starts). A Syncthing restarted by the init after a crash keeps the key from `config.xml`, so the app's key stays valid.
4. The provider never logs or stores a configuration reply as it came: it drops `gui.apiKey` before anything else sees it, and `no_leaks` covers the provider's and the init's output.

The key is then in no command line, no environment and no log. It is on disk in `config.xml`, as `key.pem` already is, in `/data/syncthing`, so section 7.3's row "Generated by Darkroom at start, held in memory" should say "rotated by the init at every start, held in memory by the app and in Syncthing's `config.xml`", and the backup of `/data` carries it like the other secrets there. A key copied from an old backup stops working at the next start. Anything running as the container's one user can still read `config.xml` or the processes' memory, which section 5 already accepts.

## 13. What Syncthing's own log says

At its default level, Syncthing logs its full device ID at every start, a peer's short device ID and address when a connection is refused ("Connection rejected (device=<short id> address=<ip>:<port> …)"), folder IDs, and on the receiving side every file name it syncs ("Synced file (… file.name=bob/IMG_1016.jpg …)"). The Send Only side logged no file name in these runs, but warnings carry addresses and IDs. The init should not copy Syncthing's output into Darkroom's log as it comes: either drop it, or pass only lines it maps to fixed texts.

## What this changes in the design

- Section 7.2: the API key is Syncthing's own, rotated at every start (section 12); the socket is `gui.unixSocketPermissions: "0600"` in a private folder, written in `config.xml` as a bare path.
- Section 7.3: the init writes the generated `config.xml`'s options once, before the first `serve` (section 11); the key row changes as in section 12.
- Section 7.3, the reconciler: a folder's type must be read back, since an unknown value becomes `sendreceive` without an error; new folders and devices are added with `"paused": true`; local discovery stays off until the configuration is approved; the reconciler never removes and re-adds a folder to repair it, and adds none before the share and placed-copy checks pass (section 6).
- Section 7.4: removing a Pixel's folder deletes Syncthing's `.stfolder` marker from `_pixels/<pixel id>/`, which is expected, and nothing else.
- Section 7.5: "make the Pixel match" is `POST /rest/db/override`, which also deletes what a person added to the folder on the Pixel; the confirmation should say so.
- Section 4: v2.1.7 needs nothing beyond `deploy/browser/seccomp.json` for what was exercised; set `setLowPriority` off.

## Not verified, and what would settle each

| Item | Why not | What would settle it |
| --- | --- | --- |
| The pinned release on aarch64 | Only the amd64 tarball was downloaded and run; the arm64 checksum is in the same signed file | Run the same steps from the `linux-arm64` tarball on an arm64 host (A1's image CI or the owner's hardware) |
| The release signing keys checked out of band | Both keys came through this sandbox's proxy | Compare the fingerprints with the ones Syncthing publishes in its documentation from another network, once, when #317 pins the checksum |
| The Syncthing app on an original Pixel pairing with v2.1.7, by QR and through the pending-device list | No Pixel here | S4 (#311) |
| Override against a Receive Only folder, and against a Pixel with versioning | The peer was a Send & Receive folder with no versioning | Repeat section 7 with the peer's folder Receive Only, then with simple versioning, and on the real Pixel in S4 |
| How soon a running instance notices an unmounted folder by itself | Each case requested a scan or restarted | Unmount and watch `/rest/db/status` without any call, through one file-watcher delay and one full rescan interval |
| `PUT /rest/config` (replacing the whole configuration in one call) | Only the per-object endpoints were used | One `PUT` of an edited `GET /rest/config` reply, read back |
| The three UDP sockets left open after turning discovery, relays and NAT off at runtime | Not traced | `strace` the process for a few minutes after the change; moot if the options are written before the first start |
| Syscalls on paths not exercised under the profile (relays, NAT, xattrs, versioning, large files) | Off by design or not tried | Only if one of them is ever turned on: repeat section 10 with it |
| Why 1.29.5 called `ioprio_set` and v2.1.7 did not | Only observed, not read in the source | Not needed with `setLowPriority` off |
| Host networking, `br0` and bridge discovery, the ports, and the socket on Unraid's appdata user-share layer | No Unraid host | S4 |
| The run with the real init, the real provider and the app | Out of scope: no repository code | #317 and #318, with section 12's checks as tests |
