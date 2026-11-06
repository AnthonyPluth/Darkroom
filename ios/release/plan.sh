#!/usr/bin/env bash
set -euo pipefail

: "${EVENT_NAME:?}" "${REF:?}" "${REF_NAME:?}"
INPUT_VERSION="${INPUT_VERSION:-}"
INPUT_UPLOAD="${INPUT_UPLOAD:-}"
out="${GITHUB_OUTPUT:-/dev/stdout}"

fail() {
  echo "::error title=ios-release::$1" >&2
  exit 1
}

semver='^[0-9]+\.[0-9]+\.[0-9]+$'

case "$EVENT_NAME" in
  push)
    [[ "$REF" == refs/tags/ios-v* ]] || fail "A push releases only from a tag named ios-v<major>.<minor>.<patch>, not from $REF."
    version="${REF_NAME#ios-v}"
    upload=true
    ;;
  workflow_dispatch)
    [[ "$REF" == refs/heads/main ]] || fail "A manual release runs only from main, not from $REF."
    version="$INPUT_VERSION"
    case "$INPUT_UPLOAD" in
      true | false) upload="$INPUT_UPLOAD" ;;
      *) fail "The upload input must be true or false." ;;
    esac
    ;;
  *)
    fail "The release workflow runs only on an ios-v* tag or a manual dispatch, not on $EVENT_NAME."
    ;;
esac

[[ "$version" =~ $semver ]] || fail "The marketing version must look like 1.2.3 (digits and two dots)."

{
  echo "version=$version"
  echo "upload=$upload"
} >> "$out"
