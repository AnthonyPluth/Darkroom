# Security

Darkroom handles a household's private photos and the keys to the services around them. Read this before touching sign-in, tokens, encryption, the browser worker or anything that decides whether a photo may be deleted from a phone.

## What must hold

- Originals are never modified, moved or deleted by the service (AGENTS.md, "Darkroom's promises", 1 and 2).
- A photo is offered for deletion from an iPhone only on the rules in promise 3, re-checked right before deleting.
- The browser worker never types a Google password or 2FA code (promise 4). Signing in is done by a person, in the embedded browser view.
- Secrets (API keys, tokens, `DARKROOM_SECRET_KEY`) are never committed, logged, returned in a reply or put in an error message. `.env.example` lists the configuration; real values stay in `.env`.
- A device (iPhone) token is stored only as its SHA-256 and shown once, in the sign-in redirect's fragment. It is bound to the Pocket ID account that signed the device in, and refused once the device is revoked, that account's access lapses or the person's email moves to another account, and is looked up only by `darkroom/domain/devices.py`'s `authenticate` (Semgrep's `darkroom-device-token-single-path`). The device API is in `docs/api/device.md`.
- Every route declares who may call it (`session`, `device`, `worker` or `public`), and an unlisted kind answers 401. A new route needs a test.
- Outbound requests go through `darkroom/tls.py`; subprocesses only through `darkroom/providers/exiftool.py` (and the worker's runner).

## Reporting a problem

Open a private security advisory on this repository, or tell the owner directly. Please don't open a public issue with details.
