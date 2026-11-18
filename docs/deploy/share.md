# Binding the working share

Darkroom places and prunes the Pixels' copies (`_pixels/`) on the working share mapped to `/photos`. Before every hourly `pixels.tend` cycle it checks that the share is the one it was bound to: a sentinel file, `.darkroom-share`, at the share root holds the content recorded in Darkroom's database, and the share root is the same mounted filesystem as when the service started. The root is resolved once, at start, and the cycle tends exactly the folder it checked, so retargeting a link in `DARKROOM_PHOTOS_ROOT` while the service runs changes nothing until a restart. The cycle checks again after the placed-copy check, just before pruning and placing. If either check fails, the cycle does nothing more: no copy is placed, pruned or re-queued and no file is touched. The `share_check_failed` event and the log line `pixel folders not tended: the share check failed` say why (`no share is bound yet`, `the sentinel is missing from the share`, `the sentinel on the share is not the one recorded`, `the sentinel on the share could not be read`, `the share root is not the filesystem it was at startup`). Without the check, a share that vanished while Darkroom ran would leave an empty folder on the container's own disk: pruning would record copies as removed that never were, and placing would write into the container.

The check never repairs itself. Only `python run.py bind-share` writes a sentinel.

## First setup and upgrades

An existing deployment has no sentinel, so from this version on placing and pruning stop until you bind the share once. Nothing else changes, and nothing is deleted while they are stopped. `python run.py check-config` warns `the working share is not bound (...)` until you do.

1. Start the container once with this version, so the database is migrated.
2. Check that no other Syncthing serves `_pixels/` on the server, such as Unraid's Syncthing app or one on the host ([Ports already in use](one-container.md#ports-already-in-use)). Darkroom's own Syncthing, always on in the one container, stays paused for every Pixel while the share has no sentinel.
3. Run the command in the container: `docker compose exec darkroom python run.py bind-share` (or the container's console on Unraid).
4. There is nothing to resume by hand: Darkroom resumes each Pixel's folder in its own Syncthing once the hourly tend has checked that Pixel's copies and none is still being written back.

## What the command does

`bind-share` refuses, and writes nothing, when:

- this build does not run the placed-copy check before tending the Pixel folders (a replacement share would never be refilled);
- the share root (`DARKROOM_PHOTOS_ROOT`) is not a mount separate from the container's own filesystem;
- the share root already holds a `.darkroom-share` (Darkroom never replaces one: check what that file is before you remove it by hand);
- the database has not been set up yet.
- another `bind-share` is running in the container (it holds `.bind-share.lock` in `DARKROOM_DATA_DIR` from its first check until the sentinel is written).

Otherwise it shows what it found (the root, what it is mounted from, how many entries are at the root and files under `_pixels/`, how many copies are recorded as placed) and asks two things, each answered by typing a word:

- `paused`: that no other Syncthing (Unraid's Syncthing app, or one on the host) serves `_pixels/` on the server. Darkroom keeps its own Syncthing paused for every Pixel while the share has no sentinel, and an empty replacement served by another Syncthing would otherwise reach each Pixel as deletions;
- `bind`: to write a new sentinel and record it.

Any other answer writes nothing. On `bind` it records a new random identity in the `stored_settings` table (as `share_sentinel`, which no environment variable sets), writes it to `.darkroom-share` at the share root (create-only, mode 0600), and records a `share_bound` event. If the file cannot be written the command says so and removes the file it had just created (never one that was put in its place); placing and pruning stay stopped and the command can be run again.

## A lost and replaced share

When the share really has been lost and replaced (restored from a backup, or a new disk), the check fails with `the sentinel is missing from the share`. Darkroom's Syncthing is already paused for every Pixel while the check fails. Map the replacement to `/photos`, start the container and run `bind-share`. The next cycle runs the placed-copy check first, which finds the placed copies that are missing and has them written again from `originals/` (see "Placed copies that are missing or changed" in [the runbook](../runbook.md)); Darkroom resumes each Pixel's folder by itself once that Pixel's copies are checked and none is still being written back.

## Rollback

The share check adds no migration. Go back to the release before it by redeploying that image only if no later migration came with this upgrade; otherwise restore the backup taken just before the upgrade, as "Stored settings" in [the runbook](../runbook.md) says. The older guard (a mount at or below `_pixels/` stops placing and pruning) then applies again. A sentinel left on the share is harmless.
