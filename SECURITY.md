# Security

Darkroom handles a household's private photos and the keys to the services around them. Read this before touching sign-in, tokens, encryption, the browser worker or anything that decides whether a photo may be deleted from a phone.

## What must hold

- Originals are never modified, moved or deleted by the service (AGENTS.md, "Darkroom's promises", 1 and 2).
- Darkroom decides nothing about deleting a photo from an iPhone: promise 3 is retired (#199) and Immich's own app clears backed-up photos. The deletion path that remained in the backend is being removed (#201); until it is, nothing turns a device's cleanup on (Semgrep's `darkroom-cleanup-never-on`).
- The browser worker never types a Google password or 2FA code (promise 4). Signing in is done by a person, in the embedded browser view.
- Secrets (API keys, tokens, `DARKROOM_SECRET_KEY`) are never committed, logged, returned in a reply or put in an error message. `.env.example` lists the configuration; real values stay in `.env`.
- A device (iPhone) token is stored only as its SHA-256 and shown once, in the sign-in redirect's fragment. It is bound to the Pocket ID account that signed the device in, and refused once the device is revoked, that account's access lapses or the person's email moves to another account, and is looked up only by `darkroom/domain/devices.py`'s `authenticate` (Semgrep's `darkroom-device-token-single-path`). The device API is in `docs/api/device.md`.
- Every route declares who may call it (`session`, `device`, `worker` or `public`), and an unlisted kind answers 401. A new route needs a test.
- A device's APNs push token is stored only sealed with `DARKROOM_SECRET_KEY` (`secretbox`) and as its SHA-256, is forgotten when the device is revoked, is read only while its device's owner can still sign in, can't be taken over by another person's device while that person can still sign in (even if its stored copy is unreadable), is sealed again when the same token is sent and the stored copy can't be read, and is sealed, unsealed and cleared only by `darkroom/domain/push_tokens.py` (Semgrep's `darkroom-push-token-one-module`).
- The APNs auth key (`APNS_KEY_FILE`) is read only by `darkroom/providers/apns.py` (Semgrep's `darkroom-apns-key-one-reader`), which refuses a file others can read; neither the key nor the provider token it signs is logged, stored or put in an error. A push goes only to the devices of the people its alert concerns, read through `push_tokens.targets` and checked again from one fresh read right before the tokens go to APNs (`push.cleared` and `push_tokens.still_held`: the alert is still open and queued, the device's person is still among the people it concerns, the device is not revoked, holds the same token and its owner can still sign in; a change to any of these before that check stops the push to that device; a revocation that lands while the request to Apple is already on its way can't be called back, and that push still arrives), and carries only the alert's kind, its id and the fixed title for that kind. A silent wake push (`darkroom/domain/wake.py`, for an iPhone that has not checked in for a day, at most one per device per 24 hours, the time recorded before sending) goes through the same `push_tokens.targets` and `push_tokens.still_held` checks and carries only `content-available`: no alert, sound, title, kind, id or name. Pushes go only to Apple's production host. Setup is in `docs/deploy/push.md`.
- Outbound requests go through `darkroom/tls.py`; subprocesses only through `darkroom/providers/exiftool.py` (and the worker's runner).

## Reporting a problem

Open a private security advisory on this repository, or tell the owner directly. Please don't open a public issue with details.
