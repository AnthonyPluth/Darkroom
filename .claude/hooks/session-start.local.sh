chromium_present() {
  local root
  for root in "${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}" "${HOME}/.cache/ms-playwright"; do
    if compgen -G "${root}/chromium-*/chrome-linux*/chrome" >/dev/null; then
      return 0
    fi
  done
  return 1
}

if ! chromium_present; then
  (cd frontend && npx playwright install --with-deps chromium >/dev/null 2>&1) || echo "session-start: Chromium isn't installed; tools/control and make verify need it (cd frontend && npx playwright install --with-deps chromium)" >&2
fi
