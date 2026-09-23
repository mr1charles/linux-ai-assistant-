#!/usr/bin/env bash
# Build the app for testing once, then run the unit tests and the UI tests
# against a real Toby bridge (ci/test_desktop.py), on a small and a large
# iPhone, in light and dark. Screenshots go to build/screenshots/.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build/results build/screenshots

export TOBY_PAIR_CODE=K7M4XQ2P
python3 ci/test_desktop.py --port 8765 --code "$TOBY_PAIR_CODE" > build/test-desktop.log 2>&1 &
HARNESS=$!
trap 'kill $HARNESS 2>/dev/null || true' EXIT
sleep 2
curl -fsS http://127.0.0.1:8765/api/hello

DEVICES=()
while IFS= read -r line; do [ -n "$line" ] && DEVICES+=("$line"); done < <(python3 ci/pick_simulators.py)
echo "Simulators:"; printf '  %s\n' "${DEVICES[@]}"
if [ "${#DEVICES[@]}" -eq 0 ]; then echo "no iPhone simulators on this machine"; exit 1; fi

xcodebuild build-for-testing -project LittleToby.xcodeproj -scheme LittleToby \
  -destination "id=${DEVICES[0]%% *}" -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO | tail -n 25

status=0
first=1
for device in "${DEVICES[@]}"; do
  udid="${device%% *}"; name="${device#* }"; slug="$(echo "$name" | tr ' ()' '-__')"
  for appearance in light dark; do
    only="-only-testing:LittleTobyUITests"
    if [ $first -eq 1 ]; then only=""; first=0; fi   # unit tests once, with the first run
    result="build/results/${slug}-${appearance}.xcresult"
    echo "== $name, $appearance =="
    TEST_RUNNER_TOBY_PAIR_CODE="$TOBY_PAIR_CODE" TEST_RUNNER_TOBY_APPEARANCE="$appearance" \
    TEST_RUNNER_TOBY_SERVER="http://127.0.0.1:8765" \
    xcodebuild test-without-building -project LittleToby.xcodeproj -scheme LittleToby \
      -destination "id=$udid" -derivedDataPath build/dd -resultBundlePath "$result" $only \
      -test-timeouts-enabled YES -maximum-test-execution-time-allowance 420 \
      | tail -n 40 || status=1
    out="build/screenshots/${slug}-${appearance}"
    mkdir -p "$out"
    xcrun xcresulttool export attachments --path "$result" --output-path "$out" >/dev/null 2>&1 \
      || echo "(couldn't export screenshots from $result)"
  done
done
echo "== test Toby's log =="; cat build/test-desktop.log
exit $status
