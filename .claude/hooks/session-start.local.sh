if ! ls /opt/pw-browsers/chromium-*/chrome-linux*/chrome "${HOME}"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome >/dev/null 2>&1; then
  (cd frontend && npx playwright install --with-deps chromium >/dev/null 2>&1) || echo "session-start: Chromium isn't installed; tools/control and make verify need it (cd frontend && npx playwright install --with-deps chromium)" >&2
fi
