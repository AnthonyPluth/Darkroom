#!/usr/bin/env bash
set -eo pipefail

image="$1"
here=$(cd "$(dirname "$0")" && pwd)
name=host-network
sandbox=(--cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json)

probe() { docker exec -i -w /app "$name" python - "$@" < "$here/one_container.py"; }
check() { python3 -I "$here/host_network.py" "$@"; }
fail() { echo "$1" >&2; docker logs "$name" 2>&1 | grep -v '"event": "request"' | tail -n 60 >&2 || true; docker rm -f "$name" >/dev/null 2>&1 || true; exit 1; }
wait_for() {
  for _ in $(seq 1 "$1"); do
    if eval "$2"; then return 0; fi
    sleep 1
  done
  fail "timed out: $2"
}
logged() { docker logs "$name" 2>&1 | grep -F "$1" | grep -F "${2:-}" >/dev/null; }
container_pids() { docker top "$name" -eo pid | tail -n +2 | tr -s ' \n' ' '; }

docker run -d --name "$name" --network host "${sandbox[@]}" --shm-size=1g --tmpfs /photos:uid=99,gid=100 -e OIDC_ISSUER=https://id.example \
  -e OIDC_CLIENT_ID=darkroom -e DARKROOM_PUBLIC_URL=http://127.0.0.1:8000 "$image" >/dev/null
wait_for 90 "curl -fsS -o /dev/null http://127.0.0.1:8000/healthz"
wait_for 30 "logged 'syncthing started paused; its API key was rotated'"
wait_for 60 "logged '\"event\": \"syncthing reconciled\"' '\"failed\": false'"
echo "with host networking the app answers on 8000 and the reconciler reached Syncthing on its socket"

wait_for 60 "probe viewer >/dev/null"
probe relay || fail "the remote browser view does not come through the app's relay with host networking"
echo "the remote browser view comes through the relay to x11vnc over the private sockets"

outside=$(hostname -I | awk '{print $1}')
check refused 127.0.0.1 ::1 "$outside" || fail "an internal listener answers from outside the container with host networking"
sudo -u nobody python3 -I - refused 127.0.0.1 ::1 "$outside" < "$here/host_network.py" || fail "an internal listener answers another local user"
docker exec -i -u 4242:4242 "$name" python3 -I - inside < "$here/host_network.py" || fail "an internal listener or socket answers another user inside the container, or the worker's routes answer on 8000"
echo "5900, 6080 and 8384 are refused from the host, from another local user and from another user inside the container; the display has no abstract socket"
echo "the worker's routes answer 404 on 8000 and its socket in /run/darkroom refuses another user inside the container"

pids=$(container_pids)
sudo ss -Hltunp | check listeners $pids || fail "something other than 8000, 22000 and 21027 listens, or 8000 or 22000 does not"
sudo ss -Hlxp | check unix-listeners $pids || fail "a listener of the container is an abstract socket, or an internal socket (the worker's API among them) is missing"
echo "only 8000, 22000 and 21027 listen; every internal listener, the worker's API included, is a socket in /run/darkroom"

probe scan || fail "a secret-shaped value is in a process's environment or command line with host networking"
docker rm -f "$name" >/dev/null
