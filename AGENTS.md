# Agent guide

Instructions for AI coding agents working in Darkroom, a self-hosted service that brings a household's photos together on Unraid: each person's iPhone originals, the extras that exist only in Google Photos (from Takeout), and one Immich library per person. Python backend in `darkroom/`, the Takeout browser worker in `darkroom_browser/`, a Svelte web app in `frontend/` and an iPhone app in `ios/` (arrives in a later issue). The design is [docs/SPEC.md](docs/SPEC.md); decisions the owner has made are in [docs/DECISIONS.md](docs/DECISIONS.md).

## Layout

- `darkroom/`: the backend, in packages whose imports `make lint` checks (import-linter's contracts in `pyproject.toml`):
  - `server/`: the HTTP handler, the route table (`routes.py`, where every route declares its `auth`), the reply helpers, `api/` and the static file server. Nothing else imports it.
  - `domain/`: what Darkroom works out from the data (people and accounts, media fingerprints, dedupe, XMP sidecars, eligibility, the ingest pipeline).
  - `providers/`: the outside things (`exiftool.py`, the only module that runs a subprocess; Immich). A provider doesn't import another; what they share has a module of its own.
  - `storage/`: the database and the folder tree. It imports nothing above it, and only it imports the database drivers.
  - At the top, what everything shares: `config.py` (every environment variable, all listed in `.env.example`), `tls.py` (the only module that opens outbound connections), `validate.py`, `dates.py`, `monitoring.py`, `oidc.py`.
  - `static/app/`: the built web app (`make frontend-build`, not committed), served at `/` by `server/static.py` with the CSP in `server/common.py`.
- `darkroom_browser/`: the browser worker's own package. It never imports `darkroom`, and `darkroom` never imports it; they talk over an internal API. `pages/` holds pure parsers over saved HTML, `jobs/` the jobs (`kind -> fn(ctx)`), `selectors.py` every page selector as a named constant, `capture.py` the failure capture, and only `live.py` imports Playwright (import-linter). Its deployment is described in `docs/deploy/browser.md`.
- `frontend/`: the web app (Svelte 5 with runes, Vite, Tailwind 4, bits-ui, TypeScript). `src/lib/api.ts` is the only `fetch`; calls go through `apiCall<"METHOD /path">` in `src/lib/contract.ts`, typed by the generated `src/lib/api-types.ts`; `src/lib/act.ts` has `errMsg` and `ignoreFailure`. The theme tokens in `src/app.css` are the dark set from the design signed off in #16 (`docs/design/web/00-tokens.dc.html`); the app is dark only, and a new colour or token goes back to #16 first.
- `ios/` (XcodeGen, SwiftUI): added by a later issue.
- `tests/`: backend tests, run with `unittest`.
- `tools/`: scripts for the repo itself: `fleet` (the shim that runs fleet's tools at the version in `.fleet/version`), `app_checks.py` (Darkroom's own checks, run by `fleet checks`), `feature_map.py`, `api_contract.py`, `scrub_snapshot.py`.
- `docs/`: `SPEC.md`, `DECISIONS.md`, the generated `feature-map.json` and `openapi.json`, and the signed-off designs in `design/` (`design/web/` for the web UI, with its README).

A broken boundary fails `make lint`: move the code to where its import is allowed rather than adding an exception. A new provider module gets added to the `providers-alone` contract.

## Commands

Python goes through Poetry (Python 3.14). `make setup` installs the dependencies; the image also needs `exiftool` (`libimage-exiftool-perl`).

- `make check`: each tool once: ruff, mypy, import-linter, Darkroom's own Semgrep rules, the feature map's and API contract's checks, the fleet checks, the Python tests in parallel with the coverage floor, and the web app's checks (`make frontend-check`).
- `make lint`: ruff, mypy, import-linter, the no-comments check and Darkroom's Semgrep rules (`.semgrep/darkroom.yml`, run with `semgrep`; install it with `pipx install semgrep==1.146.0` or `uv tool install semgrep==1.146.0` on a Python it supports).
- `make test`: `poetry run python -m unittest discover tests`. `make test-parallel` runs the same across 4 processes with coverage and its floor (`tools/fleet coverage-floor`, from `[tool.fleet.coverage]` in `pyproject.toml`: 85% overall, 90% for each module in `darkroom/domain/`, `darkroom/providers/`, `darkroom/server/api/`, `darkroom_browser/pages/` and `darkroom_browser/jobs/`).
- `make fix`: apply ruff's safe fixes; review the diff afterward.
- `make fleet-checks`: `tools/fleet checks --commits origin/main..HEAD` (configured by `[tool.fleet]` in `pyproject.toml`, plus Darkroom's own checks in `tools/app_checks.py`) then `tools/fleet drift`: one Alembic head, a test for each migration, the workflows' conventions (bash with `-eo pipefail`, SHA-pinned actions with a version comment, `persist-credentials: false`), the `Co-Authored-By` trailer on your commits and no test removed or skipped without a `Removes-Test:` or `Skips-Test:` trailer, and that the `AGENTS.md` blocks, the vendored `.claude/` files and `tools/fleet` equal fleet's at `.fleet/version`.
- `make fleet-sync version=vX.Y.Z`: moves fleet's content (the `AGENTS.md` blocks, `.claude/skills/fleet-*`, `.claude/agents/fleet-reviewer.md`, `.claude/hooks/fleet-session-start.sh`, `tools/fleet`, `.fleet/version`) to a fleet release; `fleet-sync.yml` normally does it after Dependabot bumps the workflow pins.
- `make feature-map` / `make feature-map-check`: regenerate and check `docs/feature-map.json`; every route has a test.
- `make api-contract` / `make api-contract-check`: regenerate and check `docs/openapi.json` and `frontend/src/lib/api-types.ts` from the routes and their handlers' annotations: a handler's reply is `-> Annotated[Response, contract.X]` and its body `request: Annotated[Request, contract.Y]`, with `X` and `Y` TypedDicts in `darkroom/server/contract.py`.
- `make frontend-check`: the web app's svelte-check, ESLint (with the paved-path rules, whose examples are `frontend/src/lint-rules.test.ts`), Vitest with the coverage floor in `frontend/vite.config.ts`, and the build. `make frontend-install`, `frontend-typecheck`, `frontend-lint`, `frontend-test` and `frontend-build` run one step each.
- `python run.py serve` starts the service; `python run.py check-config` reports what is missing in the configuration.
- `python run.py demo --data DIR` seeds made-up people, accounts, Pixels, iPhones and photos (`darkroom/domain/demo.py`) into an empty folder, with a session for the demo member that a server accepts only with `DARKROOM_DEMO=1` on a loopback address. `tools/verify.py seed|serve|status` runs that seed and server under libfaketime at `FROZEN_NOW`, with an allowlisted environment and a dead outbound proxy, for the browser driver and `make verify` that a later issue adds.

## Starting work

<!-- fleet:begin starting-work v2.2.0 -->
- **One issue, one session, one pull request.** Work starts from an issue written as an agent task: its scope, acceptance criteria, dependencies and the model to use (see [CLAUDE.md](CLAUDE.md)). Start a fresh session for each one, on that model, rather than running many pieces of work through one long session.
- **Decisions belong in the issue.** If the issue doesn't settle something a user would notice, ask instead of choosing. Otherwise keep existing behaviour exactly; a change that isn't asked for is a regression.
- **Done means checked.** An issue says how its result is checked (`make check`, a test that fails before the change, `make verify` for anything on screen where the repository has it); the pull request shows that output, not a description of it.
- **Verify in proportion.** Run the tests your change touches while working, and `make check` once before pushing.
- **A correction becomes a check.** When a mistake is pointed out, fix it and add what would have caught it: a lint rule, a type, a test, or a check `fleet checks` runs ("Mistakes that keep coming back" says how). A sentence in this file is the last resort, for what no check can express.
- **Parallel work stays apart.** Agents working at once take separate areas. Two pull requests that change the same files, or both add a migration, land one at a time: the second merges `main` in and re-runs its checks.
<!-- fleet:end starting-work -->
- **Issues come from the "Agent task" template** (`.github/ISSUE_TEMPLATE/agent-task.md`). Start only when everything under "Depends on" is merged.
- **No design ships without sign-off.** Any web page or iPhone screen is built only against a design the owner has signed off in `docs/design/`.

## Working in a container

<!-- fleet:begin working-in-a-container v2.2.0 -->
- **Start the session in the repository.** The session-start hook (in `.claude/hooks/`) sets up the toolchain, Poetry's environment and the web app's packages, but only for a session whose project directory is this repository. A session started one level up has none of it: the system `python3` lacks the dependencies, and the tests fail on their imports.
- **Run Python through `poetry run` or `make`** (`make test`, or `poetry run python -m unittest tests.test_x`), never the bare `python3`. `make setup` installs the dependencies by hand.
- **Subagents make their own worktree** under `.claude/worktrees/` (ignored by `.gitignore`), from the session's checkout, and push from there. A session whose working directory isn't a git repository can't create one for them.
- **Skills have two homes.** fleet's skills are under `.claude/skills/fleet-*` and are changed in fleet; the app's own are under `.claude/skills/`; skills go nowhere else (`fleet checks` checks it: `skill-homes`).
<!-- fleet:end working-in-a-container -->
- **Some checks only run in CI**: the Semgrep registry packs, npm audit, Trivy, pip-audit, the image build and its `/healthz` smoke test, and zizmor's online checks (locally, `poetry run zizmor --offline .github`).
- **The hook is configured, not edited.** The session-start hook is fleet's (`.claude/hooks/fleet-session-start.sh`, vendored); what it installs here is in `.claude/hooks/fleet.conf` (Python 3.14, the web app's packages, exiftool and libfaketime).

## Start from the feature map

[`docs/feature-map.json`](docs/feature-map.json) lists every API route with its auth kind, its handler, the web app files that call it and the tests that mention its path. Start there to find where a feature lives. It is generated: after changing routes, `apiCall` callers or tests, run `make feature-map` and commit the result (`make check` and CI fail when it is stale).

## Conventions

- No comments or docstrings in code (Python, TypeScript, JavaScript, Svelte, CSS): name things so the code says what it does, and put the why in the commit message, the pull request or the docs. `tools/fleet no-comments` (over the paths `[tool.fleet.no-comments]` lists) fails on one, and so does ESLint's `darkroom/no-comments` in `frontend/`; a tool's directive (`# noqa`, `# type:`) is not a comment.
- One paved path; lint fails on the old way (`.semgrep/darkroom.yml`). Outbound HTTP through `darkroom/tls.py` (in the worker, through `darkroom_browser/client.py`, the only module that calls darkroom); subprocesses only in `darkroom/providers/exiftool.py` (and the worker's runner); request bodies through `darkroom/validate.py`; time through `darkroom/dates.py` (never a naive `utcnow()`, never a bare `date.today()` in the domain, providers or jobs). Fix a finding with the helper, or justify an inline `nosemgrep` with a reason; don't switch the rule off.
- Every route declares an auth kind in `darkroom/server/routes.py`, is typed in `darkroom/server/contract.py` and has a test.
- Schema changes need an Alembic migration in `darkroom/storage/migrations/`, numbered after main's newest, with its own `test_<revision>_…` in `tests/test_migrations.py` (checked).
- Add or update tests with the change. Don't skip, disable or delete a test to get CI passing; when one really goes, a `Removes-Test:` or `Skips-Test:` trailer says why (checked).
- Never commit secrets or anyone's photos, file names, locations or Google data. `.env.example` lists configuration; real values stay in `.env`. Fixtures are generated and made up.
- Read `SECURITY.md` before touching sign-in, tokens, encryption, the worker or eligibility.

## Darkroom's promises

These are what the service is for; a change that breaks one is a blocking bug, whatever else it does. Each is held by a check that is in place before the code it guards (the issue that adds it is named).

1. **An original is written once.** Nothing under a person's `originals/` is modified, moved or deleted by the service. Checked by Semgrep's `darkroom-originals-read-only` (examples in `.semgrep/examples/darkroom/domain/originals.py`) and the whole-tree SHA-1 before/after test, `tests/test_place.py`'s `FixtureExportRun` (#25).
2. **Dedupe deletes nothing on Unraid.** It decides and records. Cleanup removes only staging, archives and resolved review holds, 14 days after the fact. Checked by `tests/test_cleanup.py` and the dedupe invariants (#24, #26).
3. **A photo is eligible for deletion from an iPhone only when** every resource of it (photo, Live Photo video, adjusted version) is on Unraid with a verified SHA-1 and matched in a Takeout export by an unreversed automatic tier 1 or 2 decision, from a Google account that serves its owner, re-checked right before deleting. Checked by Semgrep's `darkroom-eligibility-single-path` and `tests/test_eligibility_invariants.py` (#37).
4. **The browser worker never types a Google password or 2FA code.** A person signs in. Checked by `tests/browser/test_no_credentials.py` (#28).
5. **Metadata gaps are filled with XMP sidecars, never by rewriting media.** Checked by Semgrep's `darkroom-xmp-only-sidecars` (examples in `.semgrep/examples/darkroom/providers/exiftool.py`) and `tests/test_xmp.py`, which holds `providers/exiftool.write_xmp` to refusing any target not ending in `.xmp` (#25).
6. **No design changes without the owner's sign-off.** No web UI or iPhone screen merges unless it matches the design the owner signed off in `docs/design/` (#16, #34); a UI pull request carries screenshots. Held by review.

## Mistakes that keep coming back

<!-- fleet:begin mistakes v2.2.0 -->
**A mistake that repeats gets a check.** When review flags the same kind of mistake on a second pull request, the fix adds a check that would have caught it: a lint rule (ESLint, or Semgrep in `.semgrep/` with a failing and a passing example), a type (strict mypy, a `NewType` or `Literal`), or a test. The item below then names that check in its parenthesis (`checked: waypoint-trip-visibility`, `checked: tests/test_offline.py`). An item stays *human-judged* only when no check can express it, and says why. Every month the "Repeated review findings" issue lists the kinds that came back; a row there is closed by a check, not by a new sentence here.

Review findings on agents' pull requests fall into the same few kinds: check your change for each before you push. The reviews read agents' pull requests for the *human-judged* ones: the "Second review" on a risky pull request, the independent "Agent review" on every agent's pull request where the app turns it on, and the code-review bot the repository uses. A blocking finding fails the merge gate, and so does an open P1 or P2 review thread from that bot (the gate counts only the authors in its `blocking-thread-authors`).

- **Failure paths** (human-judged: what a person should see when something fails is a product decision, which no rule can see). For every new error, rejection or early return, know what the user sees, and test it. Don't leave stale state that says things are fine: a scan or sync that half-fails must not clear the last warning, a failed reload must not leave old data on screen looking current, and a job that couldn't start isn't a failed run.
- **Access that outlives the person** (human-judged: each new kind of credential is new code that no existing check knows; its own test holds it once written). A new kind of session, grant, token, key or connection must end when its owner can no longer sign in, like browser sessions do; test that it does.
- **Undo and restore** (human-judged: which values are awkward depends on the data; the round-trip test holds each case once written). Putting something back restores exactly what was there, without normalizing it on the way; test the round trip with awkward values.
- **GitHub Actions** (partly checked: `fleet checks` checks bash by default, SHA pins with a version comment, paginated `gh api` lists, step-only contexts in a workflow's or job's `env:` and every `runs-on:`; zizmor checks `persist-credentials`, template injection and pinning; concurrency groups and agents' allowed commands are human-judged, because the right group and the commands an agent needs depend on what the workflow does). Workflows run bash with `-eo pipefail` (each file's `defaults`); keep it. On a pull request's `closed` event `github.ref` is main's, so build concurrency groups from `github.event.pull_request.number`. Check out with `persist-credentials: false`, pin actions by SHA with the version in a comment, pass event values to scripts as environment variables, paginate `gh api` lists, and allow an agent only the exact commands it needs. Run zizmor on a workflow you change.
- **Layout** (human-judged: whether a page reads well at a width is a judgement, which no rule can make; the screenshots at both widths show it). A layout change is checked at phone and desktop widths, not only the one it was written at.
- **Verification that doesn't cover the change** (partly checked: `fleet checks verification`; coverage is judged by the review). Where the app sets `[tool.fleet.verification]`, the pull request's Verification table names every behaviour the change touches as a dimension: `verified` with evidence that exists in the diff or the tree and exercises the change, or `not verified` with why and what would close the gap. A `verified` claim whose evidence doesn't exist, or a changed behaviour missing from the table, is blocking; a vague gap reason is advisory.
<!-- fleet:end mistakes -->

Darkroom's own, after fleet's:

- **Private data in logs** (partly checked: Semgrep sends log lines through `monitoring.log`, which scrubs credentials). Log counts and fixed texts, never a file name, a path beyond the person's folder, a location, an API key or a token. Test with canary values (`tests/privacy.py`'s `no_leaks`, from #14).
- **Personal data in the repo** (human-judged). Fixtures are generated and made up. Never put a real photo, file name, location, email or Google export in a test, a fixture, an issue or a pull request.
- **Time zones** (partly checked: lint rejects a bare `date.today()` in the domain, providers and jobs, and `datetime.utcnow()`). A photo's time is a UTC instant plus the offset where it was taken; never compare a wall-clock time to an instant, and never invent an offset.
- **Deleting is the one thing that can lose a photo** (checked for eligibility; human-judged elsewhere). Anything that touches a file on Unraid or a flag that feeds eligibility gets a negative test for each way it must not act.

## Before you push

1. `make check` passes. For a CI-only check you can't run, say so in the pull request.
2. The change has its tests, and a failing-before test for a fix.
3. Generated files are current (`make feature-map`, `make api-contract`).
4. Nothing in the diff is a secret, a real photo or a real name.
5. Your commits end with the `Co-Authored-By` trailer for the model that wrote them.

## Git and PRs

<!-- fleet:begin git-and-prs v2.2.0 -->
- Work on the branch you were given; never push to `main`.
- Keep commits focused, with a clear message. An agent's commit ends with a `Co-Authored-By: Claude <Model> <version> <noreply@anthropic.com>` trailer naming the model that wrote it (checked on every pull request).
- Before pushing to a pull request, run `make check`.
- Open a pull request for every change, once it's pushed, as a draft, and summarize what changed and why. Keep the description short and professional: a few bullets on the change and how it was tested, not an essay. Don't say who asked for it or reported it.
- Keep a pull request a draft until every check is green, every review has finished and every comment is addressed. Right after opening the draft, comment `@codex review` on it if the repository uses that reviewer (it doesn't review a draft by itself, but that comment asks it to), and again after a substantive push. Mark it ready for review only when every check is green, every review that runs on it has finished (the "Second review", the independent "Agent review" where the app turns it on, and any such reviewer), and every review comment has been fixed or answered and resolved. Then watch it until it merges.
- A pull request's title decides the next version when it's merged: `feat: …` releases a minor version, `fix: …` or anything else a patch, and `feat!: …` (or a `BREAKING CHANGE:` line) a major one, for a change that breaks an existing setup.
- Where the app sets `[tool.fleet.verification]`, a pull request's description carries a Verification block (the table between the `fleet-verification` markers: each dimension of the change `verified` with its evidence, or `not verified` with why) before it is marked ready, written with the `fleet-verify-change` skill; `fleet checks` reads it.
- A pull request is ready when its "Merge gate" check passes: every check green, including the "Second review" with no blocking finding (it reviews a risky pull request, labelled `safety-critical` or `security` or touching the paths the review lists, and passes any other as not risky), no open P1 or P2 review thread from the code-review bot the gate counts (its `blocking-thread-authors`), and, where the app turns it on, the independent "Agent review" with no blocking finding. Dependabot's updates merge themselves through it.
- Don't hard-wrap lines in pull request descriptions, comments, issues or the body of a commit message: write each paragraph or list item as one line, and let GitHub wrap it to the screen. Only code and repo files keep their own wrapping. Keep the commit subject to one short line.
<!-- fleet:end git-and-prs -->
- **Codex reviews every pull request; the Claude review is only for risky ones** (the owner's decision of Nov 3, recorded in #76). Comment `@codex review` on the draft as the block says. The merge gate (`.github/workflows/merge-gate.yml`, `require-codex-review: true`) stays pending until Codex has reviewed the head commit, so ask again after each substantive push. The Claude "Second review" runs on a pull request labelled `safety-critical` or `security`, or changing `.github/**`, `.claude/**` or a path `.github/workflows/agent-review.yml` lists; any other gets a passing "Not a risky pull request". Resolving a Codex thread starts no workflow, so afterwards run "Merge gate" by hand with the pull request's number, or push. Who reviews what, and how to turn the first review back on, is in `docs/runbook.md`.
- A pull request description always describes what the code does at its current head, and is updated with every push that changes behavior, so it never drifts.

## Model routing

See [CLAUDE.md](CLAUDE.md): Opus by default, Fable for security-related issues, Sonnet only for mechanical work.
