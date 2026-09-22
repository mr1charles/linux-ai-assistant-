#!/usr/bin/env bash
# Removes Little Toby's services and command. Keeps your notes, memories,
# settings, downloaded models and this folder, so reinstalling picks up
# exactly where you left off. Delete ~/linux-agent yourself to remove those.
set -u
systemctl --user disable --now toby.service toby-fold.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/toby.service" "$HOME/.config/systemd/user/toby-fold.service"
systemctl --user daemon-reload 2>/dev/null
rm -f "$HOME/.local/bin/toby"
command -v hyprctl >/dev/null && hyprctl reload >/dev/null 2>&1   # drops Toby's runtime keybind and animations
if command -v tailscale >/dev/null; then tailscale serve reset >/dev/null 2>&1 || true; fi

# Older versions appended a block to hyprland.conf; take it out if it's there.
CONF="$HOME/.config/hypr/hyprland.conf"
if [ -f "$CONF" ] && grep -qF "# --- Little Toby ---" "$CONF"; then
  cp "$CONF" "$CONF.bak.$(date +%s)"
  sed -i '/# --- Little Toby ---/,/^$/d' "$CONF"
  echo "Removed the old Little Toby lines from hyprland.conf (a backup is next to it)."
fi
echo "Little Toby is uninstalled. Your data is still in ~/linux-agent."
