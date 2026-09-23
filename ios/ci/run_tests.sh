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

# Show progress as it happens (errors, each test case, results) and keep the
# full log in build/.
show() { grep -E --line-buffered "error:|warning: unable|Test Case|Test Suite .*(passed|failed)|Executed|\*\* |Testing started|Failing tests|XCTAssert|failed \(" || true; }

# Left to Xcode's default for the simulator, which signs to run locally (ad
# hoc, no Apple account needed). Not CODE_SIGNING_ALLOWED=NO: an unsigned
# simulator app has no entitlements, so the Keychain refuses to keep the
# pairing token and the app could never connect.
echo "== building for testing =="
xcodebuild build-for-testing -project LittleToby.xcodeproj -scheme LittleToby \
  -destination "id=${DEVICES[0]%% *}" -derivedDataPath build/dd \
  2>&1 | tee build/build-for-testing.log | show

status=0
first=1
for device in "${DEVICES[@]}"; do
  udid="${device%% *}"; name="${device#* }"; slug="$(echo "$name" | tr ' ()' '-__')"
  for appearance in light dark; do
    only="-only-testing:LittleTobyUITests"
    if [ $first -eq 1 ]; then only=""; first=0; fi   # unit tests once, with the first run
    result="build/results/${slug}-${appearance}.xcresult"
    echo "== $name, $appearance =="
    set +e
    TEST_RUNNER_TOBY_PAIR_CODE="$TOBY_PAIR_CODE" TEST_RUNNER_TOBY_APPEARANCE="$appearance" \
    TEST_RUNNER_TOBY_SERVER="http://127.0.0.1:8765" \
    xcodebuild test-without-building -project LittleToby.xcodeproj -scheme LittleToby \
      -destination "id=$udid" -derivedDataPath build/dd -resultBundlePath "$result" $only \
      -test-timeouts-enabled YES -maximum-test-execution-time-allowance 420 \
      2>&1 | tee "build/results/${slug}-${appearance}.log" | show
    rc=${PIPESTATUS[0]}
    set -e
    out="build/screenshots/${slug}-${appearance}"
    mkdir -p "$out"
    xcrun xcresulttool export attachments --path "$result" --output-path "$out" >/dev/null 2>&1 \
      || echo "(couldn't export screenshots from $result)"
    if [ "$rc" -ne 0 ]; then
      status=1
      echo "-- $name, $appearance failed (exit $rc); the end of its log: --"
      tail -n 60 "build/results/${slug}-${appearance}.log"
      break 2      # the other phones would fail the same way; report this one now
    fi
  done
done
echo "== test Toby's log =="; cat build/test-desktop.log
exit $status
