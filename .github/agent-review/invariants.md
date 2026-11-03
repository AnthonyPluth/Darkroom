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

1. **The candidate set is exactly `accounts.served_people(account)`.** `PersonIndex.load(opened, account_id)` computes the served people itself (a caller cannot pass a wider set) and loads their phone items plus the Takeout items placed from this account's own exports; `decide` ignores any item outside `person_ids` even in a hand-built index; `apply` refuses a decision whose matched item or assigned person is outside the served set, or whose matched Takeout item came from another account's export. An identical file owned by a person the account doesn't serve becomes Google-only for the default person. Held by `tests/test_dedupe_invariants.py` (`CandidateSetIsTheServedPeople`) and `tests/test_dedupe.py` (`SharedPixelTests`, `OverlappingAccountsTests`, `PureIndexTests`).
2. **Nothing is deleted, moved or written by `darkroom/domain/dedupe.py`; it only decides and records.** It imports no file or process module, calls no delete, and leaves the folder tree and every `MediaItem` row in place. Held by `tests/test_dedupe_invariants.py` (`NothingIsDeletedMovedOrWritten`: a before/after hash of a tree with originals, google-only, review and staging files, and an AST check of the module).
3. **Only an automatic tier 1 or tier 2 decision matching a phone item sets `confirmed_in_google=True`, and only `darkroom/domain/dedupe.py` writes the flag.** Tiers 3, 4 and 5, owner-made decisions (#10 item 9 default), a tier 1 match on a placed Takeout item and an ambiguous tier 2 never set it. Held by `tests/test_dedupe_invariants.py` (`OnlyAutomaticTierOneAndTwoSetTheFlag`) and Semgrep's `darkroom-confirmed-flag-single-writer` (examples in `.semgrep/examples/darkroom/domain/confirmed_flag.py`).
4. **Every decision stores the export, the account, the tier, a reason, who decided and when; reversal sets `reversed_at` and clears the flag unless another unreversed automatic tier 1 or 2 decision still matches the same item.** Reversing twice keeps the first time; re-applying a reversed decision does not confirm again. Held by `tests/test_dedupe_invariants.py` (`DecisionsAreRecordedAndReversible`) and the property test `FlagImpliesAnUnreversedAutomaticMatch` (random households: a flag always has an unreversed automatic tier 1 or 2 decision from an account serving the item's owner).
5. **Applying the same item twice yields one decision.** A decision is keyed by export and the item's path in the export (`uq_dedupe_decisions_export_source`, migration 0004); a second apply returns the first row unchanged and adds no second `ReviewItem`. Held by `tests/test_dedupe_invariants.py` (`ApplyingTwiceYieldsOneDecision`) and `tests/test_migrations.py` (`test_0004_…`).
6. **Tier 2 matches only when exactly one candidate qualifies.** Sub-second capture time separates frames when both sides have it; two or more phone candidates, or another Takeout item of the same export qualifying for the same phone item, go to tier 4 review and never set the flag; an edited copy is never tier 2. Held by `tests/test_dedupe_invariants.py` (`TierTwoNeedsExactlyOneCandidate`) and `tests/test_dedupe.py` (`TierTwoAmbiguityTests`).

## Eligibility invariants (#37)

Filled in by #37's pull request: its invariants 1 to 7, each naming the test or Semgrep rule that holds it.

## `CleanupFlow` rules (#38)

Filled in by #38's pull request: the rules the iPhone cleanup flow keeps, each naming its test.

## Review actions (#27)

Filled in by #27's pull request: a review action never sets `confirmed_in_google`, with the test that holds it.
