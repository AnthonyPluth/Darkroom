# Spike S2: the viewer relay and the token handoff

Spike S2 of the [single-container design](../design/single-container.md) (section 12, tracking issue #284, spike issue #295). It checks three things: that noVNC works through a byte relay written the way the app's own server is written, behind a reverse proxy; that the init can hand a generated token to two child processes on a pipe so that it never reaches a disk, another process's environment or a log; and, for the fallback only, a shared pairing folder between two containers. It was run once, on amd64, partly directly on the host and partly in a throwaway Docker daemon. No repository file changed except this write-up: every prototype lived in a scratch folder outside the repository and was deleted afterwards. The essential parts are quoted below. The real relay route is step A3 and the real init is step A2.

## Result in one paragraph

A relay built on the app's own `ThreadingHTTPServer` and `BaseHTTPRequestHandler` (Python 3.14.6) carried noVNC end to end: Chromium 153 loaded noVNC's page and its modules through the relay, opened the WebSocket through it, and drew the Xvfb screen. That screen came from x11vnc listening only on a Unix socket, reached through websockify. Behind nginx 1.24 as the stand-in reverse proxy, one session stayed up for 614 s. It saw each of five screen changes within 5 s, carried only binary frames, and closed nothing. The client then got back each way it was dropped: the page closed and reopened, the proxy restarted under an open session (noVNC reconnected by itself), and a page reload. Two things matter for A3 and the deployment. nginx's default 60 s read timeout cut an idle viewer, because x11vnc sends next to nothing while the screen does not change, so the proxy in front needs a long timeout, and Traefik's behaviour here is NOT VERIFIED. And noVNC 1.3 reconnects by itself only after a clean close. The token handoff worked as designed. In a container run as uid 99 with every capability dropped, the token was absent from every `/proc/<pid>/environ` and `cmdline`, from `ps auxeww` on the host, from `docker logs`, from `docker inspect` and from every file in the container's filesystem. A positive control planted the token, and the same scanners then found it. A worker killed by hand was restarted with the same token and was never refused. Two results change what section 3 says the init does. It must keep the token in memory for the container's whole life, not "generate and forget" it. It must also stop a restarted worker's whole process group, because the stand-in browser outlived its worker. For the fallback, the atomic write (temporary file, `fchmod 0640`, `fsync`, rename, `fsync` of the folder) gave no partial or missing read in 5.8 million reads against 1,385 rewrites. Two containers sharing a bind-mounted folder (read-write in one, read-only in the other) paired, and a worker answered 401 after the app restarted read the file again and carried on. Traefik, Unraid and the display stack's Debian 13 versions were not verified.

## Versions

| Piece | Version |
| --- | --- |
| Host | Ubuntu 24.04.5 (the base of today's worker image), Linux 6.18, x86_64, a cloud container with no Yama LSM (not Unraid) |
| Display stack (Ubuntu's packages, as `Dockerfile.browser` installs them) | xvfb 21.1.12, x11vnc 0.9.16, novnc 1.3.0, websockify 0.10.0 |
| Reverse proxy (stand-in for Traefik) | nginx 1.24.0 (Ubuntu) |
| Relay | Python 3.14.6 (the repository's Poetry environment), standard library only |
| Client | Playwright 1.63.0 for Node.js 22.23.4, driving its bundled Chromium build 1243 (Chromium 153.0.8010.12), headless |
| Docker Engine | 29.9.2 (overlay2, cgroup v1) |
| Container image for the handoff and pairing runs | `python:3.14-slim` (`sha256:9aaa4740…f04802f`, the same digest as S1), Python 3.14.10 |

## 1. The viewer relay

### Setup

Everything ran on the host's loopback, as root (the Docker runs below used uid 99):

```text
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp
x11vnc -display :99 -unixsock <private dir>/vnc -rfbport 0 -forever -shared -nopw -noxdamage -quiet
websockify --web=/usr/share/novnc --web-auth --auth-plugin BasicHTTPAuth --auth-source "darkroom:<viewer secret>" \
           --unix-target=<private dir>/vnc 127.0.0.1:6080
python relay.py                      # ThreadingHTTPServer on loopback port 8001, routes /viewer/*
nginx (proxy_pass to the relay, proxy_http_version 1.1, Upgrade and Connection headers, proxy_buffering off)
```

The private directory was mode `0700`. x11vnc with `-rfbport 0 -unixsock` opened no TCP port (`ss -ltn` showed none, `ss -lx` showed the socket). websockify kept today's `BasicHTTPAuth` with a generated viewer secret, as `deploy/browser/entrypoint.sh` does.

The relay is a `BaseHTTPRequestHandler` served by a `ThreadingHTTPServer` with `daemon_threads`, like `darkroom/server/handler.py`. For `GET /viewer/<rest>` it does the following:

1. Checks a session cookie (a made-up value standing in for `auth.session_principal`) and answers 401 without one.
2. For an upgrade, checks that `Origin` is the one configured origin and answers 403 otherwise.
3. Connects to the fixed upstream (`127.0.0.1:6080`, a constant, never taken from the request) and writes the request line as `GET /<rest> HTTP/1.1`. It forwards the client's headers except `Cookie`, `Authorization`, `Host` and `Content-Length`, so the session cookie never reaches websockify, and adds websockify's `Authorization: Basic` itself. A request that is not an upgrade gets `Connection: close`.
4. Copies bytes both ways until either side closes. The upstream's reply, the `101` included, goes back to the client untouched.

The core of it:

```python
upstream = socket.create_connection(UPSTREAM, timeout=10)
upstream.settimeout(None)
upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

def downstream():
    try:
        while chunk := upstream.recv(65536):
            self.connection.sendall(chunk)
    except OSError:
        pass
    finally:
        self.connection.shutdown(socket.SHUT_RDWR)

threading.Thread(target=downstream, daemon=True).start()
while chunk := self.rfile.read1(65536):
    upstream.sendall(chunk)
upstream.shutdown(socket.SHUT_RDWR)
self.close_connection = True
```

Client to upstream reads through `self.rfile.read1`, not the raw socket, so nothing the handler has already buffered is lost. Each open viewer holds two threads for its lifetime: the handler's and the copier.

The client was a Playwright script. It set the session cookie, opened `/viewer/vnc.html?autoconnect=true&path=viewer/websockify&resize=off&reconnect=true&reconnect_delay=3000` through nginx, and waited for noVNC's `noVNC_connected` state. It then read the pixel at the centre of noVNC's canvas and compared it with the colour the script had last set on the X root window with `xsetroot`. It also counted the WebSocket's frames by type (`page.on('websocket')`).

### Observations

1. **noVNC through the relay works.** `vnc.html` and its modules came through the relay as plain GETs, and the WebSocket upgraded through nginx and the relay. The canvas came up at 1280×900 showing the root window's colour. Directly against the relay without nginx it worked the same.
2. **Ten minutes.** One session, with nginx's read and send timeouts at 1 h, ran for 610 s. The script sampled every 30 s and changed the screen's colour every 120 s, so the stream stood idle for up to two minutes at a time. All 20 samples were connected and showed the expected colour within 5 s. The browser saw one WebSocket, no close, 18 binary frames in and 14 out, and no text frame. The relay logged the session as 614.5 s, 1,530 bytes down and 302 up: x11vnc sends only changes, and a solid fill compresses to almost nothing.
3. **Reconnect.** After the soak the script closed the page, opened a new one in the same browser context, and it was connected in about 4 s with the new colour. Then nginx was stopped and started again under the open session. noVNC saw a clean close and reconnected by itself after its 3 s delay, and the next colour change showed. Then the page was reloaded and connected again. So the relay holds no state that a reconnect needs.
4. **noVNC's own reconnect is narrow.** In noVNC 1.3 (`app/ui.js`, `disconnectFinished`), automatic reconnect runs only after a clean close, and it stops for good when an attempt fails. In a trial where nginx was down for one second, the single retry failed and noVNC stayed disconnected until the page was reloaded. The Remote browser page should therefore offer a reconnect or reload of its own rather than rely on noVNC's.
5. **An idle viewer is cut by the proxy's timeout.** With nginx's default `proxy_read_timeout` of 60 s, a session on an unchanging screen closed 107 s after connecting, about 60 s after the last frame. nginx's log showed `upstream timed out (110: Connection timed out) while proxying upgraded connection`. x11vnc sent nothing for long stretches while nothing changed, and noVNC sends no keepalive. The relay is a byte relay, so it cannot add WebSocket pings without parsing frames. So whatever stands in front must allow a long idle upgraded connection (nginx: `proxy_read_timeout`). For Traefik that is NOT VERIFIED: its timeouts for an upgraded connection were not tested, and nothing should assume a default.
6. **Refusals.** Through nginx, `curl` got 401 for `vnc.html` without the cookie, 200 with it, and 403 for an upgrade carrying the cookie but a foreign `Origin`. These are the relay's own checks, not the app's: A3's tests for no session, wrong origin and a fixed target still have to be written against the real route.
7. **The 101 status line.** `BaseHTTPRequestHandler` defaults to `protocol_version = "HTTP/1.0"`, as the app's `Handler` does. A relay variant that rebuilt websockify's reply with `send_response(101)` sent `HTTP/1.0 101 Switching Protocols`. Chromium 153 accepted it directly, and nginx accepted it and sent `HTTP/1.1 101` on to the client. RFC 6455 asks for HTTP/1.1, though, and Traefik was not tried. A3 should forward the upstream's reply bytes untouched, as above, or set `protocol_version = "HTTP/1.1"` on the relay's handler. It should not rely on clients tolerating 1.0.
8. **Unix sockets (section 9, C7a).** x11vnc 0.9.16 serves VNC on a Unix socket only (`-unixsock`, `-rfbport 0`), and websockify 0.10.0 reaches it with `--unix-target`. websockify 0.10.0 cannot itself listen on a Unix socket: it has no `--unix-listen`, so in this setup its listener stayed a loopback TCP port. Whether Debian 13's websockify 0.12.0 (the merged image's, S1) can is NOT VERIFIED. The other way to close that port is to drop websockify and have the relay speak WebSocket to x11vnc's socket itself, which was not tried.
9. **The viewer secret is on a command line.** As in today's `entrypoint.sh`, websockify gets its Basic-auth secret in `--auth-source`, so it shows in `ps` and `/proc/<pid>/cmdline` to every process of the same user (seen here: `ps` listed it). In two containers that was the worker container's business only. In one container every process (the app, Syncthing, the worker, Chrome) runs as the one user and can read it. With the relay holding the secret and websockify reachable only from inside, `BasicHTTPAuth` adds little beyond what C7a's Unix sockets give. A2 or C7a should either pass it some other way or drop it in favour of the socket.

## 2. The token handoff on a pipe

### The prototype

A parent ("init") generates the token with `secrets.token_urlsafe(32)` (43 printable ASCII characters, which passes `config.py`'s 32-character check). It starts two children, a stand-in app (a `ThreadingHTTPServer` that answers 200 to the right `Authorization: Bearer` and 401 otherwise, compared with `hmac.compare_digest`) and a stand-in worker that polls it every second. The worker also starts a long `sleep` as a stand-in for Chrome. Each child gets its own pipe:

```python
def start(name, token):
    read_end, write_end = os.pipe()
    child = subprocess.Popen([sys.executable, script[name], "--token-fd", str(read_end)],
                             pass_fds=(read_end,), env={**BASE_ENV, **extra_env[name]}, cwd="/")
    os.close(read_end)
    os.write(write_end, token.encode())
    os.close(write_end)
    return child
```

and each child reads it once, at start, to the end, and closes it:

```python
def read_token(fd):
    chunks = []
    while chunk := os.read(fd, 4096):
        chunks.append(chunk)
    os.close(fd)
    return b"".join(chunks).decode()
```

`BASE_ENV` is `PATH`, `HOME`, `LANG` and `PYTHONDONTWRITEBYTECODE`. The worker also gets `DARKROOM_URL` (loopback, not secret). Only the fd number appears on the command line. When a child exits, the init waits one second and starts it again with the same token.

To check for the token without putting it anywhere, a test harness started the init and received the token on a separate inherited pipe (a test-only `--reveal-fd`). The harness kept it in memory and answered on a Unix socket in a private tmpfs. Asked to, it scanned every `/proc/<pid>/environ` and `cmdline`, every regular file under a root, or whatever was piped to it, and reported matches of the token, its hex and its base64. So `docker inspect … | ask DATA` searched `docker inspect`'s output without the token ever being on a command line.

### Runs

On the host, then in a container: `docker run --init --user 99:100 --cap-drop=ALL --security-opt no-new-privileges:true -v <prototype>:/s2:ro --tmpfs /run/s2:uid=99,gid=100,mode=0700 python:3.14-slim python /s2/harness.py /run/s2/ask.sock`. In the container, the worker was killed with `SIGTERM` before the checks.

| Check | Host | Container, negative | Container, positive control |
| --- | --- | --- | --- |
| Every `/proc/<pid>/environ` and `/proc/<pid>/cmdline` | 0 matches | 0 | 3 (the planted process's environment and argv) |
| `ps auxeww` on the host | 0 | 0 (the container's processes included) | 3 |
| Logs (the children's stdout and stderr; in the container `docker logs`) | 0 | 0 | 1 (the planted log line) |
| `docker inspect` of the container | not applicable | 0 | 0 (the token is made after the container's configuration is fixed, so it cannot be there) |
| Every regular file under `/` in the container, except `/proc`, `/sys` and `/dev` (4,481 files) | not run (scanning the host's `/tmp` took too long and was stopped) | 0 | 1 (the planted file) |

For the positive control, the harness planted the token in a new process's environment and arguments (`sh -c 'sleep 300' <token>` with `CANARY=<token>`), in a file in its tmpfs and in one log line. A first attempt planted it as an argument to `sleep` itself. `sleep` exited at once and printed the token in its error, so the first control run found the token only in the logs. Both observations are recorded here.

In the container, every process ran as uid 99. The children's environments held only the allowlisted names (`PATH`, `HOME`, `LANG`, `PYTHONDONTWRITEBYTECODE`, and `DARKROOM_URL` for the worker), not the image's `PYTHON_VERSION`, `PYTHON_SHA256` or `HOSTNAME`. After its read, the worker held only fds 0, 1 and 2. The app held those and its listening socket. The stand-in browser had inherited no extra fd. After the worker was killed, the init logged `child exited, restarting with the same token`, the new worker's polls were all answered 200, and no request was answered 401.

### Two more findings

- **The stand-in browser outlived its worker.** After the worker was killed, its `sleep` was still running (adopted by `tini`). A real Chrome left behind this way would keep the profile locked for the next worker.
- **Memory is the remaining place the token lives.** A probe in the same container setup (uid 99, every capability dropped, `no-new-privileges`) read a secret out of another uid 99 process's memory through `/proc/<pid>/mem`. The reading process was the holder's parent. This host has no Yama LSM, and without Yama the kernel gives any process of the same user the same access, but a non-parent reader was not run separately. When the holder had first called `prctl(PR_SET_DUMPABLE, 0)`, the read was refused with `EACCES`. Section 5 already accepts that one user separates nothing. This finding just says where the token can still be read.

### What the init in section 3 therefore must do

- Generate the token in the init, hand it to each child on a pipe of its own inherited through `pass_fds`, with the default `close_fds=True`, and put only the fd number on the command line. Write the token, close both ends in the init, and never put the token in any child's environment.
- Have each child read the pipe to its end and close it before it starts anything else: the worker before Chrome, the app before its server. Neither may ever put the token in `os.environ`.
- **Keep the token in memory for the whole life of the container.** Handing a restarted child the same token (section 6) means the init cannot forget it after the first handoff. Section 3's "holds no secret beyond the browser token it generates and forgets" should read "holds the browser token in memory only, for the container's life, and never writes or logs it".
- Start the worker in its own process group or session (`start_new_session=True`) and stop the whole group when the worker exits or is restarted, so Chrome does not outlive it.
- Log only fixed texts, and at most the token's length. A2's `no_leaks` canary should cover the init's, the app's and the worker's output, and `check-config`.
- Optionally, have the app, the worker and the init mark themselves non-dumpable. `PR_SET_DUMPABLE` resets on `execve`, so each process sets it itself, early. That keeps a same-user process from reading the token out of memory or ptracing them. It is hygiene under decision 6, not a barrier, and its effect on Chrome's sandbox and on crash reports was not tested.

## 3. Fallback only: the shared pairing folder

### The write and the read

```python
def write_token(folder, token):
    temporary = os.path.join(folder, f".token.{secrets.token_hex(8)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        os.fchmod(fd, 0o640)
        os.write(fd, token.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, os.path.join(folder, "token"))
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_token(folder):
    try:
        fd = os.open(os.path.join(folder, "token"), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        return os.read(fd, 4096).decode()
    finally:
        os.close(fd)
```

The app writes the file before it starts listening, so any 401 from a new app means the new file is already there. The worker reads the file at start, logs `waiting for the pairing file` every 2 s while it is missing, and on a 401 reads it again before the next poll.

### Observations

1. **Atomic.** One process rewrote the token in a loop for 10 s (1,385 writes, slowed by the two `fsync` calls) while another read it in a loop: 5,797,219 reads, none missing, none partial or of the wrong length, and no temporary file left behind.
2. **Mode.** With `umask 077`, `os.open(…, 0o640)` alone gave `0600`. With the explicit `fchmod` it gave `0640`. With one user that does not matter for reading, but if the fallback ever uses a group the `fchmod` is needed.
3. **Two processes on the host.** With the worker started before the app, it waited (two `waiting for the pairing file` lines), then polled successfully. The app was then stopped and a new one started with a new token. The worker logged `app not reachable` twice, then one `answered 401, reading the pairing file again`, then carried on with 200s.
4. **Two containers.** On one Docker network, both as `--user 99:100 --cap-drop=ALL --security-opt no-new-privileges:true`, a host folder owned by 99:100 with mode `0750` was bind-mounted read-write in the app's container and read-only in the worker's. The worker started first and waited, then paired. After `docker restart` of the app, the worker answered one 401 by reading the file again, and the app logged 14 requests answered 200 and one 401. From the worker, the file was `0640`, owned by 99:100, and the folder held only `token`. A write from the worker failed with `Read-only file system`. The token was absent from `docker inspect` and `docker logs` of both containers, from both containers' processes' `/proc/<pid>/environ`, and from `ps auxeww` (each search was `grep -cF -f <the token file>`, so the token was never on a command line). The same `grep` found it in the file itself as the control.
5. **A named volume is root-owned.** A fresh named Docker volume mounted into a `--user 99:100` container was `0755 root:root`, and the write was refused (`Permission denied`). A named pairing volume would need its ownership set first, for example from a directory in the image that the volume copies on first mount, which was not tried. A host folder in appdata, as on Unraid, has no such problem, since it can be created owned by 99:100 (Unraid's usual owner for appdata, UNVERIFIED on the owner's server).

## Not verified, and what would settle each

| Item | Why not | What would settle it |
| --- | --- | --- |
| Traefik in front of the relay (the upgrade, binary frames, its timeouts on an idle upgraded connection, its forward-auth staying out of the way) | No Traefik here; nginx stood in | Run the A3 route on Unraid behind the owner's Traefik with a made-up route: a ten-minute session including an idle stretch of several minutes, then the three reconnects of observation 3 |
| The relay inside the real app (`routes.py`, `auth.session_principal`, `auth.same_origin`, the CSP and the Remote browser page's frame) | Out of scope: no repository code | A3's route tests and `tests/test_every_route.py`, then a `make verify` run of the Remote browser page |
| The display stack at Debian 13's versions (x11vnc 0.9.17, novnc 1.6.0, websockify 0.12.0) in the merged image, as uid 99 under `deploy/browser/seccomp.json` | Ubuntu 24.04's packages were run directly on the host as root | The same soak inside the merged image of S1 under `$SANDBOX` (A1's image CI could run the Playwright client) |
| websockify listening on a Unix socket, or the relay speaking WebSocket to x11vnc directly | websockify 0.10.0 has no Unix listener; the second was not tried | Try `--unix-listen` with websockify 0.12.0 (C7a) |
| A real person's browser over a LAN (latency, a long session with real use, a phone) | Headless Chromium on loopback only | The Unraid run above, from a desktop and a phone |
| The handoff under the real init with the real app and worker | Stand-ins only | A2's tests and its image CI `noop` job, with the checks of section 2 repeated |
| The handoff under `deploy/browser/seccomp.json` | The Docker runs used Docker's default seccomp profile | Repeat the container run with `$SANDBOX` (S1's flags) |
| `PR_SET_DUMPABLE` with Chrome and its sandbox | Only a Python probe was run | A2: set it in the worker and run `smoke.sh` and `chrome://sandbox` |
| A non-parent same-uid process reading the token from memory | The probe's reader was the holder's parent | One more probe with an unrelated uid 99 process |
| The pairing folder on Unraid (appdata share modes, the user-share layer, SMB) | No Unraid host | S4, with the appdata folder as the pairing folder |
| A named Docker volume prepared for 99:100 | Not tried | Mount a named volume over an image directory owned by 99:100 and repeat observation 4 |
