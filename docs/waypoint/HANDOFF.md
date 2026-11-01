# Handoff: photos on any trip (Waypoint × Immich)

Draft only. Nothing has been created on GitHub. `issues.json` holds the epic and 17 issues in creation order; `PHOTOS_DESIGN.md` becomes `docs/PHOTOS.md` in Waypoint (the issues link to it there, so land it on main before or with `PHOTOS-SCHEMA`). Every issue body follows the "Agent task" template (`.github/ISSUE_TEMPLATE/agent-task.md`) with the owner's sections added; `{{KEY}}` becomes the created issue number.

## Waves

Each wave's issues can run in parallel, one agent per issue, each in its own session and branch. An issue starts only when everything in its **Depends on** is merged to `main`. Within a wave, the files listed under "Don't edit at once" decide the merge order: the second PR to touch one of them merges `main` first, regenerates and re-runs `make check`.

| Wave | Issues | Runs in parallel? | Ends with |
| --- | --- | --- | --- |
| 0 | `PHOTOS-SPIKE`, `PHOTOS-DECISIONS` | Yes (spike is an agent with a key; decisions are the owner) | **Owner gate A** |
| 1 | `PHOTOS-DESIGN` (owner + Claude), `PHOTOS-SCHEMA`, `PHOTOS-PROVIDER`, `PHOTOS-MATCH` | Yes: three backend issues touch disjoint files; the design runs alongside | **Owner gate B** (design sign-off) when the owner is ready; backend continues meanwhile |
| 2 | `PHOTOS-SOURCES`, `PHOTOS-API`, `PHOTOS-TRIPSTATS` | Yes, but all three add routes: merge one at a time (see below) | |
| 3 | `PHOTOS-SYNC`, `PHOTOS-PROXY` | Yes (SYNC owns `photosync.py` and `jobs.py`; PROXY owns the routes and `AGENTS.md`; both append to `photos.py`, see below) | |
| 4 | `PHOTOS-DEMO` | Alone (touches `demo.py` and `verify.mjs` warm-up) | Backend complete; `python run.py demo` shows photos |
| 5 | `PHOTOS-UI-SETTINGS`, `PHOTOS-UI-GALLERY` | Yes (Settings vs Trip page) | needs gate B |
| 6 | `PHOTOS-UI-MAP`, `PHOTOS-UI-STATS` | Yes only if they coordinate on `Trip.svelte`; safer one after the other | |
| 7 | `PHOTOS-UI-REVIEW` | Alone | Epic done; owner closes `PHOTOS-EPIC` |

Critical path: SPIKE → SCHEMA → SOURCES → PROXY → DEMO → UI-GALLERY → UI-MAP/UI-STATS → UI-REVIEW. `PHOTOS-API` and `PHOTOS-TRIPSTATS` can start as soon as SCHEMA and MATCH are in, so wave 2 keeps three agents busy while SOURCES (Fable) runs.

## Owner gates

- **Gate A, before any code**: `PHOTOS-DECISIONS` answered (every numbered item), and the spike's comment read. If the spike finds Immich behaves differently from the design (for example `takenAfter` compares against file time, or `updatedAt` doesn't move on a sidecar rescan), the owner approves the design edit before `PHOTOS-SCHEMA` starts.
- **Gate B, before any UI**: "Signed off" written on `PHOTOS-DESIGN` and the approved designs merged under `docs/design/photos/`. Every UI issue carries the acceptance criterion "UI matches the signed-off design in {{PHOTOS-DESIGN}}. Any change to layout, flow or visual style needs owner sign-off before merge; include screenshots in the PR." A UI PR whose screenshots differ from the design in layout, flow or style waits for a comment from the owner.
- **Gate C, before `PHOTOS-DEMO` merges**: the owner runs `python run.py demo` and `make verify` once on their machine (or reads the verify report in the PR) to confirm the demo data looks like a plausible household and nothing in it is real.
- **Gate D, first real sync**: after `PHOTOS-SYNC` and `PHOTOS-UI-SETTINGS`, the owner connects one library with a read-only key on their instance and checks the counts and a trip before any other member connects theirs. Not an issue; a line in the epic.

## Don't edit at once

Two agents must not have PRs open that both change any of these. The second to finish rebases on `main`, regenerates where marked, and re-runs `make check`.

