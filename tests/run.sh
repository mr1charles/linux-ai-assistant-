#!/usr/bin/env bash
# Everything that can be checked without a Wayland compositor.
#
# Little Toby's real home is Hyprland, and its windows are all layer-shell
# surfaces, so the app itself can only really be exercised there. These
# checks cover the part that doesn't need a compositor — which is most of
# the code — and they run anywhere GTK3 and Xvfb are installed.
set -u
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
status=0

echo "== byte-compiling every script =="
"$PY" -m py_compile scripts/*.py || status=1

echo "== voice engine checks =="
"$PY" tests/test_voice_engine.py || status=1

echo "== screen control checks =="
"$PY" tests/test_screen_control.py || status=1

echo "== GTK CSS parse check =="
if command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a "$PY" tests/css_check.py || status=1
else
    echo "   skipped: xvfb-run not installed"
fi

echo "== headless UI smoke test =="
if command -v xvfb-run >/dev/null 2>&1; then
    # Point HOME at a throwaway directory so the test never reads or writes
    # the real history/knowledge/settings files.
    tmp_home="$(mktemp -d)"
    mkdir -p "$tmp_home/linux-agent"
    HOME="$tmp_home" xvfb-run -a "$PY" tests/smoke_headless.py || status=1
    rm -rf "$tmp_home"
else
    echo "   skipped: xvfb-run not installed"
fi

if [ "$status" -eq 0 ]; then
    echo "== all checks passed =="
else
    echo "== FAILURES above =="
fi
exit "$status"
