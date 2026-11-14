# Spike S1: the merged image

Spike S1 of the [single-container design](../design/single-container.md) (section 12, tracking issue #284). It checks whether one image can run the app and the Takeout browser worker as one unprivileged user under the deployment sandbox, and measures today's two images against the merged one. It was run once, on amd64, in a throwaway Docker daemon. No repository file changed: the experimental Dockerfile lived outside the repository and its relevant part is quoted below. The Dockerfile itself is step A1.

## Result in one paragraph

On amd64, one image built `FROM python:3.14-slim` (Debian 13) runs the app, the worker, Xvfb, x11vnc and websockify side by side as uid 99. Playwright 1.64.1 installs and runs on the app's Python 3.14, so the image does not need a second runtime. The one change is greenlet: the worker's pinned 3.1.1 has no 3.14 wheel, and the 3.5.6 that `poetry.lock` already resolves works. Chrome and Chromium both pass `deploy/browser/smoke.sh` under `deploy/browser/seccomp.json`, `no-new-privileges` and `--cap-drop=ALL`, and `chrome://sandbox` reports "adequately sandboxed" for both. Under the same flags, the app answers `/healthz` and its Docker healthcheck turns healthy, and exiftool and libheif work. The merged image is 2.56 GB (2.28 GB without Playwright's headless shell), against 0.55 GB + 3.28 GB today. A Chromium profile copied into another mount keeps its cookie and local storage. Two gaps in the seccomp profile turned up and matter for later steps. It refuses the extended-attribute syscalls, so `cp -a`, `shutil.copy2`, `shutil.copytree` and a cross-device `shutil.move` all fail with `EPERM`, and the profile move into appdata (A4) would fail unless it avoids them or the profile allows them. It also refuses `ioprio_set`, which costs Syncthing only a warning. aarch64, a real signed-in Google profile and s6-overlay were not verified.

## Versions

| Piece | Version |
| --- | --- |
| Docker Engine, buildx | 29.9.2 (containerd image store, overlay2, cgroup v1), buildx 0.38.1 |
| Host | Linux 6.18, x86_64 (a cloud container, not Unraid) |
| App base | `python:3.14-slim` (`sha256:9aaa4740…f04802f`), Debian 13 (trixie), Python 3.14.10 |
| Frontend stage | `node:22-slim` (`sha256:e9f565d3…a3ab21ee`) |
| Today's worker base | `mcr.microsoft.com/playwright/python:v1.64.1-noble` (`sha256:330d401a…6e031`), Ubuntu 24.04.5, Python 3.12.3 |
| Playwright | 1.64.1 (both images); bundled Chromium build 1248 = Chrome for Testing 156.0.8078.4 |
| Google Chrome | 157.0.8103.78 stable (amd64 only, as today) |
| greenlet | 3.1.1 today (worker), 3.5.6 in the merged worker venv (`poetry.lock`'s version) |
| exiftool, libheif, pillow-heif, Pillow | 13.25, 1.23.6, 1.8.1, 13.0.0 (the same in today's app image and the merged one) |
| Display stack (merged, Debian 13) | xvfb 21.1.16, x11vnc 0.9.17, novnc 1.6.0, websockify 0.12.0, tini 0.19.0 |
| Syncthing (only to check it runs under the profile) | 1.29.5 from Debian's archive, not the release S3 will pin |

## Method

### Building in this sandbox

This sandbox's outbound proxy re-signs TLS, so `pip`, `npm` and Playwright's installer fail inside a `RUN` step unless they trust its CA. The fix was confined to the build. Each Dockerfile was copied, and a script rewrote every `RUN` to `RUN --mount=type=bind,from=ccrca,target=/ccr …` and added an `ARG SSL_CERT_FILE=… PIP_CERT=… NODE_EXTRA_CA_CERTS=…` after each `FROM`. It was built with `docker buildx build --load --build-context ccrca=<proxy CA folder>`. A bind mount and build arguments leave no file in any layer (`docker history` shows 0 B for the `ARG` lines), so the sizes below are the images as written. Docker Hub answered 429 once; a plain `docker pull` of the two base images, then the build, worked.

### The two images built as they are

```text
docker buildx build --load -f Dockerfile -t darkroom:today .
docker buildx build --load -f Dockerfile.browser -t darkroom-browser:today .
```

### The merged image tried

The `web` and `deps` stages are today's `Dockerfile`, unchanged. The worker gets its own virtualenv on the app's Python, as section 4 asks, and the display stack comes from Debian:

```dockerfile
FROM python:3.14-slim AS worker-deps
# deploy/browser/requirements.txt with greenlet 3.1.1 replaced by 3.5.6 (the cp314 x86_64 and aarch64 wheel hashes from poetry.lock)
COPY worker-requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/worker \
    && /opt/worker/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && /opt/worker/bin/python -c "from importlib import metadata; assert metadata.version('playwright') == '1.64.1'" \
    && /opt/worker/bin/python -m pip uninstall -y pip

FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/app/.venv/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=/ms-playwright DISPLAY=:99
RUN apt-get update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl libheif1 zlib1g \
       xvfb x11-utils x11vnc novnc websockify tini \
    && rm -rf /var/lib/apt/lists/* && python -m pip uninstall -y pip
COPY --from=worker-deps /opt/worker /opt/worker
RUN /opt/worker/bin/python -m playwright install --with-deps chromium \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then /opt/worker/bin/python -m playwright install --with-deps chrome; fi \
    && rm -rf /var/lib/apt/lists/*
# then today's runtime stage: the app's .venv, darkroom/, the built frontend, run.py, the uid 99 / gid 100 user, USER darkroom,
# plus darkroom_browser/ and smoke.sh under /opt/worker/app, /tmp/.X11-unix mode 1777, and ENTRYPOINT ["/usr/bin/tini", "--"]
```

A second variant was also built with `playwright install --with-deps --no-shell chromium`, which leaves out the headless shell the worker never uses (it launches headed).

### The checks

`$SANDBOX` stands for the flags of the image CI's browser step (`.github/workflows/docker.yml`): `--cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json`. The browser checks add `--shm-size=1g`.

| Check | Command (in the merged image unless noted) |
| --- | --- |
| Smoke, both channels | `docker run --rm $SANDBOX --shm-size=1g -w /opt/worker/app -e PATH=/opt/worker/bin:/usr/bin:/bin darkroom:merged bash /opt/worker/app/smoke.sh` |
| Smoke without the profile (negative control) | the same with Docker's default seccomp profile |
| Sandbox state | a probe that opens each channel through `darkroom_browser.live.open_context` and reads `chrome://sandbox` |
| Everything together | one container under `$SANDBOX`. Inside it: `run.py serve` with a generated browser token, Xvfb, x11vnc on loopback, websockify serving noVNC, and `python -m darkroom_browser` from `/opt/worker/bin/python`, started with `env -i` and only `PATH`, `HOME`, `DISPLAY`, `PLAYWRIGHT_BROWSERS_PATH`, `DARKROOM_URL`, `DARKROOM_BROWSER_TOKEN`, `PHOTOS_ROOT` and `POLL_SECONDS`. Then the processes' uids, `/healthz`, noVNC's page, and the worker's and app's log events |
| App | `docker run -d $SANDBOX -p <port>:8000 darkroom:merged`, then `curl /healthz` and, after 31 s, the container's health status |
| exiftool, libheif | under `$SANDBOX`: `exiftool -ver`, and a probe that writes a 64×48 HEIF with pillow-heif, reads it back, writes an XMP sidecar with exiftool and reads the tag back |
| Profile into another mount | under `$SANDBOX` with two bind mounts: open Chromium on a profile in mount A through `open_context`, load a page served on loopback, set a cookie with a one-day `max-age` and a `localStorage` value, then close. Remove the `Singleton*` lock links, copy `_browser/` to mount B, delete it from A and reopen from B |
| File copying | under `$SANDBOX`: `cp -a`, `shutil.copy2`, `shutil.copytree`, a cross-mount `shutil.move`, and `os.setxattr` and `os.listxattr` on a `user.*` attribute; then the same with only `--cap-drop=ALL`, and with the profile plus the xattr syscalls |
| Syncthing | Debian's `syncthing` added to a scratch copy of the merged image, run with `--user 99:100 $SANDBOX`, with its home in a temporary folder and the GUI on loopback, until `/rest/system/status` answered |
| Sizes | `docker image inspect --format '{{.Size}}'` (uncompressed), `docker history`, and `docker save <image> \| gzip -1 \| wc -c` as an estimate of the compressed size |

The same smoke test, sandbox probe and profile-copy probe were also run on today's browser image, and `/healthz`, the media probes and the file-copying probe on today's app image, as the baseline. They gave the same results as the merged image.

## Observations

### 1. One image, one user

In the "everything together" run, every process ran as uid 99: tini, Xvfb, x11vnc, websockify, the app's Python, the worker's Python and the shells. Nothing ran as root. `/healthz` answered `{"ok": true, …}`. noVNC's `vnc.html` was served (200) from inside the container. The worker logged `browser worker ready`, and in 8 s the app logged five `POST` requests answered 200 (the worker's poll on the internal API with the token) and no error. The app logged one expected event, `inbox folders not shared with the browser worker` (`PermissionError`), because the image's `/photos` is root-owned until a share is mounted. Today's app image logs the same with nothing mounted, and with a uid 99 folder mounted on `/photos` the event did not appear.

### 2. Playwright on the app's Python: works, with greenlet 3.5.6

- `pip install --require-hashes -r deploy/browser/requirements.txt` on `python:3.14-slim` fails. greenlet 3.1.1 has no cp314 wheel, so pip builds it from source, and the slim image has no `g++` (`error: [Errno 2] No such file or directory: '/usr/bin/g++'`, `Failed building wheel for greenlet`). Adding a compiler was not tried: a newer greenlet is the better fix.
- With greenlet 3.5.6, the version `poetry.lock` resolves for the `browser` extra, which has cp314 manylinux wheels for x86_64 and aarch64, the worker venv installs with hashes required. Playwright 1.64.1's pure wheel runs on 3.14.10.
- `playwright install --with-deps chromium` and `… chrome` both worked on Debian 13 with no "unsupported OS" warning. Chromium build 1248 was downloaded, and Chrome came from Google's `.deb` through Playwright's installer, as today.
- The two-runtime variant (the worker on the Playwright image's Python) was not built, because the first variant worked.
- Debian's `novnc` pulls in Node.js 20 (`nodejs`, `libnode115`, about 80 MB), and `websockify` pulls in Debian's own Python 3.13 with numpy, about 85 MB. Those are system packages for websockify, not a second runtime for Darkroom, but the merged image does hold two Python interpreters and a Node.js it does not run. Taking noVNC's static files from a pinned release and websockify from the worker venv would remove both. That is untested, and it is A1's choice.

### 3. Chrome and Chromium under the sandbox flags

| Image | `smoke.sh` under `$SANDBOX` | `chrome://sandbox`, both channels |
| --- | --- | --- |
| Today's browser image | exit 0, no `chrome_launch_failed` | Layer 1 Namespace; PID and network namespaces Yes; Seccomp-BPF Yes, with TSYNC; "You are adequately sandboxed." |
| Merged image | exit 0, no `chrome_launch_failed` | The same |
| Merged image, Docker's default seccomp (negative control) | `chrome_launch_failed` then `chromium_launch_failed`, both `sandbox_unavailable` | not reached |

The two channels report Chrome 157 and Chromium 156 in their user agents, so the probe opened the browser it asked for each time. Yama ptrace protection reads No in both images: this host's kernel setting, not the image's. Chrome is installed only on amd64, as `Dockerfile.browser` does today.

### 4. App, exiftool and libheif under the same profile

The app under `$SANDBOX` answered `/healthz`, and Docker reported the container `healthy` after its first 30 s healthcheck interval. `exiftool -ver` printed 13.25. pillow-heif with libheif 1.23.6 wrote and read back a HEIF, and exiftool wrote an XMP sidecar and read the tag back, all as uid 99. The app image built as it is today passed the same checks under the same profile.

The profile does refuse something the app's stack may need. `deploy/browser/seccomp.json` allows neither the extended-attribute calls (`getxattr`, `lgetxattr`, `fgetxattr`, `listxattr`, `llistxattr`, `flistxattr`, `setxattr`, `lsetxattr`, `fsetxattr`, `removexattr`, `lremovexattr`, `fremovexattr`) nor `ioprio_set`, and it answers anything it does not allow with `EPERM`. Docker's default profile allows all of them. Observed as follows. Each row ran in at least one of today's images, and the `shutil` rows and the smoke test also ran in the merged image:

| Operation | `--cap-drop=ALL`, Docker's default seccomp | `$SANDBOX` | `$SANDBOX` plus the twelve xattr calls |
| --- | --- | --- | --- |
| `os.setxattr`, `os.listxattr` | ok | `EPERM` | (not rerun) |
| `cp -a`, `cp --preserve=mode` | ok | "preserving permissions … Operation not permitted", exit 1 | ok, mode 600 kept |
| `shutil.copy2`, `shutil.copytree` | ok | `PermissionError` / `shutil.Error`, `EPERM` | ok |
| `shutil.move` across mounts | ok | fails (it copies with `copy2`) | ok |
| `chmod`, `os.chmod(…, follow_symlinks=False)`, `utime`, `rename` | ok | ok | — |
| `smoke.sh` | — | exit 0 | exit 0 |
| Syncthing 1.29.5 | starts, REST answers, no warning | starts, REST answers, logs "Failed to lower process priority: set I/O priority: operation not permitted" | — |

Python's `shutil` ignores `ENOTSUP`, `ENODATA` and `EINVAL` from the xattr calls, but not `EPERM`, so under this profile any `copy2`-based copy fails. Today, `darkroom/` calls none of `copy2`, `copytree`, `copystat` or `shutil.move`. The worker's one `shutil.move` (`live.py`, a finished download into the inbox) stays on one filesystem and renames. So nothing in the current code hits this, but in one container two things now run under the browser's profile. One is A4's move of `_browser/` from the share to `/data/browser`, a copy across mounts. The other is any future copy the app makes. Either avoid xattr-preserving copies (a tar pipe kept the modes and worked, observation 7) or add the xattr calls to the profile with that reason, as section 4 already anticipates ("any syscall they need is added with a reason"). Syncthing only warns about `ioprio_set`. Section 7's provider can turn off `setLowPriority` or the profile can allow the call, and S3 should decide which with the pinned release.

### 5. Sizes

| Image | Uncompressed (`docker image inspect`) | `docker save \| gzip -1` |
| --- | --- | --- |
| `Dockerfile` (app), as it is | 547,226,140 B (547 MB) | 221,650,929 B (222 MB) |
| `Dockerfile.browser` (worker), as it is, amd64 with Chrome | 3,279,213,895 B (3.28 GB) | 1,446,141,955 B (1.45 GB) |
| Both together | 3,826,440,035 B (3.83 GB) | 1,667,792,884 B (1.67 GB) |
| Merged, as in the snippet | 2,562,439,742 B (2.56 GB) | 1,101,067,545 B (1.10 GB) |
| Merged, `--no-shell` | 2,277,355,148 B (2.28 GB) | 975,192,414 B (0.98 GB) |

The merged image is within section 4's proposed budget (no larger than the two current images together). Excluding the app image's 547 MB, it is also smaller than today's worker image alone. The reason is that the Playwright base image ships every browser: its base layer is 2.12 GB, and `/ms-playwright` in it is 1.3 GB, with Firefox and WebKit. On top of that, today's requirements install uses `--ignore-installed` and adds 149 MB by reinstalling the Playwright wheel and its Node driver.

Where the merged image's size goes (`docker history`, uncompressed):

| Layer | Size |
| --- | --- |
| Debian 13 and Python 3.14 (`python:3.14-slim`) | 120 MB |
| exiftool, libheif, Xvfb, x11vnc, noVNC, websockify, tini and their dependencies (including Node.js and Debian's Python, observation 2) | 526 MB |
| Worker venv (Playwright 1.64.1 with its Node driver, greenlet, pyee) | 149 MB |
| `playwright install --with-deps` for Chromium and Chrome | 1.42 GB: Chromium 403 MB, the headless shell 273 MB, ffmpeg 5 MB, Chrome 452 MB, and the rest Playwright's apt dependencies |
| App venv | 349 MB |
| `darkroom/`, the built frontend, `run.py`, `darkroom_browser/` | under 1.1 MB |

### 6. Architectures

amd64 was built and tested.

aarch64 is NOT VERIFIED. This daemon's builder lists only amd64 and 386 platforms, and `/proc/sys/fs/binfmt_misc` is not mounted. Registering qemu needs a privileged container (`tonistiigi/binfmt --install arm64`), and this session's permission policy refused that, so no arm64 build was attempted. Every arm64 input exists: `python:3.14-slim` is multi-arch, greenlet 3.5.6 and Playwright 1.64.1 have aarch64 wheels (the worker requirements above carry the hash), Debian 13 has the display packages, and the snippet skips Chrome on arm64. What would settle it: build the merged Dockerfile for `linux/arm64` with buildx and qemu, as the publish job in `.github/workflows/docker.yml` already does for both of today's images (`platforms: linux/amd64,linux/arm64`). Then run `smoke.sh` (Chromium only) under `$SANDBOX` on a real arm64 host or runner. A qemu-user run is not good evidence for the sandbox, because qemu-user does not emulate seccomp filters faithfully.

### 7. A profile moved into another mount

This part was VERIFIED for a profile that is not signed in, in both today's browser image and the merged image under `$SANDBOX`. Chromium opened a profile in mount A through `open_context`, set a persistent cookie and a `localStorage` value on a page served on loopback, and closed. The `SingletonLock`, `SingletonSocket` and `SingletonCookie` links were removed, and `_browser/` (7.0 MB) was copied to mount B with `tar -cf - | tar -xpf -`. The copy kept mode 600 on `Default/Cookies` and owner 99:100. With A deleted, Chromium reopened from B and read back the cookie (`probe=kept`) and the `localStorage` value. `cp -a` refused to copy the same profile under `$SANDBOX` (observation 4).

A real Google sign-in surviving the move is NOT VERIFIED. This session cannot sign in to Google, and the check needs a household account. The probe above shows that Chromium's own stores (the encrypted cookie database and LevelDB) survive a copy across mounts. It does not show that Google accepts the copied session: Google can tie a session to more than the cookie jar. What would settle it is S5a's rehearsal, or A4's. Sign in to a test Google account in today's two-container layout and run a Takeout listing. Stop the worker, copy the profile into `/data/browser` the way A4 will, start the merged image, and check that the same listing runs without a sign-in prompt, in Chrome on amd64 and in Chromium. Then do it once more with a profile Chrome had opened falling back to Chromium (`docs/deploy/browser.md` warns this may not open cleanly).

## Not verified, and what would settle each

| Item | Why not | What would settle it |
| --- | --- | --- |
| aarch64 build and smoke | No binfmt or qemu in this daemon; registering it needs a privileged container, which was refused here | A `linux/arm64` buildx build of the merged Dockerfile (CI's publish job already has qemu), then `smoke.sh` under `$SANDBOX` on a real arm64 machine |
| A signed-in Google profile surviving the move | No Google account in this session | The test-account rehearsal described in observation 7 (S5a or A4) |
| The two-runtime variant | Not needed: Playwright runs on 3.14 | Only if a later Playwright release drops a Python version the app uses |
| s6-overlay against the small init (section 3) | Out of this run's scope | A1, if the small init is questioned |
| Syncthing at the release S3 pins, and its full use of the profile (sync, scans, QUIC) | Only Debian's 1.29.5 was started, and only until its REST API answered | S3, under `$SANDBOX` |
| On Unraid (the seccomp file on the host, the template's flags, cgroup v2) | This host is a cloud container with cgroup v1 | S4 |
| The merged image running under the design's own init (restarts, per-child environment, the token handoff) | The run started each process by hand | S2 and A1 |
