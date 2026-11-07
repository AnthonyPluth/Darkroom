# Security

Darkroom handles a household's private photos and the keys to the services around them. Read this before touching sign-in, tokens, encryption, the browser worker or anything that decides whether a photo may be deleted from a phone.

## What must hold

- Originals are never modified, moved or deleted by the service (AGENTS.md, "Darkroom's promises", 1 and 2).
- Darkroom decides nothing about deleting a photo from an iPhone: promise 3 is retired (#199) and Immich's own app clears backed-up photos. The deletion path that remained in the backend is being removed (#201); until it is, nothing turns a device's cleanup on (Semgrep's `darkroom-cleanup-never-on`).
- The browser worker never types a Google password or 2FA code (promise 4). Signing in is done by a person, in the embedded browser view.
- Secrets (API keys, tokens, `DARKROOM_SECRET_KEY`) are never committed, logged, returned in a reply or put in an error message. `.env.example` lists the configuration; real values stay in `.env`.
- A device (iPhone) token is stored only as its SHA-256 and shown once, in the sign-in redirect's fragment. It is bound to the Pocket ID account that signed the device in, and refused once the device is revoked, that account's access lapses or the person's email moves to another account, and is looked up only by `darkroom/domain/devices.py`'s `authenticate` (Semgrep's `darkroom-device-token-single-path`). The device API is in `docs/api/device.md`.
- Every route declares who may call it (`session`, `device`, `worker` or `public`), and an unlisted kind answers 401. A new route needs a test.
- No push token can be registered any more: the push-token routes and `darkroom/domain/push_tokens.py` were removed (#201), and nothing reads a stored token (Semgrep's `darkroom-push-token-one-module` fails any read or write of the push-token columns). A token stored before that is still kept only sealed with `DARKROOM_SECRET_KEY` (`secretbox`) and as its SHA-256, and revoking the device clears it (`devices.revoke`, the rule's one exception, which clears all three columns at once). The columns go with the migration that drops them.
- The APNs auth key (`APNS_KEY_FILE`) is read only by `darkroom/providers/apns.py` (Semgrep's `darkroom-apns-key-one-reader`), which refuses a file others can read; neither the key nor the provider token it signs is logged, stored or put in an error. Nothing sends a push any more: the alert push and the silent wake push were removed (#201), and the provider goes in the next pull request.
- Outbound requests go through `darkroom/tls.py`; subprocesses only through `darkroom/providers/exiftool.py` (and the worker's runner).

## Reporting a problem

Open a private security advisory on this repository, or tell the owner directly. Please don't open a public issue with details.
