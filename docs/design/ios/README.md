# iPhone app design (proposal, needs owner sign-off)

Status: **proposal**. The owner's note on #34 (2026-11-06) says the iPhone screen designs are approved, but it links no boards and none are in the repository, so promise 6 (AGENTS.md) still blocks the screens of #35 and #38. This page writes the design down so it can be read, corrected and signed off in #34, in the same way `docs/design/web/` records what #16 approved. Nothing here is built yet. Where the owner's boards exist and differ from this page, the boards win and this page is corrected in the pull request that exports them.

How to read it: every screen has its states, the exact copy, a layout precise enough to write in SwiftUI, accessibility behaviour, and the DarkroomCore type behind each element. Fourteen decisions the code cannot settle are under "Open points", each marked **needs owner sign-off** with a recommended default. The screens are drawn on the recommended defaults, so a different answer changes the lines that name that point (for example "(point 2)").

Scope: the app shell, the sign-in screen, the upload status screen and Settings (#35). The cleanup notification and batch screens (#38) get their own design pull request after these, and Settings only reserves their line.

Types marked (#166) are in the open pull request #166 (`ServerAddress`, `DeviceSignIn`, `DeviceSession`, `UploadStatusTracker`, `UploadStatus`) and are not on `main` yet. The others are on `main` in `ios/Packages/DarkroomCore`.

## Principles

1. **Same look as the web app.** The colours, type scale, spacing and radii of `docs/design/web/00-tokens.dc.html`, with chips, cards and copy written the way the web phone boards write them (sentence case, "Sign-in needed", state shown by a label and a shape, never by colour alone).
2. **Waiting is not failing.** No home network, no signal and a server that cannot be reached are calm states with a warn tone and no red. Red is for a sign-in that is needed and for files Darkroom refused.
3. **Say files, not photos.** The store counts resources: a Live Photo is two files and an edited photo is three, the same files that land in `originals/`.
4. **Nothing the app cannot know.** An element appears only when a DarkroomCore type or a server reply feeds it. Where one is missing the design says so and names the point that decides it.

## Tokens in SwiftUI

Dark only, as on the web (point 6 asks the owner to confirm). The app sets `UIUserInterfaceStyle` to `Dark` in `project.yml`'s Info.plist keys and `.preferredColorScheme(.dark)` on the root view, so system alerts, the keyboard and the `ASWebAuthenticationSession` sheet are dark too.

Colours are one asset-catalog colour set each (a single dark value, no light one), exposed as `Color.canvas` and so on in `Theme.swift`. A new colour goes back to #16 first, as on the web.

| Token | Value | Use in the app |
| --- | --- | --- |
| `canvas` | `#16140F` | Screen background |
| `rail` | `#1B1813` | Tab bar background (`.toolbarBackground`) |
| `surface` | `#1F1C17` | Cards, text field, secondary buttons |
| `raised` | `#2B2721` | Row dividers, neutral chips, progress track |
| `ink` | `#F1ECE2` | Body text |
| `inkMuted` | `#B3AB9D` | Captions, labels |
| `line` | `#3A352C` | Card borders (1 pt) |
| `lineStrong` | `#7A7260` | Control borders (1 pt) |
| `accent` | `#E2693F` | Primary button, selected tab, brand dot |
| `accentInk` | `#1A0E08` | Text on accent |
| `ok`, `okBg` | `#7CC38F`, `#1D2B20` | Up to date chip |
| `warn`, `warnBg` | `#E0B253`, `#332A12` | Waiting for home network, needs attention |
| `danger`, `dangerBg` | `#F08A84`, `#3A1A1A` | Sign-in needed, stopped, sign-in errors |

Type uses the system text styles, so every size follows Dynamic Type. The web scale maps to them like this:

| Web | App | Used for |
| --- | --- | --- |
| 28 bold | `.title.bold()` | Screen title, big counts |
| 20 bold | `.title3.bold()` | Brand name, card titles |
| 16 semibold | `.headline` | Row titles |
| 14 | `.subheadline` | Body text, key/value rows |
| 13 | `.footnote` | Helper and error text, build version |
| 12 semibold | `.caption.weight(.semibold)` | Chips, section labels (uppercase, tracking 0.06 em) |
| mono 13 | `.footnote.monospaced()` | File names, the server host |

Spacing is multiples of 4 pt. Side gutter 16, gap between cards 16, between sections 24, card padding 16, card radius 12 (`RoundedRectangle(cornerRadius: 12, style: .continuous)`) with a 1 pt `line` stroke, control radius 8, chip radius full. Every control is at least 44 pt high; primary buttons are 48. A screen's content sits in a column of at most 560 pt, centred, so landscape (which `project.yml` allows) and larger phones stay readable.

Shared pieces, each a small view in `Darkroom/Views/`:

- **`StatusChip(tone, label)`**: `HStack(spacing: 6)` of an 8 pt shape and the label, `.caption.weight(.semibold)`, padding 10 by 2, capsule filled with the tone's `Bg`, text and shape in the tone's colour. Shapes: circle for ok, diamond (a square rotated 45 degrees) for warn, rounded square for danger, circle in `inkMuted` on `raised` for neutral. `.accessibilityElement(children: .combine)`.
- **`Card`**: `VStack(alignment: .leading, spacing: 12)` with 16 padding on `surface`, the `line` stroke, radius 12.
- **`PrimaryButton`**: full width, height 48, `accent` fill, `accentInk` `.headline` label, radius 8. Disabled: 40 percent opacity and `.disabled(true)`.
- **`SecondaryButton`**: same size, `surface` fill, `lineStrong` stroke, `ink` label.
- **`KeyValueRow(label, value)`**: label in `inkMuted` on the left, value on the right; at accessibility text sizes it stacks, label above value.

## App shell

`DarkroomApp` shows one of two roots, chosen by `DeviceSession.phase` (#166):

| Phase | Root |
| --- | --- |
| `.signedOut`, `.signingIn` | Sign-in screen |
| `.signedIn` | `TabView` with two tabs, Uploads and Settings |

On launch the app calls `DeviceSession.restore()` before drawing anything and shows `canvas` with a centred `ProgressView` until it returns; if it throws `SignInError.storage` the sign-in screen opens with the "Couldn't read the sign-in saved on this iPhone" notice (copy under Sign-in, errors).

Tab bar: `rail` background, 1 pt `line` top border, system labels, `.tint(.accent)` for the selected tab. Tabs: **Uploads** (`arrow.up.circle`) and **Settings** (`gearshape`). The cleanup screens of #38 add their own tab or sheet in their design. There is no navigation bar on the tabs' root screens: each draws its own title (`.title.bold()`) at the top of the scroll content, as the web pages do. Pushed screens ("Show all" below) use the system back button.

Each screen is a `ScrollView` on `canvas`, `VStack(alignment: .leading, spacing: 24)`, 16 side padding, 20 top padding, 28 bottom padding.

## Screen 1: Sign-in

Shown when signed out and while a sign-in is in progress. A single screen; the Pocket ID page itself is the system `ASWebAuthenticationSession` sheet (callback scheme `darkroom`), which Darkroom's server draws and the app does not style.

### Layout (top to bottom)

1. Brand row: 12 pt `accent` circle and "Darkroom" in `.title3.bold()`, 8 pt apart. The circle is decorative (`.accessibilityHidden(true)`).
2. Title "Sign in" (`.title.bold()`), then the sub-line "Darkroom uploads this iPhone's photos to your own server at home." in `.subheadline`, `inkMuted`.
3. A banner, only when there is a notice or an error (below). `Card` with a 1 pt `accent` border for an error and the plain `line` border for a notice; a `StatusChip` first (danger, "Couldn't sign in"; neutral, "Signed out"), then the message in `.subheadline`.
4. Label "Darkroom address" (`.caption.weight(.semibold)`, uppercase) and a `TextField` on `surface` with a `lineStrong` stroke, radius 8, height 48, 12 horizontal padding, `.font(.body.monospaced())`. Placeholder "darkroom.example.com". `.keyboardType(.URL)`, `.textContentType(.URL)`, `.textInputAutocapitalization(.never)`, `.autocorrectionDisabled()`, `.submitLabel(.go)`; return key runs the same action as the button. Below it a `.footnote` helper in `inkMuted`: "The address you open Darkroom at on a computer. Away from home, turn Tailscale on first." When the text is invalid, the helper is replaced by the error text in `danger` with the field stroke in `danger`, and the field is described to VoiceOver by that text.
5. Label "Name of this iPhone" and a second `TextField`, same style as the address field but in the body font, placeholder "Sam's iPhone", `.textContentType(.name)`, `.textInputAutocapitalization(.words)`, `.submitLabel(.go)`, with the `.footnote` helper "Shown on the People page so you can tell the family's iPhones apart." (point 9). It is prefilled from the last label used on this iPhone (`UserDefaults`), otherwise from `UIDevice.current.name` unless that is the generic "iPhone", otherwise empty. It is required: if it is empty after `DeviceSignIn.cleanedDeviceName` (which would fall back to "iPhone"), the helper is replaced by "Name this iPhone so you can find it on the People page." in `danger` with the field stroke in `danger`, and no attempt starts.
6. `PrimaryButton` "Continue with Pocket ID".

The server field is prefilled from `DeviceSession.savedServer` (a re-sign-in keeps the address) and is empty on first launch. Content is vertically top-aligned, not centred, so the keyboard does not push it; the `ScrollView` uses `.scrollDismissesKeyboard(.interactively)`.

### States

| State | What shows | Source |
| --- | --- | --- |
| Signed out, first launch | Items 1, 2, 4, 5, 6. Empty address field, name field prefilled by the rule above, button enabled. | `DeviceSession.Phase.signedOut`, `savedServer == nil` |
| Signed out, returning | Same with the saved address filled in. | `DeviceSession.savedServer` |
| Signing in | The sheet is up. Behind it the button reads "Waiting for sign-in" with a `ProgressView`, disabled; the field is disabled. | `DeviceSession.Phase.signingIn(ServerAddress)` |
| Cancelled | The sheet closed with no callback. The screen returns to signed out and shows a notice "Sign-in was cancelled." in the banner (a notice, not an error). | `ASWebAuthenticationSessionError.canceledLogin`, then `DeviceSession.cancel()` |
| Needs sign-in again | Reached from the Uploads tab (see Screen 2), not here as a state: the same screen with the notice "Your sign-in ended. Sign in again to keep uploading." | `UploadStatus.Phase.needsSignIn`, then `DeviceSession.tokenRejected(_:)` |
| Error | The banner with the message for the failure, the field kept as typed. The next tap on the button starts a new attempt with a new state. | below |

The attempt starts with `DeviceSession.begin(serverText:deviceName:)`, which returns the start URL; the `ASWebAuthenticationSession` opens it and its completion handler calls `DeviceSession.complete(callback:)` with the callback URL. The callback is read only from that completion, never from `.onOpenURL`, so a link from another app cannot finish a sign-in. The device name sent is the person's device label (point 9), cleaned by `DeviceSignIn.cleanedDeviceName`.

### Errors and copy

The title of the banner chip is "Couldn't sign in" for all of these.

| Cause | Message | Source |
| --- | --- | --- |
| Empty iPhone name | Name this iPhone so you can find it on the People page. | the label is empty after `DeviceSignIn.cleanedDeviceName` (under the name field) |
| Empty address | Enter your Darkroom's address. | `ServerAddress.Problem.empty` (shown under the field) |
| Not https | Darkroom needs an address that starts with https. | `.notHTTPS` (under the field) |
| Unreadable address | That doesn't look like an address. Try something like darkroom.example.com. | `.invalid` (under the field) |
| Sign-in came back without a token | Darkroom didn't send the sign-in back. Try again. | `SignInError.noToken` |
| State did not match | That sign-in didn't match this attempt, so the app ignored it. Try again. | `SignInError.stateMismatch` |
| Took too long | That sign-in took more than 10 minutes. Try again. | `SignInError.expired` (`DeviceSession.attemptLifetime`) |
| Keychain refused | Couldn't save the sign-in on this iPhone. Try again, and restart the iPhone if it keeps happening. | `SignInError.storage` from `complete` |
| Keychain unreadable at launch | Couldn't read the sign-in saved on this iPhone. Sign in again. | `SignInError.storage` from `restore` |
| The sheet could not open | Couldn't open the sign-in page. Try again. | any other `ASWebAuthenticationSessionError` |
| No person with this email | No person with this email. Ask whoever runs Darkroom to add your email to your person on the People page, then sign in again. | point 3 |
| Account not allowed | This account isn't allowed to use the Darkroom app. If it is yours, ask whoever runs Darkroom to add it to the family group in Pocket ID, then try again. | point 3 |

`SignInError.alreadySignedIn` and `.notSigningIn` cannot be reached from this screen (it shows only when signed out and the button is disabled while signing in); they are not given copy. A server that cannot be reached, a provider outage and a start with a bad name show the server's own page inside the sheet and end as a cancel, which is why the cancel notice also says "If the page showed a problem, tell whoever runs Darkroom." when point 3 is not adopted (see there).

### Accessibility

- The fields' labels are "Darkroom address" and "Name of this iPhone"; each hint is its helper text, and the focus order is address, name, button. Errors under the field are announced when they appear (`AccessibilityNotification.Announcement`), and the banner is `.accessibilityElement(children: .combine)` with `.accessibilityAddTraits(.isStaticText)` and focus moved to it when it appears (`@AccessibilityFocusState`).
- "Waiting for sign-in" is the button's label while signing in; the disabled trait is set.
- Title is `.accessibilityAddTraits(.isHeader)`.
- Dynamic Type: all text scales; the banner and field grow in height (`minHeight`, never a fixed height); nothing is truncated, and the helper wraps. At the largest accessibility size the screen scrolls.

## Screen 2: Uploads (status)

The home tab. It answers three questions without a tap: is it working, is anything wrong, is my phone caught up.

### Layout (top to bottom)

1. Title "Uploads" (`.title.bold()`, header trait) and a `.subheadline` `inkMuted` sentence that says the state (table below). When the person is known (point 2) a second line "Uploading as Sam".
2. **State card** (`Card`): a `StatusChip`, a title in `.title3.bold()`, a detail in `.subheadline` `inkMuted`, and, for two states, a button. While syncing the card also shows an indeterminate `ProgressView` (linear, `accent` tint, `raised` track) under the title.
3. **Sync now**: `PrimaryButton` "Sync now", directly under the state card, except when the state card carries its own button (needs sign-in, stopped), when this one becomes a `SecondaryButton`. While syncing it reads "Syncing" with a small `ProgressView`, disabled. Pulling the screen down does the same thing (`.refreshable`).
4. **Files**: section label "FILES" and a two-column grid (`LazyVGrid`, two flexible columns, 16 gap) of four count tiles. A tile is a `Card` with the number (`.title.bold()`, `.monospacedDigit()`) over the label (`.subheadline`, `inkMuted`). Under the grid a `.footnote` `inkMuted` line: "Counts are files. A Live Photo is two files and an edited photo is three."
5. **Details** (`Card` of `KeyValueRow`s separated by `raised` 1 pt dividers): "Last synced", "Server" (host, monospaced).
6. **Needs attention**, only when files failed: section label "NEEDS ATTENTION, 3" and a `Card` of up to 5 rows, then a "Show all 12" row when there are more, which pushes a list of every failed file (same row, same card). A row: file name (`.footnote.monospaced()`, `ink`, wraps, never truncated) over the reason (`.footnote`, `inkMuted`), `raised` divider between rows. A recoverable failure (point 13) adds the caption "Will try again" and a `SecondaryButton` "Try again" (44 pt); the section header gains a "Try all again" button when any row is recoverable.

### The four counts (point 1)

| Tile | Meaning | Count of |
| --- | --- | --- |
| On Darkroom | Darkroom has the file: uploaded, or already there so nothing was sent. | `uploaded` |
| Waiting to upload | Hashed, being checked, queued or sending. The same number the check-in reports, so it matches "Uploads pending" on the People page. | `hashed` + `checked` + `uploading` |
| Only in iCloud | The file is not on the iPhone, so the app does not upload it and never fetches it. | `notLocal` |
| Needs attention | Darkroom or the app gave up on the file, or the app could not read or hash it; each is listed with its reason. | `failed` (point 12 puts ingestion failures here too) |

The tiles add up to every file the app knows about, with none counted twice. When `uploading` is above 0 the Waiting tile's label gains a second line "N sending now" (`.footnote`).

### States

The state card is chosen by `UploadStatus.Phase`, then by the counts when the phase is `.idle`. The sentence under the title and the card use this copy:

| State | Sentence under the title | Chip | Card title | Card detail | Button in card |
| --- | --- | --- | --- | --- | --- |
| Up to date: idle, waiting 0, attention 0 | All photos stored on this iPhone are on Darkroom. | ok "Up to date" | Up to date | New photos upload on their own when iOS gives Darkroom background time, and whenever you open the app. | none |
| Up to date with iCloud-only files: idle, waiting 0, attention 0, iCloud-only above 0 | Everything stored on this iPhone is on Darkroom. | ok "Up to date" | Up to date | N files are only in iCloud, so Darkroom can't upload them from this iPhone. | none |
| Waiting to upload: idle, waiting above 0 | 12 files are waiting to upload. | neutral "Waiting" | Waiting to upload | Uploads start when iOS gives Darkroom background time. Tap Sync now to start them now. | none |
| Syncing: `.uploading` | Uploading. 12 files left. | neutral "Syncing" | Syncing | Uploads finish even if you leave the app. | none |
| Waiting for home network: `.waitingForHomeNetwork` | Waiting for your home network. 12 files are waiting. | warn "Waiting for home network" | Waiting for home network | Darkroom can't be reached right now. Uploads start again by themselves on home Wi-Fi or over Tailscale. Nothing is wrong. | none |
| Needs sign-in: `.needsSignIn` | Sign in again to keep uploading. | danger "Sign-in needed" | Sign-in needed | Darkroom no longer accepts this iPhone. Sign in again to keep uploading. Files already on Darkroom stay there. | "Sign in again" (primary) |
| Stopped: `.error(message)` | Uploads stopped. | danger "Stopped" | Uploads stopped | the phase's message (`UploadStatusTracker.queueUnreadable` or `.syncStopped`), then "Try again." | "Try again" (primary; runs Sync now) |
| Needs attention: idle, attention above 0, waiting 0 | 3 files need attention. | warn "Needs attention" | 3 files need attention | Darkroom or this iPhone couldn't handle these files, including ones the app couldn't read. The list below says why. | "Try all again" when any file is recoverable (point 13), else none |
| Photos access needed | Darkroom can't see your photos. | danger "Photos access needed" | Photos access needed | Allow access to all photos so Darkroom can upload them. | "Open Settings" (point 7) |

Rules that apply across rows:

- Failure with other work: the Needs attention list shows below whatever state is on top (a sync can be running while two files have failed). The state card shows the Needs attention state only when nothing else is waiting.
- Pause and charging are not states (point 5).
- First launch before the first scan has finished: use the card title "Getting ready" with the detail "Darkroom is reading this iPhone's photos. This can take a while the first time." and the neutral chip "Starting". The state ends when the first scan finishes (point 8), not when a count becomes non-zero, so an empty photo library goes to "Up to date" with 0 files. Before that signal exists the card would otherwise read "Waiting to upload" with "0 files".
- Tapping "Sign in again" opens the same `ASWebAuthenticationSession` for the saved server; on success the card returns to a normal state and a Sync now runs. If the person cancels, the card stays. (Implementation note for #35: `DeviceSession.begin` refuses while signed in, so it needs a re-sign-in path that keeps the old token until the new one is saved, or calls `tokenRejected(_:)` and shows the Sign-in screen with the notice above. The design allows either; the first keeps the counts on screen.)

### Refreshing

The screen reads `UploadStatusTracker.status()` when it appears, when the app becomes active, after `syncNow()` returns, and every 2 seconds while the phase is `.uploading`. It never polls otherwise, and a failed read shows the Stopped state (the tracker already maps a store failure to `.error(queueUnreadable)`). Sync now calls `UploadStatusTracker.syncNow()`. Note for #35: `UploadCoordinator.networkReady()` returns false without probing while the reachability backoff (30 s up to 15 min) runs, so a Sync now tapped in the waiting state would change nothing. A manual sync should skip the backoff; that is a core change for the PR that builds this screen. The server row shows the URL authority including a non-default port, never only `url.host`. A manual sync must also run the library scan (`ResourceIngester.run()`) before the upload coordinator, because the coordinator only works on files already in the store and would never find a new photo.

### Last synced (point 4)

"Last synced" reads "Not yet" before the first sync, then a relative time under 24 hours ("12 minutes ago", live, `Text(date, style: .relative)` with " ago" added by the formatter) and "Yesterday 21:30" or "2 May 18:05" after that, the same wording as the web app's `when()`.

### Accessibility

- Every tile is one element: "On Darkroom, 1,240 files". The state card is one element read as "Up to date. New photos upload on their own..."; its button stays a separate element. The sentence under the title is part of the header group.
- When the state changes while the screen is open (Syncing to Up to date, to Waiting for home network, to Sign-in needed), the app posts `AccessibilityNotification.Announcement` with the card title. Counts changing every 2 seconds during a sync are not announced.
- The 2-second refresh does not move the VoiceOver cursor: tiles keep their identity (`.id` by tile name) and only their text changes.
- The sync `ProgressView` has the label "Syncing" and no value (it is indeterminate).
- Failed rows read "IMG_0042.HEIC, the server refused it (409)". File names are not spelled out character by character.
- Chips are read as their label; the shape is hidden from VoiceOver.
- Dynamic Type: at accessibility sizes (`dynamicTypeSize.isAccessibilitySize`) the tile grid becomes one column, `KeyValueRow` stacks, and the chip wraps below the title. Cards never have a fixed height. The tab bar labels scale with the system.
- Colour is never the only signal: each tone has a shape and a word. Contrast: `ink` and `inkMuted` on `surface` are 14.4:1 and 7.5:1; `accent` on `rail` (selected tab) 5.3:1; the chip pairs `danger` on `dangerBg` 6.5:1, `warn` on `warnBg` 7.2:1, `ok` on `okBg` 7.1:1.
- Reduce Motion: no animation in the screen beyond the system `ProgressView`; state changes cross-fade at most.

### DarkroomCore types behind each element

| Element | Type and member |
| --- | --- |
| State card, chip, sentence | `UploadStatus.phase` (#166) plus `UploadStatus.count(_:)` |
| Tiles | `UploadStatus.counts` (`ResourceState` to `Int`, from `ResourceStore.counts()`) |
| "N sending now" | `UploadStatus.count(.uploading)` |
| Last synced | `UploadStatus.lastSync` (#166), persisted (point 4) |
| Needs attention rows | `UploadStatus.failures`: `FailedResource.filename` and `.reason` (#166), from `ResourceRecord.failureReason`, which holds ingestion failures too (point 12) |
| Sync now, Try again | `UploadStatusTracker.syncNow()` (#166), which runs `UploadCoordinator.run()` |
| Sign in again | `DeviceSession.begin` and `.complete`, `UploadStatusTracker.signInChanged()` (#166) |
| Server row | `DeviceSession.Phase.signedIn(ServerAddress)`, `ServerAddress.url`'s authority, host and port |
| Uploading as | `GET /api/device/me` (point 2, not built) |
| Photos access state | `PhotoLibrary` authorization (arrives with the PhotoKit adapters; no type yet) |

## Screen 3: Settings

### Layout (top to bottom)

1. Title "Settings".
2. **Account** (`Card`, label "ACCOUNT"): `KeyValueRow` "Signed in as" with the person's name (point 2; the row is left out when unknown), `KeyValueRow` "This iPhone" with the device label (point 9).
3. **Server** (`Card`, label "SERVER"): `KeyValueRow` "Address" with the host and any non-default port (the URL authority, for example `darkroom.example.com:8443`) in `.footnote.monospaced()`. Under it a `.footnote` `inkMuted` line: "To use a different Darkroom, sign out first." The address is read-only while signed in, because `DeviceSession.begin` refuses to start while signed in.
4. **Cleanup** (`Card`, label "CLEANUP"): the text "Removing photos from this iPhone is turned on or off on the People page." in `.subheadline`. Nothing else until #38, which adds the on/off value (from `EligibleReply.cleanupEnabled`) and a link to the cleanup screens.
5. `SecondaryButton` "Sign out" with `danger` text. It opens a `.confirmationDialog`: title "Sign out of Darkroom?", message "Uploads stop on this iPhone, including any already started. Files already on Darkroom stay there. To stop this iPhone for good, revoke it on the People page.", buttons "Sign out" (destructive) and "Cancel".
6. Footer, centred: "Build 0.1.0 (1)" in `.footnote.monospaced()`, `inkMuted`, with `.accessibilityIdentifier("build-version")` as the placeholder `RootView` has today (`BuildVersion.current.label`).

### States

| State | What shows |
| --- | --- |
| Normal | As above. |
| Signing out | The button is disabled for the moment `DeviceSession.signOut()` runs; it does not take long, so no spinner. |
| Sign-out failed | A `danger` `.footnote` under the button: "Couldn't remove the sign-in from this iPhone. Try again." (`SignInError.storage`). The person stays signed in. |
| Signed out | The app returns to the Sign-in screen with the address filled in and no notice. |

Signing out clears only the token on the phone (`DeviceSession.signOut()`); the device stays on Darkroom's People page until someone revokes it, and the push token is not forgotten. The dialog says so in plain words and does not claim more. The app stores the pair (server URL, `person_id` from `GET /api/device/me`, point 2) with the upload store. On any new device session, `checked` and `uploading` records go back to `hashed` with their attempt counts cleared, because the new session's server-side declaration does not include them and their next upload would be refused as not checked. `uploaded` and `failed` records are kept only when the new session's pair equals the stored pair, so nothing already on Darkroom is hashed or sent again; for a different pair they also go back to `hashed` with attempts cleared (or the store is keyed by the pair), because Darkroom for that person has none of those files and "On Darkroom" would otherwise be false. Names are never compared. Telling Darkroom that the device has signed out needs a device route and belongs with the push work.

### Accessibility

Headers carry the header trait; each `KeyValueRow` is one element ("Address, darkroom.example.com"); the sign-out button has the hint "Asks to confirm"; the dialog is a system dialog. At accessibility sizes the rows stack. The build version row is read as "Build 0.1.0, 1".

### DarkroomCore types

`DeviceSession.Phase.signedIn(ServerAddress)` for the address, `DeviceSession.signOut()` and `SignInError.storage` for sign out, `BuildVersion.current` (app target) for the footer, `EligibleReply.cleanupEnabled` for the cleanup value once #38 lands.

## Open points (all need owner sign-off)

Each is a decision the code and the existing pages cannot make. The recommended default is what the screens above are drawn with; nothing is built on a point until the owner answers it in #34.

### 1. What the hashed and pending counts mean

**Needs owner sign-off.** #34 asks for counts "hashed / pending / uploaded / not local / failed". The store has six states and the check-in reports `pending_uploads` as `hashed + checked + uploading` (`UploadCoordinator.checkin`). Shown as peers, "hashed" would be inside "pending" and counted twice. Also, only hashed files are in the store: a file waiting to be hashed is not a record, so "hashed of N" cannot be drawn without a library total the store does not have.

Recommended default: four counts that do not overlap and are named for what a person cares about: **On Darkroom** (`uploaded`), **Waiting to upload** (`hashed` + `checked` + `uploading`, the number the People page shows), **Only in iCloud** (`notLocal`) and **Needs attention** (`failed`). Hashing is not a count; a first-run line "Getting ready" covers the time before anything is in the store. They count files, not photos. Alternative: show hashed as its own tile and make Waiting `checked` + `uploading`; that makes the phone's number differ from the People page's, and is not recommended.

### 2. "Signed in as" has no device-readable source

**Needs owner sign-off.** `/api/me` is session-authenticated and returns the email, groups and subject, so a device token cannot ask who it is, and the sign-in callback carries only the token and the state. Without a source the phone cannot show which person it uploads for, which matters because that person owns everything the iPhone uploads.

Recommended default: add `GET /api/device/me` (auth kind `device`) that replies `{"person_id": "<opaque id>", "person": "Sam", "device": "Sam's iPhone"}` and nothing else: a stable opaque identifier of the person, scoped to this server (the person's id, never the email), the display name and this device's name, no email, subject or groups. The display name and device label can change and are not unique, so only `person_id` is used to decide whether a session is the same person (point 14). It needs its contract type, a `docs/api/device.md` entry, the generated files and a test that a revoked or lapsed token gets 401, in a backend pull request before #35's PR 3. The app fetches it after sign-in and on the Uploads tab's appearing, caches the name for the next launch (not in the Keychain: it is not a secret) and shows the row only while it has a name. Rejected: the callback carrying the name (it goes stale when a person is renamed and puts more in the URL); letting a device token call `/api/me` (it would hand a device the email and groups and widen a session route). Alternative if the owner would rather not add a route: drop "Signed in as" and "Uploading as" from the screens; they are the only elements that depend on it, and Settings then shows only the iPhone's name and the server.

### 3. "No person with this email" is invisible to the app

**Needs owner sign-off.** `device_callback` answers a refusal (`devices.NoPerson`, `oidc.NotAllowed`) with a web page inside the sheet and creates no device. The app only learns that the sheet closed, exactly as for a cancel, so it cannot say what happened.

Recommended default: for those two refusals, whose fix is something the person can ask for, the server answers `303` to `darkroom://signed-in#error=no_person&state=<state>` (or `error=not_allowed`) in place of the page, using the same per-attempt `state` the success redirect echoes. The app accepts it only when the state equals the attempt in progress, shows the matching message from the errors table, and creates no session. The error carries no email, name or token. Every other failure (provider unreachable, a bad name or state at the start, something wrong with the sign-in) stays a page in the sheet, because the fix is "close it and try again" and the sheet already says so. The existing pages stay the fallback for a browser. This needs a change to `device_callback`, `DeviceSignIn.callback(from:)` to parse the error fragment, and tests on both sides.

Fallback if the owner does not want a server change: the app cannot tell the cases apart, so a cancelled sheet shows the notice "Sign-in was cancelled. If the page showed a problem, tell whoever runs Darkroom." and nothing more.

### 4. What "last synced" means

**Needs owner sign-off.** Nothing records a last check-in on the phone. `UploadStatusTracker.lastSync` is set when a pass ends `.finished`, but `UploadCoordinator.checkin()` also returns `.finished` when the check-in was refused or failed, so `.finished` does not mean Darkroom heard from the phone. And `lastSync` lives in memory only, so it is empty after iOS ends the app (#108 item 74).

Recommended default: **Last synced is the last time a pass finished and Darkroom accepted its check-in**, so the phone and the People page agree, once the server field is clarified (below). It is the device's clock, shown as a relative time. To make it true: `UploadCoordinator.Result` gains a flag for "the check-in was accepted" and the tracker sets `lastSync` only when it is true; the tracker persists `lastSync` (one value beside the ingest versions file or the store, loaded when it starts, an unreadable value read as "Not yet") which is #108 item 74; and the screen does not draw a separate "last check-in" row, since it would be the same time. A pass that uploaded files but could not check in leaves the old time, which is honest: the server has not heard from the phone.

**Server side.** `devices.authenticate` advances `Device.last_checkin` on every accepted device request (an upload, a check, a push-token write), not only on `POST /api/device/checkin`. So the People page's "Last check-in" currently means "last accepted request", and the phone's value would differ from it. Options: (a) expose the server's timestamp to the app (for example in the `GET /api/device/me` reply, point 2) and show that; (b) change the server field to move only on the explicit check-in route; (c) define the local value around any accepted request and drop the claim that it equals the People page. Recommended default: (b), because "Last check-in" then means what its name says and the phone's local value can match it; it is a small change in `devices.authenticate` and its tests, and the People and Status pages need no copy change. Until it is decided the screens show only the local value and say nothing about the People page (#108 item 83).

### 5. Paused and charging states

**Needs owner sign-off.** Neither exists on the server or in the app: there is no pause setting, `BGProcessingTask` asks for the network and not for power, and no screen in #35 mentions either.

Recommended default: **out of scope.** No pause button, no "only while charging" option and no such chips are drawn; the states above are all that exists. If the owner wants either, it is a feature with its own design (a setting in Settings, `requiresExternalPower` on the processing task, a state on the Uploads tab and a way for Darkroom to tell a paused phone from a quiet one) and goes to a follow-up issue first.

### 6. Dark only, or light and dark

**Needs owner sign-off.** #34 asks for mockups "in light and dark appearance", and AGENTS.md says a new colour goes back to #16, while the web app and `docs/SPEC.md` are dark only and #16's tokens have no light set.

Recommended default: the app is **dark only**, with the web's tokens exactly. It needs no new colours and keeps one look across the web and the phone. If the owner wants a light appearance, a light token set has to be designed and signed off first, in #16, and each screen here gets a second asset-catalog value.

### 7. A "Photos access needed" state

**Needs owner sign-off.** #34 does not list a screen for photo access, but the app cannot upload without full access to the library (#35 scope) and `UploadStatus.Phase` has no case for it.

Recommended default: the Uploads tab shows the "Photos access needed" state in the table above, with "Open Settings" (`UIApplication.openSettingsURLString`), for denied, restricted and limited access alike; the first time, the system permission prompt runs on first launch after sign-in, with the usage text "Darkroom uploads your photos and videos to your own server at home." It needs a new `Phase` case (or a separate authorization value) when the PhotoKit adapters land.

### 8. When "Getting ready" ends

**Needs owner sign-off.** The first draft ended "Getting ready" when any count became non-zero, so a genuinely empty library would stay on it forever. The core has no signal for "the first scan finished" (the store holds only files already read).

Recommended default: the core records a persisted **first scan finished** value (recorded right after a successful `ResourceIngester.run()`, kept beside `lastSync`, point 4; not derived from an upload pass, which ends `.waitingForNetwork` when the first launch is offline and can finish over an empty store) and the card shows "Getting ready" until it exists. An empty library then reads "Up to date" with 0 files. It is a small core change in the PR that builds the Uploads screen.

### 9. Naming the iPhone

**Needs owner sign-off.** The sign-in sends a device name that the People page shows. From iOS 16 `UIDevice.current.name` returns the generic "iPhone" unless the app holds Apple's user-assigned-device-name entitlement, and `project.yml` declares none, so a household's phones could all appear as "iPhone" and be hard to tell apart or revoke.

Recommended default: ask the person. The sign-in screen gets a second field "Name of this iPhone" under the address (label above, `.footnote` helper "Shown on the People page, for example Sam's iPhone."), prefilled with `UIDevice.current.name` unless that is the generic "iPhone", kept in `UserDefaults`, required (non-empty after `DeviceSignIn.cleanedDeviceName`), and shown in Settings as "This iPhone". Settings lets the name be read but not changed while signed in, since the name is fixed at sign-in. The alternative, requesting the entitlement, needs an Apple approval and a rebuilt provisioning profile and is not recommended.

### 10. What sign out stops

**Needs owner sign-off.** Sign out clears only the Keychain token (`DeviceSession.signOut()`). Background `URLSession` upload tasks that are queued or running keep the bearer header already in their requests, and the server's device token stays valid, so uploads could go on after the dialog says they stop.

Recommended default: sign out first cancels every outstanding background upload task and sets the store's `uploading` files back to `checked`, then clears the token; the dialog says "Uploads stop on this iPhone, including any already started." The device is still not revoked on the server (that stays a People page action), and the dialog keeps saying so. The alternative is to leave tasks running and write "Uploads already started finish." in the dialog; it is honest but leaves a signed-out phone uploading.

### 11. Where the first-scan marker is written

**Needs owner sign-off.** See point 8. The marker must come from the scanner, not from the upload pass.

Recommended default: persist it immediately after a successful `ResourceIngester.run()`, so an offline first launch (upload pass `.waitingForNetwork`) still leaves "Getting ready", and an upload pass over an empty store never writes it by itself.

### 12. Where ingestion failures are recorded

**Needs owner sign-off.** `ResourceIngester.run()` only counts a resource it cannot name, export or hash in `IngestSummary.failed`; it creates no `ResourceRecord`. The screens read every count and the Needs attention list from the store, so such a file would be in no tile, and once the first scan has finished the screen could say "Up to date" for a photo that was never uploaded.

Recommended default: the ingester records each of those files as a `failed` `ResourceRecord` with its reason (for example "the file could not be read"), the way `notLocal` rows are stored without a hash. Then the four tiles, the Needs attention list and "Up to date" all come from the one store, with no second source to keep in step, and "Up to date" is impossible while any such file exists. A file whose version changes is scanned again and leaves `failed` when it succeeds. The alternative is to carry `IngestSummary.failed` into `UploadStatus` as a separate count that is added to Needs attention and blocks "Up to date"; it keeps the store unchanged but loses the file names and reasons after a relaunch, and is not recommended.

### 13. Retrying a file that gave up

**Needs owner sign-off.** After the retry limit (a transient server error or a timeout three times) `UploadCoordinator` moves a resource to `failed`. `UploadPlanner.checkBatches` selects only `hashed` files and `ResourceIngester.run()` skips an unchanged `failed` record, so neither Sync now nor a later pass ever tries it again, even after the server recovers, and no screen offers a retry.

Recommended default: separate **recoverable** failures (the retry limit reached: "gave up after N attempts: ..." and the server refusing a check or upload with a 5xx or 408) from **permanent** ones (a file that could not be read or hashed, a 4xx refusal, "different bytes than the hash promised"). Needs attention rows for recoverable failures carry the caption "Will try again" and a "Try again" button per row and one "Try all again" button on the section header; Sync now also retries recoverable failures once per tap. Retrying moves the file from `failed` to `hashed` (an allowed transition) and resets its attempt count, so the next pass checks it again. Permanent failures stay listed with their reason and no button; they leave the list only when the file changes. This needs a recoverable flag (or a reason code) on `ResourceRecord`, a store call to reset a failure, and a `FailedResource.canRetry` field. The alternative is one blanket retry of every failure, which repeats refusals that cannot work and is not recommended.

Known design gaps in this point, tracked on #108 for the build pull request: item 84 (the empty-name check must look at the trimmed raw input, before `cleanedDeviceName`'s "iPhone" fallback), item 85 (a per-row "Try again" needs a tracker action that takes the resource key), item 86 (read and hash failures from PhotoKit can be transient, so they count as recoverable or the library is rescanned automatically), item 87 (the cached person name must be keyed to the device session and hidden when `GET /api/device/me` fails, so a previous person's name never shows after signing in elsewhere), item 88 (the "Up to date" and "Up to date with iCloud-only files" rows overlap when iCloud-only is above 0: the more specific row wins), item 89 (sign out must call the existing `DELETE /api/device/push-token` while the bearer token is still available, so the old person stops getting alerts and a different person can register the same token without a 409, and the design must say what the person sees if that request fails), item 90 (the 4xx recovery paths stay ahead of the generic permanent rule: 404 and 409 mean a stale declaration and go back to `hashed` for another check, and 422 rehashes once before the file is treated as permanent).

### 14. Upload state across sign-ins

**Required by review; the owner already approved the surrounding design.** The app persists (server URL, `person_id`) with the upload store, where `person_id` is the stable opaque id from `GET /api/device/me` (point 2). On any new device session, `checked` and `uploading` records go back to `hashed` with attempts cleared. `uploaded` and `failed` records are kept only when the pair equals the stored one, and otherwise reset the same way (or the store is keyed by the pair). The reset compares the pair, never display names.

## Not in this proposal

- The cleanup notification and the batch screens (#38). They get their own design pull request and will draw on `CleanupFlow`, `CleanupState`, `EligibleList.totalBytes`, `RejectedAsset.message` and `PhotoDeleter`.
- The sign-in sheet's own page and the system deletion confirmation, which the system and Darkroom's server draw.
- iPad, which `project.yml` does not target (`TARGETED_DEVICE_FAMILY: 1`).

## Checking a screen built from this page

A pull request that builds a screen carries screenshots from the owner's device build, in the states above, at an iPhone SE width (375 pt) and an iPhone Pro Max width (430 pt), at the default text size and at the largest accessibility size, since the simulator in CI cannot run PhotoKit or Pocket ID. The `iOS` check proves it compiles; the owner's device run proves it looks like this.
