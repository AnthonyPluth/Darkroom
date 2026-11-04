# Darkroom iPhone app

The app replaces Möbius Sync: it uploads each photo's original resources to Darkroom and, later, clears photos off the phone once Darkroom has confirmed them (`docs/SPEC.md#iphone-app`). This directory holds the project setup only. The real screens come with #35 against the design signed off in #34.

- `project.yml`: the XcodeGen spec. The Xcode project is generated from it and never committed.
- `Darkroom/`: the app target (SwiftUI lifecycle, iOS 17+): `DarkroomApp`, `AppDelegate` (background `URLSession` completion handler plumbing), a placeholder `RootView` that shows only the build version, and `Keychain`.
- `Packages/DarkroomCore/`: a SwiftPM package with the logic, no UIKit or PhotoKit (Foundation and swift-crypto only): `APIClient`, `SHA1Hasher`, the request and reply models, `UploadPlanner`, `ResourceStore` with an in-memory implementation, `ReachabilityPolicy` and the `CleanupFlow` placeholder.
- `Config/Base.xcconfig`, `Local.xcconfig.example`: build settings. Your own `Local.xcconfig` is gitignored.

## First build on a Mac

1. `brew install xcodegen`
2. `cp ios/Local.xcconfig.example ios/Local.xcconfig`, then set `DARKROOM_BUNDLE_ID` (a reverse-DNS id you own) and `DARKROOM_TEAM_ID` (your Apple Developer team id). Nothing in the repository holds either.
3. `cd ios && xcodegen generate`, then open `Darkroom.xcodeproj`.
4. Signing: the target uses automatic signing with your team. Select your iPhone as the run destination and press Run (turn on Developer Mode on the phone when asked, and trust the developer profile under Settings, General, VPN & Device Management).
5. The placeholder app launches and shows `Build <version> (<build>)`.

The background task identifiers are `<bundle id>.refresh` and `<bundle id>.hash`; both are listed in the generated Info.plist.

## Installing on the family's iPhones

- TestFlight: archive in Xcode (Product, Archive), upload to App Store Connect, add the family as internal testers. Builds last 90 days and push alerts need TestFlight or a production build (APNs production).
- Ad hoc: add each iPhone's UDID to your developer account, archive with an ad hoc distribution profile and install through Xcode's Devices window or Apple Configurator. The profile lapses yearly.

Which one to use is decided in #10 item 7.

## Tests

`swift test --package-path ios/Packages/DarkroomCore` runs the DarkroomCore tests on macOS, and on Linux where a Swift 6 toolchain exists (it needs no Apple frameworks). They cover the planner, the hasher against known SHA-1 vectors, the API client against a stubbed `URLProtocol`, the store and the reachability policy. The app target has no tests of its own.

`swiftformat` reads `ios/.swiftformat`.

## The `iOS` check

`.github/workflows/ios.yml` (workflow `iOS`, a required workflow of the merge gate) runs on pull requests and pushes to `main`:

1. A `changes` job on Linux decides whether `ios/` or the workflow file changed. If not, the `build` job is skipped, a skipped job counts as passed, and backend pull requests never wait on macOS.
2. The `build` job runs on `macos-15` (pinned, so Xcode does not change under a pull request): select the Xcode version in `ios/.xcode-version`, install XcodeGen and xcbeautify at pinned versions (checksums verified), `xcodegen generate`, `swift test` for DarkroomCore, then an unsigned `xcodebuild` build for `generic/platform=iOS Simulator` with `CODE_SIGNING_ALLOWED=NO`. No team id, certificate or secret is involved.
3. Both steps pipe through `xcbeautify --renderer github-actions`, so a compile error becomes a `file:line` annotation on the pull request. The `.xcresult` is uploaded when the build fails. SwiftPM and DerivedData are cached on the Xcode version and `Package.resolved`.

Cost: Darkroom is private, so each macOS minute counts ten times against the account's included minutes. Measured durations are in the table below.

| Run | Duration |
| --- | --- |
| Cold cache | not yet measured |
| Warm cache | not yet measured |

Signing, PhotoKit, background tasks and notifications cannot run in CI, so the owner's Mac and device checks stay.
