# Alerts by email (Resend)

Darkroom can email its alerts to one address, the owner's, through [Resend](https://resend.com). Without these settings alerts are shown on the Status page and recorded as `alert_disabled`.

| Variable | What it is |
| --- | --- |
| `RESEND_API_KEY` | A Resend API key (a "sending access" key is enough), sealed (below). |
| `ALERT_EMAIL_FROM` | The sender: a bare address on a domain you have verified in Resend, for example `alerts@mail.example.com`. |
| `ALERT_EMAIL_TO` | The one address that receives every alert. |

All three or none: a partly set group is a configuration error. The key is sealed with Darkroom's secret key, like the other credentials Darkroom keeps (the `secret-key` file Darkroom generates in `DARKROOM_DATA_DIR` on first start, or `DARKROOM_SECRET_KEY` when it is set): once the service has started, run `printf %s 're_...' | python run.py seal-secret` in the container and put what it prints (`enc:v1:...`) in `.env`. Seal it again if the secret key changes. `python run.py check-config` checks the settings and says whether alerts are emailed; it never prints the key or either address.

What is sent: a subject that is the alert kind's fixed title (for example "Google sign-in needed"), a fixed line asking you to open the Status page, and a link to `DARKROOM_PUBLIC_URL` (for "Google sign-in needed" and "Wrong Google account", which you answer in the remote browser, a link to its Remote browser page, `<DARKROOM_PUBLIC_URL>/browser`, behind Darkroom's sign-in); never an email address, name, file name, count or the alert's own text. When an alert that repeats is closed, a short "Resolved" email follows; one-shot alerts (an import finished, a renewal, a failure) have none.

How it is sent: opening, repeating or closing an alert queues it, and a job sends what is queued every minute, outside the job that raised the alert, so a slow or unreachable Resend holds nothing up (each send gives up after 10 seconds). While Resend can't be reached, or answers 429 or 5xx, the send is tried again every 5 minutes for an hour and then recorded as `alert_failed`; any other refusal (a wrong key, an unverified sending domain) is recorded as `alert_failed` at once. A delivered email is recorded as `alert_sent` (with `, resolved` for a close email). The next repeat of an open alert tries again either way. Logs name only the status code or the error's type, never the key, an address or Resend's reply. Each email carries an `Idempotency-Key` (an HMAC, keyed from the secret key, of the alert's id, when it opened, its repeat number and whether it is the open or the Resolved email with when it closed, so two Darkroom instances or a reset database never share a key; nothing personal, never logged), the same on every retry of one email and different for the next repeat and for the Resolved email. If Resend accepts a send but the answer is lost, the retry therefore does not send a second email (Resend remembers a key for 24 hours; the retries stop after one hour). A `409` from Resend is retried like a `429`. "Sent" means Resend accepted the request, not that the email reached the inbox.

## What you need for prompt emails to arrive

When a download needs you to verify it's you, or a job needs a Google sign-in, the job waits ("waiting for you", reason `verify` or `sign_in`) and opens the "Google sign-in needed" alert at once. It reaches your inbox only when all of this is in place:

- [ ] `RESEND_API_KEY`, `ALERT_EMAIL_FROM` and `ALERT_EMAIL_TO` are all set (all three or none).
- [ ] `RESEND_API_KEY` is the sealed form (`enc:v1:...`) printed by `python run.py seal-secret` with this Darkroom's secret key, not the raw `re_...` key, and was sealed again after any change of the secret key.
- [ ] The sender's domain is verified in Resend.
- [ ] `DARKROOM_PUBLIC_URL` is the address you open Darkroom at from your phone (`https://...`), so the email's link to the Remote browser page works where you read it. Without it the email has no link.
- [ ] The remote browser view is available (the Remote browser page shows the browser, not a message that the view is unavailable).
- [ ] `python run.py check-config` says alerts are emailed, and after the next alert the event log shows `alert_sent` rather than `alert_disabled` or `alert_failed`.

The email says only that Darkroom needs a look and links to the page; open it, answer Google's prompt in the remote browser, and the job carries on by itself. A "Resolved" email follows once the job has resumed.
