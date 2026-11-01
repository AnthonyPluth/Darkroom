#!/usr/bin/env bash
set -eo pipefail

: "${DISPLAY:=:99}"
: "${SCREEN:=1280x900x24}"
: "${VNC_PORT:=5900}"
: "${NOVNC_PORT:=6080}"
: "${PHOTOS_ROOT:=/photos}"

if [ -z "${DARKROOM_BROWSER_TOKEN:-}" ] || [ "${#DARKROOM_BROWSER_TOKEN}" -lt 32 ]; then
  echo '{"event": "worker refuses to start", "problem": "DARKROOM_BROWSER_TOKEN is unset or shorter than 32 characters"}' >&2
  exit 1
fi

mkdir -p "$PHOTOS_ROOT/_browser"

Xvfb "$DISPLAY" -screen 0 "$SCREEN" -nolisten tcp -nolisten unix >/dev/null 2>&1 &
for _ in $(seq 1 50); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -forever -shared -nopw -noxdamage -quiet -bg >/dev/null 2>&1
websockify --web=/usr/share/novnc "$NOVNC_PORT" "localhost:$VNC_PORT" >/dev/null 2>&1 &

exec python -m darkroom_browser
