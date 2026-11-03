# Darkroom

Self-hosted family photo coordinator. An iPhone app uploads originals to Unraid, a browser worker pulls Google Takeout exports, and Darkroom dedupes both into one Immich library per person. Photos are cleared off the iPhones only once they exist on Unraid and in Google.

See [docs/SPEC.md](docs/SPEC.md) for the full design. Work is tracked as GitHub issues, grouped by phase under epic issues.

## Running it

```text
make setup          # Python 3.14 + Poetry; exiftool must be installed too
make check          # lint, checks, tests with the coverage floor
python run.py check-config
python run.py serve # GET /healthz
python run.py demo --data demo   # made-up people, Pixels, iPhones and photos in ./demo, to look at without a Google account
```

`run.py demo` prints how to serve what it seeded: `DARKROOM_DEMO=1` on a loopback address, with the demo member's session cookie in `demo/data/demo-session.json`. A server with sign-in configured, or listening beyond loopback, refuses that session and refuses to start in demo mode. `tools/verify.py` runs the same seed and server under libfaketime on a frozen clock with an allowlisted environment and a dead outbound proxy, for the browser driver and `make verify` that a later issue adds. It only ever writes inside the empty folder `--data` names.

`AGENTS.md` is the guide for anyone (human or agent) changing the code. Configuration is in `.env.example`.
