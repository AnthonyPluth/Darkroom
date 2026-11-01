# Claude Code notes

Repo conventions, commands and layout are in [AGENTS.md](AGENTS.md); read it first, especially "Darkroom's promises".

## Model routing

Opus is the default. An issue names its model (the "Agent task" template); when delegating to a subagent, set its `model` accordingly:

- **Opus**: the default for everything: features, migrations, bug fixes, tests, refactors, UI, the dedupe and ingest pipeline, and the deletion-safety work (eligibility, cleanup).
- **Fable**: security-related issues: sign-in and sessions, device and worker tokens, `secretbox`, the MCP server's OAuth and access checks, anything that handles an API key or decides who may see a person's photos.
- **Sonnet**: only for mechanical work: boilerplate, renames, fixtures, docs typos, changes an issue spells out step by step.

Don't spawn a subagent for a task that takes a couple of tool calls to do inline.
