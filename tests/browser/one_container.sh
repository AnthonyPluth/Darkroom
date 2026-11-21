#!/usr/bin/env bash
set -eo pipefail

name="$1"
port="$2"
here=$(cd "$(dirname "$0")" && pwd)

probe() { docker exec -i -w /app "$name" python - "$@" < "$here/one_container.py"; }
healthy() { curl -fsS -o /dev/null "http://127.0.0.1:$port/healthz"; }
fail() { echo "$1" >&2; docker logs "$name" 2>&1 | grep -v '"event": "request"' | tail -n 60 >&2; exit 1; }
wait_for() {
  for _ in $(seq 1 "$1"); do
    if eval "$2"; then return 0; fi
    sleep 1
  done
  fail "timed out: $2"
}
wait_until() {
  while [ "$(date +%s)" -le "$1" ]; do
    if eval "$2"; then return 0; fi
    sleep 1
  done
  fail "timed out: $2"
}
job_done() { [ "$(probe status "$1")" = done ]; }
field() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"; }

wait_for 60 healthy
worker_route() { curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data '{}' "http://127.0.0.1:$port/internal/browser/jobs/claim"; }
[ "$(worker_route)" = 404 ] || fail "the worker's routes answer on the web port; they belong only on the private socket"
echo "the worker's routes answer 404 on the web port"
wait_for 60 '[ "$(field "$(probe)" workers | tr -cd , | wc -c)" = 0 ] && [ "$(field "$(probe)" workers)" != "[]" ]'
probe viewer || fail "websockify is not locked with a secret given on a pipe"
probe csp || fail "the shipped noVNC does not load under the viewer's policy through the relay"
probe relay || fail "the remote browser view does not come through the app's relay with the secret handed on a pipe"
probe scan || fail "a secret-shaped value is in a process's environment or command line"

first=$(probe enqueue noop)
wait_for 90 "job_done $first"
echo "noop job $first done in the one container"

connect=$(probe enqueue connect)
killed=$(probe kill-worker) || fail "no browser started under the worker"
echo "killed the worker while its browser ran: $killed"
for _ in $(seq 1 20); do healthy || fail "/healthz stopped answering after the worker was killed"; sleep 1; done
after=$(probe)
python3 -c 'import json,sys; old, now = json.loads(sys.argv[1]), json.loads(sys.argv[2]); sys.exit(bool(set(old["browsers"]) & set(now["browsers"]) or now["strays"]))' "$killed" "$after" || fail "browser processes left behind: $after"
[ "$(field "$after" workers)" != "[$(field "$killed" worker)]" ] && [ "$(field "$after" workers)" != "[]" ] || fail "the worker was not restarted: $after"
echo "worker restarted, none of the killed worker's browser processes left and every browser under the new worker: $after"
docker exec "$name" test -d /data/browser/1/profile || fail "the browser profile is not under the browser-state root in appdata"
docker exec "$name" test ! -e /photos/_browser || fail "the worker wrote browser state onto the working share"
echo "the browser profile is in appdata, not on the share"

other=$(probe enqueue-other noop)
probe enqueue noop >/dev/null
wait_for 120 '[ "$(probe status "$other")" != queued ] && [ "$(probe claimed-since "$(field "$killed" at)")" -gt 0 ]'
! docker logs "$name" 2>&1 | grep -F '"method": "POST", "status": "401"' || fail "the app refused a worker request"
echo "the restarted worker claimed another account's job on the internal API at once, so the app accepts its token: the same one"
probe scan || fail "a secret-shaped value is in a process's environment or command line after the restart"

resume_by=$(python3 -c 'import sys; print(int(float(sys.argv[1]) + int(sys.argv[2])))' "$(field "$killed" at)" "$(probe resume-seconds)")
wait_until "$resume_by" '[ "$(probe account-resumed "$connect")" -gt 0 ]'
[ "$(probe lease-lost "$connect")" = 1 ] || fail "the killed worker's job $connect did not end with its lease running out"
echo "the killed worker's job $connect ended when its lease ran out, and its account's next job started after the hold that follows (its Chrome may still be closing), within $(probe resume-seconds) s of the kill"
