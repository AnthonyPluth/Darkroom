#!/usr/bin/env bash
set -eo pipefail

image="$1"
port="$2"
here=$(cd "$(dirname "$0")" && pwd)
code=$(cat "$here/syncthing_probe.py")
sandbox=(--cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json)
net=syncthing-peer
darkroom=st-darkroom
peer=st-peer
differs=3
share_changed=3
peer_changed=4
work=$(mktemp -d)
started=""

cleanup() {
  docker rm -f "$darkroom" "$peer" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
  sudo rm -rf --one-file-system "$work" || true
}
trap cleanup EXIT

probe() { docker exec -i "$1" python3 -c "$code" "${@:2}"; }
seed() { docker exec -i -w /app "$darkroom" python - "$@" < "$here/syncthing_seed.py"; }
host() { sudo python3 -I "$here/syncthing_host.py" "$@"; }
logs() { for name in "$darkroom" "$peer"; do docker container inspect "$name" >/dev/null 2>&1 || continue; docker logs "$name" 2>&1 | grep -v '"event": "request"' | tail -n 60 >&2 || true; done; }
fail() { echo "$1" >&2; logs; exit 1; }
wait_for() {
  for _ in $(seq 1 "$1"); do
    if eval "$2"; then return 0; fi
    sleep 1
  done
  fail "timed out: $2"
}
field() { python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])[sys.argv[2]], sort_keys=True))' "$1" "$2"; }
logged_since_start() { local logs; logs=$(docker logs --since "$started" "$darkroom" 2>&1) && grep -qF "$1" <<<"$logs"; }
start_darkroom() {
  started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  docker "$@" >/dev/null
  wait_for 90 "curl -fsS -o /dev/null http://127.0.0.1:$port/healthz 2>/dev/null"
  wait_for 60 "logged_since_start '\"event\": \"syncthing is a tested release; the transport may start\"'"
}
folder_state() { probe "$darkroom" folder "$folder"; }
folder_gone() { [ "$(folder_state)" = null ]; }
folder_running() { local state; state=$(folder_state) && grep -qF '"paused": false' <<<"$state"; }
peer_files() { docker exec "$peer" find /data/pixel -name 'IMG_*.jpg' -type f | wc -l; }
snapshot() { host snapshot "$work/share" > "$work/$1.share.json"; seed copies > "$work/$1.copies.json"; host snapshot "$work/peer/pixel" > "$work/$1.peer.json"; }
unchanged() {
  host snapshot "$work/share" > "$work/$1.share.after.json"
  seed copies > "$work/$1.copies.after.json"
  local found=0
  host compare "$work/$1.share.json" "$work/$1.share.after.json" "$work/$1.copies.json" "$work/$1.copies.after.json" || found=$?
  if [ "$found" = "$differs" ]; then echo "$2" >&2; logs; exit "$share_changed"; fi
  [ "$found" = 0 ] || fail "the share could not be compared"
}
peer_unchanged() {
  host snapshot "$work/peer/pixel" > "$work/$1.peer.after.json"
  local found=0
  host compare "$work/$1.peer.json" "$work/$1.peer.after.json" || found=$?
  if [ "$found" = "$differs" ]; then echo "$2" >&2; logs; exit "$peer_changed"; fi
  [ "$found" = 0 ] || fail "the peer's folder could not be compared"
}

sudo install -d -o 99 -g 100 -m 0755 "$work/share" "$work/data" "$work/peer"
docker rm -f "$darkroom" "$peer" >/dev/null 2>&1 || true
docker network rm "$net" >/dev/null 2>&1 || true
docker network create "$net" >/dev/null
docker run -d --name "$peer" --network "$net" "${sandbox[@]}" -v "$work/peer:/data" --entrypoint /usr/bin/env "$image" \
  PYTHONPATH=/opt/init python3 -P -m darkroom_init.syncthing /data/syncthing >/dev/null
wait_for 60 "probe $peer id >/dev/null 2>&1"
theirs=$(probe "$peer" id)
docker exec "$peer" mkdir -p /data/pixel

start_darkroom run -d --name "$darkroom" --network "$net" "${sandbox[@]}" --shm-size=1g -v "$work/share:/photos" -v "$work/data:/data" \
  -e OIDC_ISSUER=https://id.example -e OIDC_CLIENT_ID=darkroom -e DARKROOM_PUBLIC_URL=http://127.0.0.1:8000 -p "$port:8000" "$image"
