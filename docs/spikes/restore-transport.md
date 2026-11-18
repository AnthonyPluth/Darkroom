# Spike S5b: backup and restore, transport

Spike S5b of the [single-container design](../design/single-container.md) (section 12, tracking issue #284, spike issue #313). It is meant to rehearse the transport's side of section 9 with the one container, Darkroom's Syncthing and a Pixel: the pairing after appdata is restored, a lost share and a replacement bound by the operator, an unbound or unmounted share, a database older than the share, and the stale restore of section 7.5, on SQLite and on PostgreSQL. It is the owner's rehearsal, on made-up photos, and this page was written from the record alone (#387): nothing was run for it.

## How to report back

Run an item's commands from [Commands for each item](#commands-for-each-item) (or follow its steps), fill in its `Result:` block and paste the block into a comment on #387, and we will fold it in. Several items can go in one comment, and one that was not run can say so. Say which database each result is for, and whether it ran against the original Pixel or a second Syncthing standing in for one. Before pasting, replace every device ID, address and file name with the words this page uses ("the Pixel", "a made-up photo", "the replacement share"), and paste the counts and the comparison of a listing, never the listing itself: the repository is public.

```text
Spike S5b (docs/spikes/restore-transport.md), item <number>, <SQLite or PostgreSQL>, <the Pixel or a stand-in>
Version, date, who ran it: <release tag>, <date>, <who>
Pass or fail: <pass / fail / not run, and why>
Result:
<the output the item asks for, pasted, with the replacements above>
```

## Result in one paragraph

No result is recorded. #313 was closed as completed on 2026-11-16 with no comment, no linked pull request and no write-up, and nothing in the repository holds one. #379 (C8) was reviewed with S5b missing from the pre-deploy list, #386 (2026-11-17) added it to the design's C8 row and note as still unrecorded, and #389 added it to [docs/deploy/one-container.md](../deploy/one-container.md#before-the-first-deploy), which says the transport's restore is "the documented procedure, not a rehearsed one". Every item below is therefore NOT VERIFIED. The logic each item rests on is covered by tests against a fake Syncthing and, for Send Only, by image CI against a second real Syncthing (next section); none of that is a restore of a real deployment with a Pixel. S5b also depends on S4 (#311), which has no recorded result either ([unraid-pixel.md](unraid-pixel.md)). The second half of this page is what the owner has to run and record.

## What other work checked, which is not this spike

| Where | What it showed | What it leaves to S5b |
| --- | --- | --- |
| [S3](syncthing-api.md) sections 6 and 7 | The folder marker puts a folder on an unmounted or empty mount into an error state with no deletion announced; a lost index, and "override" on a Send Only folder making the peer match, deleting what the peer added | The same with Darkroom's reconciler and a restored appdata |
| `tests/test_share_check.py` | A missing or changed sentinel, a share never bound and a different filesystem at the root each stop the cycle before a row or file changes; a share taken away between two cycles marks nothing pruned or placed; `bind-share` refuses a root on the container's own filesystem, without the typed confirmation and with another sentinel present; a replacement is refilled after the bind | A real share lost and replaced on the owner's server. What "the container's own filesystem" means is narrow: `share.separate_mount()` accepts any mount whose device differs from the container's `/`, so it tells an unmapped `/photos` from a mapped one, not the intended photo disk from some other host folder (item 3) |
| `tests/test_pixel_check.py` | A missing or changed placed copy is written again and read back before its Pixel is released; a file matching a queued copy is adopted, one matching nothing holds the Pixel untouched; the control entries are never counted; nothing is deleted | The same after a real restore, with a Pixel connected |
| `tests/test_transport.py` (`HeldPixelTests`, `ReconcilerTests`) | A held Pixel's folder is not shared before the check has run, nor between re-queueing and a verified replacement; a failed share check adds nothing and pauses the whole transport | What the Pixel receives, and when |
| C4 image CI (`tests/browser/syncthing_peer.sh`) | With a second pinned Syncthing as the Pixel: after `bind-share` and a restart the tend checks the placed copies, places them, and the reconciler resumes the folder; the share is unchanged while the peer edits, deletes and adds | A restore, a lost share and an original Pixel |

One thing the design describes is not built: section 7.5 says that after a lost or stale index Darkroom shows what a Pixel still holds and offers a manual "make the Pixel match". There is no override call in `darkroom/providers/syncthing.py` and no route for it, so item 5 records what Darkroom shows today, not that offer.

## Versions

To be recorded by the owner. Placeholders in angle brackets.

| Piece | Version |
| --- | --- |
| Host | `<Unraid or other>`, Docker `<version>` |
| Darkroom image | `ghcr.io/anthonypluth/darkroom:<release tag>`, its digest |
| Darkroom's Syncthing | the version `/rest/system/version` reports (v2.1.7 is pinned) |
| The Pixel | an original Pixel with `<Syncthing app and version>`, or a second Syncthing `<version>` standing in for one (say which, per item) |
| PostgreSQL | `<version>` |

With the variables of [Before you start](#before-you-start) set:

```sh
docker version --format '{{.Server.Version}}'
docker inspect --format '{{.Config.Image}}' "$C"
docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE"
curl -fsS http://127.0.0.1:8000/healthz; echo
docker logs "$C" 2>&1 | grep -F '"event": "darkroom started"' | tail -1
syncthing_version
docker exec <postgres container> psql -U <postgres user> -tAc 'show server_version'   # PostgreSQL only
```

`/healthz` answers `{"ok": true, "version": ...}`: that shows Darkroom answering, not which release runs (its `version` is a fixed package number). The `darkroom started` line names the release and the build's commit (`"version": "v1.2.3"`, `"commit": "abc1234"`), as does the Version row in the About card at the bottom of the Status page; an image built from a checkout without the release build args shows `dev`; `syncthing_version` prints `v2.1.7 release`. The Pixel's app version is at the bottom of its App info page (Android's Settings, Apps, the Syncthing app).

```text
Result:
Version, date, who ran it:
```

## What the owner runs, and what to record

Made-up photos only. Write down no device ID, address or file name: say "the Pixel", "a made-up photo", "the replacement share". Set-up for every item: one person with a few made-up originals, one Pixel paired (S4's items 6 and 7), the share bound (`python run.py bind-share`), and the copies placed and on the Pixel (the People page shows it up to date). `<appdata>` and `<backup>` as in [restore-app-browser.md](restore-app-browser.md). Before and after each item, take a listing with checksums of the Pixel's folder on the Pixel (or the stand-in's folder) and of `_pixels/<pixel id>/` on the share, so "nothing deleted from the Pixel" is a comparison, not an impression.

| # | Item (from #313) | Run | Record |
| --- | --- | --- | --- |
| 1 | The Pixel's pairing survives a restore of appdata | Stop the container, take the backup (it includes `/data/syncthing`), remove the container and appdata, create a new container, put the backup back, start | Darkroom's device ID unchanged (the one the People page's pairing step shows; Syncthing's API is only on a socket inside the container); the Pixel connects with no new pairing; the People page shows it connected and up to date; the Pixel's listing unchanged |
| 2 | The share lost, a replacement bound | Stop the container. Map a replacement share at the same path: a different, empty folder or mount (not the old share with folders moved), with `originals/` restored from its backup but without the old share's `.darkroom-share` sentinel and without `_pixels/`. Only a new root without the original sentinel rehearses this: on the bound share the share check (`ShareCheck.problem()` in `darkroom/domain/share_check.py`) looks only at the root's filesystem and the sentinel, so it passes there. Start, and wait for the next tend (hourly, or `python run.py jobs pixels.tend` in the container). Then run `python run.py bind-share`: it prints what it found (the root, what it is mounted from, the number of entries, the files under `_pixels/`, the copies recorded as placed), asks for `paused` (with Darkroom's own Syncthing its prompt says that Darkroom keeps it paused while the share has no sentinel, and asks for `paused` once you have checked that no other Syncthing serves `_pixels/`) and then for `bind` | Before the bind: `share_check_failed` with the reason `the sentinel is missing from the share`, the "Photo share check failed" alert, the transport paused, nothing sent to the Pixel. What `bind-share` printed. After it: `placed copies checked` with each copy `requeued` (`pixel_copy_missing`), the Pixel held, each copy written again and read back (`pixel_copy_replaced`), and only then the folder shared; the Pixel's listing unchanged at every step (no deletion reached it) |
| 2b | `_pixels/` missing on the bound share (a separate recovery check, not the lost share) | Stop the container. On the same, still-bound share move `_pixels/<pixel id>/` off the share (keep it), leaving the sentinel in place. Start | That the share check passes (no `share_check_failed`, no rebind asked for), which is what Darkroom does today; that the placed-copy check before the first tend requeues every copy of that Pixel (`pixel_copy_missing`) and holds it, the tend writes them again and reads them back, and only then the folder is shared (as `tests/test_pixel_check.py`'s missing-folder test expects); whether Syncthing marks the folder in error while its marker is gone (S3 section 6) and whether any deletion reaches the Pixel; the Pixel's listing before and after |
| 3 | An unbound or unmounted share is refused | (a) With the share bound, stop, point the share mapping at an empty folder on the host, start. (b) Remove the share mapping altogether, so `/photos` is a folder of the container's own filesystem, start, and run `python run.py bind-share`. (c) With the mapping of (a), run `python run.py bind-share` and stop at the last prompt (type anything but `bind`). (d) With the container running, unmount the share on the host | (a): `share_check_failed` with `the sentinel is missing from the share`, no placing, pruning or deletion, the Pixel's listing unchanged. (b): the same refusal by the share check, and `bind-share` refuses with `the share root is not a mount separate from the container's own filesystem`. (c): what `bind-share` printed. It does not refuse here: `share.separate_mount()` accepts any mount whose device differs from the container's `/`, and `share_bind.survey()` does not reject an empty root, so an empty host folder can be bound once the operator confirms; the only guard today is reading its summary (0 files under `_pixels/` against the copies recorded as placed). Record it as that, not as a refusal. (d): what `ls /photos` shows inside the container and what the next tend's share check says. With Docker's default private mount propagation the container may keep its view of the unmounted filesystem, so the check can still pass; Darkroom guarantees only that a root whose sentinel or filesystem has changed is refused |
| 4 | A database older than the share | Upload a few made-up photos to the person's Immich with Immich's mobile app and run `python run.py reconcile-immich --person <person>` in the container, which writes them to `originals/immich/` and queues their Pixel copies (`python run.py index` queues none); stop the container before the next hourly tend places them (no `pixel folders tended` line with them placed in the log) and take the backup. Take it after the share was bound, so the restored database records this share's sentinel. Start again and place the copies (`python run.py jobs pixels.tend`). Stop; restore only the database from the backup (appdata's `darkroom.db`, or the PostgreSQL dump) and keep the share and `/data/syncthing` as they are; start. Then add one stray file to `_pixels/<pixel id>/<person>/` that matches nothing | Files that match a copy the restored database has as queued adopted (`pixel_copy_adopted`), not touched. Any file the restored database knows nothing about (an original indexed after the backup) is unaccounted, not adopted. The stray file: `pixel_files_unaccounted` with a count and no name, the "Pixel folder held" alert (it opens only with Syncthing running), the folder kept paused until the file is removed by hand, then resumed |
| 5 | The stale restore of section 7.5: an older index, and a lost key | (a) Take the backup. Let a Takeout match prune some of the placed copies, so their deletions reach the Pixel; a prune needs a Takeout import that contains those photos, so the made-up photos have to be in the test Google account's library. Stop; restore the whole of appdata from the backup (database and `/data/syncthing` both older than the Pixel's state); start. (b) Separately, delete `/data/syncthing` only (a lost device key) and start | (a) What the placed-copy check does with copies the restored database still has as placed but whose files were pruned (expected from section 9: `pixel_copy_missing`, written again and sent to the Pixel again, pruned again only on a later match); what the Pixel holds afterwards; whether any deletion is announced to it; what the People and Status pages show. (b) A new device ID for Darkroom; the Pixel no longer connects; what Darkroom shows; pairing again from the People page (Pair again); the Pixel's listing after the new pairing |
| 6 | SQLite and PostgreSQL | Items 1 to 5 on SQLite, then with `DATABASE_URL` set (for item 1 the dump and appdata from the same stopped point) | A row per item and database in a results table here, and any not run named as not supported |

For each item, also note whether it ran against the original Pixel or a second Syncthing standing in for one (#313 allows the stand-in "where the write-up says so"; items 1, 2 and 5 are the ones that need the real Pixel to say what it holds). A defect found becomes its own issue and is linked from this page.

## Commands for each item

Each item below has what to run, what a pass and a fail look like (taken from the code, the runbook and the tests, not from a run), and an empty block to fill in and paste ([How to report back](#how-to-report-back)). The commands are for the Unraid terminal (as root). Every log line Darkroom writes is one JSON object with its keys sorted, so the commands grep for `"event": "<name>"`. The service tends the Pixel folders once as it starts and then hourly, so a restart is the way to run a tend now: `python run.py jobs pixels.tend` in the container writes copies back too, but it does not resume Darkroom's Syncthing, and its log lines go to that command's own output.

### Before you start

Set these in every new terminal (the values in angle brackets are yours; the examples are made up):

```sh
C='<container>'                      # the container's name on the Docker tab
TAG='<release tag>'                  # the tag in the template's Repository, for example v1.2.3
IMAGE="ghcr.io/anthonypluth/darkroom:$TAG"
APPDATA=/mnt/user/appdata/darkroom   # the template's Appdata path
SHARE='<photo share path>'           # the template's Photo share path, for example /mnt/user/photos
BACKUP=/mnt/user/backups/darkroom-s5b   # where the copy of appdata goes, outside appdata
mkdir -p "${BACKUP%/*}"                # cp -a does not create a missing parent folder
PIXEL='<pixel id>'                   # the folder name under $SHARE/_pixels/ (ls "$SHARE/_pixels")
SANDBOX='--user=99:100 --cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=/boot/config/darkroom/seccomp.json'

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

copies() {  # the Pixel copies by Pixel and state, from Darkroom's SQLite database (read only)
  docker exec -i "$C" python - <<'PY'
import sqlite3
db = sqlite3.connect("file:/data/darkroom.db?mode=ro", uri=True)
for pixel, state, count in db.execute("select pixel_id, state, count(*) from pixel_copies group by pixel_id, state order by pixel_id, state"):
    print(f"pixel {pixel}: {count} {state}")
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

share_listing() {  # SHA-1 of every file in the Pixel's folder on the share, without Syncthing's .stfolder and .stignore
  (cd "$SHARE/_pixels/$PIXEL" && find . -type f ! -path './.st*' -exec sha1sum {} + | sort -k2)
}
compare() {  # compare two listings without printing a file name
  if diff -q "$1" "$2" >/dev/null; then echo "unchanged ($(wc -l < "$2") files)"; else echo "changed: $(diff "$1" "$2" | grep -c '^<') gone or different, $(diff "$1" "$2" | grep -c '^>') new or different"; fi
}
tended() {  # the transport's log lines since a time, for example tended 10m
  docker logs --since "${1:-10m}" "$C" 2>&1 | grep -E '"event": "(pixel folders tended|pixel folders not tended[^"]*|placed copies checked|syncthing reconciled|syncthing transport paused: the share check failed)"' | tail -n 12
}
```

On PostgreSQL, `events` does not apply: run `psql "$DATABASE_URL" -c "select to_timestamp(time), type, subject, detail from events order by id desc limit 30"` wherever `psql` reaches the database (and `select pixel_id, state, count(*) from pixel_copies group by 1, 2` in place of `copies`). Copies are queued only by the Immich reconcile (and a person's one legacy import), never by `python run.py index`, which only indexes what is in `originals/`. So made-up photos reach a Pixel by the real path: upload them to the person's test Immich user with Immich's mobile app (the reconcile takes only phone uploads, assets with the `mobile-app` key and no library), then run the reconcile. It runs only once that person's Takeout is imported ([runbook](../runbook.md#immich-reconcile)). 

A made-up Takeout, hand-dropped and imported by the documented route ([runbook](../runbook.md#hand-dropping-an-export)): the Immich reconcile runs for a person only once a Takeout of theirs is imported, and a test account never gets a real one in time. `made_up_takeout` zips the photos of a host folder of made-up photos as a Takeout export (with Google's JSON sidecars), puts it in that person's `_inbox/<person>/manual/` and imports it; the person must have a Google account in Darkroom (name it as a third argument when they have several):

```sh
made_up_takeout() {  # made_up_takeout <person> <host folder of made-up photos> [<Google account email>]
  rm -rf "${APPDATA:?}/made-up-takeout" && mkdir -p "$APPDATA/made-up-takeout" && cp "$2"/* "$APPDATA/made-up-takeout"/ && chown -R 99:100 "$APPDATA/made-up-takeout"
  docker exec -i "$C" python - /data/made-up-takeout "/photos/_inbox/$1/manual" <<'PY'
import datetime, json, sys, zipfile
from pathlib import Path
source, target = Path(sys.argv[1]), Path(sys.argv[2])
target.mkdir(parents=True, exist_ok=True)
name = target / f"takeout-{datetime.datetime.now(datetime.UTC):%Y%m%dT%H%M%SZ}-001.zip"
with zipfile.ZipFile(name, "w") as made:
    for photo in sorted(source.iterdir()):
        folder = "Takeout/Google Photos/Photos from 2026"
        made.write(photo, f"{folder}/{photo.name}")
        made.writestr(f"{folder}/{photo.name}.supplemental-metadata.json", json.dumps({"title": photo.name, "photoTakenTime": {"timestamp": str(int(photo.stat().st_mtime))}}))
print(f"wrote {name.name} with {len(made.namelist()) // 2} photos")
PY
  docker exec "$C" python run.py import-takeout --person "$1" ${3:+--account "$3"}
  rm -rf "${APPDATA:?}/made-up-takeout"
}
```

Expected: `wrote takeout-<time>-001.zip with N photos`, then `Export <id>: N added, 0 skipped as duplicates, 0 in review, N missing GPS` (an earlier export of the same person is reported as `Export <id> is already imported.` first); the export is then `imported`, which is what the reconcile asks for.

The set-up every item assumes, as commands: the person, their Google account and the Pixel added and paired on **People** (S4's items 6 and 7), then:

```sh
made_up_takeout '<person>' '<host folder of made-up photos>'      # once per person; other photos than the ones uploaded below
# upload a few other made-up photos to the person's Immich with Immich's mobile app
docker exec "$C" python run.py reconcile-immich --person '<person>'   # N queued for the Pixel folder, N above 0
docker exec -it "$C" python run.py bind-share                      # once; type paused, then bind
docker restart "$C"; sleep 120; tended 5m; copies                   # "held": 0, and the copies placed
```

The Immich reconcile also needs `IMMICH_URL` (and `IMMICH_ADMIN_KEY`) in the template, and the person's **Immich user ID** and **Immich API key** (a key of that same Immich user) on their **Edit** form on **People**; without them it prints `reconcile-immich: <person>: this person has no Immich user and key`.

The Pixel's own listing, from a computer with `adb` and USB debugging on the Pixel (or a stand-in's folder with the same `find`):

```sh
adb shell 'cd /sdcard/<the folder on the Pixel> && find . -type f ! -path "./.st*" -exec sha1sum {} +' | sort -k2 > pixel-<step>.txt
```

Without `adb`, record the file count and size the Syncthing app shows for the folder. Take `share_listing > /tmp/share-before.txt` and the Pixel's listing before each item, and again after each step; `compare /tmp/share-before.txt /tmp/share-after.txt` prints what to paste. Taking the backup is the same each time (it includes `/data/syncthing`):

```sh
docker stop "$C"
rm -rf "$BACKUP" && cp -a "$APPDATA" "$BACKUP"
docker exec <postgres container> pg_dump -U <postgres user> -Fc darkroom > "$BACKUP.dump"   # PostgreSQL only, still stopped
docker start "$C"
```

With `DATABASE_URL` set (PostgreSQL), the database is not in appdata ([postgres.md](../deploy/postgres.md)): copying appdata back restores `/data/syncthing` and the key but leaves the database as it is now. Wherever a step below puts the backup back, restore the dump taken at the same stopped point too, before starting the container, with the standard PostgreSQL tool (`--clean` drops the current tables first):

```sh
restore_dump() {  # PostgreSQL only: put the database back to the dump taken with the backup; the container must be stopped
  docker exec -i <postgres container> pg_restore -U <postgres user> -d darkroom --clean --if-exists --no-owner < "$BACKUP.dump"
}
```

### Item 1: the Pixel's pairing survives a restore of appdata

```sh
sha256sum "$APPDATA/syncthing/cert.pem"; share_listing > /tmp/share-before.txt   # and the Pixel's listing
docker stop "$C" && rm -rf "$BACKUP" && cp -a "$APPDATA" "$BACKUP"
docker exec <postgres container> pg_dump -U <postgres user> -Fc darkroom > "$BACKUP.dump"   # PostgreSQL only
docker rm "$C"; rm -rf "$APPDATA"; cp -a "$BACKUP" "$APPDATA"
restore_dump                                     # PostgreSQL only
# Docker tab, Add Container, Template: darkroom under the user templates, Apply
sleep 120
sha256sum "$APPDATA/syncthing/cert.pem"
tended 5m; events share_check_failed pixel_copy_missing syncthing_reconcile_failed | head -n 5
share_listing > /tmp/share-after.txt; compare /tmp/share-before.txt /tmp/share-after.txt
```

The certificate is Syncthing's device key, so the same hash means the same device ID (the People page's pairing step shows the ID itself). Pass: the same hash; no `syncthing identity generated` in the log; the Pixel connects with no new pairing and no new waiting device; its row on **People** reaches **Connected** ("Up to date"); `placed copies checked` with `"requeued": 0`; both listings unchanged. Fail: a new device ID, a waiting device, or a listing changed.

```text
Result:
Version, date, who ran it:
```

### Item 2: the share lost, a replacement bound

```sh
share_listing > /tmp/share-before.txt   # and the Pixel's listing
docker stop "$C"
NEW='<replacement share path>'         # a different, empty folder or mount, for example /mnt/user/photos-replacement
mkdir -p "$NEW/<person>/originals" && chown -R 99:100 "$NEW"   # rsync does not create the missing <person>/ parent
rsync -a '<the share backup>/<person>/originals/' "$NEW/<person>/originals/"   # each person's originals/ only
ls -A "$NEW"                           # no .darkroom-share, no _pixels
```

Then on the Edit page set **Photo share** to `$NEW`'s path, Apply (it starts the container and the start runs a tend):

```sh
sleep 90; tended 5m
events share_check_failed share_bound | head -n 3
# the Pixel's listing now
docker exec -it "$C" python run.py bind-share    # type paused, then bind
docker restart "$C"
sleep 120; tended 5m
events pixel_copy_missing pixel_copy_replaced pixel_copy_replacement_pending share_bound | head -n 20
SHARE="$NEW"; share_listing > /tmp/share-after.txt; compare /tmp/share-before.txt /tmp/share-after.txt
```

Pass before the bind: `{"event": "pixel folders not tended: the share check failed", "reason": "the sentinel is missing from the share"}` and `syncthing transport paused: the share check failed` in the log, a `share_check_failed` event with that reason, the **Status** page's "Photo share check failed" alert and its Syncthing card **Paused**, nothing sent to the Pixel. `bind-share` prints the share root, `Mounted from: ...`, `Entries at the root: N; files under _pixels/: 0`, `Pixel copies recorded as placed: N ...`, `No sentinel is on this share yet.`, then the `Syncthing is Darkroom's own ...` question and, after `bind`, `Bound. ...`. After the restart: `placed copies checked` with `"requeued"` equal to the copies placed and `"held": 1`, a `pixel_copy_missing` event per copy, then a `pixel_copy_replaced` per copy, and only then the folder shared (`pixel folders tended` with `"held": 0`). The new share's listing equals the old one, and the Pixel's listing is unchanged at every step. Fail: anything placed, pruned or sent before the bind, a deletion reaching the Pixel, or the folder shared while a copy is still missing.

```text
Result:
What bind-share printed (root and mount replaced by "the replacement share"):
Version, date, who ran it:
```

Put **Photo share** back afterwards; the old share still has its own sentinel, which the database no longer records, so it is now a share to bind again (or restore the backup).

### Item 2b: `_pixels/` missing on the bound share

```sh
share_listing > /tmp/share-before.txt   # and the Pixel's listing
docker stop "$C"
mv "$SHARE/_pixels/$PIXEL" '<somewhere outside the share>/pixel-aside'
docker start "$C"
sleep 120; tended 5m
events share_check_failed share_check_passed pixel_copy_missing pixel_copy_replaced | head -n 20
ls -a "$SHARE/_pixels/$PIXEL" | grep -F .stfolder || echo "no .stfolder marker"
share_listing > /tmp/share-after.txt; compare /tmp/share-before.txt /tmp/share-after.txt
```

Pass: no new `share_check_failed` and no rebind asked for (the sentinel is still there, which is what Darkroom does today); `placed copies checked` with every copy of that Pixel requeued and `"held": 1`; `pixel_copy_missing` then `pixel_copy_replaced` for each; and only then the folder shared (`"held": 0`). Record whether Darkroom's Syncthing marks the folder in error while its marker is gone (the People row and the Status page's Syncthing card), whether any deletion reaches the Pixel, and the two comparisons. Put the moved folder back only if the item failed.

```text
Result:
Version, date, who ran it:
```

### Item 3: an unbound or unmounted share is refused

(a) With the share bound, stop the container, set **Photo share** to an empty folder on the host, Apply:

```sh
mkdir -p '<an empty folder>' && chown 99:100 '<an empty folder>'
sleep 90; tended 5m; events share_check_failed | head -n 2
```

Pass: `"reason": "the sentinel is missing from the share"`, no placing, pruning or deletion, the Pixel's listing unchanged.

(b) With no share mapped (remove the **Photo share** entry on the Edit page; if Unraid does not save the template without it, run only the command below and say so), `/photos` is a folder of the container's own filesystem:

```sh
docker stop "$C"
docker run --rm -it $SANDBOX -v "$APPDATA:/data" "$IMAGE" python run.py bind-share
```

Pass: `bind-share: the share root is not a mount separate from the container's own filesystem; map the working share to it and start again; nothing was written`; and, if the service ran without the mapping, the share check's refusal as in (a).

(c) With (a)'s mapping, run `bind-share` and stop at the last question:

```sh
docker exec -it "$C" python run.py bind-share    # type paused, then anything but bind
```

Expected: it does not refuse here. It prints the summary (`files under _pixels/: 0` against the copies recorded as placed) and, at the last answer, `bind-share: not confirmed; nothing was written`. Record it as that, not as a refusal: `share.separate_mount()` accepts any mount whose device differs from the container's `/`, so an empty host folder could be bound once confirmed.

(d) With the share mapped as deployed and the container running, unmount the share's disk on the host (a disk mounted by Unassigned Devices, for example; a user share cannot be unmounted on its own), then:

```sh
docker exec "$C" sh -c 'ls -A /photos | wc -l; stat -f -c %T /photos'
docker exec "$C" python run.py jobs pixels.tend
tended 2h
```

Record what the container sees and what the next hourly tend's share check says. With Docker's default private mount propagation the container may keep its view of the unmounted file system, so the check can still pass; Darkroom guarantees only that a root whose sentinel or file system has changed is refused.

```text
Result (a):
Result (b):
Result (c):
Result (d):
Version, date, who ran it:
```

### Item 4: a database older than the share

```sh
# upload a few made-up photos to the person's Immich with Immich's mobile app first
copies                                          # note the counts before
docker exec "$C" python run.py reconcile-immich --person '<person>'
copies                                          # "queued" up by the number just uploaded
docker logs --since 10m "$C" 2>&1 | grep -F '"event": "pixel folders tended"' | tail -n 1   # must not show them placed yet
docker stop "$C" && rm -rf "$BACKUP" && cp -a "$APPDATA" "$BACKUP"
docker exec <postgres container> pg_dump -U <postgres user> -Fc darkroom > "$BACKUP.dump"   # PostgreSQL only
docker start "$C"; sleep 120; tended 5m; copies # the start's tend places them: "queued" back down, "placed" up
docker stop "$C"
rm -f "$APPDATA/darkroom.db-wal" "$APPDATA/darkroom.db-shm"
cp -a "$BACKUP"/darkroom.db* "$APPDATA"/        # SQLite: the database and its WAL files only
restore_dump                                     # PostgreSQL only, in place of the two lines above
docker start "$C"; sleep 120; tended 5m
events pixel_copy_adopted pixel_files_unaccounted | head -n 20
```

On PostgreSQL, `restore_dump` takes the place of the two SQLite lines (keep `/data/syncthing` and the share as they are). Then one stray file that matches nothing, and a restart to run the check:

```sh
head -c 4000 /dev/urandom > "$SHARE/_pixels/$PIXEL/<person>/made-up-stray.jpg"; chown 99:100 "$SHARE/_pixels/$PIXEL/<person>/made-up-stray.jpg"
docker restart "$C"; sleep 120; tended 5m
events pixel_files_unaccounted alert_sent | head -n 5
mv "$SHARE/_pixels/$PIXEL/<person>/made-up-stray.jpg" /tmp/
docker restart "$C"; sleep 120; tended 5m; events pixel_files_unaccounted | head -n 2
```

Pass before the backup: `reconcile-immich` prints `<person>: N listed, N stored (N copied to originals, ...), N queued for the Pixel folder, ...` with N the photos uploaded, and `copies` shows that many more `queued` for the Pixel (none queued means there is nothing to adopt: check that the person's Takeout is imported and the uploads came from the mobile app). After the database restore, `placed copies checked` with `"adopted"` equal to the copies placed after the backup, a `pixel_copy_adopted` event each, and their files untouched. With the stray file: `pixel_files_unaccounted` with `files 1` (no name anywhere), `"held": 1`, the **Status** page's "Pixel folder held" alert (with Syncthing running), the folder kept paused; after it is moved out, `files 0`, `"held": 0` and the folder resumed. Fail: an adopted file changed, a file deleted, or a name in the log.

```text
Result:
Version, date, who ran it:
```

### Item 5: the stale restore of section 7.5

(a) Take the backup (on PostgreSQL with its dump, as in "Taking the backup" above). Let a Takeout match prune some placed copies, and see the deletions reach the Pixel. A prune needs a Takeout match for the Google account the Pixel uploads to, made after the copy was placed (the reconcile's copies are always-send): either the photos in the test account's real library and a real Takeout, or a second hand-dropped Takeout of the same made-up photos that went up through Immich:

```sh
made_up_takeout '<person>' '<the photos uploaded through Immich>' '<email of the Google account the Pixel uploads to>'
docker restart "$C"; sleep 120
tended 2h                                        # "pruned" above 0
share_listing > /tmp/share-pruned.txt            # and the Pixel's listing
docker stop "$C"; rm -rf "$APPDATA"; cp -a "$BACKUP" "$APPDATA"
restore_dump                                     # PostgreSQL only: without it the pruned copies stay pruned and nothing is found missing
docker start "$C"
sleep 120; tended 5m
events pixel_copy_missing pixel_copy_replaced | head -n 20
share_listing > /tmp/share-after.txt; compare /tmp/share-pruned.txt /tmp/share-after.txt
```

Expected from section 9: the copies the restored database has as placed but whose files were pruned are `pixel_copy_missing`, written again (`pixel_copy_replaced`) and sent to the Pixel again, and pruned again only on a later match. Record what the Pixel holds afterwards, whether any deletion is announced to it, and what **People** and **Status** show.

(b) A lost device key:

```sh
docker stop "$C"
mv "$APPDATA/syncthing" /mnt/user/appdata/darkroom-syncthing-aside
docker start "$C"; sleep 60
docker logs --since 2m "$C" 2>&1 | grep -F '"event": "syncthing identity generated"'
```

Expected: `syncthing identity generated` and a new device ID on the pairing step; the Pixel no longer connects. Record what Darkroom shows, then pair again (**People**, the Pixel's **Edit**, **Pair again**, and S4's items 6 and 7 from the scan on) and the Pixel's listing after the new pairing.

```text
Result (a):
Result (b):
Version, date, who ran it:
```

### Item 6: SQLite and PostgreSQL

| Item | SQLite | PostgreSQL | Version, date, who ran it |
| --- | --- | --- | --- |
| 1 | not run | not run | |
| 2 | not run | not run | |
| 2b | not run | not run | |
| 3 | not run | not run | |
| 4 | not run | not run | |
| 5 | not run | not run | |

## What this changes in the design

Nothing yet. Until the table above is filled in:

- Section 9's restore of the transport (the placed-copy check after a restore, the bind of a replacement, a database older than the share) and section 7.5's stale-restore row remain the documented procedure, not a rehearsed one.
- C8's check is not met: S5b is one of the things listed as coming before the first deploy, in the design's plan row and in [docs/deploy/one-container.md](../deploy/one-container.md#before-the-first-deploy).
- Section 7.5's "make the Pixel match" is not built (above); whether it is needed is for the owner once item 5 shows what a stale restore leaves on the Pixel.

## Not verified, and what would settle each

| Item | Why not | What would settle it |
| --- | --- | --- |
| Every item in the table above | No result recorded on #313 or anywhere else; it needs the owner's deployment and a paired Pixel (S4) | The owner's run of the table, recorded on this page |
| Override against a Pixel with versioning (S3's open item) | Needs the Pixel | Only if "make the Pixel match" is built: repeat item 5 with it |
