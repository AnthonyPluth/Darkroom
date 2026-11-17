# Spike S5a: backup and restore, app and browser

Spike S5a of the [single-container design](../design/single-container.md) (section 12, tracking issue #284, spike issue #312). It is meant to rehearse, on a fresh one container built from main and before Syncthing is involved, the backup, restore and rollback of section 9: appdata restored alone, the browser-profile copy interrupted and repeated, a rollback after the one container has done browser work, and the secret key supplied from outside appdata, each on SQLite and on PostgreSQL. It is the owner's rehearsal, with a low-value Google test account, and this page was written from the record alone (#387): nothing was run for it.

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

Since #312 was written, D1 (#327, #351) removed the two-container layout. Item 3's "then the two-container layout serving that account and capture" now means pinning the newest `v3` release and following its "Going back to the two containers" ([one-container.md](../deploy/one-container.md#going-back-to-the-two-containers)), which also runs `copy-browser-state --set-aside` back to the share.

## Versions

To be recorded by the owner. Placeholders in angle brackets.

| Piece | Version |
| --- | --- |
| Host | `<Unraid or other>`, Docker `<version>` |
| Darkroom image | `ghcr.io/anthonypluth/darkroom-single:<release tag>` built from main at `<commit>`, its digest |
| For item 3 | the newest `v3` release's tag, and the image tags it pulls |
| PostgreSQL | `<version>` (CI runs `postgres:17`) |
| Browser | `BROWSER_CHANNEL` and the Chrome or Chromium version in the image |

## What the owner runs, and what to record

Use a low-value Google test account and made-up photos only. Write down no account, address or file name: say "the test account", "a made-up capture". Each run starts from a fresh one container with `DARKROOM_SYNCTHING=0` (Syncthing is S5b). `<appdata>` is the appdata folder on the host and `<backup>` a copy of it; in the container `python run.py` runs from `/app`.

Taking the backup, the same each time: stop the container, `cp -a <appdata> <backup>` (and, on PostgreSQL, `pg_dump` of the database taken while it is stopped, as [docs/deploy/postgres.md](../deploy/postgres.md) says), note what the deployment holds at that point (people, accounts, the test account's sign-in, one capture).

| # | Item (from #312) | Run | Record |
| --- | --- | --- | --- |
| 1 | Appdata restored alone onto a fresh one container | Set up sign-in, one person, the Immich key and the test account signed in through the Remote browser page; take the backup. Remove the container and appdata; create a new container from the template or compose with the same variables; put `<backup>` back as appdata (owner 99:100; on PostgreSQL restore the dump into an empty database); start it | Sign-in works with an existing session and with a new one; `check-config` reads the sealed values (no "cannot be read" line); the Immich reconcile runs with the restored key; the test account's next Takeout job or the Remote browser page shows it still signed in, or Google asks for a sign-in (record which); deleting that account removes its folder under `/data/browser/` |
| 2 | The profile copy interrupted part-way, then repeated | With the profiles on the share, as a `v3` two-container deployment leaves them, run `python run.py copy-browser-state --source /photos/_browser --target /data/browser` and kill it part-way (`docker kill` on the container running it, or Ctrl-C after the first folders) | What the kill left (expected: a hidden `.browser.copying-<random>` folder beside the target and no `/data/browser`); `_browser/` on the share unchanged (`find _browser -type f -exec sha1sum {} +` before and after); what the next run says about the hidden folder ([browser.md](../deploy/browser.md#copying-browser-profiles-copy-browser-state) says it is reported); the repeated command after removing that folder by hand: "Copied and verified ... The source was not changed"; the test account still signed in afterwards |
| 3 | A rollback after the one container has added an account and written a capture | On the one container, add a second test account (signed in) and make a job fail once so it writes a capture. Stop the container. Follow the newest `v3` release's "Going back to the two containers": `python run.py copy-browser-state --source /data/browser --target /photos/_browser --set-aside`, then the two containers of that release | The old `_browser/` set aside as `_browser.before-<time>` and nothing deleted; the two-container layout serves the second account (signed in) and the capture on its failure page |
| 4 | The key supplied from outside appdata | Repeat item 1 with `DARKROOM_SECRET_KEY` set in the template or `.env` (a key generated for the test, never one in use) and no `secret-key` file in appdata. Start the restored container once without the variable, then with it | Without it: `serve` refuses with `config: there is no secret key, but sealed values are already stored ...` and `check-config` says the same; with it: everything in item 1's record |
| 5 | Each of the above on SQLite and on PostgreSQL, with the generated and the external key | Items 1 to 3 with the generated key on SQLite, then on PostgreSQL (`DATABASE_URL`); item 4 on each | A row per combination in a results table here, and any combination not run named as not supported (section 9 says none is described as supported before its rehearsal) |

A defect found during the rehearsal becomes its own issue (#312's out of scope) and is linked from this page.

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
