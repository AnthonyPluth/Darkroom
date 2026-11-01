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
