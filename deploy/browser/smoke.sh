#!/usr/bin/env bash
set -eo pipefail

: "${DISPLAY:=:99}"
export DISPLAY
log=$(mktemp)
Xvfb "$DISPLAY" -screen 0 1280x900x24 -nolisten tcp -nolisten unix >"$log" 2>&1 &
display_pid=$!
trap 'kill "$display_pid" 2>/dev/null || true; rm -f "$log"' EXIT
ready=false
for _ in $(seq 1 50); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then ready=true; break; fi
  sleep 0.1
done
if [ "$ready" != true ]; then cat "$log"; exit 1; fi

for channel in chrome chromium; do
  if ! BROWSER_CHANNEL="$channel" python - 2>"$log" <<'PY'
import os
import tempfile
from pathlib import Path

from darkroom_browser.live import open_context

assert os.geteuid() != 0
with tempfile.TemporaryDirectory() as directory:
    browser = open_context(Path(directory) / "_browser" / "1" / "profile")
    try:
        browser.goto("data:text/html,<title>Darkroom browser smoke</title><p>renderer ready</p>")
        assert "renderer ready" in browser.content()
    finally:
        browser.close()
PY
  then cat "$log"; exit 1; fi
  cat "$log"
  if grep -q 'chrome_launch_failed' "$log"; then exit 1; fi
done
