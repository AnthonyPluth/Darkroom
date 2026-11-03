# Darkroom's invariants

The promises Darkroom keeps, numbered, each with the test or Semgrep rule that holds it. The second review (fleet's `agent-review.yml`, on a pull request labelled `safety-critical` or `security` or changing a path `.github/workflows/agent-review.yml` lists) checks every one a change touches: it still holds, the named check still holds it, and the change carries a positive and a negative test for it. Items 1 to 6 are `AGENTS.md`'s "Darkroom's promises"; a check named with an issue number is added by that issue, before the code it guards. The sections after them belong to the safety-critical issues, each filled in by its own pull request and never rewritten afterwards: a later change appends.

1. **An original is written once.** Nothing under a person's `originals/` is modified, moved or deleted by the service. Held by Semgrep's `darkroom-originals-read-only` and a before/after listing test (#25).
2. **Dedupe deletes nothing on Unraid.** It decides and records. Cleanup removes only staging, archives and resolved review holds, 14 days after the fact. Held by `tests/test_cleanup.py` and the dedupe invariants (#24, #26).
3. **A photo is eligible for deletion from an iPhone only when** every resource of it (photo, Live Photo video, adjusted version) is on Unraid with a verified SHA-1 and matched in a Takeout export by an unreversed automatic tier 1 or 2 decision, from a Google account that serves its owner, re-checked right before deleting. Held by Semgrep's `darkroom-eligibility-single-path` and `tests/test_eligibility_invariants.py` (#37).
4. **The browser worker never types a Google password or 2FA code.** A person signs in. Held by `tests/browser/test_no_credentials.py` (#28).
5. **Metadata gaps are filled with XMP sidecars, never by rewriting media.** Held by Semgrep's `darkroom-xmp-only-sidecars` (#25).
6. **No design changes without the owner's sign-off.** No web UI or iPhone screen merges unless it matches the design the owner signed off in `docs/design/` (#16, #34); a UI pull request carries screenshots. Held by review.

## Dedupe invariants (#24)

Filled in by #24's pull request: its invariants 1 to 6, each naming the test or Semgrep rule that holds it.

## Eligibility invariants (#37)

Filled in by #37's pull request: its invariants 1 to 7, each naming the test or Semgrep rule that holds it.

## `CleanupFlow` rules (#38)

Filled in by #38's pull request: the rules the iPhone cleanup flow keeps, each naming its test.

## Review actions (#27)

Filled in by #27's pull request: a review action never sets `confirmed_in_google`, with the test that holds it.
