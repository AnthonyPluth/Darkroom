# Darkroom

Self-hosted family photo coordinator. An iPhone app uploads originals to Unraid, a browser worker pulls Google Takeout exports, and Darkroom dedupes both into one Immich library per person. Photos are cleared off the iPhones only once they exist on Unraid and in Google.

See [docs/SPEC.md](docs/SPEC.md) for the full design. Work is tracked as GitHub issues, grouped by phase under epic issues.

## Running it

```text
make setup          # Python 3.14 + Poetry; exiftool must be installed too
make check          # lint, checks, tests with the coverage floor
python run.py check-config
python run.py serve # GET /healthz
```

`AGENTS.md` is the guide for anyone (human or agent) changing the code. Configuration is in `.env.example`.