mine=$(probe "$darkroom" id)
pixel=$(seed seed "$theirs") || fail "the made-up Pixel could not be added"
folder="darkroom-pixel-$pixel"
bound=$(printf 'paused\nbind\n' | docker exec -i -w /app "$darkroom" python run.py bind-share) || fail "bind-share did not bind the share"
grep -qF "Syncthing is Darkroom's own" <<<"$bound" || fail "bind-share did not word its prompt for Darkroom's own Syncthing"
echo "a made-up Pixel with the peer's device ID, five queued copies and a bound share"

start_darkroom restart "$darkroom"
wait_for 60 "[ \"\$(seed placed)\" = 5 ]"
probe "$peer" add "$mine" "tcp://$darkroom:22000" "$folder" /data/pixel sendreceive
wait_for 180 folder_running
wait_for 180 "[ \"\$(peer_files)\" = 5 ]"
wait_for 60 "host matches $work/share/_pixels/$pixel $work/peer/pixel"
echo "after a restart and a tend, Darkroom placed five copies, approved and resumed the Pixel's folder, and the peer received them"

snapshot hostile
docker exec "$peer" sh -c 'set -e; cd /data/pixel/ci/2026; printf more >> IMG_0001.jpg; head -c 5000 /dev/urandom > IMG_0002.jpg; rm IMG_0003.jpg; mv IMG_0004.jpg IMG_0040.jpg
  head -c 4000 /dev/urandom > IMG_9001.jpg; printf "<x/>" > IMG_9001.xmp; mkdir -p ../added; head -c 3000 /dev/urandom > ../added/IMG_9002.jpg; printf "IMG_0005.jpg\n" > /data/pixel/.stignore'
probe "$peer" scan "$folder"
wait_for 120 "probe $darkroom knows $folder ci/2026/IMG_9001.jpg"
sleep 20
probe "$darkroom" scan "$folder"
sleep 10
unchanged hostile "the peer's edits, deletions, additions or rename reached the share"
[ "$(probe "$darkroom" need "$folder")" -gt 0 ] || fail "Darkroom's Syncthing did not see the peer's changes, so the comparison proved nothing"
shown=$(folder_state)
[ "$(field "$shown" type)" = '"sendonly"' ] && [ "$(field "$shown" path)" = "\"/photos/_pixels/$pixel\"" ] || fail "the Pixel's folder is not Send Only at its path: $shown"
echo "the peer appended to, overwrote, deleted, renamed and added files and replaced its ignores; the whole share kept its SHA-1 and Darkroom's Syncthing pulled none of it"

snapshot removal
seed device "$pixel" - || fail "the Pixel's device ID could not be cleared"
wait_for 150 folder_gone
facts=$(probe "$darkroom" facts)
python3 -c 'import json,sys; sys.exit(set(json.loads(sys.argv[1])["devices"]) != {sys.argv[2]})' "$facts" "$mine" || fail "the Pixel's device stayed configured: $facts"
sleep 10
unchanged removal "removing the Pixel's folder from Syncthing's configuration changed the share"
peer_unchanged removal "removing the Pixel's folder from Darkroom's configuration changed the peer's copy"
[ "$(peer_files)" -gt 0 ] || fail "the peer lost its files"
echo "with the Pixel's device ID cleared, the folder and device left the configuration, and both sides kept every file"

seed device "$pixel" "$theirs" || fail "the Pixel's device ID could not be set again"
wait_for 150 folder_running
wait_for 120 "probe $darkroom connected $theirs"
docker exec "$peer" sh -c 'head -c 4000 /dev/urandom > /data/pixel/ci/2026/IMG_9003.jpg'
probe "$peer" scan "$folder"
wait_for 120 "probe $darkroom knows $folder ci/2026/IMG_9003.jpg"
snapshot persisted
docker stop "$darkroom" >/dev/null
host retype "$work/data/syncthing/config.xml" "$folder" sendreceive
start_darkroom start "$darkroom"
wait_for 60 "seed removed $pixel 2>/dev/null"
folder_gone || fail "the persisted Send and Receive folder is still configured"
sleep 75
folder_gone || fail "the Pixel's folder came back after it was removed"
! probe "$darkroom" connected "$theirs" || fail "Darkroom's Syncthing connected to the peer after removing its folder"
unchanged persisted "a persisted folder that was not Send Only ran and changed the share"
docker exec "$peer" test -f /data/pixel/ci/2026/IMG_9003.jpg || fail "the peer lost the file it added"
echo "a persisted Send and Receive folder for the Pixel was removed while paused, before it ever ran; the share kept its SHA-1 and the peer kept its files"
