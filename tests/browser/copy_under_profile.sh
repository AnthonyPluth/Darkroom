#!/usr/bin/env bash
set -euo pipefail

profile=/photos/_browser/1/profile
mkdir -p "$profile/Default"
printf 'cookies' > "$profile/Default/Cookies"
chmod 600 "$profile/Default/Cookies"
ln -s host-1 "$profile/SingletonLock"
if cp -a /photos/_browser /data/cp-a-probe 2>/dev/null; then
  echo "cp -a copied across mounts, so this container does not refuse the xattr calls the deployment profile refuses" >&2
  exit 1
fi
python run.py copy-browser-state --source /photos/_browser --target /data/browser
[ "$(stat -c %a /data/browser/1/profile/Default/Cookies)" = 600 ]
[ "$(cat /data/browser/1/profile/Default/Cookies)" = cookies ]
[ ! -e /data/browser/1/profile/SingletonLock ] && [ ! -L /data/browser/1/profile/SingletonLock ]
[ -L "$profile/SingletonLock" ] && [ "$(cat "$profile/Default/Cookies")" = cookies ]
echo "browser profiles copied and verified across mounts under the deployment profile; the source is unchanged"
