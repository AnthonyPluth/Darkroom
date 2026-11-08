# Running on PostgreSQL

Darkroom keeps its index and decisions in SQLite by default (`darkroom.db` in the data folder, nothing to set up). It also runs on PostgreSQL (tested on 16 and 17): set `DATABASE_URL` and start the container; the tables are created on start by the same migrations.

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

One difference in guarantees: SQLite lets one writer at a time in, so every write waits for a change in progress. On PostgreSQL only the changes that must not overlap wait for each other, through one database-wide advisory lock (deleting an account, editing or deleting a Pixel, starting an export job, reversing a dedupe decision, pruning a Pixel's folder, removing a duplicate's losing copy); they wait up to 30 seconds and then say Darkroom is busy, as on SQLite. Other writes, such as an import deciding matches, do not wait for that lock.

The test suite runs against PostgreSQL with `make test-pg` (`TEST_DATABASE_URL` names a server; each test makes and drops its own database), and CI does so on `postgres:17`.
