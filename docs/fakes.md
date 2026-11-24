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

| `POST /api/assets` | multipart upload for a person key with `asset.upload` in `state.permissions`: `201 {"id", "status": "created"}`, or `200` `duplicate` with the existing ID for the same owner and checksum; `400` past `state.quotas[user_id]`; a `livePhotoVideoId` links the still and hides the video |
| `POST /api/assets/bulk-upload-check` | `accept`, or `reject`/`duplicate` with `assetId` and `isTrashed`, for the key's own assets by checksum |

An uploaded asset gets `exifInfo` (date, `timeZone`, GPS, description: EXIF read with Pillow, then the XMP sidecar's values over it) and a `duration` once `state.extraction_delay` seconds have passed; a duplicate upload keeps the existing asset's values. `POST /api/search/metadata` also filters by `checksum`. `state.admins` makes a person's `/api/users/me` say `isAdmin`. For the managed upload (#531): `state.lose_uploads` makes that many uploads create the asset and then drop the connection without an answer, `state.upload_answer` replaces every upload's answer with a `200` of that body (nothing is stored), `FakeAsset.offline` sets `isOffline`, and `state.search_shared` puts the assets in `state.shared` (which another owner's key can already read) into a metadata search as well. For the metadata completion (#532): `FakeAsset.sidecar` keeps the XMP an upload sent, the EXIF `ImageDescription` is read as the description, and the sidecar's description is unescaped as XML. These answers are what source inspection of Immich v3.3.6 suggests, not live results: `tools/spikes/immich_live.py` is tested against them (`tests/test_spike_immich_live.py`) and #266's live run is what checks them.

`PUT /api/assets/<id>` (for the enrichment harness, #574 slice 1) overwrites `description`, `latitude`/`longitude` and `dateTimeOriginal` unconditionally, as the pinned Immich does, refuses a null date or place and one coordinate without the other with 400, answers 403 to a key without `asset.update` or `all` and 400 to an asset the key's user does not own, and moves `updatedAt`. `state.key_permissions` gives a key its own grants (default: `state.permissions`). `GET /api/albums` answers an empty list and `GET /api/assets/<id>/thumbnail` the asset's bytes. The fuller fake (lost-answer and ignored-write modes) is that design's slice 2.

A missing or wrong key gets `401`. Modes, set with `fake.mode(name)` or `POST /__fake/mode {"mode": ...}`: `ok`, `down` (drops the connection), `slow` (delays each answer), `401`, `500`.

### What it does not cover

Thumbnails, real metadata extraction, trash and restore, partner sharing beyond `state.shared` and `state.search_shared`, scan progress, jobs, or anything Waypoint's photos epic needs. Scans finish at once and change nothing.

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

Domain verification, API key permissions, rate limits, the 24 hour expiry of an idempotency key, a request still in progress under the same key (Resend's other `409`), validation of the addresses or the body, and delivery to an inbox. Any path answers like `/emails`.

## Fake Syncthing (`tests/fakes/syncthing.py`)

A loopback stand-in for the parts of Syncthing's REST API that `providers/syncthing.py` calls. It binds only to `127.0.0.1`. Its behaviour follows what spike S3 observed on Syncthing v2.1.7 (`docs/spikes/syncthing-api.md`); it was not run against a real Syncthing.

Run in a test:

```python
fake = FakeSyncthing(self, "api-key", own_device_id)
client = syncthing.Client(fake.url, "api-key")
fake.folders, fake.devices, fake.ignores, fake.scans, fake.requests
```

### What it covers

| Request | Answer |
| --- | --- |
| `GET /rest/system/status`, `GET /rest/system/version` | the own device ID (`myID`); `v2.1.7` with `isRelease` |
| `GET /rest/config` | folders, devices, options and `gui`, with `gui.apiKey` as Syncthing sends it |
| `GET`/`POST /rest/config/folders`, `GET`/`PATCH`/`DELETE /rest/config/folders/<id>` | the folder table; an unknown `type` is stored as `sendreceive` with a 200, and the own device is added to every folder's devices |
| `POST /rest/config/devices`, `PATCH`/`DELETE /rest/config/devices/<id>` | the device table; a device ID with a wrong check character gets `400` |
| `PATCH /rest/config/options`, `GET`/`PATCH /rest/config/gui` | the options; the GUI settings with `apiKey` (a new `apiKey` replaces the key at once) |
| `GET`/`POST /rest/db/ignores`, `POST /rest/db/scan`, `GET /rest/db/completion` | per folder (`404` for an unknown one); a scan of a folder in `fake.errors` gets `500`; completion from `fake.completions` |
| `GET /rest/system/connections`, `GET /rest/stats/device`, `GET /rest/cluster/pending/devices` | `fake.connections`, `fake.stats`, `fake.pending` |

A missing or wrong key gets `403` with `CSRF Error` (`X-API-Key` or `Authorization: Bearer`, as S3 found). `fake.forgetful` answers every write with a 200 and keeps nothing, `fake.forced_type` stores every folder with that type, `fake.status` answers everything with that status and `fake.raw` replaces a 200's body. Anything else gets `404`.

### What it does not cover

`PUT /rest/config`, override, revert, shutdown, events, `/rest/db/status` and `/rest/db/need`, pending folders, the Unix socket, restarts, or any syncing: nothing is scanned, connected or sent.

### In the demo (`tools/demo_syncthing.py`)

`make verify`, `tools/verify.py serve` and `tools/control` run the demo server through `python -m tools.demo_syncthing`, which starts this fake in the server's own process on a Unix socket in a private folder (mode 0700) of the demo's data folder, `data/run/`, or in a short private temporary folder when that path is too long for a Unix socket; it points Darkroom at it, and at that folder as the one private socket folder Darkroom's internal-socket check accepts in place of `/run/darkroom` (in the demo's own process only) and hands Darkroom its key on a pipe. So the demo shows the People page and the Pixel form as they are with Darkroom's Syncthing on. The fake's own device ID and the waiting phone's are made up (`OWN_ID`, `PHONE_ID`). The waiting list is empty at Darkroom's first read and holds one phone ("Pixel 9a") from the second on, so the pairing flow (`frontend/verify/flows/pixel-pairing.json`) shows nobody waiting, then one device; once no Pixel holds the phone's ID any more (the flow deletes the Pixel it paired), the next read is empty again, so the flow runs at both widths on one server.
