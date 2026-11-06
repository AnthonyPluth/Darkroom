# iPhone app design: cleanup (proposal, needs owner sign-off)

Status: **proposal, not signed off.** This is the design the first page of `docs/design/ios/README.md` promised for #38 (the cleanup notification and the batch screens). Nothing here is built. The logic behind it is on `main`: `CleanupFlow`, `CleanupRunner` and the three device routes (#37, #38's core). What is missing is every screen, which promise 6 (AGENTS.md) holds until the owner signs this off in #34. It reuses the README's tokens, shared pieces (`Card`, `StatusChip`, `PrimaryButton`, `SecondaryButton`, `KeyValueRow`), layout rules and copy style without change; it adds no colour.

How to read it: every screen has its states, the exact copy, a layout precise enough to write in SwiftUI, accessibility behaviour, and the DarkroomCore type behind each element. Fourteen decisions the code cannot settle are under "Open points", each marked **needs owner sign-off** with a recommended default. The screens are drawn on the defaults, so a different answer changes the lines that name that point (for example "(point 2)").

## Safety rules the screens must not weaken

These come from `.github/agent-review/invariants.md` (the eligibility invariants of #37 and the `CleanupFlow` rules of #38) and from #108 items 103 and 114. A design that breaks one is a blocking bug, whatever it looks like.

1. **Nothing is removed without Darkroom's answer just before.** The list is at most an hour old when "Remove from iPhone" is tapped; an older list is replaced by a fresh one and nothing is confirmed (rule 1). The screen shows the replacement and asks again; it never removes on the same tap.
2. **Only what is shown is asked about, and only what Darkroom confirms is removed.** The ids sent to confirm are exactly the ids on screen that still match the phone; the system delete gets only the confirmed ids (rules 2 and 3). No screen offers a way to delete anything else, and there is no path that skips the confirm.
3. **Cancelling the system dialog tells Darkroom nothing** and removes nothing (rule 5). Only what iOS removed is reported (rule 4).
4. **Cleanup off on the server means nothing is offered** (rule 6). A list already on screen is replaced by the "off" state. The app has no switch for it: it is turned on and off on the People page, with the "iCloud Photos is off on this iPhone" confirmation, and the phone only shows the answer.
5. **A photo with a file Darkroom does not back up is never offered** (#108 item 114). PhotoKit lists content the server has no kind for: the RAW half of a RAW+JPEG pair, the edited half of a Live Photo, the unedited base of an edit. Darkroom does not upload them and the server judges only what the phone reports, so such an asset would look complete. The adapter must keep it off the list entirely (not send it to be judged), and the screen says how many stayed (point 7).
6. **What iOS removed and what Darkroom recorded are shown separately.** A photo can be gone from the iPhone and not recorded (a report refused, the phone disconnected, or cleanup switched off after the confirm, #108 item 103). The screen says so plainly and never says "removed and recorded" for one of those. A removal that Darkroom did not record is not a lost photo: the file is on Unraid and in Google, and it is in Recently Deleted for 30 days.
7. **No claim the app cannot back.** "Frees space" is not said while the photos are in Recently Deleted (point 3). Counts come from the list and the report, never from a guess.

## Where cleanup lives (point 1)

The tab bar gains a third tab, **Cleanup** (`trash`, between Uploads and Settings), shown whenever the phone is signed in, so the person can open the batch without the notification and see why there is none. It carries a count badge (`.badge(count)`, the system badge, `accent` fill) while a batch is waiting and nothing otherwise (point 11). The cleanup flow itself is one screen with states, not a stack: the batch, the system dialog and the result are the same screen.

`DarkroomApp`'s phase table in the README is unchanged: `.signedIn` shows `TabView` with three tabs.

## Screen 4: Cleanup notification

A local notification (`UNUserNotificationCenter`), posted by the app after a background check. It is not a push: Darkroom's server never sends it, and it carries no photo, file name, place or date.

### Content

| Part | Text |
| --- | --- |
| Title | Darkroom |
| Body | 24 photos (3.2 GB) can be removed from this iPhone. |
| Singular | 1 photo (4.2 MB) can be removed from this iPhone. |
| Sound, badge | none (point 5) |
| Interruption level | `.active` |
| Identifier | `cleanup-batch`, so a new one replaces the old |

The size is `ByteCountFormatter` with `.file` style over `EligibleList.totalBytes`. "Photos" counts assets, videos included (point 3). The body states what is on the list when it was checked; the screen it opens shows its own, fresher numbers.

### When it is posted

The check is a `BGAppRefreshTask` that runs `CleanupRunner.refresh()`. The notification is posted only when all hold:

- the outcome is `.listed` with at least one photo (the server said cleanup is on and the list is not empty);
- notifications are allowed (point 4);
- the set of photos on the list has a member that the last notification did not name, or none was posted in the last 24 hours (point 5), so an unchanged list does not nag;
- the app is not in the foreground (in the foreground the tab badge updates and no banner shows).

It is never posted for `.idle` (cleanup off, or nothing to remove), a failed list call, `.needsSignIn`, or any outcome with a report still waiting (`reportPending`, `reportUnsaved`, `reportUnreadable`): those are shown in the app (Screen 5) and are not alerts. When a batch ends (removed, cancelled and gone, cleanup switched off, list empty on the next check) the app removes the delivered notification (`removeDeliveredNotifications`).

### Tapping it

Opens the app on the Cleanup tab and runs a fresh `CleanupRunner.refresh()`. The tap never trusts the notification's count: the screen shows "Checking with Darkroom" (Screen 5, Checking) until the new list is back. A cold launch from the notification restores the session first, as the README's launch rule does.

### Permission (point 4)

The first time the Cleanup tab is shown with cleanup on and notifications not yet asked, a `Card` above the batch reads: chip neutral "Notifications", title "Get told when photos can be removed", detail "Darkroom checks in the background and sends one notification when there are photos it can remove from this iPhone. It never removes anything without asking you.", button "Allow notifications" (`SecondaryButton`) which runs the system prompt, and a "Not now" text button. Nothing is requested at launch or at sign-in. When notifications are off in iOS Settings the card becomes: "Notifications are off for Darkroom. You can still open Cleanup yourself." with "Open Settings". The same permission serves alerts (#108 item 105); whichever design lands first owns the prompt and this card reuses it.

### Accessibility

The notification text is read by the system. The tab badge is read as "Cleanup, 24 photos" by the tab's accessibility label.

### DarkroomCore types behind it

`CleanupRunner.refresh()` and `CleanupRunner.Outcome.listed(EligibleList)` (`EligibleList.entries.count`, `.totalBytes`), `UNUserNotificationCenter` in the app target. The "last named" set and the last-posted time are two small persisted values in the app target (no core type yet).

## Screen 5: Cleanup

The tab. `ScrollView` on `canvas`, the README's column and spacing, title "Cleanup" (`.title.bold()`, header trait) and a `.subheadline` `inkMuted` sentence that says the state. A bottom-pinned action area (`.safeAreaInset(edge: .bottom)`, `rail` background, 1 pt `line` top border, 16 padding) holds the main button in the states that have one, so it stays on screen while the grid scrolls.

### States

The state comes from `CleanupRunner.Outcome`, `CleanupRunner.state` and `cleanupEnabled`.

| State | Source | Sentence under the title | Chip | Card title | Card detail | Button |
| --- | --- | --- | --- | --- | --- | --- |
| Not checked yet | no `refresh()` yet this launch, `cleanupEnabled == nil` | Darkroom hasn't checked yet. | neutral "Not checked" | Not checked yet | Open this tab or pull down to check with Darkroom. | "Check now" |
| Checking | `refresh()` or `remove` in flight | Checking with Darkroom. | neutral "Checking" | Checking with Darkroom | This takes a moment. Nothing is removed while it checks. | none, controls disabled |
| Off | `cleanupEnabled == false` (the flow is `idle`) | Cleanup is off for this iPhone. | neutral "Off" | Cleanup is off | Removing photos from this iPhone is turned on or off on the People page, where you confirm that iCloud Photos is off. Nothing is removed while it is off. | "Check again" (secondary) |
| Nothing to remove | `.idle`, `cleanupEnabled == true` | Nothing to remove right now. | ok "Nothing to remove" | Nothing to remove | Photos appear here once Darkroom has them on Unraid and has found them in a Google Takeout export. | "Check again" (secondary) |
| Batch ready | `.listed(list)` | 24 photos (3.2 GB) can be removed. | neutral "Ready" | see Batch below | | "Remove from iPhone" (primary) |
| Asking Darkroom | `.confirming` (inside `remove`) | Asking Darkroom to confirm. | neutral "Checking" | Asking Darkroom to confirm | Darkroom checks each photo again right now. | none |
| Waiting for iOS | `.deleting` | Waiting for you in the iOS dialog. | neutral "Waiting" | Waiting for you | iOS asks you to confirm. Nothing is removed unless you tap Delete there. | none |
| Telling Darkroom | `.reporting` | Telling Darkroom what was removed. | neutral "Sending" | Telling Darkroom | | none |
| Result | `.finished(report)` | see Results | | | | |
| Couldn't check | `refresh()` threw, other than sign-in | Couldn't reach Darkroom. | warn "Waiting for home network" | Couldn't reach Darkroom | Nothing was removed. Try again on home Wi-Fi or over Tailscale. | "Try again" |
| Sign-in needed | `.needsSignIn` | Sign in again to check. | danger "Sign-in needed" | Sign-in needed | Darkroom no longer accepts this iPhone, so nothing can be removed. Files already on Darkroom stay there. | "Sign in again" (as on Uploads) |
| Photos access needed | PhotoKit `resources(of:)` throws, or access is limited | Darkroom can't see all your photos. | danger "Photos access needed" | Photos access needed | Allow access to all photos so Darkroom can check which ones it can remove. | "Open Settings" |

A failed check is a calm warn state with no red, as for uploads (README principle 2). A check that cannot reach Darkroom never leaves old data looking current: the previous batch is dropped from view and the "Couldn't check" card replaces it, because `CleanupRunner` returns to `idle` on a failed list.

### Batch (Batch ready)

Top to bottom:

1. **Summary `Card`.** Chip, title "24 photos (3.2 GB)" in `.title3.bold()`, detail "Darkroom has all of each of these on Unraid and has found it in a Google Takeout export. Checked 12 minutes ago." The age is a live relative time (`TimelineView`, one-minute period) from `EligibleList.generatedAt`. At 45 minutes or more, the detail adds "Darkroom checks again right before removing."
2. **Grid.** Label "TO REMOVE" and `LazyVGrid` with adaptive columns (minimum 96 pt, 4 pt gaps, radius 8 tiles, aspect 1:1). A tile is the asset's thumbnail from `PHImageManager` with `isNetworkAccessAllowed = false` (never fetched from iCloud), `deliveryMode` `.opportunistic`, content mode fill. A Live Photo tile shows the `livephoto` symbol bottom-left and a video tile the duration bottom-right, in a `canvas` 70 percent capsule so they read on any image. A thumbnail that cannot load is a `raised` tile with the `photo` symbol. Tiles do nothing when tapped (point 2). Assets no longer in the library are dropped from the grid, and the summary count and size follow; at zero the screen goes to "Nothing to remove".
3. **More than the batch holds.** When Darkroom has more than the 500 a list can carry (`EligibleList.maxAssets`), a `.footnote` `inkMuted` line: "Showing the first 500. The rest appear after these are removed."
4. **Kept on the iPhone (point 7).** When the adapter excluded assets for an unbacked resource, a `Card` with a neutral chip "Kept": "N photos stay on this iPhone. They have files Darkroom doesn't back up yet, such as the RAW half of a RAW and JPEG pair." The photos are not named or shown.
5. **iCloud reminder (point 6).** `.footnote` `inkMuted`: "Cleanup is for iPhones where iCloud Photos is off. With it on, removing a photo here also removes it from iCloud and your other devices."
6. **Action area.** `PrimaryButton` "Remove from iPhone", and under it a `.footnote` `inkMuted` line: "iOS asks you to confirm. Removed photos stay in Recently Deleted for 30 days." While nothing else is happening a `SecondaryButton` "Check again" sits above the primary button's line, so the person can refresh.

Pulling down runs `refresh()` (`.refreshable`). The screen also runs it when it appears and when the app becomes active, never more often than every five minutes unless the person pulls down; it never refreshes while `confirming`, `deleting` or `reporting`.

### The tap, step by step

1. Tap "Remove from iPhone": `CleanupRunner.remove(shown:)` with every id on screen.
2. If the list is an hour old or more, the runner refetches and returns `.refetched`. The screen shows the new list with a `Card` at the top: chip warn, "Your list was out of date", "It was more than an hour old, so Darkroom checked again. Look over the new list and tap Remove from iPhone again." Nothing was confirmed or removed.
3. Otherwise the state is Asking Darkroom, then the system dialog (Waiting for iOS), then Telling Darkroom, then the result. During these the tab bar and the button are disabled and the pull-to-refresh is off.
4. A confirm that cannot be completed (`.confirmFailed`) returns to Batch ready with a `danger`-free warn `Card` on top: "Couldn't check with Darkroom, so nothing was removed. Try again on home Wi-Fi or over Tailscale." The list is kept, and the tap rule above judges its age again.

The system dialog is iOS's own ("Allow Darkroom to delete N photos?"), which the app does not style. It can show fewer photos than the batch, because Darkroom may have kept some back (Results).

### Results

A `Card` with a chip and a title, then up to four sections, each only when it has photos. The first line of the card is chosen by what happened.

| Outcome | Chip | Title | Detail |
| --- | --- | --- | --- |
| Removed, all recorded | ok "Removed" | 24 photos removed | They're in Recently Deleted for 30 days. Photos only free their space once they leave Recently Deleted, which you can do in Photos. Darkroom has recorded it. |
| Cancelled in iOS (`removed` empty, nothing refused) | neutral "Cancelled" | Nothing was removed | You cancelled in iOS, so Darkroom wasn't told anything. The photos are still on this iPhone. |
| Some removed | ok "Removed" | 20 of 24 photos removed | The sections below say what happened to the other 4. |
| Everything kept back | neutral "Kept" | Nothing was removed | Darkroom kept every photo on the list. The reasons are below. |
| Cleanup switched off meanwhile (every `refused` has `cleanup_disabled`) | neutral "Off" | Cleanup was switched off | It was turned off on the People page while you were removing, so nothing was removed. |

Sections after the first line:

- **Kept on this iPhone** (`report.refused`, the server's message for each, from `eligibility.REASONS`): rows of a 44 pt thumbnail, the date the photo was taken (`.footnote`), and the reason in `.footnote` `inkMuted`. Up to five, then "Show all 12" which pushes the same list. Rows with the same reason are grouped under it with a count when there are more than three. The wording is the server's, which is written for the app (for example "Part of this photo is not on Unraid yet.").
- **Changed on this iPhone** (`report.changed`): "N photos changed on this iPhone after Darkroom checked them, so they were kept. They'll be checked again."
- **Not removed by iOS** (`report.notRemoved`): "N photos couldn't be removed by iOS. They're still on this iPhone."
- **Removed, but Darkroom didn't record it** (`report.unrecorded`, warn tone): see below.

"Check again" (secondary) at the bottom starts `refresh()`; photos kept back that are still eligible come back on the new list.

### Removed but not recorded

Shown when photos left the iPhone and Darkroom did not record them. The card is warn tone with the chip "Not recorded". Its copy depends on the reason, and the app writes it itself instead of showing the server's message, because the server's "Darkroom did not confirm this photo for removal" would wrongly suggest the photos were not removed.

| Reason | Copy |
| --- | --- |
| `not_confirmed_for_deletion` (point 8: cleanup switched off after the confirm, #108 item 103) | N photos were removed from this iPhone, but Darkroom didn't record it, because cleanup was switched off while you were removing them. The photos are in Recently Deleted for 30 days, and they're still on Unraid and in Google. |
| `report_refused` (`CleanupRunner.reportRefusedReason`) | N photos were removed from this iPhone, but Darkroom refused the report of it. The photos are in Recently Deleted for 30 days, and they're still on Unraid and in Google. Tell whoever runs Darkroom. |
| `device_disconnected` (`CleanupRunner.disconnectedReason`) | N photos were removed from this iPhone, but Darkroom had already disconnected it, so the removal wasn't recorded. They're in Recently Deleted for 30 days. Sign in again to keep using Darkroom on this iPhone. |
| `unknown_asset`, `already_deleted`, any other | N photos were removed from this iPhone, but Darkroom couldn't record it. The photos are in Recently Deleted for 30 days. |

Darkroom's People page may keep counting these as eligible until the next sync; the copy does not promise otherwise. Nothing here offers a retry, because the server's answer is final.

### A report that has not been sent

When iOS removed photos and the report could not be sent, the removal is real and the app keeps a record of it (`PendingDeletionReport`). The screen shows the removal's result, plus:

| Outcome | Chip | Copy | Button |
| --- | --- | --- | --- |
| `.reportPending` | warn "Waiting for home network" | N photos were removed. Darkroom hasn't been told yet. It will be told by itself when this iPhone reaches Darkroom again. | "Tell Darkroom now" (`retryReport()`) |
| `.reportUnsaved` | danger "Not saved" | N photos were removed, and this iPhone couldn't keep a note to tell Darkroom later. Keep the app open and tap Tell Darkroom now. | "Tell Darkroom now" |
| `.recordNotCleared` | neutral "Done" | Darkroom has been told. This iPhone couldn't clear its note, so it may tell Darkroom again, which does no harm. | none |
| `.reportUnreadable` | danger "Paused" | This iPhone has a note about an earlier removal that it can't read, so Cleanup is paused. The photos it names are in Recently Deleted. Tell whoever runs Darkroom. | "Try again" (point 9) |
| `.disconnected` | warn | as the `device_disconnected` row above, with "Sign in again" | |

While a report is waiting, `refresh()` sends it first (the runner does this) and the batch does not appear until it is sent or dropped, so no new batch is offered over an unreported removal. The notification is not posted in these states (Screen 4).

### Accessibility

- The summary card is one element: "24 photos, 3.2 gigabytes, checked 12 minutes ago". The grid is a container labelled "Photos to remove, 24" whose tiles are elements ("Photo, taken 3 May 2026, 4.2 megabytes", "Live Photo, ...", "Video, 12 seconds, ..."); the container has the rotor entry "Photos" so a VoiceOver user can skip 500 tiles from the heading. Sizes come from `EligibleAsset.bytes`.
- Moving between states posts `AccessibilityNotification.Announcement` with the card title ("Asking Darkroom to confirm", "24 photos removed", "Nothing was removed, you cancelled"), and focus moves to the result card when it appears (`@AccessibilityFocusState`). The 12-minute age is not announced as it changes.
- "Remove from iPhone" has the hint "Asks Darkroom to confirm, then iOS asks you". It is the only button that can lead to a deletion; its label never changes to "Delete" or "Done".
- Dynamic Type: all text scales; the pinned action area grows and, at accessibility sizes (`dynamicTypeSize.isAccessibilitySize`), the grid becomes two columns, rows stack the thumbnail above its text and the pinned area scrolls with the content instead of staying fixed, so the text is never clipped.
- Colour is never the only signal: each tone has a shape and a word (README). Reduce Motion: state changes cross-fade at most; thumbnails do not animate in.
- Contrast: the chip and text pairs are the README's.

### DarkroomCore types behind each element

| Element | Type and member |
| --- | --- |
| State, sentence | `CleanupRunner.Outcome`, `CleanupRunner.state` (`CleanupState`), `cleanupEnabled` |
| Count, size, age | `EligibleList.entries.count`, `.totalBytes`, `.generatedAt`, `.isStale(at:)` |
| Thumbnails, dates, Live Photo and video marks | `PHAsset.fetchAssets(withLocalIdentifiers:)` on `EligibleList.Entry.asset.id` (app target, no core type) |
| "Remove from iPhone" | `CleanupRunner.remove(shown:)`; the system dialog is `PhotoDeleter.delete(_:)` (the PhotoKit adapter, `PHAssetChangeRequest.deleteAssets`) |
| Refused, changed, not removed | `CleanupReport.refused` (`RejectedAsset.message`), `.changed`, `.notRemoved` |
| Removed, recorded, not recorded | `CleanupReport.removed`, `.recorded`, `.unrecorded` (`RejectedAsset.reason`) |
| Unsent report | `CleanupRunner.retryReport()`, `PendingDeletionReport`, `Outcome.reportPending`, `.reportUnsaved`, `.recordNotCleared`, `.reportUnreadable`, `.disconnected` |
| "Kept" count (point 7) | not built: `PhotoDeleter.resources(of:)` must also say that an asset has a resource of an unlisted type, and `CleanupRunner` must keep such an asset off the list and count it |
| Sign in again | `.needsSignIn`, `DeviceSession` as on Uploads |

## Settings: the Cleanup card

Replaces the README's Cleanup card ("Nothing else until #38"). `Card`, label "CLEANUP":

1. `KeyValueRow` "Cleanup": "On", "Off" or "Not checked yet" (`CleanupRunner.cleanupEnabled`), with the `.footnote` `inkMuted` line "Set on the People page. There is nothing to switch here."
2. `KeyValueRow` "Notifications": "Allowed" or "Off", with "Open Settings" (a plain button) when off.
3. Only when cleanup is on and `UIApplication.shared.backgroundRefreshStatus` is not `.available`: a `.footnote` line "Background App Refresh is off, so Darkroom only checks when you open it." with "Open Settings" (point 10).
4. A "Open Cleanup" row that switches to the Cleanup tab, only when cleanup is on.

The sign-out dialog gains one sentence when a removal has not been reported (point 9): "Darkroom hasn't been told about N removed photos yet, and after you sign out it can't be." The button stays "Sign out"; it is not blocked.

## Open points (all need owner sign-off)

Each is a decision the code and the existing pages cannot make. The recommended default is what the screens above are drawn with; nothing is built on a point until the owner answers it in #34.

### 1. Where cleanup lives

**Needs owner sign-off.** The README leaves "their own tab or sheet" open.

Recommended default: a **third tab, Cleanup**, always present when signed in, with a count badge while a batch waits; the notification opens it. A tab works without the notification (notifications can be off, or missed) and explains why there is nothing to remove. Alternative: no tab, a sheet opened only by the notification and a Settings row; a person who dismissed the notification would have no obvious way back.

### 2. All or nothing

**Needs owner sign-off.** `CleanupFlow` can remove a chosen subset, so the screen could let a person keep some photos.

Recommended default: **all or nothing**, no per-photo selection, tiles not tappable. The batch is what Darkroom has verified; the system dialog is the last gate; a photo the person wants to keep on the phone can be kept by cancelling and will return in every later list. Alternative: a per-photo "Keep" (tap to deselect), which adds selection state, a permanent "kept" memory or a list that never empties, and a larger accessibility surface.

### 3. What "photos" and "frees" mean

**Needs owner sign-off.** The issue's notification text is "N photos (X GB) can be removed from this iPhone". The count is of assets, so a video is one, a Live Photo is one and an edited photo is one, unlike the Uploads counts, which are files. And removed photos keep their space in Recently Deleted for 30 days.

Recommended default: say **photos** for every asset (videos included) in the notification and the screen, say "removed" and never "frees" or "saves", and state in the result that space is freed only when Recently Deleted is emptied. Alternative: "photos and videos" when the batch has both (the app knows `mediaType`), which is exact but longer in a notification.

### 4. When to ask for notification permission

**Needs owner sign-off.** The same question as #108 item 105 (alerts have no prompt either). Silent wake-ups need none, but this alert and #59's do.

Recommended default: **never at launch or sign-in.** Ask the first time the Cleanup tab shows with cleanup on, with the card above explaining it, and the system prompt only after the person taps "Allow notifications"; Settings shows the state and an "Open Settings" when it is off. If item 105's design asks earlier, this card is dropped and Settings keeps its rows.

### 5. How often the notification comes

**Needs owner sign-off.** The issue says a notification after the background check.

Recommended default: one notification identifier (a new one replaces the old), posted when the list has a photo the last notification did not name or after 24 hours with a non-empty list, never while the app is open, `.active` with no sound and no app-icon badge, and removed when the batch is gone. Alternative: `.passive`, which does not light the screen and is quieter, at the cost of being easier to miss; or one notification per check, which repeats daily for an unchanged list.

### 6. The iCloud Photos reminder

**Needs owner sign-off.** Cleanup is safe only with iCloud Photos off (SPEC), and the app cannot read that setting. The People page asks when cleanup is turned on.

Recommended default: show the reminder line on **every batch**, under the grid. It is one `.footnote`, and it is the last thing a person reads before the system dialog. Alternative: show it only the first time, or not at all because the People page already asked.

### 7. Photos with files Darkroom does not back up (#108 item 114)

**Needs owner sign-off, and it is a safety rule, not only a layout choice.** PhotoKit lists RAW of a RAW+JPEG pair (`alternatePhoto`), the edited half of a Live Photo (`fullSizePairedVideo`) and the unedited base of an edit (`adjustmentBasePhoto`). Darkroom's server has kinds for photo, Live Photo video and adjusted only, so it would judge such an asset complete and cleanup could remove a RAW that is on Unraid nowhere.

Recommended default: the cleanup adapter treats any asset with a content resource of an unlisted type as **not deletable**: it is never sent to be judged (judging it is not safe, because the server compares only what the phone reports), never shown in the batch, and counted in the "Kept" card ("N photos stay on this iPhone. They have files Darkroom doesn't back up yet...") so the person knows why some photos never go. It leaves when Darkroom backs those files up, a separate decision for #108 item 114 (add server kinds, or accept). Alternative: show them in the batch as unavailable rows; not recommended, since the list would then name photos that are not offered. Also needed in the build pull request: a test that a RAW+JPEG asset, a Live Photo with an edited paired video and an edit with an unedited base are each not offered.

### 8. A removal reported after cleanup was switched off (#108 item 103)

**Needs owner sign-off.** Switching cleanup off clears every confirm, so a report of photos iOS then removed is refused as `not_confirmed_for_deletion` and `deleted_at` is not recorded. No photo can be lost and Status is not inflated.

Recommended default: **accept as is**, as #108 item 103 recommends, and tell the truth on screen: the "Removed, but Darkroom didn't record it" copy for that reason above, which says the photos were removed, are in Recently Deleted and are still on Unraid and in Google, and why the record is missing. If the owner instead has the server record deletions confirmed since the last switch, the row for that reason goes and nothing else here changes.

### 9. A removal that cannot be reported

**Needs owner sign-off.** The runner keeps a record of removed photos and sends it later (`reportPending`), and can fail to save, clear or read it. A signed-out iPhone's record can never be accepted, because a new sign-in is a new device.

Recommended default: the states in "A report that has not been sent"; one "Tell Darkroom now" button; no new batch while a report waits; the sign-out dialog **warns** with the sentence in Settings and does not block. For `reportUnreadable` the screen can only say that Cleanup is paused and to tell whoever runs Darkroom; it needs a reset the owner can choose (for example a "Forget the note" button that drops the record after saying that Darkroom stays unaware of those photos). That reset is not drawn, because dropping a record is itself a deletion-safety decision. Alternative: block sign-out while a report waits, which can trap a person on a phone that cannot reach Darkroom.

### 10. Background checks and Background App Refresh

**Needs owner sign-off.** iOS decides when a `BGAppRefreshTask` runs, and the person can turn Background App Refresh off for the app.

Recommended default: the check runs when iOS allows it, and always when the Cleanup tab appears or the app becomes active (at most every five minutes, or on pull down). Settings says when Background App Refresh is off (the line in the Cleanup card), so a missing notification has an explanation. No "check every N hours" setting: iOS would not honour it. Alternative: no mention of Background App Refresh, which leaves a silent failure.

### 11. Tab badge

**Needs owner sign-off.**

Recommended default: the Cleanup tab shows the count of photos on the current list (`.badge`), and nothing when there is no batch. It is the only badge in the app. Alternative: a dot with no number, or none (then the notification and the tab are the only cues).

### 12. Old lists

**Needs owner sign-off.** `CleanupFlow` requires a refetch for a list an hour old or more and a refetched list does not remove anything.

Recommended default: as drawn, a refetch shows the new list and a "Your list was out of date" card and needs a second tap. Alternative: a refetch followed by an immediate confirm of the new list, which would remove photos the person has not seen; it breaks the rule that only what is shown is removed and is not recommended.

### 13. Thumbnails and iCloud-only content

**Needs owner sign-off.** The thumbnails come from the library. Eligible photos are on the phone (Darkroom uploaded them), but a thumbnail can still be missing.

Recommended default: the library is never asked to download anything (`isNetworkAccessAllowed = false`); a tile with no thumbnail is a neutral placeholder and its photo is still in the batch. Alternative: allow network access for thumbnails, which would pull full photos from iCloud on a phone that is meant to be offline.

### 14. Dark only, and how big

**Needs owner sign-off.** Inherits README point 6 (dark only). The batch can hold 500 photos.

Recommended default: dark only, no new tokens; the grid is lazy and thumbnails are loaded at tile size (about 3 times 96 pt), so 500 tiles stay light. A person who wants a smaller batch has no control (point 2).

## Not in this proposal

- The system deletion dialog (iOS draws it) and Photos' Recently Deleted.
- Turning cleanup on or off, and the "iCloud Photos is off on this iPhone" confirmation: the People page (#37, #174).
- Eligibility rules (#37), the server's reason wording, and anything that backs up RAW and similar files (#108 item 114's separate decision).
- Alerts and the notification permission for push (#59, #108 item 105), except where point 4 reuses it.
- iPad.

## Checking a screen built from this page

A pull request that builds these screens carries screenshots from the owner's device build, in the states above, at an iPhone SE width (375 pt) and an iPhone Pro Max width (430 pt), at the default text size and at the largest accessibility size, since the simulator in CI cannot run PhotoKit or a real deletion. The owner's verification list from #38 stays: with iCloud Photos off and cleanup enabled, the notification arrives with a count; the batch opens; cancelling the iOS dialog deletes nothing and posts nothing; confirming puts the photos in Recently Deleted and records `deleted_at`; a reversed decision on the Review page removes that photo from the next list; a Live Photo is offered only when both halves matched; with cleanup disabled on the server there is no notification. Added by this design: a RAW+JPEG photo is never offered, and switching cleanup off between the confirm and the iOS dialog shows "Removed, but Darkroom didn't record it".
