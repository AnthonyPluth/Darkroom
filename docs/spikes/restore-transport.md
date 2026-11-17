# Spike S5b: backup and restore, transport

Spike S5b of the [single-container design](../design/single-container.md) (section 12, tracking issue #284, spike issue #313). It is meant to rehearse the transport's side of section 9 with the one container, Darkroom's Syncthing and a Pixel: the pairing after appdata is restored, a lost share and a replacement bound by the operator, an unbound or unmounted share, a database older than the share, and the stale restore of section 7.5, on SQLite and on PostgreSQL. It is the owner's rehearsal, on made-up photos, and this page was written from the record alone (#387): nothing was run for it.

## Result in one paragraph

No result is recorded. #313 was closed as completed on 2026-11-16 with no comment, no linked pull request and no write-up, and nothing in the repository holds one. #379 (C8) was reviewed with S5b missing from the pre-deploy list, #386 (2026-11-17) added it to the design's C8 row and note as still unrecorded, and #389 added it to [docs/deploy/one-container.md](../deploy/one-container.md#before-the-first-deploy), which says the transport's restore is "the documented procedure, not a rehearsed one". Every item below is therefore NOT VERIFIED. The logic each item rests on is covered by tests against a fake Syncthing and, for Send Only, by image CI against a second real Syncthing (next section); none of that is a restore of a real deployment with a Pixel. S5b also depends on S4 (#311), which has no recorded result either ([unraid-pixel.md](unraid-pixel.md)). The second half of this page is what the owner has to run and record.

## What other work checked, which is not this spike

| Where | What it showed | What it leaves to S5b |
| --- | --- | --- |
| [S3](syncthing-api.md) sections 6 and 7 | The folder marker puts a folder on an unmounted or empty mount into an error state with no deletion announced; a lost index, and "override" on a Send Only folder making the peer match, deleting what the peer added | The same with Darkroom's reconciler and a restored appdata |
| `tests/test_share_check.py` | A missing or changed sentinel, a share never bound and a different filesystem at the root each stop the cycle before a row or file changes; a share taken away between two cycles marks nothing pruned or placed; `bind-share` refuses on the container's own filesystem, without the typed confirmation and with another sentinel present; a replacement is refilled after the bind | A real share unmounted, lost and replaced on the owner's server |
| `tests/test_pixel_check.py` | A missing or changed placed copy is written again and read back before its Pixel is released; a file matching a queued copy is adopted, one matching nothing holds the Pixel untouched; the control entries are never counted; nothing is deleted | The same after a real restore, with a Pixel connected |
| `tests/test_transport.py` (`HeldPixelTests`, `ReconcilerTests`) | A held Pixel's folder is not shared before the check has run, nor between re-queueing and a verified replacement; a failed share check adds nothing and pauses the whole transport | What the Pixel receives, and when |
| C4 image CI (`tests/browser/syncthing_peer.sh`) | With a second pinned Syncthing as the Pixel: after `bind-share` and a restart the tend checks the placed copies, places them, and the reconciler resumes the folder; the share is unchanged while the peer edits, deletes and adds | A restore, a lost share and an original Pixel |

One thing the design describes is not built: section 7.5 says that after a lost or stale index Darkroom shows what a Pixel still holds and offers a manual "make the Pixel match". There is no override call in `darkroom/providers/syncthing.py` and no route for it, so item 5 records what Darkroom shows today, not that offer.

## Versions

To be recorded by the owner. Placeholders in angle brackets.

| Piece | Version |
| --- | --- |
| Host | `<Unraid or other>`, Docker `<version>` |
| Darkroom image | `ghcr.io/anthonypluth/darkroom-single:<release tag>`, its digest |
| Darkroom's Syncthing | the version `/rest/system/version` reports (v2.1.7 is pinned) |
| The Pixel | an original Pixel with `<Syncthing app and version>`, or a second Syncthing `<version>` standing in for one (say which, per item) |
| PostgreSQL | `<version>` |

## What the owner runs, and what to record

Made-up photos only. Write down no device ID, address or file name: say "the Pixel", "a made-up photo", "the replacement share". Set-up for every item: one person with a few made-up originals, one Pixel paired (S4's items 6 and 7), the share bound (`python run.py bind-share`), and the copies placed and on the Pixel (the People page shows it up to date). `<appdata>` and `<backup>` as in [restore-app-browser.md](restore-app-browser.md). Before and after each item, take a listing with checksums of the Pixel's folder on the Pixel (or the stand-in's folder) and of `_pixels/<pixel id>/` on the share, so "nothing deleted from the Pixel" is a comparison, not an impression.

| # | Item (from #313) | Run | Record |
| --- | --- | --- | --- |
| 1 | The Pixel's pairing survives a restore of appdata | Stop the container, take the backup (it includes `/data/syncthing`), remove the container and appdata, create a new container, put the backup back, start | Darkroom's device ID unchanged (the pairing step's QR or `/rest/system/status`); the Pixel connects with no new pairing; the People page shows it connected and up to date; the Pixel's listing unchanged |
| 2 | The share lost, a replacement bound | Stop the container. Map a replacement share at the same path: a different, empty mount (another disk or pool share, not the old one with folders moved), with `originals/` restored from its backup but without the old share's `.darkroom-share` sentinel and without `_pixels/`. Only a new root without the original sentinel rehearses this: on the bound share the share check (`ShareCheck.problem()` in `darkroom/domain/share_check.py`) looks only at the root's filesystem and the sentinel, so it passes there. Start; see the share check fail. Run `python run.py bind-share`, confirm | Before the bind: `share_check_failed` and the "Photo share check failed" alert, the transport paused, nothing sent to the Pixel. After it: `placed copies checked` with each copy `requeued` (`pixel_copy_missing`), the Pixel held, each copy written again and read back (`pixel_copy_replaced`), and only then the folder shared; the Pixel's listing unchanged at every step (no deletion reached it) |
| 2b | `_pixels/` missing on the bound share (a separate recovery check, not the lost share) | Stop the container. On the same, still-bound share move `_pixels/<pixel id>/` off the share (keep it), leaving the sentinel in place. Start | That the share check passes (no `share_check_failed`, no rebind asked for), which is what Darkroom does today; that the placed-copy check before the first tend requeues every copy of that Pixel (`pixel_copy_missing`) and holds it, the tend writes them again and reads them back, and only then the folder is shared (as `tests/test_pixel_check.py`'s missing-folder test expects); whether Syncthing marks the folder in error while its marker is gone (S3 section 6) and whether any deletion reaches the Pixel; the Pixel's listing before and after |
| 3 | An unbound or unmounted share is refused | (a) Start with the share mapping pointing at an empty folder on the host's own disk. (b) With the container running, unmount the share. (c) Run `bind-share` against the empty folder of (a) | (a) and (b): the share check's reason (`the share root is not the filesystem it was at startup`, or a missing sentinel) and no placing, pruning or deletion; the Pixel's listing unchanged. (c): `bind-share` refuses, with its reason |
| 4 | A database older than the share | Take the backup. Let the tend place more copies (add made-up originals). Stop; restore only the database from the backup (appdata's `darkroom.db`, or the PostgreSQL dump) and keep the share as it is; start. Then add one stray file to `_pixels/<pixel id>/<person>/` that matches nothing | Files that match a queued copy adopted (`pixel_copy_adopted`), not touched; the stray file held: `pixel_files_unaccounted` with a count and no name, the "Pixel folder held" alert, the folder unshared until the file is removed by hand, then shared again |
| 5 | The stale restore of section 7.5: an older index, and a lost key | (a) Take the backup. Let a Takeout match prune some of the placed copies, so their deletions reach the Pixel. Stop; restore the whole of appdata from the backup (database and `/data/syncthing` both older than the Pixel's state); start. (b) Separately, delete `/data/syncthing` only (a lost device key) and start | (a) What the Pixel holds afterwards (copies Darkroom pruned since the backup, still on the Pixel or not), whether any deletion is announced to it, and what the People and Status pages show. (b) A new device ID for Darkroom; the Pixel no longer connects; what Darkroom shows; pairing again from the People page; the Pixel's listing after the new pairing |
| 6 | SQLite and PostgreSQL | Items 1 to 5 on SQLite, then with `DATABASE_URL` set (for item 1 the dump and appdata from the same stopped point) | A row per item and database in a results table here, and any not run named as not supported |

For each item, also note whether it ran against the original Pixel or a second Syncthing standing in for one (#313 allows the stand-in "where the write-up says so"; items 1, 2 and 5 are the ones that need the real Pixel to say what it holds). A defect found becomes its own issue and is linked from this page.

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
