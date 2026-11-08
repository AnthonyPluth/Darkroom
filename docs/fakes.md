# Test fakes

## Fake Immich (`tests/fakes/immich.py`)

A loopback stand-in for the parts of Immich Darkroom calls: the library rescan and the reads that check a key. It binds only to `127.0.0.1` and refuses any other address. Field names come from Immich's public API reference as understood when this was written; they have not been checked against the family server, because the Immich spike (#21) has not been run yet. Update this file and the fake when it is.

Run in a test:

```python
with FakeImmich([FakePerson("jo", "u-jo", "lib-jo", "key-jo")], admin_key="admin-key") as fake:
    ...
    fake.scans()
```

Run standalone, from a JSON state file (people, library ids and keys):

```
python -m tests.fakes.immich --port 2283 --state state.json
```

```json
{"admin_key": "adm", "people": [{"slug": "jo", "user_id": "u-jo", "library_id": "lib-jo", "key": "k-jo", "import_paths": ["/photos/jo/originals"]}]}
```

### What it covers

| Request | Answer |
| --- | --- |
| `GET /api/server/ping` | `{"res": "pong"}`, no key needed |
| `GET /api/server/about` | `{"version": ...}`, no key needed |
| `GET /api/users/me` | the key owner (`isAdmin` for the admin key) |
| `GET /api/libraries` | one library per person (`id`, `ownerId`, `importPaths`, `exclusionPatterns`); a person key sees only its own, the admin key sees all |
| `POST /api/libraries/{id}/scan` | `204` for the admin key, recorded in order; `403` for a person key; `404` for an unknown library |

A missing or wrong key gets `401`. Modes, set with `fake.mode(name)` or `POST /__fake/mode {"mode": ...}`: `ok`, `down` (drops the connection), `slow` (delays each answer), `401`, `500`.

### What it does not cover

Assets, search, thumbnails, uploads, sidecar handling, scan progress, jobs, or anything Waypoint's photos epic needs. Scans finish at once and change nothing.

## Fake Resend (`tests/fakes/resend.py`)

A loopback stand-in for Resend's send-email endpoint, for `providers/resend.py`. It binds only to `127.0.0.1`. Field names and the `Authorization: Bearer` header come from Resend's public API reference and have not been checked against a real account: delivery to a real Resend account and inbox is not verified.

Run in a test:

```python
fake = FakeResend(self)
resend.Resend("re_key", "alerts@mail.example.test", "owner@example.test", fake.url).send(note)
fake.requests[0].json()
```

### What it covers

| Request | Answer |
| --- | --- |
| `POST /emails` | `fake.status` (200 by default) with `{"id": ...}`, or a `{"name", "message"}` error body from 300 up; the request's path, headers and body are recorded in `fake.requests` |

An `Idempotency-Key` header is remembered in `fake.sent`: the same key with the same body is accepted again but counts once (`len(fake.sent)` is the number of distinct emails), and with a different body it answers `409`. `fake.delay` holds each answer back (a slow server) and `fake.location` adds a `Location` header (a redirect, which the provider must not follow).

### What it does not cover

Domain verification, API key permissions, rate limits, idempotency keys, validation of the addresses or the body, and delivery to an inbox. Any path answers like `/emails`.