| File | Why | Issues that touch it |
| --- | --- | --- |
| `waypoint/storage/migrations/versions/`, `waypoint/storage/schema.py`, `waypoint/storage/models.py` | One Alembic head (`make fleet-checks`); the epic has exactly one migration | SCHEMA only. No other issue adds a migration; if one needs a column, it stops and asks. |
| `waypoint/server/routes.py`, `waypoint/server/contract.py`, `docs/openapi.json`, `frontend/src/lib/api-types.ts`, `docs/feature-map.json`, `docs/src/content/docs/contributing/feature-map.md` | Route table and generated contract/feature map (`make api-contract`, `make feature-map`) conflict on every route PR | SOURCES, API, PROXY, TRIPSTATS. Merge one at a time; regenerate after rebasing. |
| `waypoint/server/api/photos.py` | New module shared by three issues | SOURCES creates it; API and PROXY add to it after SOURCES merges (their Depends on say so). |
| `waypoint/server/mcp_access.py` (`BLOCKED`), `tests/test_every_route.py` (`NOT_JSON`) | Small lists edited by several PRs | SOURCES, API, PROXY. |
| `waypoint/domain/photos.py` | Started by MATCH, extended by API and PROXY | API and PROXY may run together only if PROXY adds `servable`/`placeholder` at the end of the file and API adds `for_trip`/`clusters`/`needs_a_look`/`decide` in a block above; otherwise serialize. |
| `pyproject.toml` (import-linter), `.semgrep/waypoint.yml`, `.semgrep/examples/` | Contracts and rules | PROVIDER only in this epic. |
| `waypoint/server/jobs.py` | Scheduler | SOURCES (sweep) then SYNC (schedule). SYNC depends on SOURCES, so no overlap. |
| `waypoint/storage/secretbox.py`, `waypoint/storage/settings_keys.py` | Secret registration | SCHEMA adds the settings key; SOURCES adds the secret column. Sequential by dependency. |
| `waypoint/domain/demo.py`, `frontend/verify/verify.mjs`, `docs/src/assets/screenshots/` | Demo data and the screenshot baselines | DEMO adds data; UI-GALLERY adds the `trip` page to `PAGES`; every UI PR commits the images `make verify` changed, so UI PRs merge one at a time or re-run verify after rebasing. |
| `frontend/src/pages/Trip.svelte` | The trip page | UI-GALLERY, then UI-MAP and UI-STATS (sequence them), then UI-REVIEW if mounted there. |
| `frontend/src/lib/components/RouteReplay.svelte`, `RouteReplayMap.svelte`, `frontend/src/lib/route-replay.ts` | The map | UI-MAP only. |
| `frontend/src/pages/Settings.svelte`, `frontend/src/lib/components/settings/PhotosSection.svelte` | Settings | UI-SETTINGS, then UI-REVIEW. |
| `docs/src/content/docs/start/photos.md`, `start/trips.md` | Docs page every route must map to | Created by SOURCES; appended by API, PROXY, TRIPSTATS and each UI issue. Append-only sections keep rebases trivial. |
| `AGENTS.md` | The promises list | PROXY adds the "Photos stay in Immich" line. Nobody else. |
| `docs/PHOTOS.md` | The design | SPIKE and DECISIONS edit it before code; afterwards any PR that departs from it edits the relevant section in the same PR. |

## Models

The owner's routing for this feature: Opus by default, Fable for anything security-related (Immich API key storage and handling, the proxy and its access checks, per-member visibility), Sonnet only for mechanical work. Note this is stricter than Waypoint's `CLAUDE.md` (which defaults to Sonnet); the owner's routing wins for these issues, and `CLAUDE.md` may want a line saying so.

| Key | Model | Why |
| --- | --- | --- |
| `PHOTOS-EPIC` | n/a | Tracking issue |
| `PHOTOS-SPIKE` | Opus | Interprets Immich's behaviour and amends the design |
| `PHOTOS-DECISIONS` | Owner (Sonnet to write up answers) | Decisions; the write-up is mechanical |
| `PHOTOS-DESIGN` | Opus (owner signs off) | Design judgement |
| `PHOTOS-SCHEMA` | Opus | Schema, migration, backup round trip |
| `PHOTOS-PROVIDER` | Opus | New provider, Semgrep rule, contracts |
| `PHOTOS-MATCH` | Opus | The algorithm and its edge cases |
| `PHOTOS-SOURCES` | **Fable** | API key storage and handling, owner-only routes, lapsed access |
| `PHOTOS-SYNC` | Opus | Budgeted job with cursors |
| `PHOTOS-API` | **Fable** | Per-member visibility of photos |
| `PHOTOS-PROXY` | **Fable** | The thumbnail/original proxy and its access checks |
| `PHOTOS-TRIPSTATS` | Opus | Per-trip figures |
| `PHOTOS-DEMO` | Sonnet | Mechanical seeding on existing patterns |
| `PHOTOS-UI-SETTINGS` | Opus | UI against a signed-off design |
| `PHOTOS-UI-GALLERY` | Opus | UI, performance for long trips |
| `PHOTOS-UI-MAP` | Opus | Map geometry and replay integration |
| `PHOTOS-UI-STATS` | Opus | UI |
| `PHOTOS-UI-REVIEW` | Opus | UI with owner-only actions |

