# Spike S5a: backup and restore, app and browser

Spike S5a of the [single-container design](../design/single-container.md) (section 12, tracking issue #284, spike issue #312). It is meant to rehearse, on a fresh one container built from main and before Syncthing is involved, the backup, restore and rollback of section 9: appdata restored alone, the browser-profile copy interrupted and repeated, a rollback after the one container has done browser work, and the secret key supplied from outside appdata, each on SQLite and on PostgreSQL. It is the owner's rehearsal, with a low-value Google test account, and this page was written from the record alone (#387): nothing was run for it.

## How to report back

Run an item's commands from [Commands for each item](#commands-for-each-item) (or follow its steps), fill in its `Result:` block and paste the block into a comment on #387, and we will fold it in. Several items can go in one comment, and one that was not run can say so. Say which database and which key each result is for (item 5). Before pasting, replace every account, address, host name and file name with the words this page uses ("the test account", "a made-up capture"): the repository is public.

```text
Spike S5a (docs/spikes/restore-app-browser.md), item <number>, <SQLite or PostgreSQL>, <generated or external key>
Version, date, who ran it: <release tag>, <date>, <who>
Pass or fail: <pass / fail / not run, and why>
Result:
<the output the item asks for, pasted, with the replacements above>
```

## Result in one paragraph

No result is recorded. #312 was closed as completed on 2026-11-16 with no comment, no linked pull request and no write-up, and nothing in the repository holds one. #351 (D1), merged a minute before #312 was closed, says "the owner steps S5a (#312, the restore rehearsal) and the scoped #54 re-scan are still open; the owner chose to proceed because nothing is deployed". [docs/deploy/one-container.md](../deploy/one-container.md#before-the-first-deploy) still says the backup, restore and rollback are "the documented procedure, not a rehearsed one". Every item below is therefore NOT VERIFIED, and section 9 still describes no combination of database and key as supported by a rehearsal. Parts of the procedure were exercised by an agent in a throwaway Docker daemon and by the test suite (next section), without a Google sign-in, without PostgreSQL and without the external key. The second half of this page is what the owner has to run and record.

## What other work checked, which is not this spike

| Where | What it showed | What it leaves to S5a |
| --- | --- | --- |
| #340 (A5), in a throwaway Docker daemon, amd64 | A fresh one container generates `/data/secret-key` (mode 0600, owner 99:100) on its first start. `copy-browser-state --source /photos/_browser --target /data/browser` against a made-up profile folder reported "Copied and verified 1 files in 3 folders ... The source was not changed", and the one container then started. After a change in appdata, the reverse copy with `--set-aside` copied it back and renamed the old folder to `_browser.before-<time>` | A signed-in profile, a restore of appdata, an interruption, PostgreSQL, the external key |
| S1 observation 7 ([merged-image.md](merged-image.md)) | Chromium's own stores survive a copy across mounts | Whether Google accepts the copied session |
| Image CI (`tests/browser/copy_under_profile.sh`) | The copy across mounts under the deployment profile, modes kept, Chromium's lock links left out, the source unchanged | The same with a real profile |
| `tests/test_browser_copy.py` | An interrupted copy leaves the source and no target; a copy that differs is not put in place; `--set-aside` renames the old target only after the copy is verified; nothing is deleted either way | The same on the owner's mounts, interrupted for real |
| `tests/test_secretbox.py` | The key file is written once with mode 0600 and never regenerated; `serve` refuses to start on sealed values without their key; `DARKROOM_SECRET_KEY` overrides the file | The refusal and the recovery on a restored deployment, on both databases |

Since #312 was written, D1 (#327, #351) removed the two-container layout. The newest `v3` release (v3.16.0 when this page was written) is the last with both layouts, and its "Going back to the two containers" runs `copy-browser-state --set-aside` back to the share. It works only on a database that release can start on: releases after it added migrations (0032 and 0033 by v4), and an older image does not start on a newer revision (design section 9). So item 3's rollback is rehearsed within v3.16.0, and going back to it from a later release is restoring the backup taken before that upgrade.

## Versions

To be recorded by the owner. Placeholders in angle brackets.

| Piece | Version |
| --- | --- |
| Host | `<Unraid or other>`, Docker `<version>` |
| Darkroom image | `ghcr.io/anthonypluth/darkroom:<release tag>` built from main at `<commit>`, its digest |
| For item 3 | the newest `v3` release's tag, and the image tags it pulls |
| PostgreSQL | `<version>` (CI runs `postgres:17`) |
| Browser | `BROWSER_CHANNEL` and the Chrome or Chromium version in the image |

With the variables of [Before you start](#before-you-start) set:

```sh
docker version --format '{{.Server.Version}}'
docker inspect --format '{{.Config.Image}}' "$C"
docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE"
curl -fsS http://127.0.0.1:8000/healthz; echo
docker logs "$C" 2>&1 | grep -F '"event": "darkroom started"' | tail -1
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$C" | grep -E '^BROWSER_CHANNEL='
docker exec "$C" /opt/google/chrome/chrome --version
docker exec "$C" sh -c 'ls -d /ms-playwright/chromium-*'
docker exec <postgres container> psql -U <postgres user> -tAc 'show server_version'   # PostgreSQL only
```

`/healthz` answers `{"ok": true, "version": ...}`: that shows Darkroom answering, not which release runs (its `version` is a fixed package number). The `darkroom started` line names the release and the build's commit (`"version": "v1.2.3"`, `"commit": "abc1234"`), as does the Version row in the About card at the bottom of the Status page; an image built from a checkout without the release build args shows `dev`. Keep the `grep` on the `BROWSER_CHANNEL` line: without it the command prints every variable, secrets included.

```text
Result:
Version, date, who ran it:
```

## What the owner runs, and what to record

Use a low-value Google test account and made-up photos only. Write down no account, address or file name: say "the test account", "a made-up capture". Each run starts from a fresh one container. Darkroom's Syncthing runs in it too, since it is always on, but with no Pixel paired it syncs nothing (the transport is S5b). `<appdata>` is the appdata folder on the host and `<backup>` a copy of it; in the container `python run.py` runs from `/app`.

Taking the backup, the same each time: stop the container, `cp -a <appdata> <backup>` (and, on PostgreSQL, `pg_dump` of the database taken while it is stopped, as [docs/deploy/postgres.md](../deploy/postgres.md) says), note what the deployment holds at that point (people, accounts, the test account's sign-in, one capture).

| # | Item (from #312) | Run | Record |
| --- | --- | --- | --- |
| 1 | Appdata restored alone onto a fresh one container | Set up sign-in, one person, the Immich key and the test account signed in through the Remote browser page; take the backup. Remove the container and appdata; create a new container from the template or compose with the same variables; put `<backup>` back as appdata (owner 99:100; on PostgreSQL restore the dump into an empty database); start it | `serve` starts (it refuses to start when sealed values are stored and their key is missing, or when a stored setting's secret cannot be read with the key, and says which); `check-config` names where the key comes from; sign-in works with an existing session and with a new one; the Immich reconcile runs with the restored key; the test account's next Takeout job or the Remote browser page shows it still signed in, or Google asks for a sign-in (record which); deleting that account removes its folder under `/data/browser/` |
| 2 | The profile copy interrupted part-way, then repeated | Start from profiles on the share's `_browser/` (left by a `v3` two-container deployment, or by item 3's copy back) and an appdata with no `/data/browser` yet: the command refuses a target that exists unless `--set-aside` is given. With the container stopped (no profile may be open), run `python run.py copy-browser-state --source /photos/_browser --target /data/browser` in a one-off container of the same image with the same mappings and sandbox flags, and kill it part-way (`docker kill` on that container, or Ctrl-C after the first folders) | What the kill left (expected: a hidden `.browser.copying-<random>` folder beside the target and no `/data/browser`); `_browser/` on the share unchanged (`find _browser -type f -exec sha1sum {} +` before and after); what the next run says about the hidden folder ([browser.md](../deploy/browser.md#copying-browser-profiles-copy-browser-state) says it is reported); the repeated command after removing that folder by hand: "Copied and verified ... The source was not changed"; the test account still signed in afterwards |
| 3 | A rollback after the one container has added an account and written a capture | On v3.16.0's one container (see above), add a second test account (signed in) and make a job fail once so it writes a capture. Stop the container. Follow that release's "Going back to the two containers": `python run.py copy-browser-state --source /data/browser --target /photos/_browser --set-aside`, then the two containers of the same release. Separately, from the current release, note that the way back is restoring the backup taken before upgrading past v3.16.0, which discards everything since | The old `_browser/` set aside as `_browser.before-<time>` and nothing deleted; the two-container layout serves the second account (signed in) and the capture on its failure page. If v3.16.0's container is started on a database a later release migrated, the refusal it gives |
| 4 | The key supplied from outside appdata | Repeat item 1 with `DARKROOM_SECRET_KEY` set in the template or `.env` (a key generated for the test, never one in use) and no `secret-key` file in appdata. Start the restored container once without the variable, then with it | Without it: `serve` refuses with `config: there is no secret key, but sealed values are already stored ...` and `check-config` says the same; with it: everything in item 1's record |
| 5 | Each of the above on SQLite and on PostgreSQL, with the generated and the external key | Items 1 to 3 with the generated key on SQLite, then on PostgreSQL (`DATABASE_URL`); item 4 on each | A row per combination in a results table here, and any combination not run named as not supported (section 9 says none is described as supported before its rehearsal) |

A defect found during the rehearsal becomes its own issue (#312's out of scope) and is linked from this page.

## Commands for each item

Each item below has what to run, what a pass and a fail look like (taken from the code and the tests, not from a run), and an empty block to fill in and paste ([How to report back](#how-to-report-back)). The commands are for the Unraid terminal (as root); with compose, `docker compose exec darkroom` takes the place of `docker exec "$C"` and `./data` that of `$APPDATA`. Every log line Darkroom writes is one JSON object with its keys sorted, so the commands grep for `"event": "<name>"`; a refusal to start is a plain `config: ...` line.

### Before you start

Set these in every new terminal (the values in angle brackets are yours; the examples are made up):

```sh
C='<container>'                      # the container's name on the Docker tab
TAG='<release tag>'                  # the tag in the template's Repository, for example v1.2.3
IMAGE="ghcr.io/anthonypluth/darkroom:$TAG"
APPDATA=/mnt/user/appdata/darkroom   # the template's Appdata path
SHARE='<photo share path>'           # the template's Photo share path, for example /mnt/user/photos
BACKUP=/mnt/user/backups/darkroom-s5a   # where the copy of appdata goes, outside appdata
mkdir -p "${BACKUP%/*}"                # cp -a does not create a missing parent folder
SANDBOX='--user=99:100 --cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=/boot/config/darkroom/seccomp.json'
```

`$SANDBOX` is left unquoted where it is used, so it splits into the template's flags.

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

Taking the backup is the same each time:

```sh
docker stop "$C"
rm -rf "$BACKUP" && cp -a "$APPDATA" "$BACKUP"
stat -c '%u:%g %a %n' "$BACKUP" "$BACKUP/secret-key" "$BACKUP/darkroom.db"
(cd "$APPDATA" && find . -type f | wc -l); (cd "$BACKUP" && find . -type f | wc -l)
docker exec <postgres container> pg_dump -U <postgres user> -Fc darkroom > "$BACKUP.dump"   # PostgreSQL only, still stopped
docker start "$C"
```

Expected: `99:100` on all three (`600` on `secret-key`, which is absent when the key is external, item 4), and the two counts equal. The `pg_dump` line names your PostgreSQL container, user and database.

### Item 1: appdata restored alone onto a fresh one container

Set-up, click by click, on a fresh container (no Pixel paired):

1. Sign in to Darkroom.
2. **People**, **Add person**: a made-up name; fill **Immich user ID** with a test Immich user's ID and **Immich API key** with a key of that user. The Immich reconcile also needs `IMMICH_URL` (and `IMMICH_ADMIN_KEY`) in the template, and the person's **Immich user ID** and **Immich API key** (a key of that same Immich user) on their **Edit** form on **People**; without them it prints `reconcile-immich: <person>: this person has no Immich user and key`.
3. **People**, **Google accounts**, **Add account**: the test account.
4. **Exports**, the test account's row, **Connect**; then **Open remote browser** and sign in by hand.
5. Import a made-up Takeout for the person, so the reconcile after the restore can run: `made_up_takeout '<person>' '<host folder of made-up photos>'` (Before you start), then `docker exec "$C" python run.py reconcile-immich` once, which must print `<person>: N listed, ...` already before the backup.
6. Note what the deployment holds, then take the backup (above).

Remove and restore:

```sh
docker stop "$C"
docker rm "$C"                         # or Docker tab, the icon, Remove (leave the image)
rm -rf "$APPDATA"                      # only once the counts above matched
cp -a "$BACKUP" "$APPDATA"
stat -c '%u:%g %n' "$APPDATA" "$APPDATA/secret-key"
```

On PostgreSQL also restore the dump into an empty database (`dropdb` and `createdb -O <darkroom role>` in your PostgreSQL container, then `docker exec -i <postgres container> pg_restore -U <postgres user> -d darkroom < "$BACKUP.dump"`). Then Docker tab, **Add Container**, **Template**: `darkroom` under the user templates (it keeps the variables you set), check them, **Apply**. Then:

```sh
sleep 60
docker logs "$C" 2>&1 | grep -E '^config: |"event": "(init started|secret key generated|serving|child exited; restarting it)"' | tail -n 10
docker exec "$C" python run.py check-config 2>&1 | grep -E '^(Secret key|Syncthing):|^config:'
docker exec "$C" python run.py reconcile-immich
ls "$APPDATA/browser"
```

Pass: `{"event": "serving", "port": 8000}`, no `secret key generated` (a new key would mean the file was not restored) and no `config:` line; `check-config` prints `Secret key: from the secret-key file in DARKROOM_DATA_DIR.`; `reconcile-immich` prints a line per person (`<person>: N listed, N stored ...`) and no `reconcile-immich: <person>: <error>`; `ls` lists the test account's numeric folder (its account id). Fail: `serve` refuses (`config: there is no secret key ...` or `config: ... is stored sealed but cannot be read with this secret key ...`, with `child exited; restarting it` repeating).

Then in the browser:

1. Reload Darkroom in the window that was signed in before: it stays signed in (the existing session). Open a private window and sign in again (a new session).
2. **Exports**, **Check exports now**, then **Browser**: record whether the test account shows **Session ok** or Google asks for a sign-in (**Sign-in needed**).
3. Last, delete the account: **People**, **Google accounts**, the test account's **Edit**, **Delete account**, and confirm. Then `ls "$APPDATA/browser"`: its numeric folder is gone. Restore the backup again before the next item.

```text
Result:
Version, date, who ran it:
```

### Item 2: the profile copy interrupted part-way, then repeated

Start with profiles in the share's `_browser/` and no `browser/` in appdata (the command refuses a target that exists unless `--set-aside` is given). Move appdata's aside if there is one, outside appdata:

```sh
docker stop "$C"
test -e "$APPDATA/browser" && mv "$APPDATA/browser" /mnt/user/appdata/darkroom-browser-aside
(cd "$SHARE/_browser" && find . -type f -exec sha1sum {} + | sort -k2 | sha1sum)   # note this hash
docker run -d --name darkroom-copy $SANDBOX -v "$APPDATA:/data" -v "$SHARE:/photos" "$IMAGE" \
  python run.py copy-browser-state --source /photos/_browser --target /data/browser
sleep 1; docker kill darkroom-copy; docker logs darkroom-copy; docker rm darkroom-copy
ls -A "$APPDATA" | grep -F '.browser.copying-'; test -e "$APPDATA/browser" && echo "browser: present" || echo "browser: absent"
(cd "$SHARE/_browser" && find . -type f -exec sha1sum {} + | sort -k2 | sha1sum)   # the same hash
```

Pass: the log is empty or partial (no `Copied and verified`), one `.browser.copying-<random>` folder, `browser: absent`, and the share's hash unchanged. If the log already says `Copied and verified`, the copy beat the kill: `rm -rf "$APPDATA/browser"` (a copy; the source is untouched) and run it again with `sleep 0.2`.

Then repeat it, with the hidden folder still there:

```sh
docker run --rm $SANDBOX -v "$APPDATA:/data" -v "$SHARE:/photos" "$IMAGE" \
  python run.py copy-browser-state --source /photos/_browser --target /data/browser
rm -rf "$APPDATA"/.browser.copying-*
(cd "$SHARE/_browser" && find . -type f -exec sha1sum {} + | sort -k2 | sha1sum)
docker start "$C"
```

Pass: first `copy-browser-state: 1 unfinished earlier copy folder(s) next to the target, which you may remove by hand: /data/.browser.copying-...` (it reports the folder and does not refuse because of it), then `Copied and verified N files in N folders into /data/browser; left out N browser lock links. The source was not changed.`; the share's hash still the same. Then **Exports**, **Check exports now**, and **Browser**: the test account still **Session ok**. Fail: a copy refused, a different hash, or `browser/` present after the kill.

```text
Result:
Version, date, who ran it:
```

### Item 3: a rollback after the one container has added an account and written a capture

On v3.16.0, the last release with both layouts. Its own page names its images and the steps; read it rather than this one for that release:

```sh
wget -qO- https://raw.githubusercontent.com/AnthonyPluth/Darkroom/v3.16.0/docs/deploy/one-container.md | less   # "Going back to the two containers"
```

1. On v3.16.0's one container: **People**, **Add account**, a second test account, signed in through **Exports**, **Connect** and the remote browser.
2. Make one job fail so it writes a capture (for example **Connect** and close the sign-in without finishing it, or let it time out); its failure page shows the capture.
3. Stop the container and copy the profiles back with v3.16.0's one-container image (the one its page names, not `ghcr.io/anthonypluth/darkroom`):

   ```sh
   docker stop "$C"
   docker run --rm $SANDBOX -v "$APPDATA:/data" -v "$SHARE:/photos" -w /app <v3.16.0's one-container image> \
     python run.py copy-browser-state --source /data/browser --target /photos/_browser --set-aside
   ls -d "$SHARE"/_browser.before-*
   ```

4. Start that release's two containers as its page says, with autostart off on the one container.

Pass: `Copied and verified ... The source was not changed.` and `The folder that was at /photos/_browser is now /photos/_browser.before-<time>.` (`<time>` as `YYYYMMDDTHHMMSSZ`, v3.16.0 prints the same lines); nothing deleted; the two containers show the second account signed in and the capture on its failure page. If v3.16.0's container is started on a database a later release migrated, record its refusal (expected: Alembic cannot locate the newer revision, and it does not start). From the current release, the way back is restoring the backup taken before upgrading past v3.16.0, which discards everything since.

```text
Result:
Version, date, who ran it:
```

### Item 4: the key supplied from outside appdata

Generate a test key (never one in use), put it in the template's **DARKROOM_SECRET_KEY** (shown in the Edit page's Advanced View) on a fresh container, and set up as item 1:

```sh
openssl rand -base64 32
ls -l "$APPDATA/secret-key" 2>/dev/null || echo "no secret-key file"
docker exec "$C" python run.py check-config 2>&1 | grep -E '^Secret key:'
```

Expected: `no secret-key file` and `Secret key: from DARKROOM_SECRET_KEY.`. Keep the key with the backup. Take the backup and restore it as in item 1, then start the restored container once with the variable empty:

```sh
sleep 30
docker logs "$C" 2>&1 | grep -E '^config: |"event": "child exited; restarting it"' | tail -n 3
docker exec "$C" python run.py check-config 2>&1 | grep -E '^config:|^Secret key:'
curl -fsS http://127.0.0.1:8000/healthz || echo "healthz: no answer"
```

Pass without the key: `config: there is no secret key, but sealed values are already stored (an Immich key, a sign-in session, a stored setting or RESEND_API_KEY), and Darkroom will not make a new key ...`, the init restarting the app (`child exited; restarting it`, `"code": 1`), `check-config` saying the same, and `healthz: no answer`; no `secret-key` file appears. Then put the same key back in the variable, Apply, and record item 1's checks (with `Secret key: from DARKROOM_SECRET_KEY.`). Fail: Darkroom starts without the key, or writes a new `secret-key` file.

```text
Result:
Version, date, who ran it:
```

### Item 5: each of the above on SQLite and on PostgreSQL, with the generated and the external key

Fill in a row per combination run; a combination not run stays "not run" and is not supported.

| Database | Key | Item 1 | Item 2 | Item 3 | Item 4 | Version, date, who ran it |
| --- | --- | --- | --- | --- | --- | --- |
| SQLite | generated | not run | not run | not run | n/a | |
| SQLite | external | n/a | n/a | n/a | not run | |
| PostgreSQL | generated | not run | not run | not run | n/a | |
| PostgreSQL | external | n/a | n/a | n/a | not run | |

## What this changes in the design

Nothing yet. Until the table above is filled in:

- Section 9's "Backup and restore" and "Upgrade and rollback" remain the documented procedure, not a rehearsed one, for every combination of database and key.
- The "Before the first deploy" list in [docs/deploy/one-container.md](../deploy/one-container.md#before-the-first-deploy) keeps S5a. A5's check ("S5a rehearsed") was waived by the owner for #340 and #351 because nothing is deployed; it is still not met.

## Not verified, and what would settle each

| Item | Why not | What would settle it |
| --- | --- | --- |
| Every item in the table above | No result recorded on #312 or anywhere else; it needs the owner's deployment, a Google test account and a PostgreSQL server | The owner's run of the table, recorded on this page |
| A signed-in Google profile surviving the move into appdata (S1's open item) | No Google account in an agent's session | Item 2, with the test account signed in before the copy |
| Unraid's own appdata backup plugin as the backup | Not in #312's scope | Item 1 with the plugin's archive as `<backup>`, if the owner uses it |
