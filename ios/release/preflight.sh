#!/usr/bin/env bash
set -euo pipefail

variables=(DARKROOM_BUNDLE_ID DARKROOM_TEAM_ID)
secrets=(ASC_KEY_ID ASC_ISSUER_ID ASC_KEY_P8)

missing_variables=()
missing_secrets=()
for name in "${variables[@]}"; do
  if [[ -z "${!name:-}" ]]; then missing_variables+=("$name"); fi
done
for name in "${secrets[@]}"; do
  if [[ -z "${!name:-}" ]]; then missing_secrets+=("$name"); fi
done

summary="${GITHUB_STEP_SUMMARY:-/dev/null}"

if ((${#missing_variables[@]} + ${#missing_secrets[@]} > 0)); then
  message="The ios-release environment is not set up."
  if ((${#missing_secrets[@]} > 0)); then
    message+=" Missing secrets of the ios-release environment: ${missing_secrets[*]}."
  fi
  if ((${#missing_variables[@]} > 0)); then
    message+=" Missing Actions variables: ${missing_variables[*]}."
  fi
  message+=" See docs/deploy/ios-release.md."
  echo "::error title=iOS release is not configured::$message"
  echo "$message" >> "$summary"
  exit 1
fi

malformed=()
[[ "$DARKROOM_BUNDLE_ID" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] || malformed+=(DARKROOM_BUNDLE_ID)
[[ "$DARKROOM_TEAM_ID" =~ ^[A-Z0-9]{10}$ ]] || malformed+=(DARKROOM_TEAM_ID)
[[ "$ASC_KEY_ID" =~ ^[A-Z0-9]{10}$ ]] || malformed+=(ASC_KEY_ID)
[[ "$ASC_ISSUER_ID" =~ ^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$ ]] || malformed+=(ASC_ISSUER_ID)
[[ "$ASC_KEY_P8" == *"-----BEGIN PRIVATE KEY-----"* ]] || malformed+=(ASC_KEY_P8)

if ((${#malformed[@]} > 0)); then
  message="These values are set but do not have the expected format: ${malformed[*]}. See docs/deploy/ios-release.md."
  echo "::error title=iOS release is misconfigured::$message"
  echo "$message" >> "$summary"
  exit 1
fi

echo "::add-mask::$DARKROOM_BUNDLE_ID"
echo "::add-mask::$DARKROOM_TEAM_ID"

: "${RUNNER_TEMP:?}"
umask 077
key_dir="$RUNNER_TEMP/asc"
mkdir -p "$key_dir"
chmod 700 "$key_dir"
key_file="$key_dir/AuthKey_${ASC_KEY_ID}.p8"
printf '%s\n' "$ASC_KEY_P8" > "$key_file"
chmod 600 "$key_file"
echo "The App Store Connect key is in place (mode 600)."
