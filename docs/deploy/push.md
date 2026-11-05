# Push alerts (APNs)

Darkroom sends its alerts to the Darkroom iPhone app through Apple's push service, production only (TestFlight builds). Darkroom only makes outbound calls to `api.push.apple.com` on port 443 over HTTP/2; nothing new is exposed to the internet. A push carries the alert's kind, its id and the fixed title for that kind, never an email address, name, file name or count.

| Variable | What it is |
| --- | --- |
| `APNS_KEY_ID` | The 10-character key ID Apple shows for the auth key. |
| `APNS_TEAM_ID` | The Apple Developer team ID (10 characters). |
| `APNS_BUNDLE_ID` | The iPhone app's bundle ID, the push topic. |
| `APNS_KEY_FILE` | Absolute path, inside the container, to the `.p8` auth key. |

Set all four or none; with none set, alerts are recorded as events and not delivered.

Once they are set, an alert goes to the iPhones of the person it concerns (an alert about a Google account goes to every person that account serves), and only to an iPhone that is not revoked, whose owner can still sign in and that has sent its push token. A job sends the queued pushes every minute, checking each device again right before handing its token to APNs (a device revoked by then, holding a different token, whose owner can no longer sign in, or whose person the alert no longer concerns, for example because the account no longer serves them, is skipped and recorded as `push_skipped`); while Apple can't be reached it tries again every 5 minutes for an hour. Each push is recorded as an event (`push_sent`, `push_failed`, `push_token_invalid`), and so is what happened to the alert (`alert_sent`, `alert_failed`, `alert_no_device`, or `alert_disabled` when the settings were removed meanwhile). A token Apple reports invalid is forgotten, unless the iPhone sent it again after the time Apple gives. To set it up:

1. In the Apple Developer account, under Certificates, Identifiers & Profiles, Keys, create a key with Apple Push Notifications service enabled, and download its `AuthKey_<key id>.p8` (Apple lets you download it once).
2. Put it next to Darkroom's data on Unraid, readable only by the container's user (`chmod 600`), and mount it read-only, for example `./secrets/AuthKey.p8:/run/secrets/apns.p8:ro`, with `APNS_KEY_FILE=/run/secrets/apns.p8`.
3. Run `python run.py check-config`. It checks the IDs' format, that the file is a regular file only its owner can read, and that it holds an unencrypted P-256 key, without printing any of the key.

Darkroom reads the key only in `darkroom/providers/apns.py` (Semgrep's `darkroom-apns-key-one-reader`), signs a provider token with it (ES256, reused for 45 minutes, as Apple asks), and never logs or stores the key or the token.

## Wake pushes

The job that sends alerts also sends a silent wake push to an iPhone that has not checked in for more than a day (any signed-in request from the app counts as a check-in), so the app can look for new photos and videos without being opened (#60). A wake goes to the same production host, only to an iPhone that is not revoked, whose owner can still sign in and that has sent its push token, and is checked again right before the token goes to APNs (it is skipped if the iPhone checked in meanwhile). It is a `background` push with `content-available` and nothing else: no alert, sound, badge, title, kind, id, name or count.

Each iPhone is sent at most one wake per 24 hours, whether or not Apple accepted it: the time is recorded before sending, a failed wake is not retried before the next interval, and a check-in does not reset it. A wake is recorded as an event (`wake_sent`, `wake_failed`, or `wake_token_invalid`, which also forgets the token as above). With the settings above unset, no wake is sent. iOS throttles silent pushes and does not deliver them to an app the user force-quit, so a wake supplements the app's nightly processing task.
