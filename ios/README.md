# Darkroom iPhone app

The app replaces Möbius Sync: it uploads each photo's original resources to Darkroom and, later, clears photos off the phone once Darkroom has confirmed them (`docs/SPEC.md#iphone-app`). The design is `docs/design/ios/README.md`. The sign-in screen is built; the Uploads and Settings tabs are placeholders until the rest of #35.

- `project.yml`: the XcodeGen spec. The Xcode project is generated from it and never committed.
- `Darkroom/`: the app target (SwiftUI lifecycle, iOS 17+): `DarkroomApp`, `AppDelegate` (background `URLSession` completion handler plumbing), `RootView` (the app shell: dark only, the sign-in screen while signed out, two placeholder tabs while signed in), `AppModel` (the `DeviceSession` and what the sign-in screen shows), `Theme` and `Views/` (the design's colours and shared pieces, the sign-in screen, the signed-in placeholder), `Keychain` and `KeychainSessionStorage` (the device token and server address behind `DeviceSession`).
- `Packages/DarkroomCore/`: a SwiftPM package with the logic, no UIKit or PhotoKit (Foundation and swift-crypto only): `APIClient`, `SHA1Hasher`, the request and reply models, `UploadPlanner`, `ResourceStore` with its state machine, an in-memory implementation and `SQLiteResourceStore` (the one the app persists in), `ReachabilityPolicy`, and `CleanupFlow`, the pure state machine of a cleanup round (list, confirm, delete, report) with the `CleanupAPI` calls it drives, `DeviceSession` (the sign-in state, the token and the server address behind a `SessionStorage`, with `ServerAddress` and `DeviceSignIn` for the address and the `darkroom://signed-in#token=` callback), `UploadStatusTracker` (what the upload status screen shows, over the store and an `UploadRunner`), and `CleanupRunner`, which runs a round against the API, the store and a `PhotoDeleter` (the PhotoKit adapter, the background refresh and the notification come with the app's screens).
- `Darkroom/Background/`: the wake-ups that run the upload check without the app open (#60) and push registration (#59). `BackgroundWork` registers and schedules the nightly `BGProcessingTask` (`<bundle id>.nightly`) and holds the `BackgroundCheckHook` the upload code installs its check into; `PushCenter` registers for remote notifications and keeps Darkroom's copy of the token current while signed in; `AppDelegate+Push` receives the token and silent pushes. The `aps-environment` entitlement is in `Config/Darkroom-Debug.entitlements` (`development`) and `Config/Darkroom-Release.entitlements` (`production`), set per configuration in `project.yml`. In `DarkroomCore`: `PushClient` (the push-token routes), `PushRegistrar` and `SignOutFlow` (forget the token, stop uploads, clear the session), and `NightlyProcessing` and `SilentPush` (scheduling, expiry and the time limit, over small protocols so `swift test` covers them).
- `Config/Base.xcconfig`, `Local.xcconfig.example`: build settings. Your own `Local.xcconfig` is gitignored.

## First build on a Mac

1. `brew install xcodegen`
2. `cp ios/Local.xcconfig.example ios/Local.xcconfig`, then set `DARKROOM_BUNDLE_ID` (a reverse-DNS id you own) and `DARKROOM_TEAM_ID` (your Apple Developer team id). Nothing in the repository holds either.
3. `cd ios && xcodegen generate`, then open `Darkroom.xcodeproj`.
4. Signing: the target uses automatic signing with your team. Select your iPhone as the run destination and press Run (turn on Developer Mode on the phone when asked, and trust the developer profile under Settings, General, VPN & Device Management).
5. The app launches on the sign-in screen. Once signed in, the Settings tab shows `Build <version> (<build>)`.

The background task identifiers are `<bundle id>.refresh`, `<bundle id>.hash` and `<bundle id>.nightly`; all are listed in the generated Info.plist.

## Installing on the family's iPhones

- TestFlight: with no Mac, `.github/workflows/ios-release.yml` archives, signs and uploads from GitHub Actions; the one-time Apple setup, the release steps and the key's rotation are in [docs/deploy/ios-release.md](../docs/deploy/ios-release.md). With a Mac, archive in Xcode (Product, Archive) and upload to App Store Connect. Either way, add the family as internal testers. Builds last 90 days and push alerts need TestFlight or a production build (APNs production).
- Ad hoc: add each iPhone's UDID to your developer account, archive with an ad hoc distribution profile and install through Xcode's Devices window or Apple Configurator. The profile lapses yearly.

Which one to use is decided in #10 item 7.

## Tests

`swift test --package-path ios/Packages/DarkroomCore` runs the DarkroomCore tests on macOS, and on Linux where a Swift 6 toolchain exists (it needs no Apple frameworks). They cover the planner, the hasher against known SHA-1 vectors, the API client against a stubbed `URLProtocol`, the resource state machine, the cleanup flow (its rules are listed in `.github/agent-review/invariants.md`) and both stores (a shared contract, plus the SQLite store reopened from disk) and the reachability policy. The SQLite store and its tests build only where the `SQLite3` module exists (Apple platforms), so a Linux run skips them. The app target has no tests of its own.

`swiftformat` reads `ios/.swiftformat`.

## The `iOS` check

`.github/workflows/ios.yml` (workflow `iOS`, a required workflow of the merge gate) runs on pull requests and pushes to `main`:

1. A `changes` job on Linux decides whether `ios/` or the workflow file changed. If not, the `build` job is skipped, a skipped job counts as passed, and backend pull requests never wait on macOS.
2. The `build` job runs on `macos-15` (pinned, so Xcode does not change under a pull request): select the Xcode version in `ios/.xcode-version`, install XcodeGen and xcbeautify at pinned versions (checksums verified), `xcodegen generate`, `swift test` for DarkroomCore, then an unsigned `xcodebuild` build for `generic/platform=iOS Simulator` with `CODE_SIGNING_ALLOWED=NO`. No team id, certificate or secret is involved.
3. Both steps pipe through `xcbeautify --renderer github-actions`, so a compile error becomes a `file:line` annotation on the pull request. The `.xcresult` is uploaded when the build fails. SwiftPM and DerivedData are cached on the Xcode version and `Package.resolved`.

Cost: Darkroom is public, so GitHub-hosted runner minutes are free. If the repository ever goes private, each macOS minute counts ten times against the account's included minutes. Measured durations are in the table below.

| Run | Duration |
| --- | --- |
| Cold cache | not yet measured |
| Warm cache | not yet measured |

`ios-release.yml` is not a check: it runs only on an `ios-v*` tag or a manual dispatch from `main`, in the `ios-release` environment, and is described in [docs/deploy/ios-release.md](../docs/deploy/ios-release.md).

Signing, PhotoKit, background tasks and notifications cannot run in CI, so the owner's Mac and device checks stay.
