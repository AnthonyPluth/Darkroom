#!/usr/bin/env bash
set -eo pipefail

image="$1"
port="$2"
here=$(cd "$(dirname "$0")" && pwd)
code=$(cat "$here/syncthing_probe.py")
sandbox=(--cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json)
net=syncthing-ci
photo=/photos/_pixels/ci/IMG_0001.jpg

probe() { docker exec -i "$1" python3 -c "$code" "${@:2}"; }
fail() { echo "$1" >&2; for name in darkroom peer; do docker logs "$name" 2>&1 | grep -v '"event": "request"' | tail -n 40 >&2 || true; done; exit 1; }
wait_for() {
  for _ in $(seq 1 "$1"); do
    if eval "$2"; then return 0; fi
    sleep 1
  done
  fail "timed out: $2"
}
field() { python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])[sys.argv[2]], sort_keys=True))' "$1" "$2"; }
logged() { docker logs "$1" 2>&1 | grep -qF "$2"; }

docker network create "$net" >/dev/null
docker run -d --name darkroom --network "$net" "${sandbox[@]}" --shm-size=1g --tmpfs /photos:uid=99,gid=100 -e DARKROOM_SYNCTHING=1 -e OIDC_ISSUER=https://id.example \
  -e OIDC_CLIENT_ID=darkroom -e DARKROOM_PUBLIC_URL=http://127.0.0.1:8000 -p "$port:8000" "$image" >/dev/null
wait_for 90 "curl -fsS -o /dev/null http://127.0.0.1:$port/healthz"
wait_for 30 "logged darkroom 'syncthing started paused; its API key was rotated'"
logged darkroom '"event": "syncthing is a tested release; the transport may start"' || fail "the app did not read a tested Syncthing version with the key it was handed"
! logged darkroom '"event": "syncthing transport kept off"' || fail "the app kept the transport off"

facts=$(probe darkroom facts) || fail "Syncthing's API did not answer on its socket"
echo "Syncthing in the one container: $facts"
[ "$(field "$facts" version)" = '"v2.1.7"' ] && [ "$(field "$facts" release)" = true ] || fail "Syncthing is not the pinned release"
[ "$(field "$facts" devices)" = "{\"$(probe darkroom id)\": true}" ] || fail "Syncthing did not start paused"
[ "$(field "$facts" options)" = '{"autoUpgradeIntervalH": 0, "crashReportingEnabled": false, "globalAnnounceEnabled": false, "localAnnounceEnabled": false, "natEnabled": false, "relaysEnabled": false, "setLowPriority": false, "urAccepted": -1}' ] \
  || fail "upgrades, reporting, discovery, relays or NAT are not off"
[ "$(field "$facts" socket_mode)" = '"0o600"' ] && [ "$(field "$facts" socket_folder_mode)" = '"0o700"' ] || fail "the API socket is not private"
python3 -c 'import json,sys; ports = json.loads(sys.argv[1])["listening"]; sys.exit(8384 in ports or 22000 not in ports)' "$facts" || fail "Syncthing's API listens on TCP, or its sync port is not up"
address=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' darkroom)
! curl -sS --max-time 3 -o /dev/null "http://$address:8384/rest/noauth/health" 2>/dev/null || fail "Syncthing's API answers from outside the container"
docker exec -i -w /app darkroom python - scan < "$here/one_container.py" || fail "a secret-shaped value is in a process's environment or command line with Syncthing on"
[ "$(docker logs darkroom 2>&1 | probe darkroom key-in-stdin)" = absent ] || fail "Syncthing's API key is in the container's log"
echo "Syncthing v2.1.7 started paused, its API only on a private socket, its key in no log, environment or command line"

docker run -d --name peer --network "$net" "${sandbox[@]}" --tmpfs /data:uid=99,gid=100 --entrypoint /usr/bin/env "$image" \
  PYTHONPATH=/opt/init python3 -P -m darkroom_init.syncthing /data/syncthing >/dev/null
wait_for 60 "probe peer id >/dev/null 2>&1"
docker exec peer python3 -c 'import socket,sys; s = socket.socket(); s.settimeout(3); sys.exit(s.connect_ex(("darkroom", 8384)) == 0)' || fail "Syncthing's API is reachable from another container"
mine=$(probe darkroom id)
theirs=$(probe peer id)
docker exec darkroom sh -c "mkdir -p /photos/_pixels/ci && head -c 200000 /dev/urandom > $photo"
probe darkroom add "$theirs" tcp://peer:22000 ci /photos/_pixels/ci sendonly
probe darkroom restart || fail "the init did not restart Syncthing"
facts=$(probe darkroom facts)
python3 -c 'import json,sys; f = json.loads(sys.argv[1]); sys.exit(not (set(f["devices"]) == set(sys.argv[2:]) and all(f["devices"].values()) and f["folders"] == {"ci": True}))' "$facts" "$mine" "$theirs" \
  || fail "a restart did not pause the configured device and folder: $facts"
docker exec peer mkdir -p /data/received
probe peer add "$mine" tcp://darkroom:22000 ci /data/received receiveonly
sleep 30
! probe darkroom connected "$theirs" || fail "the paused instance connected to the peer"
docker exec peer test ! -e /data/received/IMG_0001.jpg || fail "the paused instance sent a file"
echo "with the device and folder configured and the peer waiting, nothing connected or synced for 30 s"
probe darkroom resume "$theirs" ci
wait_for 120 "docker exec peer test -f /data/received/IMG_0001.jpg"
[ "$(docker exec darkroom sha256sum "$photo" | cut -d' ' -f1)" = "$(docker exec peer sha256sum /data/received/IMG_0001.jpg | cut -d' ' -f1)" ] || fail "the peer got a different file"
echo "once resumed, the file reached the peer"

docker stop darkroom >/dev/null
docker logs darkroom 2>&1 | grep -F '"event": "child stopped"' | grep -o '"child": "[a-z]*"' | tr '\n' ' ' \
  | grep -Fx '"child": "worker" "child": "viewer" "child": "vnc" "child": "display" "child": "app" "child": "syncthing" ' || fail "the init did not stop Syncthing last"
docker rm -f darkroom peer >/dev/null
docker network rm "$net" >/dev/null
