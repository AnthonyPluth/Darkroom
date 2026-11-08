# Alerts on a phone (ntfy)

Darkroom can push its alerts to a phone through [ntfy](https://ntfy.sh): the public server or one you run on Unraid, with the ntfy app subscribed to a topic. Without these settings alerts are shown on the Status page and recorded as `alert_disabled`, as before.

| Variable | What it is |
| --- | --- |
| `NTFY_URL` | The ntfy server, for example `https://ntfy.sh` or `http://unraid.lan:8080`. `http://` only for a server on the LAN. |
| `NTFY_TOPIC` | The topic, sealed (below). On the public server the topic name is what keeps the alerts to you, so pick a long random one. |
| `NTFY_TOKEN` | Optional: an ntfy access token for a server that requires sign-in, sealed. |

The topic and the token are sealed with `DARKROOM_SECRET_KEY`, like the other credentials Darkroom keeps: run `printf %s 'the-topic' | python run.py seal-secret` in the container and put what it prints (`enc:v1:…`) in `.env`. Seal them again if the secret key changes. `python run.py check-config` checks the settings and says where alerts go, naming only the server's host.

What is sent: the alert kind's fixed title (for example "Google sign-in needed"), a fixed line asking you to open the Status page, and a link to `DARKROOM_PUBLIC_URL`; never an email address, name, file name, count or the alert's own text. When an alert that repeats is closed, a short "Resolved" note follows; one-shot alerts (an import finished, a renewal, a failure) have none.

How it is sent: opening, repeating or closing an alert queues it, and a job sends what is queued every minute, outside the job that raised the alert, so a slow or unreachable server holds nothing up (each send gives up after 10 seconds). While the server can't be reached, or answers 429 or 5xx, the send is tried again every 5 minutes for an hour and then recorded as `alert_failed`; any other refusal (a wrong token, say) is recorded as `alert_failed` at once. A delivered note is recorded as `alert_sent` (with `, resolved` for a close note). The next repeat of an open alert tries again either way. Logs name only the status code or the error's type, never the topic, the token or the server's address.
