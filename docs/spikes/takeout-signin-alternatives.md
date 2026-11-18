# Spike: signing in to Takeout without a passkey in the remote browser

Spike issue #432, written 2026-11-18 from documentation and the code alone: nothing was run against a Google account for it, and every item below that depends on how Google behaves is marked KNOWN (with its source) or UNVERIFIED. Related: #284 (the one container) and #424 ([per-part ingest](../design/per-part-ingest.md)).

## The problem

1. Google asks for the owner's passkey when they sign in to Takeout. The passkey cannot be used in the Chrome that runs inside Darkroom's container and is shown through the Remote browser page (noVNC): that browser runs in the container on the server, which has no platform authenticator, no security key and no Bluetooth device passed in, and noVNC only carries pixels, keys and mouse events.
2. During a full export (about 25 parts of about 53 GB, about 1.3 TB) Google asks for the account password again before every part. Since v4.8.2 (#422) the download job waits for the owner to answer that prompt in the remote browser (waiting reason `verify`, `wait_for_verification` in `darkroom_browser/jobs/download.py`) and types nothing.
3. Storing a sealed password and typing it automatically is ruled out: it breaks promise 4 (the worker never types a Google password or 2FA code, held by `tests/browser/test_no_credentials.py` and `selectors.TYPEABLE`, which is empty); it does not answer a 2-step prompt or a passkey; automated sign-in risks Google's unusual-activity checks; and a whole-account password is a high-value secret. Nothing below proposes it.

The owner's constraints for this page: no stored password, no cloud storage as an intermediary (the owner does not have about 1.3 TB free in Google, Dropbox, OneDrive or Box), and no browser extension.

## Result in one paragraph

The two problems have different answers. Problem 1 is a choice on Google's sign-in screen, and is KNOWN to be solvable without touching the passkey: "Try another way" offers the password and the account's other second steps, and turning off "Skip password when possible" makes the password the default (option a). Problem 2 is the one that costs the owner time, and the facts that would settle it are UNVERIFIED: Google documents only that downloading your data is a sensitive action and that it asks for the password again "if you haven't recently", without saying how recent. If "recently" is a time window measured from the verification, Darkroom's one-part-at-a-time download is the cause: a 53 GB part takes longer than any plausible window at home-connection speeds, so every next click lands outside it. Starting several parts right after one verification would then cut 25 prompts to a few. The first step is the owner's experiment in [section 3](#3-the-first-experiment-the-owner-runs) on a disposable account in their own Chrome, which costs no Darkroom change. Session handoff (c), a security key (e), a lighter viewer (f), virtual authenticators and synced passkeys (g), Google's official Data Portability API (i, which has no Google Photos scope) and Takeout's private endpoints (j) each either do not help with problem 2, or cost more than they save, or need promise 4 amended.

## 1. What is known about Google's prompts

| Fact | Status and source |
| --- | --- |
| Each archive expires after about 7 days and can be downloaded 5 times | KNOWN: [Takeout help](https://support.google.com/accounts/answer/3024190) ("Your archive expires in about 7 days", "We only allow each archive to be downloaded 5 times") |
| Takeout asks for the password again before a download "if you haven't recently", and with 2-Step Verification "you may also be asked to complete an additional verification step" | KNOWN: the same page. How recent is not stated |
| "Downloading your data" is one of Google's sensitive actions, for which it may ask you to verify it's you | KNOWN: [Verify it's you when you complete a sensitive action](https://support.google.com/accounts/answer/7162782) |
| A device, phone number, security key or passkey associated with the account for less than 7 days can get a sensitive action blocked for 7 days | KNOWN: the same page. This matters for a disposable test account and for any new authenticator (option e) |
| The length of the "recent" window for Takeout | UNVERIFIED. No Google page found states it. The only documented fixed interval found is Workspace's Admin console, which asks again every hour ([Workspace security challenges](https://knowledge.workspace.google.com/admin/security/protect-google-workspace-accounts-with-security-challenges)); it says nothing about Takeout |
| Whether the window runs from the verification, from the last sensitive action, or is per download or per part link | UNVERIFIED. A third-party tool notes that "Google may require repeated sign-ins" ([gargantuan-takeout-rocket](https://github.com/nelsonjchen/gargantuan-takeout-rocket)); guides repeat that Google asks per file. None of this is Google's statement |
| Google prompts (tap on a signed-in phone) confirm it's you at sign-in, in addition to or instead of a password | KNOWN: [Sign in with Google prompts](https://support.google.com/accounts/answer/7026266). Whether Takeout's re-verification offers one is UNVERIFIED |
| A passkey on a phone can be used from a computer by QR code, which needs Bluetooth on both | KNOWN: [Sign in with a passkey](https://support.google.com/accounts/answer/13548313). No Bluetooth device is passed into the container, so the container's Chrome cannot use it (whether it would work with one passed in is UNVERIFIED) |

Darkroom's own record: the owner reports a password prompt before each of the 25 parts, and Darkroom fetches one part at a time (`download_export` loops over the parts and `fetch_part` waits for each to finish before clicking the next). A 53 GB part takes about 14 minutes at 500 Mbit/s and about 71 minutes at 100 Mbit/s (53 GB × 8 / rate), so the next click always comes at least that long after the previous verification.

## 2. The options

### a. The password path on Google's sign-in

What it is. Google's sign-in is passkey-first by default once a passkey exists ("By default, when you create a passkey you opt in to a passkey-first, password-less sign in experience", [2-Step Verification](https://support.google.com/accounts/answer/185839)). On the passkey screen, "Try another way" returns to the account's other choices, and turning off **Skip password when possible** under "How you sign in to Google" makes the password the default ([passkey help](https://support.google.com/accounts/answer/13548313)). Removing the passkey (Security, "Passkeys and security keys", Remove) is a third way, and the only one that changes the account's protection.

The screens. KNOWN from Google's help: a passkey screen with "Try another way"; with the setting off, the password first, then the second step. The second-step choices listed by Google are Google prompts, passkeys and security keys, an authenticator app, a text or voice code, a QR code and backup codes ([2-Step Verification](https://support.google.com/accounts/answer/185839)). UNVERIFIED: which of them a given account is offered, in what order, and which the Takeout re-verification offers (it may offer the passkey first again; whether "Try another way" is there too, and whether it lists "Tap Yes on your phone"). Those depend on the account and on Google's risk checks, so the experiment records them rather than this page guessing.

How it fits Darkroom. Unchanged: the owner signs in in the remote browser; the worker waits (`sign_in`, `verify`) and types nothing. The setting is an account choice the owner makes once, on their own computer.

By hand. Once: turn off "Skip password when possible" (or press "Try another way" at each sign-in). Per sign-in: password and the second step. Per part: whatever the re-verification asks, unless option b's change removes most of them.

Cost. None in Darkroom. Removing the passkey would lower the account's resistance to phishing everywhere, for a gain the setting already gives; this page does not recommend it.

Promises. None touched: the owner types, the worker does not.

What it does for problem 2. Nothing by itself: the per-part prompt already takes the password today. It does decide what each re-verification costs: a password, or (UNVERIFIED) a tap on the phone if Takeout's re-verification offers a Google prompt. A tap from the phone, with the remote browser open on the phone too, is the cheapest per-part answer that keeps promise 4.

Experiment. Part of section 3, steps 2 and 3.

### b. Why Google asks per part, and what Darkroom could do

The hypotheses, from section 1:

- **H1, a time window from the last verification.** Every part's click comes 14 to 70 minutes after the previous answer, so if the window is shorter than one part's download, every click prompts. Then several clicks inside the window would each start without a prompt.
- **H2, every download is a sensitive action.** Each click prompts whatever the timing. Then nothing Darkroom can do avoids the prompts, and only option a's cheaper answer helps.
- **H3, tied to the part's link.** A signed link per part that each needs its own verification. Behaves like H2 for Darkroom.

What Darkroom does today. One part at a time: click, wait for the download to start, collect it, check size and zip, then the next. A prompt is noticed after 30 seconds without a download, the job waits ("waiting for you", reason `verify`), an alert goes out (emailed when Resend is set up), and once the prompt is gone the part is counted `asked_to_verify` (no attempt used, one of Google's 5 downloads used) and the link is clicked again; a download that starts by itself after the answer is kept.

What Darkroom could do, depending on the result (none of this is built, and each is its own issue after the experiment):

1. **Under H1: start the next parts right after a verification.** After the owner answers a prompt, click the following parts' links one after another within a short time, so each download starts inside the same window, and let them run side by side in the one browser. One answer could start every remaining part, or a batch of them. This changes the download job's shape: it must match each started download to its part (it already checks the export id `j` and part id `i` on the download's address), budget Google's 5 downloads per part across parallel clicks, and keep the stall rule (today the size of `_inbox/.downloads/<account id>/` as a whole, which still works for several downloads). Per-part ingest (#424) already queues each part as it is recorded `done`, one at a time per account, so parts finishing close together need nothing new there. Disk: the same total, arriving sooner. UNVERIFIED: whether Google or Chrome limits parallel downloads from Takeout, and whether a download started inside the window keeps running when the window closes (it must, or H1 alone would not explain the owner's record).
2. **An optional "download window".** An Exports page action, "Download now, I'm here": the job asks for one verification immediately, then starts as many parts as the window allows (the number measured in section 3). It needs a design signed off before it is built (promise 6).
3. **Under either hypothesis: shorten the gap between a prompt and the owner seeing it.** Today the prompt is noticed after 30 seconds without a download, and the alert is the existing waiting-for-you alert. An immediate email naming the part, with a link that opens the remote browser on a phone, makes each prompt a short interruption rather than a stalled part. The alert mechanism exists; whether it fires immediately for `verify` and how it reads are a small follow-up, and any wording change on screen needs the owner.
4. **Under H2 or H3:** nothing avoids the prompts. Keep one part at a time, and make each answer cheap (option a: a Google prompt if offered).

Promises. None touched by 1 to 4: the owner answers every prompt; the worker clicks only part links it has checked.

Experiment. Section 3, steps 4 to 6, settle H1 against H2 and H3 and measure the window.

### c. Signing in on the owner's computer and handing the session to the server

What it is. The owner signs in with the passkey in their own Chrome, and the session (Google's cookies) is copied into the account's profile in appdata (`browser/<account id>/profile/`), or into the worker's Chrome with Playwright's cookie APIs.

What is known about Google's binding. Device Bound Session Credentials: Chrome generates a key pair at sign-in, keeps the private key in secure hardware such as a TPM where there is one, and proves possession of it before refreshing short-lived cookies ([Chrome DBSC docs](https://developer.chrome.com/docs/web-platform/device-bound-session-credentials)). It is generally available in Chrome on Windows from Chrome 146 and on by default for Workspace accounts ([Workspace Updates, May 2026](https://workspaceupdates.googleblog.com/2026/05/prevent-account-takeovers-with-DBSC-now-generally-available-in-the-Chrome-browser-for-Windows.html); [requirements](https://knowledge.workspace.google.com/admin/security/prevent-cookie-theft-with-session-binding): Chrome 146 or later on Windows, a TPM). Without secure key storage "DBSC gracefully falls back to standard behavior". UNVERIFIED: whether it is on for personal accounts (reported by press, not found on a Google page that could be fetched), and when macOS follows.

What that means here. A session made in Chrome on a Windows computer with a TPM is meant to stop refreshing on any other device, so a copied session would lapse within the life of its short-lived cookies; that is the purpose of DBSC. A session from a computer without DBSC (macOS for now, or a personal account if it is not on) is a plain cookie session and might move; whether Google then signs it out for appearing from another device and address is UNVERIFIED.

What it does for problem 2. Nothing: the per-part re-verification is about how recently the person verified, and a moved session carries no fresh verification. The owner would still answer the prompt in the remote browser, or hand over a new session per part.

Security cost. Google's session cookies are bearer credentials for the whole account, short of a password change. Moving them means a file or a paste that holds them outside both browsers, on the owner's computer, in transit and in Darkroom. Darkroom must never store or log them beyond the browser profile, and the import route would be a new kind of credential that must end when the account is deleted ("Access that outlives the person"). Chrome also encrypts its cookie store on the owner's computer, so getting them out at all takes a tool or an extension, which is the kind of access cookie-stealing malware uses.

Promises. Promise 4's words hold (nothing is typed), but this is new handling of a credential, which SECURITY.md and the review would treat as security work. Not recommended: high cost, no help with problem 2, and DBSC is designed to defeat it.

Experiment. Not proposed; if the owner wants it settled: a disposable personal account, signed in in Chrome on a computer without DBSC, the profile copied with `copy-browser-state` (which already copies the profile folder), then a status check in Darkroom, and the same from Windows to see the session lapse.

### d. Takeout delivered to Drive, Dropbox, OneDrive or Box (ruled out by the owner: storage)

Takeout can deliver to these instead of download links, and an archive added to Drive counts toward the account's storage ([Takeout help](https://support.google.com/accounts/answer/3024190)), which Gmail, Drive and Photos share ([Google storage](https://support.google.com/googleone/answer/9312312)). A 1.3 TB export needs that much free; the owner does not have it, so this was not researched further.

### e. A USB security key forwarded into the container (short)

Only for a hardware FIDO2 key registered on the account as a passkey or security key. Either the key stays plugged into the server and its device node (`/dev/hidraw<N>`) is passed into the container, or it stays at the owner's computer and is exported with USB/IP, whose protocol has no authentication or encryption of its own ([kernel USB/IP protocol](https://www.kernel.org/doc/html/latest/usb/usbip_protocol.html)) and needs the `vhci-hcd` module on Unraid. Either way the owner still touches the key (and may enter its PIN in Chrome's own dialog, shown in the viewer) for every sign-in and every re-verification, so it does not reduce problem 2; a key at the server means walking to it. A new key is also subject to Google's 7-day rule for sensitive actions. Promise 4 holds (the owner touches, the worker types nothing). Cost: a key, a change to the Unraid template's device list, and the risk of a network-reachable USB/IP service. Not recommended unless the owner already has a key and the server is at hand.

### f. A lighter remote browser than noVNC (short)

The DevTools protocol can stream the page (`Page.startScreencast`) and inject input (`Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`), which could replace Xvfb, x11vnc and noVNC with a lighter viewer. It changes nothing for passkeys: a WebAuthn request is answered by an authenticator attached to the machine where Chrome runs, and any viewer that only carries the picture and input leaves the authenticator on the wrong machine. (Windows' Remote Desktop has WebAuthn redirection for its own sessions; nothing like it exists for a Linux browser shown in a web page, as far as this spike could find: UNVERIFIED.) It could make the per-part prompt easier to answer from a phone, but noVNC already works there; the gain is the viewer's weight, not sign-in.

### g. Other approaches

- **WebAuthn virtual authenticators.** The DevTools protocol's WebAuthn domain lets Chrome use a software authenticator (`WebAuthn.addVirtualAuthenticator`), and `WebAuthn.addCredential` takes the credential's "ECDSA P-256 private key in PKCS#8 format" ([protocol definition](https://github.com/ChromeDevTools/devtools-protocol/blob/master/json/browser_protocol.json), and the [DevTools WebAuthn panel](https://developer.chrome.com/docs/devtools/webauthn)). It cannot hold the owner's existing passkey: that private key never leaves Google Password Manager, iCloud Keychain, Windows Hello or a hardware key, by design. It could hold a new passkey registered for the purpose, but then Darkroom stores a private key that signs in to the whole account and the worker performs the sign-in: a stored whole-account credential used by the worker, which is what promise 4 exists to prevent. It would need promise 4 amended and is not recommended.
- **Google Password Manager passkeys in the container's Chrome.** Google Password Manager syncs passkeys to Chrome on Windows, macOS and Linux behind a Google Password Manager PIN ([Google, September 2024](https://blog.google/technology/safety-security/google-password-manager-passkeys-update-september-2024/); [Chrome blog](https://developer.chrome.com/blog/passkeys-gpm-desktop)). If the owner's passkey is in Google Password Manager, signing the container's Chrome in to sync could make it usable there. It would also bring every saved password of the owner's into a profile on the server, and needs a sign-in to Chrome first (the password path once). UNVERIFIED: whether Chrome allows sync sign-in in a Playwright-launched profile, and whether a synced passkey answers Takeout's re-verification. Not recommended for the exposure; listed because it is the one way the owner's own passkey could work in the container.
- **Reading the library another way.** The Photos Library API no longer reads a user's whole library: since March 31, 2025 apps can only "list, search, and retrieve albums and media items that were created by your app" ([Photos API updates](https://developers.google.com/photos/support/updates)). Takeout stays the only export.
- **Fewer parts.** Darkroom's Create schedule job already picks 50 GB parts, the largest size Takeout's form offered when that job was written ([SPEC](../SPEC.md#takeout-browser-worker)), so the part count cannot drop further unless Google adds a larger size.

### h. A Chrome extension in the owner's own Chrome (owner is not interested)

An extension on takeout.google.com in the owner's normal Chrome, where the passkey works, would click the parts and report to Darkroom with a pairing token. Its trade-offs: the multi-day download depends on the owner's computer staying awake and online; Chrome's `chrome.downloads` can only save relative to the Downloads directory ([API reference](https://developer.chrome.com/docs/extensions/reference/api/downloads)), so saving into the share means pointing Chrome's download folder at a mounted share; handing a download to a server-side helper would need the session's cookies (option c's cost); and the re-verification per part would still be asked, though with the passkey at hand. Not researched further and not recommended.

### i. Google's Data Portability API (official; does not cover Google Photos today)

What it is. An OAuth API through which a person authorises an app to receive a copy of some of their Google data: the app starts an archive job, polls its state, and downloads the result from signed URLs; access is one-time per scope, or time-based, "allowing exports every 24 hours until consent expires" ([introduction](https://developers.google.com/data-portability/user-guide/introduction)). The person grants 30 or 180 days, or a single copy ([Share a copy of your data with a third party](https://support.google.com/accounts/answer/14452558)). If it covered the library it would be the clean answer: one consent in the owner's own browser (the passkey works there), an OAuth refresh token in Darkroom (sealed with `secretbox`), no browser for downloads, and promise 4 untouched.

What is KNOWN, from Google's pages as fetched on 2026-11-18:

- **No Google Photos.** The scope list has no Google Photos library scope; the nearest are `dataportability.maps.photos_videos` ("photos and videos you posted on Maps") and `dataportability.streetview.imagery` ([scopes](https://developers.google.com/data-portability/user-guide/scopes)), and the schema reference has no Photos section ([schema reference](https://developers.google.com/data-portability/schema-reference)). Google's help lists the products as Chrome, Maps, Play, Search, Shopping and YouTube ([help](https://support.google.com/accounts/answer/14452558)). This decides the option: it cannot replace Takeout for Darkroom until Google adds Photos.
- **Regions.** Available in the EEA countries, Switzerland and the United Kingdom only ([help](https://support.google.com/accounts/answer/14452558), the list under "Where is this feature available?"); not worldwide. Accounts of users under 18 are not supported ([overview](https://developers.google.com/data-portability/user-guide/overview)).
- **Verification.** Apps must be verified before publication; restricted scopes add "an additional security review during verification", and apps are re-verified "annually or if you make changes to the approved app" ([overview](https://developers.google.com/data-portability/user-guide/overview)). An app in Testing status gets refresh tokens that expire after 7 days ([OAuth 2.0](https://developers.google.com/identity/protocols/oauth2)), so a personal unverified app would need a fresh consent every week. Whether Google's "Personal mode" for single-user apps ([unverified apps](https://support.google.com/cloud/answer/7454865)) applies to this API is UNVERIFIED.
- **Limits.** A second one-time archive request with the same grant returns `RESOURCE_EXHAUSTED` ([quickstart](https://developers.google.com/data-portability/user-guide/quickstart)). Archive size and part limits, quotas, signed-URL lifetime and whether a re-verification is asked at download time: UNVERIFIED (not found on the pages fetched). Setup asks for billing on the Cloud project; whether calls are charged: UNVERIFIED.
- **Format.** Each product has its own schema ([schema reference](https://developers.google.com/data-portability/schema-reference)); with no Photos schema there is nothing to compare with Takeout's JSON sidecars, so whether `pairing` and `metadata` would apply is unknown.

What Darkroom would need, if Google adds Photos: a consent flow for the owner (an OAuth client and a redirect to Darkroom behind its sign-in), a sealed refresh token per Google account that ends when the account is deleted ("Access that outlives the person"), a job that starts an archive, polls it and streams each signed URL into `_inbox/<person>/export-<id>/` through `darkroom/tls.py`, and a check of the archive's format against the pairing code. No browser, no remote viewer.

Experiment. Not worth running until Photos is in the scope list: the scope page is the check, and anyone can re-read it. If it appears and the owner is in a listed country: a Google Cloud project, the API enabled, an OAuth consent screen in Testing with the owner's test account as a test user, the quickstart's one-time flow for the smallest Photos scope on a test account with a few made-up photos, and a comparison of the archive with a Takeout export of the same photos. By hand: creating the project and consenting.

### j. Reverse engineering Takeout's private web endpoints (not recommended)

What it is. Calling the undocumented requests Takeout's own page makes (Google's web apps commonly use a `batchexecute`-style RPC endpoint) from Darkroom's code instead of clicking the page. It would replace page parsing with request parsing.

Why it does not help. Those requests are authorised by the same signed-in session cookies as the page, so they need the same sign-in (problem 1 stays) and, for downloading, the same "verify it's you" step for a sensitive action (problem 2 stays: the prompt is Google's server deciding the session is not recent enough, whatever client asks). It also adds option c's cost, because the cookies would have to leave the browser for Darkroom's code to use them. Brittleness: undocumented RPC ids and payloads change without notice, as the page's own shape has changed during this work (#418, #419, #423). Terms: [Google's Terms of Service](https://policies.google.com/terms) ask users not to reverse engineer Google's services or underlying technology except as applicable law allows, and not to use automated means to access content against the machine-readable instructions on Google's pages; how that applies to scripting one's own Takeout is the owner's to judge, and this page does not claim it is allowed. Requests that look unlike a browser are the kind of thing that can trigger Google's unusual-activity checks on the account, which risks the owner's real account.

A safe investigation, if the owner still wants to know: on the disposable account only, in the owner's own Chrome, open DevTools' Network panel and record which requests the Takeout downloads page makes when it lists exports and when a part is clicked (method, host and path only, no cookies, no bodies kept, no request replayed). That shows whether the download goes through a separate signed link after the verification, which section 3's step 6 also probes. Nothing is replayed and nothing is added to Darkroom.

Not recommended unless the official route (option i) is unavailable and Takeout's page cannot be driven at all; even then the per-part prompt would remain.

## 3. The first experiment the owner runs

It settles option b's hypotheses and option a's choices, in the owner's own Chrome, without Darkroom and without changing it. Use a disposable Google account, never the real one, and made-up photos only. Write down no account, address or file name: say "the test account".

Before you start:

- The test account must be at least 7 days old and signed in on a phone (Google app or Gmail) for 7 days, or Google may block "downloading your data" for 7 days ([sensitive actions](https://support.google.com/accounts/answer/7162782)). Turn on 2-Step Verification with Google prompts on that phone, and add a passkey, so it matches the owner's account. Then wait the 7 days.
- Upload about 5 GB of made-up photos or videos to its Google Photos (within the free 15 GB). Any generated files will do, for example short videos of noise.
- Use a fresh Chrome profile on your own computer for the test account, with "Ask where to save each file before downloading" off.

Steps (each click on a part uses one of its 5 downloads; the order below fits in 5 parts' budgets):

1. In Takeout, create an export of Google Photos only, `.zip`, the smallest part size the form offers (1 GB or 2 GB), delivered by link. Note the number of parts (aim for at least 5). Record when it is ready.
2. Sign out, sign in again. Record the screens in order: what the first screen asks for, whether "Try another way" is there and what it lists (by kind: password, passkey, phone prompt, code, other). Then turn off "Skip password when possible" on the account and sign in once more; record the screens again.
3. Open the export's download list and click part 1. Record what appears: a password screen, a passkey screen, a phone prompt, or the download starting. If there is a "Try another way", record what it lists. Answer with the password and note the time (T0).
4. **Back to back:** as soon as part 1 has started downloading, click parts 2, 3 and 4 within a minute. Record, for each, whether it prompted and the time. (H1 predicts no prompts; H2 or H3 a prompt each.)
5. **The window:** if step 4 had no prompts, wait for those downloads to finish, then click part 5 at about 10 minutes after T0; if no prompt, click part 1 again at about 20 minutes after T0, part 2 at 40, part 3 at 60. Record the first click that prompted and its time since T0. If step 4 prompted every time, skip this step.
6. **A download that outlasts the window:** after answering a prompt (T1), click a part, and in DevTools (Network, throttling) slow the download so it takes longer than the window found in step 5. When it finishes, click another part at once. Record whether it prompted, and whether the slow download finished or broke when the window passed. This tells "time since verification" from "time since the last download".
7. Delete the export and the test photos when done, or keep them for a run in Darkroom.

Report back on #432 with this block, with the replacements above:

```text
Spike takeout-signin-alternatives, section 3
Date, Chrome version, connection speed (roughly), who ran it:
Step 2, sign-in screens (default, then with "Skip password when possible" off):
Step 3, part 1's prompt and the choices under "Try another way":
Step 4, parts 2 to 4 (prompted or not, minutes after T0):
Step 5, first click that prompted (minutes after T0), or "skipped":
Step 6, prompted after the slow download? Did the slow download finish?
Anything else Google showed (a warning, a block, an email):
```

If the result supports H1, the next step is an agent task to start several parts after one verification (option b, items 1 and 2), checked once in Darkroom's own remote browser with the same test account before it touches the real one. If it supports H2 or H3, the next step is option b's item 3 and option a's cheapest answer.

## 4. Comparison

| Option | Darkroom effort | Owner effort per export | Risk | Verified? |
| --- | --- | --- | --- | --- |
| a. Password path ("Try another way", setting off) | None | One sign-in with password and second step; still one answer per part | None new; removing the passkey would lower account security (not proposed) | Choices on the screen KNOWN in general; Takeout's re-verification choices UNVERIFIED |
| b1. Start several parts after one verification | One download-job issue (parallel clicks, matching, the 5-download budget) | One answer per batch, possibly one per export | Parallel downloads untested with Takeout; a wrong match uses Google's downloads | UNVERIFIED (H1); section 3 settles it |
| b2. "Download window" action | b1 plus a signed-off design | One sitting, then nothing until the next batch | As b1 | UNVERIFIED (H1) |
| b3. Immediate alert, phone-friendly link | Small, existing alert path | One answer per part, quicker | None | Darkroom side KNOWN; helps whatever the hypothesis |
| c. Session handoff | New credential path, security review | One handoff per sign-in, and still the per-part prompts | High: whole-account cookies outside both browsers; DBSC is built to stop it | DBSC on Windows KNOWN; personal accounts and macOS UNVERIFIED |
| d. Cloud delivery | Not researched | n/a | n/a | Ruled out by the owner (storage) |
| e. USB security key | Template device change, or USB/IP on Unraid | A touch per sign-in and per part | USB/IP unauthenticated on the network; 7-day rule for a new key | Device passing KNOWN in general; untested here |
| f. Lighter viewer | A new viewer | Unchanged | Low | No passkey change: KNOWN from where WebAuthn runs |
| g. Virtual authenticator | Moderate | None | Stored whole-account key used by the worker: needs promise 4 amended | KNOWN that it cannot import the existing passkey |
| g. Synced Password Manager passkeys | Low | A PIN per sign-in | Every saved password of the owner's on the server | UNVERIFIED in a Playwright-launched profile |
| h. Chrome extension | High (an extension, pairing, a second parser) | Computer awake for days | Session handling if a helper downloads | Owner is not interested |
| i. Data Portability API | Consent flow, sealed refresh token, archive job (if Photos were covered) | One consent per 30 or 180 days | Low; a new OAuth credential to seal and end with the account | KNOWN: no Google Photos scope; EEA, Switzerland and UK only |
| j. Takeout's private endpoints | High and brittle | Unchanged: same sign-in and per-part prompt | Terms of service, account restriction, cookies outside the browser | Not tried; not recommended |

## 5. Recommendation

1. **Now, no code:** use the password path for the first sign-in (turn off "Skip password when possible", or press "Try another way"), keeping the passkey on the account. This ends problem 1.
2. **Next, the experiment in section 3** on a disposable account in the owner's own Chrome. It is the cheapest way to learn what problem 2 is, and it costs no Darkroom change.
3. **Then, by its result:** under H1, an agent task for option b1 (start several parts after one verification), followed by the "download window" (b2) only if the owner wants a button for it; under H2 or H3, option b3 (an immediate, phone-friendly alert) and, if the test showed one, answering with a Google prompt on the phone.

Option i, Google's official Data Portability API, would have been the first choice, but it has no Google Photos scope today (and is limited to the EEA, Switzerland and the UK), so it is not available whatever the owner's country; it is worth re-checking its scope list from time to time. Option j is not recommended.

None of these amends a promise. Options c and g are not recommended; g's virtual authenticator would need promise 4 amended, and this page does not propose that.

## 6. Questions only the owner can answer

1. Where does the account's passkey live: Google Password Manager (an Android phone), iCloud Keychain, Windows Hello, or a hardware key?
2. Which second steps does the account have: Google prompts on a phone, an authenticator app, text codes, a security key, backup codes? Has Takeout's per-part prompt ever offered anything but the password?
3. Is the account a personal Google account or a Workspace one? (DBSC is on by default for Workspace.)
4. Roughly how fast is the home connection, as seen by the server: how long does one 53 GB part take today?
5. Is there a disposable Google account at least 7 days old, with a phone signed in to it, for section 3? Will you run section 3 in your own Chrome?
6. Are you willing to turn off "Skip password when possible" on the real account? (Removing the passkey is not recommended.)
7. If parts can start back to back after one verification, do you want Darkroom to start every remaining part at once, a fixed batch, or only when you press a "download now" button while you are at the remote browser?
8. Would an email the moment Google asks (with a link that opens the remote browser on your phone) be enough on its own, if the prompts cannot be avoided?
9. Do you have a hardware FIDO2 key, and is the server somewhere you can reach to touch one?
10. Which country is the account in? It matters only if Google adds Google Photos to the Data Portability API, which is limited to the EEA, Switzerland and the UK.

## Sources

- Google: [Takeout help](https://support.google.com/accounts/answer/3024190), [sensitive actions](https://support.google.com/accounts/answer/7162782), [passkeys](https://support.google.com/accounts/answer/13548313), [2-Step Verification](https://support.google.com/accounts/answer/185839), [Google prompts](https://support.google.com/accounts/answer/7026266), [storage](https://support.google.com/googleone/answer/9312312), [Workspace security challenges](https://knowledge.workspace.google.com/admin/security/protect-google-workspace-accounts-with-security-challenges), [Photos API updates](https://developers.google.com/photos/support/updates), [Password Manager passkeys](https://blog.google/technology/safety-security/google-password-manager-passkeys-update-september-2024/), Data Portability API [introduction](https://developers.google.com/data-portability/user-guide/introduction), [overview](https://developers.google.com/data-portability/user-guide/overview), [scopes](https://developers.google.com/data-portability/user-guide/scopes), [schema reference](https://developers.google.com/data-portability/schema-reference), [quickstart](https://developers.google.com/data-portability/user-guide/quickstart) and [availability](https://support.google.com/accounts/answer/14452558), [OAuth 2.0](https://developers.google.com/identity/protocols/oauth2), [unverified apps](https://support.google.com/cloud/answer/7454865), [Terms of Service](https://policies.google.com/terms)
- Chrome: [DBSC](https://developer.chrome.com/docs/web-platform/device-bound-session-credentials), [DBSC on Windows, Workspace Updates](https://workspaceupdates.googleblog.com/2026/05/prevent-account-takeovers-with-DBSC-now-generally-available-in-the-Chrome-browser-for-Windows.html), [session binding requirements](https://knowledge.workspace.google.com/admin/security/prevent-cookie-theft-with-session-binding), [Password Manager passkeys on desktop](https://developer.chrome.com/blog/passkeys-gpm-desktop), [DevTools WebAuthn panel](https://developer.chrome.com/docs/devtools/webauthn), [DevTools protocol definition](https://github.com/ChromeDevTools/devtools-protocol/blob/master/json/browser_protocol.json), [`chrome.downloads`](https://developer.chrome.com/docs/extensions/reference/api/downloads)
- Other: [kernel USB/IP protocol](https://www.kernel.org/doc/html/latest/usb/usbip_protocol.html), [gargantuan-takeout-rocket](https://github.com/nelsonjchen/gargantuan-takeout-rocket) (third party)
