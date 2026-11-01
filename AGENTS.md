# Agent guide

Instructions for AI coding agents working in Darkroom, a self-hosted service that brings a household's photos together on Unraid: each person's iPhone originals, the extras that exist only in Google Photos (from Takeout), and one Immich library per person. Python backend in `darkroom/`, the Takeout browser worker in `darkroom_browser/`, a Svelte web app in `frontend/` and an iPhone app in `ios/` (both arrive in later issues). The design is [docs/SPEC.md](docs/SPEC.md); decisions the owner has made are in [docs/DECISIONS.md](docs/DECISIONS.md).

## Layout

- `darkroom/`: the backend, in packages whose imports `make lint` checks (import-linter's contracts in `pyproject.toml`):
  - `server/`: the HTTP handler, the route table (`routes.py`, where every route declares its `auth`), the reply helpers, `api/` and the static file server. Nothing else imports it.
  - `domain/`: what Darkroom works out from the data (people and accounts, media fingerprints, dedupe, eligibility, the ingest pipeline).
  - `providers/`: the outside things (`exiftool.py`, the only module that runs a subprocess; Home Assistant; Immich). A provider doesn't import another; what they share has a module of its own.
  - `storage/`: the database and the folder tree. It imports nothing above it, and only it imports the database drivers.
  - At the top, what everything shares: `config.py` (every environment variable, all listed in `.env.example`), `tls.py` (the only module that opens outbound connections), `validate.py`, `dates.py`, `monitoring.py`, `oidc.py`.
  - `static/`: the built web app, served as is.
- `darkroom_browser/`: the browser worker's own package. It never imports `darkroom`, and `darkroom` never imports it; they talk over an internal API. `pages/` holds pure parsers over saved HTML, `jobs/` the jobs, and only `live.py` will import Playwright.
- `frontend/` (Svelte 5, Vite, Tailwind 4), `ios/` (XcodeGen, SwiftUI): added by later issues.
- `tests/`: backend tests, run with `unittest`.
- `tools/`: scripts for the repo itself: `no_comments.py`, `coverage_floor.py`, `fleet_checks.py`, `feature_map.py`, `api_contract.py`.
- `docs/`: `SPEC.md`, `DECISIONS.md`, the generated `feature-map.json` and `openapi.json`, and later the signed-off designs in `design/`.

A broken boundary fails `make lint`: move the code to where its import is allowed rather than adding an exception. A new provider module gets added to the `providers-alone` contract.

## Commands

Python goes through Poetry (Python 3.14). `make setup` installs the dependencies; the image also needs `exiftool` (`libimage-exiftool-perl`).

- `make check`: each tool once: ruff, mypy, import-linter, Darkroom's own Semgrep rules, the feature map's and API contract's checks, the fleet checks, the Python tests in parallel with the coverage floor, and the web app's checks once `frontend/package.json` exists.
- `make lint`: ruff, mypy, import-linter, the no-comments check and Darkroom's Semgrep rules (`.semgrep/darkroom.yml`, run with `semgrep`; install it with `pipx install semgrep==1.146.0` or `uv tool install semgrep==1.146.0` on a Python it supports).
- `make test`: `poetry run python -m unittest discover tests`. `make test-parallel` runs the same across 4 processes with coverage and its floor (`tools/coverage_floor.py`: 85% overall, 90% for each module in `darkroom/domain/`, `darkroom/providers/`, `darkroom/server/api/`, `darkroom_browser/pages/` and `darkroom_browser/jobs/`).
- `make fix`: apply ruff's safe fixes; review the diff afterward.
- `make fleet-checks`: `tools/fleet_checks.py`: one Alembic head, a test for each migration, the workflows' conventions (bash with `-eo pipefail`, SHA-pinned actions with a version comment, `persist-credentials: false`), and on a pull request the `Co-Authored-By` trailer on your commits and no test removed or skipped without a `Removes-Test:` or `Skips-Test:` trailer.
- `make feature-map` / `make feature-map-check`: regenerate and check `docs/feature-map.json`; every route has a test.
- `make api-contract` / `make api-contract-check`: regenerate and check `docs/openapi.json` from `darkroom/server/contract.py`.
- `python run.py serve` starts the service; `python run.py check-config` reports what is missing in the configuration.

## Starting work

- **One issue, one session.** Work starts from an issue made with the "Agent task" template (`.github/ISSUE_TEMPLATE/agent-task.md`): its scope, acceptance criteria, dependencies and the model to use (see [CLAUDE.md](CLAUDE.md)). Start only when everything under "Depends on" is merged.
- **Decisions belong in the issue.** If the issue doesn't settle something the owner would notice, ask instead of choosing; where it names a default from #10, use it.
- **Done means checked.** An issue says how its result is checked; the pull request shows that output, not a description of it.
- **A correction becomes a check.** When a mistake is pointed out, fix it and add what would have caught it (a Semgrep rule with examples in `.semgrep/`, a test, or a check in `tools/fleet_checks.py`).
- **Parallel work stays apart.** Agents working at once take separate areas. Two pull requests that change the same files, or both add a migration, land one at a time: the second merges `main` in and re-runs its checks.
- **No design ships without sign-off.** Any web page or iPhone screen is built only against a design the owner has signed off in `docs/design/`.

## Working in a container

- Run Python through `poetry run` or `make`, never the bare `python3`: Darkroom needs Python 3.14 and its locked dependencies.
- Some checks only run in CI: the Semgrep registry packs, Trivy, pip-audit, CodeQL, the image build and its `/healthz` smoke test, and zizmor's online checks (locally, `poetry run zizmor --offline .github`).
- A session whose working directory isn't a git repository can't make a worktree; start it in the repository.

## Start from the feature map

[`docs/feature-map.json`](docs/feature-map.json) lists every API route with its auth kind, its handler, the web app files that call it and the tests that mention its path. Start there to find where a feature lives. It is generated: after changing routes, `apiCall` callers or tests, run `make feature-map` and commit the result (`make check` and CI fail when it is stale).

## Conventions

- No comments or docstrings in code (Python, TypeScript, JavaScript, Svelte, CSS): name things so the code says what it does, and put the why in the commit message, the pull request or the docs. `tools/no_comments.py` fails on one; a tool's directive (`# noqa`, `# type:`) is not a comment.
- One paved path; lint fails on the old way (`.semgrep/darkroom.yml`). Outbound HTTP through `darkroom/tls.py`; subprocesses only in `darkroom/providers/exiftool.py` (and the worker's runner); request bodies through `darkroom/validate.py`; time through `darkroom/dates.py` (never a naive `utcnow()`, never a bare `date.today()` in the domain, providers or jobs). Fix a finding with the helper, or justify an inline `nosemgrep` with a reason; don't switch the rule off.
- Every route declares an auth kind in `darkroom/server/routes.py`, is typed in `darkroom/server/contract.py` and has a test.
- Schema changes need an Alembic migration in `darkroom/storage/migrations/`, numbered after main's newest, with its own `test_<revision>_…` in `tests/test_migrations.py` (checked).
- Add or update tests with the change. Don't skip, disable or delete a test to get CI passing; when one really goes, a `Removes-Test:` or `Skips-Test:` trailer says why (checked).
- Never commit secrets or anyone's photos, file names, locations or Google data. `.env.example` lists configuration; real values stay in `.env`. Fixtures are generated and made up.
- Read `SECURITY.md` before touching sign-in, tokens, encryption, the worker, the MCP server or eligibility.

## Darkroom's promises

These are what the service is for; a change that breaks one is a blocking bug, whatever else it does. Each is held by a check that is in place before the code it guards (the issue that adds it is named).

1. **An original is written once.** Nothing under a person's `originals/` is modified, moved or deleted by the service. Checked by Semgrep's `darkroom-originals-read-only` and a before/after listing test (#25).
2. **Dedupe deletes nothing on Unraid.** It decides and records. Cleanup removes only staging, archives and resolved review holds, 14 days after the fact. Checked by `tests/test_cleanup.py` and the dedupe invariants (#24, #26).
3. **A photo is eligible for deletion from an iPhone only when** every resource of it (photo, Live Photo video, adjusted version) is on Unraid with a verified SHA-1 and matched in a Takeout export by an unreversed automatic tier 1 or 2 decision, from a Google account that serves its owner, re-checked right before deleting. Checked by Semgrep's `darkroom-eligibility-single-path` and `tests/test_eligibility_invariants.py` (#37).
4. **The browser worker never types a Google password or 2FA code.** A person signs in. Checked by `tests/browser/test_no_credentials.py` (#28).
5. **Metadata gaps are filled with XMP sidecars, never by rewriting media.** Checked by Semgrep's `darkroom-xmp-only-sidecars` (#25).
6. **No design changes without the owner's sign-off.** No web UI or iPhone screen merges unless it matches the design the owner signed off in `docs/design/` (#16, #34); a UI pull request carries screenshots. Held by review.

## Mistakes that keep coming back

Review findings on agents' pull requests fall into a few kinds. Check your change for each before you push. When one can be checked, it moves out of this list into a check.

- **Failure paths** (human-judged). For every new error, rejection or early return, know what the user sees, and test it. Don't leave stale state that says things are fine: a run that half-fails must not clear the last good status or an alert that is still true.
- **Private data in logs** (partly checked: Semgrep sends log lines through `monitoring.log`, which scrubs credentials). Log counts and fixed texts, never a file name, a path beyond the person's folder, a location, an API key or a token. Test with canary values (`tests/privacy.py`'s `no_leaks`, from #14).
- **Personal data in the repo** (human-judged). Fixtures are generated and made up. Never put a real photo, file name, location, email or Google export in a test, a fixture, an issue or a pull request.
- **Access that outlives the person** (human-judged). A new kind of session, grant, token or device registration must end when its owner can no longer sign in.
- **Time zones** (partly checked: lint rejects a bare `date.today()` in the domain, providers and jobs, and `datetime.utcnow()`). A photo's time is a UTC instant plus the offset where it was taken; never compare a wall-clock time to an instant, and never invent an offset.
- **Undo and restore** (human-judged). Putting something back restores exactly what was there, without normalizing it on the way; test the round trip with awkward values.
- **Deleting is the one thing that can lose a photo** (checked for eligibility; human-judged elsewhere). Anything that touches a file on Unraid or a flag that feeds eligibility gets a negative test for each way it must not act.
- **GitHub Actions** (checked: `tools/fleet_checks.py` and zizmor). Bash with `-eo pipefail`, actions pinned by SHA with a version comment, `persist-credentials: false`, paginated `gh api` lists.
- **Layout** (human-judged). A layout change is checked at phone and desktop widths, not only the one it was written at.

## Before you push

1. `make check` passes. For a CI-only check you can't run, say so in the pull request.
2. The change has its tests, and a failing-before test for a fix.
3. Generated files are current (`make feature-map`, `make api-contract`).
4. Nothing in the diff is a secret, a real photo or a real name.
5. Your commits end with the `Co-Authored-By` trailer for the model that wrote them.

## Git and PRs

- Work on the branch you were given; don't push to `main`.
- Keep commits focused, with a clear message. An agent's commit ends with a `Co-Authored-By: Claude <Model> <version> <noreply@anthropic.com>` trailer naming the model that wrote it (checked on every pull request).
- Open a pull request for every change you're asked to make, once it's pushed, and summarize what changed and why in a few bullets, with how it was tested.
- A pull request's title decides the next version when it's merged: `feat: …` releases a minor version, `fix: …` or anything else a patch, and `feat!: …` (or a `BREAKING CHANGE:` line) a major one.
- A pull request is ready when its "Merge gate" check passes: every check green (`.github/scripts/merge-gate.sh`).
- Don't hard-wrap lines in pull request descriptions, comments, issues or commit message bodies: write each paragraph or list item as one line. Only code and repo files keep their own wrapping.

## Model routing

See [CLAUDE.md](CLAUDE.md): Opus by default, Fable for security-related issues, Sonnet only for mechanical work.
