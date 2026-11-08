# Running on PostgreSQL

Darkroom keeps its index and decisions in SQLite by default (`darkroom.db` in the data folder, nothing to set up). It also runs on PostgreSQL: set `DATABASE_URL` and start the container; the tables are created on start by the same migrations.

| `DATABASE_URL` | Meaning |
| --- | --- |
| empty | SQLite, `sqlite:///<DARKROOM_DATA_DIR>/darkroom.db` |
| `sqlite:///path/to/file.db` | SQLite in a file of your choosing |
| `postgresql://user:password@host:5432/database` | PostgreSQL; `postgres://` and `postgresql+psycopg://` mean the same (Darkroom uses the `psycopg` driver, which the image already contains) |

Any other kind of database is refused at start with a message naming what Darkroom supports. A password with special characters must be percent-encoded in the URL.

What is not done for you:

- Nothing moves an existing SQLite database into PostgreSQL. A Darkroom started against an empty PostgreSQL database starts with no people, accounts, Pixels or decisions; the photo files are untouched. There is no migration tool.
- Database backups are yours to take (`pg_dump`); Darkroom does not back the database up on either engine.
- `python run.py demo` and `make verify` stay on SQLite whatever `DATABASE_URL` says.

Writes are serialised on PostgreSQL as they are on SQLite. SQLite lets one writer at a time in, so every write waits for the change in progress; on PostgreSQL Darkroom takes one database-wide advisory lock (`pg_advisory_xact_lock`) before the first write statement of every transaction and holds it until the transaction commits or rolls back (the hook is in `darkroom/storage/db.py`, on the engine, so no caller has to remember it). Reads never take it, and neither do the statements of a transaction that has only read. A write that cannot get the lock within 30 seconds fails as "busy", as on SQLite. This is what lets the deletions rely on "re-check under the write lock": while a duplicate removal, a Pixel prune, an account delete or a Pixel delete holds the lock between its last check and its commit, no import, job claim or heartbeat can commit a change that the check should have seen. Code that must also re-read what it decides on calls `db.write_lock` before reading, which is a no-op on SQLite and re-entrant within a transaction on PostgreSQL.

The cost is throughput: on PostgreSQL too, only one transaction at a time writes. A long import transaction holds the lock until it commits, so web edits, browser job claims and heartbeats, and other imports wait for it (up to 30 seconds, then "busy"). That is already how SQLite behaves, so nothing here is slower than the default, but PostgreSQL does not make Darkroom's writes concurrent; it gives you a server database, `pg_dump` backups and a database that is not a file on the same share as the photos. Anything that writes to Darkroom's database outside Darkroom is not serialised unless it takes the same advisory lock (`SELECT pg_advisory_xact_lock(1146243922)`).

What was run where: the whole suite passed against a local PostgreSQL 16.15 when PostgreSQL support was written; CI runs `make test-pg` (the whole suite and the migration tests) on `postgres:17` for every pull request, and that job is the proof for the serialisation described above. Other PostgreSQL versions are not tested by Darkroom.

The test suite runs against PostgreSQL with `make test-pg` (`TEST_DATABASE_URL` names a server; each test makes and drops its own database).