## Standard brief for each agent

Paste this at the start of the session, with the issue number filled in.

> You are working on Waypoint (AnthonyPluth/Waypoint) issue #N, part of the photos epic. Read `AGENTS.md` (especially "Starting work", "Conventions", "Waypoint's promises" and "Mistakes that keep coming back"), `CLAUDE.md`, `docs/PHOTOS.md`, then the issue. One issue, one branch, one PR; do not start anything outside the issue's Scope, and do not add a migration unless the issue is `PHOTOS-SCHEMA`. If the issue leaves open something a user would notice, stop and ask in the issue rather than choosing.
>
> Before writing code: run `make check` once on a clean checkout so you know the baseline is green. Name real modules and follow the patterns the issue points at; no comments or docstrings in code; outbound requests only through `waypoint.tls.urlopen`; database only through `waypoint.storage.db`; trips and segments only through `waypoint.domain.visibility`; request bodies through `waypoint.validate`; jobs take the `today` they are given. New routes: type them in `waypoint/server/contract.py`, run `make api-contract` and `make feature-map`, give them a docs page, and add tests that a stranger gets 404 and an assistant is blocked where the issue says so. Anything that reads a secret or Immich data runs its tests inside `tests/privacy.py`'s `no_leaks` with canary values.
>
> The repository is public: no real names, places, dates, asset ids, coordinates, hosts or keys from the owner's instance in code, tests, fixtures, screenshots, the issue or the PR. Demo data only in screenshots.
>
> UI issues: build to the signed-off design under `docs/design/photos/`; any layout, flow or visual change needs owner sign-off before merge. Run `make verify <pages>` and commit the images it changed; check phone and desktop widths.
>
> Done means checked: every acceptance criterion in the issue is shown in the PR with the command output (`make check`, the named tests, `make verify`), not described. Run `make test-pg` if you touched the database, migrations or tests. Open the PR with a short description (what and why, how tested), a `feat:` title, and the `Co-Authored-By` trailer naming your model on each commit. Watch for Codex's review comments and address P0–P2 findings. If a check fails on something another open PR also changes, merge `main` in and re-run rather than working around it.
>
> If you learn something that contradicts `docs/PHOTOS.md`, change the doc in the same PR and say so in the PR description.

## Things in Waypoint that shape the work (read before creating the issues)

- **No campground kind.** Segment kinds are `flight`, `hotel`, `car`, `train`, `cruise`, `parking`, `visit` (`waypoint/domain/trips.py`, `KINDS`). A campground is a `hotel` stay; the design and issues say "stays" and never add a kind.
- **The route map already exists** (`waypoint/domain/tripmap.py`, `GET /api/trips/{id}/places`, `frontend/src/lib/components/RouteReplay*.svelte`), but only for a trip that has ended with two placeable stops, and its roads cover the US only. Photos add a layer to it; `PHOTOS-UI-MAP` widens when the map shows.
- **No per-trip stats exist**; household stats do (`waypoint/domain/stats.py`). `PHOTOS-TRIPSTATS` adds `GET /api/trips/{id}/summary`. There is no point-in-polygon state lookup (`waypoint/storage/states.py` holds border lines for drawing), so "states" come from addresses, visit details and Immich's reverse-geocoded `exifInfo.state`.
- **Raster images are policed**: `tools/fleet_checks.py` rejects any committed image outside `make verify`'s screenshot folder, the logo catalogue, `waypoint/static/` and `docs/public/`. The owner's `docs/design/` folder therefore holds HTML/SVG mockups, or the design issue extends `IMAGE_HOMES`.
- **`make verify` has no `trip` page** today (`PAGES` in `frontend/verify/verify.mjs`); `PHOTOS-UI-GALLERY` adds it, which also whitelists `trip-phone.png` and friends for the fleet check.
- **Decrypting is fenced** by Semgrep (`waypoint-decrypt`: storage layer, `oidc.py`, `domain/loyalty.py`, `providers/gmail.py`), hence `waypoint/storage/photo_sources.py` rather than decrypting in the provider.
- **Demo must work offline**: `make verify` blocks the network, so the proxy serves generated placeholders for a `demo` source.
- **Home is a household setting** the browser reads only with a trip's map data and assistants can't reach; the matcher uses it server-side only, and nothing in a photo reply names it.
- **No open issues** exist in the repository today, so nothing here duplicates or contradicts an open item; closed issue #374 reverted a tiled trip map (PMTiles), which is why the design keeps the map vector-only and adds no tiles.
