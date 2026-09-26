#!/usr/bin/env bash
# Starts one Toby component (the assistant, or the lid fold) inside your
# Hyprland session.
#
# systemd starts user services before or without the desktop's environment
# in many setups (plain Hyprland doesn't export WAYLAND_DISPLAY to systemd
# unless your config does it). Rather than depend on that, this waits for a
# Hyprland instance to exist, finds its socket and Wayland display itself,
# and only then starts. If Hyprland restarts, the component exits with it,
# systemd restarts this, and it finds the new instance.
set -u
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Parts that never touch the desktop (Telegram) set TOBY_NO_DESKTOP=1 and
# skip the wait.
if [ -z "${TOBY_NO_DESKTOP:-}" ]; then
  instance=""
  for _ in $(seq 1 900); do
    if [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] && [ -S "$RUNTIME/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock" ]; then
      instance="$HYPRLAND_INSTANCE_SIGNATURE"
    else
      instance="$(ls -t "$RUNTIME/hypr" 2>/dev/null | head -n1)"
      [ -n "$instance" ] && [ ! -S "$RUNTIME/hypr/$instance/.socket.sock" ] && instance=""
    fi
    [ -n "$instance" ] && break
    sleep 2
  done
  [ -n "$instance" ] || { echo "No Hyprland session found."; exit 1; }
  export HYPRLAND_INSTANCE_SIGNATURE="$instance"

  if [ -z "${WAYLAND_DISPLAY:-}" ] || [ ! -S "$RUNTIME/${WAYLAND_DISPLAY}" ]; then
    WAYLAND_DISPLAY="$(ls "$RUNTIME" 2>/dev/null | grep -E '^wayland-[0-9]+$' | head -n1)"
  fi
  export WAYLAND_DISPLAY XDG_RUNTIME_DIR="$RUNTIME" GDK_BACKEND=wayland
fi

if [ -f "$HERE/.env" ]; then
  set -a; . "$HERE/.env"; set +a
fi

PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" "$HERE/scripts/$1"
