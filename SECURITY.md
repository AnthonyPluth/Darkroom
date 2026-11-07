# Security

Darkroom handles a household's private photos and the keys to the services around them. Read this before touching sign-in, tokens, encryption, the browser worker or anything that deletes a file on Unraid.

## What must hold

- Originals are never modified, moved or deleted by the service (AGENTS.md, "Darkroom's promises", 1 and 2).
- Darkroom decides nothing about deleting a photo from an iPhone: promise 3 is retired (#199) and Immich's own app clears backed-up photos. The deletion path, the device sign-in, the push to iPhones and the tables behind them were removed (#201).
- The browser worker never types a Google password or 2FA code (promise 4). Signing in is done by a person, in the embedded browser view.
- Secrets (API keys, tokens, `DARKROOM_SECRET_KEY`) are never committed, logged, returned in a reply or put in an error message. `.env.example` lists the configuration; real values stay in `.env`.
- Every route declares who may call it (`session`, `worker` or `public`), and an unlisted kind answers 401. A new route needs a test.
- Outbound requests go through `darkroom/tls.py`; subprocesses only through `darkroom/providers/exiftool.py` (and the worker's runner).

## Reporting a problem

Open a private security advisory on this repository, or tell the owner directly. Please don't open a public issue with details.
