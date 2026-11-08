# Security

Darkroom handles a household's private photos and the keys to the services around them. Read this before touching sign-in, tokens, encryption, the browser worker or anything that decides whether a photo may be deleted from a phone.

## What must hold

- Originals are never modified, moved or deleted by the service (AGENTS.md, "Darkroom's promises", 1 and 2).
- Darkroom decides nothing about deleting a photo from an iPhone: promise 3 is retired (#199) and Immich's own app clears backed-up photos. The deletion path that remained in the backend is being removed (#201); until it is, nothing turns a device's cleanup on (Semgrep's `darkroom-cleanup-never-on`).
- The browser worker never types a Google password or 2FA code (promise 4). Signing in is done by a person, in the embedded browser view.
- Secrets (API keys, tokens, `DARKROOM_SECRET_KEY`) are never committed, logged, returned in a reply or put in an error message. `.env.example` lists the configuration; real values stay in `.env`.
- No device (iPhone) token can be issued or used any more: device sign-in, the `device` auth kind and `devices.authenticate` were removed (#201). A token stored before that is only its SHA-256, and no module looks a device up by it (Semgrep's `darkroom-device-token-single-path`); the table goes with the migration.
- Every route declares who may call it (`session`, `worker` or `public`), and an unlisted kind answers 401. A new route needs a test.
- No push token can be registered or read any more: the push-token routes and `darkroom/domain/push_tokens.py` were removed (#201), and Semgrep's `darkroom-push-token-one-module` fails any read or write of the push-token columns. No route revokes a device any more either (the People page's revoke went with the device list, #201); a token stored before the removal stays sealed with `DARKROOM_SECRET_KEY` and as its SHA-256, unread, until the migration drops the columns with the table.
- Darkroom holds no APNs key: the APNs provider and its `APNS_*` settings were removed (#201). Alerts reach a phone only through ntfy, when it is set up (`docs/deploy/ntfy.md`).
- Outbound requests go through `darkroom/tls.py`; subprocesses only through `darkroom/providers/exiftool.py` (and the worker's runner).

## Reporting a problem

Open a private security advisory on this repository, or tell the owner directly. Please don't open a public issue with details.
