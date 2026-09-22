#!/usr/bin/env bash
# Removes Little Toby's autostart/keybind and stops any running instance.
# Does NOT delete ~/linux-agent, your .env, knowledge/notes data, or Ollama models.
set -e

echo "== Little Toby uninstaller =="

pkill -f linux_agent_apple.py 2>/dev/null && echo "-- Stopped running instance." || true

systemctl --user disable --now linux-toby 2>/dev/null && echo "-- Disabled systemd user service." || true

HYPR_CONF="$HOME/.config/hypr/hyprland.conf"
if [ -f "$HYPR_CONF" ] && grep -qF "Little Toby" "$HYPR_CONF"; then
  cp "$HYPR_CONF" "$HYPR_CONF.bak.$(date +%s)"
  sed -i '/# --- Little Toby ---/,+3d' "$HYPR_CONF"
  echo "-- Removed Little Toby's block from hyprland.conf (backup saved alongside it)."
  echo "   Reload Hyprland ('hyprctl reload') for the change to take effect."
fi

echo ""
echo "Done. Your project files, .env, knowledge.json, study_notes.json, and"
echo "session_log.md are still in $(dirname "${BASH_SOURCE[0]}") if you want to keep them"
echo "or come back later — delete that folder manually to remove everything."
