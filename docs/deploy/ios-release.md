# iPhone release (TestFlight, from GitHub Actions)

`.github/workflows/ios-release.yml` archives, signs and uploads the iPhone app to TestFlight from a GitHub-hosted `macos-26` runner, so no Mac is needed (#160). Signing is cloud-managed: `xcodebuild -allowProvisioningUpdates` with an App Store Connect API key asks Apple for the distribution certificate and profile, so none is stored anywhere.

The unsigned build and tests that pull requests need are `ios.yml`'s (the `iOS` check). They use no secret and keep working whatever is or isn't set up here.

## What the workflow does

1. `plan` (Linux, no environment, no secret): accepts only an `ios-v<major>.<minor>.<patch>` tag push or a manual dispatch from `main`, takes the marketing version from the tag (or the dispatch's `version`), and checks the commit is on `main`. Any other trigger fails here with the reason.
2. `release` (`macos-26`, `environment: ios-release`): waits for the owner's approval, then checks the configuration and the app's icon (below), selects Xcode from `ios/.xcode-version-release`, installs XcodeGen and xcbeautify at the pins and checksums `ios.yml` uses, writes `ios/Local.xcconfig` from the variables, generates the project, runs the DarkroomCore tests, writes the App Store Connect key, archives `Release` for `generic/platform=iOS` with `MARKETING_VERSION` from the tag and `CURRENT_PROJECT_VERSION` from the run number, checks the archive carries both, exports the signed `.ipa` and, unless this is a dry run, uploads it with `xcrun altool` and checks the result with `ios/release/check-upload.sh`.
3. An `always()` step removes the key file, the archive and the export.

A tag push always uploads. A manual dispatch uploads only when its `upload` input is `true`; the default `false` archives and exports (so signing is exercised) and uploads nothing.

## App icon

The app has its App Store icon: option 3, Negative (#175), as an `AppIcon` set in `ios/Darkroom/Assets.xcassets` with `ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon` in `ios/project.yml`. App Store Connect rejects an iOS build without one, so the first step of a release (`ios/release/check-icon.sh`, run by the configuration check) still checks that the set holds an opaque 1024 x 1024 PNG, and a run that would upload (a tag, or a dispatch with `upload` on) fails there with the missing pieces named, before the key is written or anything is signed. A dry run only warns. The check does not judge the design; whether Apple accepts the finished build is still only known from a real upload.

## Xcode version

Apple accepts iOS uploads only from apps built with Xcode 26 or later (App Store Connect's "Supported Xcode versions"). The `iOS` check still builds with the Xcode in `ios/.xcode-version` (16.4, on `macos-15`), so the release has its own pin, `ios/.xcode-version-release` (26.6, on `macos-26`, whose image carries it), and the two move independently. The DarkroomCore package declares swift-tools-version 5.9, so it builds on both; the first release run is the proof that the app and its tests compile on Xcode 26. When Apple raises the minimum, change the release pin and runner; moving the `iOS` check forward is a separate change.

## What the workflow keeps from the public

Darkroom is a public repository, so the workflow has no `pull_request` or `pull_request_target` trigger: a pull request, including one from a fork, can't start it or read its secrets (`tools/app_checks.py`'s `ios_release_workflow` fails a pull request that adds either trigger, a write permission, a missing `environment: ios-release`, or toolchain pins that differ from `ios.yml`'s). The workflow declares `permissions: {}` and each job `contents: read`, pins every action by SHA and checks out with `persist-credentials: false`. The three secrets exist only in the `ios-release` environment (limited to `main` and `ios-v*` tags, the owner as required reviewer) and are passed only to the steps that use them: the configuration check, archive, export and upload. The key is written by `ios/release/write-key.sh` only after the third-party tools have been installed and the tests have run, just before the archive, to `$RUNNER_TEMP/asc/AuthKey_<key id>.p8` with mode 600 in a mode 700 folder, no third-party binary runs while it exists (the archive and export steps log `xcodebuild`'s raw output instead of piping it through xcbeautify; only Apple's tools run with the key on disk), and nothing prints a value. The bundle id and team id are not secrets (every build carries them) but the repository doesn't commit them: they are passed only to the steps that need them (the configuration check, the `ios/Local.xcconfig` write and the export), so those steps' headers show them in the public log, and the check masks them in all output after it. Failed runs upload no artifact: an `.xcresult` would be public.

## One-time setup (owner)

Apple Developer account:

1. Enroll in the Apple Developer Program.
2. Certificates, Identifiers & Profiles, Identifiers: create the App ID for the bundle id you choose (a reverse-DNS id you own), with **Push Notifications** enabled. The app declares the `aps-environment` entitlement (`ios/Config/Darkroom-Release.entitlements`, `production`, for the Release archive TestFlight gets; `Darkroom-Debug.entitlements`, `development`, for builds run from Xcode), so the profile must carry the capability: enable it before the first signed build. With cloud-managed signing the archive step asks Apple for a profile that includes it; whether Apple adds the capability to the App ID itself or the step fails is not verified (see the list at the end). If you enabled it after an earlier profile was made, run again so the profile is regenerated.
3. App Store Connect, Apps: create the app record for that bundle id.
4. Users and Access, Integrations, App Store Connect API: create a team key with the **Admin** role. Cloud-managed distribution certificates and profiles are created and used on the key's behalf, which Apple limits to Account Holders and Admins (Apple's cloud-managed certificates page: "Required role: Account Holder or Admin", with extra permissions for other roles); an App Manager key can upload a build but the archive and export steps would likely fail on signing. The cost is that an Admin key can do more than upload (manage users, certificates, apps), so keep it only in the `ios-release` environment, and revoke and replace it if it may have leaked. If you prefer least privilege, you can try an App Manager key first in a dry run (optional); it works only if Apple's role permissions let it use cloud signing for your team, and you fall back to Admin if the archive step fails on signing. Note the **Issuer ID** (a UUID shown above the keys) and the **Key ID** (10 characters), and download `AuthKey_<key id>.p8`: Apple allows the download once.
5. TestFlight: add the family as internal testers (they need an Apple ID invited under Users and Access).
6. Cloud-managed signing may need at least one device registered in the developer account (Devices); if the archive step fails asking for one, register an iPhone's UDID and run again.

GitHub (Settings, Environments, `ios-release`; the environment itself comes from Infrastructure):

| Where | Name | Value |
| --- | --- | --- |
| Environment secret | `ASC_KEY_ID` | The key's 10-character ID. |
| Environment secret | `ASC_ISSUER_ID` | The issuer UUID. |
| Environment secret | `ASC_KEY_P8` | The whole contents of the `.p8` file, `-----BEGIN PRIVATE KEY-----` line to `-----END PRIVATE KEY-----` line. |
| Actions variable (repository or environment) | `DARKROOM_BUNDLE_ID` | The bundle id of the App ID. |
| Actions variable (repository or environment) | `DARKROOM_TEAM_ID` | The 10-character team id. |

Add the secrets to the environment, never as repository secrets, which any workflow on any branch could read. If any of the five is missing or malformed, the configuration check, the first step of the `release` job, fails and names it (secrets by name, never by value) in the annotation and the run summary; nothing is built.

## Cutting a release

1. Merge what should ship to `main`.
2. Tag the merge commit and push the tag: `git tag ios-v0.1.0 <sha>` and `git push origin ios-v0.1.0`. The marketing version is `0.1.0`, the build number is the run number, and the build appears in TestFlight as `0.1.0 (<run number>)` after Apple finishes processing it. The app's placeholder screen shows `Build 0.1.0 (<run number>)`.
3. Open the run (Actions, iOS release) and approve the `ios-release` deployment. The tag must be on a commit that is on `main`; otherwise `plan` fails.
4. Testers update in TestFlight. Builds expire after 90 days.

A dry run: Actions, iOS release, Run workflow on `main`, set `version` and leave `upload` off. It still waits for the approval and still needs all five values, because it signs.

A build number must be higher than any earlier build of the same marketing version. Re-running a failed run reuses its run number, so after an upload that Apple rejected for that reason, start a new run (a new dispatch with `upload` on, or a new tag).

## Rotating the key

1. In App Store Connect, create a new Admin key and download its `.p8`.
2. Replace `ASC_KEY_ID`, `ASC_ISSUER_ID` (unchanged if the team is the same) and `ASC_KEY_P8` in the `ios-release` environment.
3. Run a dry run from `main` to confirm the new key signs.
4. Revoke the old key in App Store Connect.

If the key may have leaked (a log, a pull request, a laptop), revoke it first, then rotate. A revoked key makes the next run fail at the archive step; nothing already in TestFlight changes.

## Not verified against Apple's documentation

Test these with the first real key (a dry run covers the first four):

- That the archive carries `aps-environment` `production` from `ios/Config/Darkroom-Release.entitlements` and that cloud-managed signing produces a profile with Push Notifications (or fails clearly when the App ID lacks it).
- That an Admin key is what cloud-managed signing needs through `-allowProvisioningUpdates` (Apple's pages confirm the Account Holder or Admin requirement for cloud-managed certificates but do not name the API key case), and whether App Manager works.
- That the archive step doesn't ask for a registered device.
- That `xcodebuild -exportArchive` with `method: app-store-connect`, `destination: export` and automatic signing yields `Darkroom.ipa` under Xcode 26.6.
- That the app and DarkroomCore tests compile on Xcode 26.6 on `macos-26`.
- That `xcrun altool --upload-app` with `API_PRIVATE_KEYS_DIR` accepts the key and the upload appears in TestFlight (Apple's upload page says altool is supported).
- That `ios/release/check-upload.sh`'s error and success matching, including the mixed case of a transient `ERROR:` followed by `UPLOAD SUCCEEDED` and a Delivery UUID (reported with Xcode 26.5, fastlane #30033, which the check currently fails; deferred as #108 item 92), is untested against a real upload.
- That `ios/release/check-upload.sh` reads altool's real output right. Xcode 26's `altool` can print `ERROR:` records and still exit 0 (fastlane #29740), so the step fails on a non-zero exit, on any error record or `product-errors`, and when none of altool's success texts (`No errors uploading`, `UPLOAD SUCCEEDED`, `Successfully uploaded`, `Delivery UUID`) appears, and the summary runs only after that. The error and success texts are matched from fastlane's report and altool's older output, not from a real run: a real upload that fails this check although TestFlight received the build means a success text needs adding, and a duplicate build number must make it fail.
- That the `macos-26` image still carries Xcode 26.6 (checked against the runner-images readme today).

## Not yet proven

The signed path has not run: the key, the bundle id, the team id and the environment's secrets did not exist when the workflow was written. What was checked without them: zizmor and the repository's workflow conventions, the configuration, key-writing and plan scripts (`tests/test_ios_release.py`: the complete case, the key's mode, each missing or malformed value, and that no value is printed) and the rule on triggers and permissions. The first run with the secrets is the real test of the archive, export and upload steps; read its annotations and fix them in a follow-up.
