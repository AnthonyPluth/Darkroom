#!/usr/bin/env bash
set -euo pipefail

ios_dir="${IOS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
spec="$ios_dir/project.yml"
set_dir="$ios_dir/Darkroom/Assets.xcassets/AppIcon.appiconset"

level=error
if [[ "${1:-}" == --warn ]]; then level=warning; fi

problems=()

if ! grep -qE '^[[:space:]]*ASSETCATALOG_COMPILER_APPICON_NAME:[[:space:]]*AppIcon[[:space:]]*$' "$spec"; then
  problems+=("ios/project.yml does not set ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon")
fi

if [[ ! -f "$set_dir/Contents.json" ]]; then
  problems+=("ios/Darkroom/Assets.xcassets/AppIcon.appiconset/Contents.json is missing")
else
  file_name="$(tr -d '\n' < "$set_dir/Contents.json" | grep -oE '"filename"[[:space:]]*:[[:space:]]*"[^"]+"[^{}]*"size"[[:space:]]*:[[:space:]]*"1024x1024"|"size"[[:space:]]*:[[:space:]]*"1024x1024"[^{}]*"filename"[[:space:]]*:[[:space:]]*"[^"]+"' | grep -oE '"filename"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 | sed -E 's/.*"([^"]+)"$/\1/' || true)"
  if [[ -z "$file_name" ]]; then
    problems+=("the AppIcon set lists no 1024x1024 image with a file name")
  elif [[ ! -f "$set_dir/$file_name" ]]; then
    problems+=("the AppIcon set names $file_name, which is not in the set")
  else
    description="$(file -b "$set_dir/$file_name")"
    if [[ "$description" != *"PNG image data, 1024 x 1024"* ]]; then
      problems+=("the 1024x1024 app icon is not a 1024 x 1024 PNG")
    elif [[ "$description" == *"RGBA"* || "$description" == *"alpha"* ]]; then
      problems+=("the app icon has an alpha channel; App Store Connect needs an opaque PNG")
    fi
  fi
fi

if ((${#problems[@]} > 0)); then
  message="The app has no App Store icon, so App Store Connect would reject the upload:"
  for problem in "${problems[@]}"; do message+=" $problem;"; done
  message+=" add an opaque 1024x1024 PNG (no alpha) as AppIcon (see docs/deploy/ios-release.md)."
  echo "::$level title=iOS app icon::$message"
  echo "$message" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
  if [[ "$level" == error ]]; then exit 1; fi
fi
