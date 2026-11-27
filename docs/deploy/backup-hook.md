# Telling Darkroom about your backups (recovery points)

Before Darkroom may ever delete an inbox file that an Immich-managed import left behind, it needs evidence that a backup of Immich and of Darkroom covers the photos those files stand for ([docs/design/recovery-point-gate.md](../design/recovery-point-gate.md), #545). Darkroom has no code for your backup tool and never reads Immich's storage. Your backup job tells Darkroom two things through two commands in the container: `recovery-point begin` before the backup starts, and `recovery-point finish` when it ends.

To set up managed inbox release, see the [restore rehearsal and acceptance commands](../runbook.md#recovery-points-for-managed-inbox-files). This guide covers how to report each backup.

## What the two commands do

- `python run.py recovery-point begin` lists every photo Darkroom has imported into Immich, asks Immich (reads only) which of them are still the person's own original with the same checksum, and stores that list as a recovery point in the state `open`. It prints `recovery point <id> opened: ...` and exits 0. When Immich cannot be read it stores the point as `failed` and exits 1, so the hook can stop instead of taking a backup that covers nothing.
- `python run.py recovery-point finish <id> --status succeeded|failed ...` closes the point with what the backup tool reported. A `succeeded` report must say when the snapshot was taken (`--taken-at`, a UTC time with its offset), how many days the tool keeps it (`--kept-for-days`, 1 to 3650), which of the four parts it holds (`--scopes`) and a label for the tool (`--tool`, lowercase letters, digits and hyphens). `--snapshot` is an optional identifier of the snapshot (letters, digits and `. _ : -`, never a path). Darkroom shows none of these back, and logs none of them.

The four scopes are `immich-db` (Immich's database), `immich-media` (Immich's photos and videos), `darkroom-db` (Darkroom's database; with PostgreSQL, a `pg_dump`) and `darkroom-secret` (Darkroom's `secret-key` file). A report that leaves any of them out is stored as `failed` and the command exits 1, because a backup without all four could not restore a working pair.

What `finish` refuses, and records nothing for: a point that is not open (a finished or abandoned point is never reopened), a point that has been open for more than 24 hours, a `--taken-at` before the point was opened or after now, a manifest that changed since `begin`, and any malformed value. The point then stays open until the next daily cleanup tick marks it `abandoned`.

## What the owner sees

- A point left `open` for more than 24 hours is marked `abandoned` by the daily cleanup tick; a backup that never reported counts as a failed one.
- When the latest finished point is `failed` or `abandoned`, one alert, "Immich backup failed", opens ("The last Immich backup Darkroom was told about did not succeed, so Darkroom keeps every Immich import's files in the inbox. Check the backup job."). It closes when a later point succeeds. An older good point does not cover for a newer failed one.
- Once you have recorded a restore rehearsal and accepted the remote-deletion boundary (until then the files stay for that reason alone and no alert is raised), when an Immich-managed import has been waiting 14 days plus 3 days for its files to be released and no succeeded point with all four scopes, taken within the last 14 days and still kept by the tool, exists that lists every photo of the waiting imports, one alert, "Immich backup out of date", opens. It closes when one does.

The alerts name no person, file or photo. They go through the alert channel you set up ([alerts-email.md](alerts-email.md)).

## Example hook

A generic script for any backup tool that can be run from a shell. `backup-tool run` stands for your tool's command; it must back up Immich's database and media and Darkroom's database and secret key, and exit non-zero when it fails. `darkroom` is the container's name. Adjust the label, the days the tool keeps a snapshot and the scopes it really backed up.

```sh
#!/bin/sh
set -u

darkroom() {
    docker exec darkroom python run.py "$@"
}

begun=$(darkroom recovery-point begin) || exit 1
point=$(printf '%s\n' "$begun" | sed -n 's/^recovery point \([0-9][0-9]*\) opened.*/\1/p')
[ -n "$point" ] || exit 1

sleep 1

taken=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if backup-tool run; then
    darkroom recovery-point finish "$point" --status succeeded --taken-at "$taken" --kept-for-days 30 \
        --scopes darkroom-db,darkroom-secret,immich-db,immich-media --tool example-tool || exit 1
else
    darkroom recovery-point finish "$point" --status failed --tool example-tool
    exit 1
fi
```

Notes on the example:

- `begin` runs first so every photo it lists is already in Darkroom's database, and so in the Darkroom backup that follows. Never run it after the backup.
- The time passed to `--taken-at` is read before the backup starts, never after it ends: a backup can only hold what existed when it began, and a later time would overstate how recent the coverage is and how long it is kept. The one-second pause keeps that time from falling before the point's own opening time, which `finish` refuses. If your tool reports its own snapshot time, pass that instead.
- `--kept-for-days` is how long your tool keeps this snapshot (its retention), not how often you back up.
- Run the hook from the same scheduler as the backup, so a backup that is skipped also skips `begin`.
