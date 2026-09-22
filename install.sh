#!/usr/bin/env bash
# One-shot installer for Little Toby (Arch/CachyOS + Hyprland).
# Run from the repo root: ./install.sh
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VOSK_MODEL_URL="https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
VOSK_MODEL_DIR="$REPO_DIR/vosk-model-small-en-us-0.15"

echo "== Little Toby installer =="

if ! command -v pacman >/dev/null; then
  echo "This installer targets Arch/CachyOS (pacman). Install the system packages"
  echo "listed below manually for other distros, then re-run this script with"
  echo "SKIP_SYSTEM_PACKAGES=1 to just handle Python deps + config:"
  echo "  gtk3, gtk-layer-shell, python-gobject, python-cairo, espeak-ng, ollama,"
  echo "  ydotool, grim, tesseract (+ eng language data)"
fi

if [ -z "$SKIP_SYSTEM_PACKAGES" ] && command -v pacman >/dev/null; then
  echo "-- Installing system packages (sudo required)"
  sudo pacman -S --needed --noconfirm \
    python python-pip \
    gtk3 python-gobject gtk-layer-shell python-cairo \
    espeak-ng \
    ollama \
    ydotool grim tesseract tesseract-data-eng

  echo "-- Enabling ydotool daemon (needed for mouse/keyboard control + close_tab/close_active_window)"
  sudo systemctl enable --now ydotool.service 2>/dev/null || \
    echo "   Couldn't enable ydotool.service automatically — you may need to start ydotoold yourself."
  # ydotool needs the user in the 'input' group to talk to /dev/uinput
  sudo usermod -aG input "$USER" 2>/dev/null || true
fi

echo "-- Installing Python packages"
pip install --user --break-system-packages -r "$REPO_DIR/requirements.txt" || {
  echo "   Some packages failed — likely opencv-python or mediapipe (Camera Mode)."
  echo "   Retrying without them so the rest of Toby still works..."
  grep -vE '^(opencv-python|mediapipe)$' "$REPO_DIR/requirements.txt" > /tmp/toby-requirements-core.txt
  pip install --user --break-system-packages -r /tmp/toby-requirements-core.txt
  echo "   Camera Mode will report itself unavailable until mediapipe/opencv-python install"
  echo "   successfully — try 'pip install --user --break-system-packages mediapipe opencv-python'"
  echo "   yourself later; mediapipe support for brand-new Python releases sometimes lags."
}

echo "-- Setting up Ollama model (qwen2.5:7b-instruct, ~4.7GB — used for reliable JSON tool-calling)"
if command -v ollama >/dev/null; then
  (systemctl --user enable --now ollama 2>/dev/null || ollama serve >/tmp/ollama.log 2>&1 &)
  sleep 2
  ollama pull qwen2.5:7b-instruct || echo "Could not pull model automatically — run 'ollama pull qwen2.5:7b-instruct' manually."
else
  echo "ollama not found — install it, then run: ollama pull qwen2.5:7b-instruct"
fi

if [ ! -d "$VOSK_MODEL_DIR" ]; then
  echo "-- Downloading offline speech model (~50MB, used only by voice/wake-word mode)"
  curl -L "$VOSK_MODEL_URL" -o /tmp/vosk-model.zip
  unzip -q /tmp/vosk-model.zip -d "$REPO_DIR"
  rm /tmp/vosk-model.zip
fi

HAND_MODEL_URL="https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
FACE_MODEL_URL="https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
if [ ! -f "$REPO_DIR/hand_landmarker.task" ]; then
  echo "-- Downloading hand tracking model (~8MB, used only by Camera Mode)"
  curl -L "$HAND_MODEL_URL" -o "$REPO_DIR/hand_landmarker.task" || \
    echo "   Couldn't download it — Camera Mode will report itself unavailable until this exists."
fi
if [ ! -f "$REPO_DIR/face_landmarker.task" ]; then
  echo "-- Downloading face tracking model (~4MB, used only by Camera Mode)"
  curl -L "$FACE_MODEL_URL" -o "$REPO_DIR/face_landmarker.task" || \
    echo "   Couldn't download it — Camera Mode will report itself unavailable until this exists."
fi

if [ ! -f "$REPO_DIR/.env" ]; then
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  echo "-- Created .env from template — edit it to add Discord/Gmail credentials (optional)"
fi

chmod +x "$REPO_DIR/scripts/"*.py

# -- Hyprland keybind: Super+G to summon Toby ------------------------------
HYPR_CONF="$HOME/.config/hypr/hyprland.conf"
BIND_LINE="bind = SUPER, G, exec, pkill -SIGUSR1 -f linux_agent_apple.py"
EXEC_LINE="exec-once = bash -c 'set -a; source $REPO_DIR/.env; set +a; python3 $REPO_DIR/scripts/linux_agent_apple.py'"
BLUR_LINES=$'layerrule = blur, apple-agent\nlayerrule = ignorezero, apple-agent\nlayerrule = blur, apple-agent-island\nlayerrule = ignorezero, apple-agent-island\nlayerrule = blur, apple-agent-sidebar\nlayerrule = ignorezero, apple-agent-sidebar'

if [ -f "$HYPR_CONF" ]; then
  if grep -qF "linux_agent_apple.py" "$HYPR_CONF"; then
    echo "-- hyprland.conf already references Little Toby — leaving it alone."
  else
    read -r -p "-- Add the Super+G keybind + autostart to $HYPR_CONF now? [Y/n] " ans
    ans="${ans:-Y}"
    if [[ "$ans" =~ ^[Yy] ]]; then
      cp "$HYPR_CONF" "$HYPR_CONF.bak.$(date +%s)"
      {
        echo ""
        echo "# --- Little Toby ---"
        echo "$BIND_LINE"
        echo "$EXEC_LINE"
        echo "$BLUR_LINES"
      } >> "$HYPR_CONF"
      echo "   Added (backup saved alongside hyprland.conf). Reload Hyprland (SUPER+SHIFT+R or"
      echo "   'hyprctl reload') or log out/in for it to take effect."
    else
      echo "   Skipped — see below for the lines to add yourself."
    fi
  fi
else
  echo "-- Couldn't find $HYPR_CONF — add these lines to your Hyprland config yourself:"
fi

echo ""
echo "== Done =="
echo "Try it right now:"
echo "  cd $REPO_DIR"
echo "  set -a; source .env; set +a       # bash/zsh — use 'source scripts/load-env.fish' on fish"
echo "  python3 scripts/linux_agent_apple.py &          # Little Toby (recommended)"
echo "  python3 scripts/linux_agent_assistant.py --assistant  # wake-word voice mode"
echo ""
echo "To toggle Toby from a Hyprland keybind (Super+G), make sure these lines are in"
echo "~/.config/hypr/hyprland.conf:"
echo "  $BIND_LINE"
echo "  $EXEC_LINE"
echo "  $BLUR_LINES"
echo ""
echo "Or run it as a systemd user service instead of exec-once:"
echo "  cp systemd/linux-toby.service ~/.config/systemd/user/"
echo "  systemctl --user enable --now linux-toby"
echo ""
echo "Double-click Toby's face to expand the sidebar (chat history, knowledge"
echo "tree/bubbles, Study Mode badge). First mouse/keyboard control or package"
echo "install request will ask for a one-time Yes/No confirmation."
