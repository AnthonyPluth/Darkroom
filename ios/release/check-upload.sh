#!/usr/bin/env bash
set -euo pipefail

status="${1:?exit status}"
log="${2:?altool output file}"

cat "$log"

fail() {
  echo "::error title=TestFlight upload failed::$1"
  exit 1
}

[[ "$status" == 0 ]] || fail "altool exited with status $status."
grep -qiE '(^|[[:space:]*])error[: ]|product-errors' "$log" && fail "altool reported an error while exiting 0 (a duplicate build number, a rejected binary or a failed validation); see the output above."
grep -qiE 'no errors uploading|upload succeeded|successfully uploaded|delivery uuid' "$log" || fail "altool printed no sign that the build was delivered."
echo "Delivery verified."
