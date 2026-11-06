#!/usr/bin/env bash
set -euo pipefail

: "${ASC_KEY_ID:?}" "${ASC_KEY_P8:?}"
: "${RUNNER_TEMP:?}"
umask 077
key_dir="$RUNNER_TEMP/asc"
mkdir -p "$key_dir"
chmod 700 "$key_dir"
key_file="$key_dir/AuthKey_${ASC_KEY_ID}.p8"
printf '%s\n' "$ASC_KEY_P8" > "$key_file"
chmod 600 "$key_file"
echo "The App Store Connect key is in place (mode 600)."
