#!/usr/bin/env bash
# Little Toby installer — one command, one password prompt.
#
#   curl -fsSL https://raw.githubusercontent.com/mr1charles/linux-ai-assistant-/main/install.sh | bash
#   or, from a checkout:  ./install.sh
#
# Options:  --update    refresh dependencies only (used by `toby update`)
#           --no-models skip the AI and speech model downloads
#           --phone     also install Tailscale for the phone app
#
# What it does, in order, and nothing else:
#   1. installs system packages with pacman (the one sudo prompt)
#   2. makes a private Python environment for Toby's own packages
#   3. downloads the local AI model and the speech model
#   4. installs the `toby` command and two user services, and starts them
# It never edits your Hyprland config. The Super+G key is added to the
# running compositor by Toby itself, only if that key is free.
set -euo pipefail

REPO_URL="${TOBY_REPO:-https://github.com/mr1charles/linux-ai-assistant-.git}"
BRANCH="${TOBY_BRANCH:-main}"
DATA_DIR="$HOME/linux-agent"

UPDATE=0; MODELS=1; PHONE=0
for arg in "$@"; do
  case "$arg" in
    --update) UPDATE=1 ;;
    --no-models) MODELS=0 ;;
    --phone) PHONE=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
  esac
done

step() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$*"; }

# -- 0. get the code, if run straight from the web ---------------------------
# Toby's data (memories, notes, settings, models) always lives in
# ~/linux-agent. The code goes there too on a fresh machine — but if
# ~/linux-agent already holds something else (an older copy of Toby from
# another repository, or files that aren't a git checkout at all), it is
# left exactly as it is and the code goes in ~/.local/share/little-toby.
same_repo() {
  [ -d "$1/.git" ] || return 1
  local url; url="$(git -C "$1" remote get-url origin 2>/dev/null || true)"
  [ "${url%.git}" = "${REPO_URL%.git}" ]
}
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
if [ -z "$SELF_DIR" ] || [ ! -f "$SELF_DIR/scripts/toby_cli.py" ]; then
  command -v git >/dev/null || sudo pacman -S --needed --noconfirm git
  if same_repo "$DATA_DIR" || [ ! -e "$DATA_DIR" ]; then
    CODE_DIR="$DATA_DIR"
  else
    CODE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/little-toby"
  fi
  step "Downloading Little Toby into $CODE_DIR"
  if [ -d "$CODE_DIR/.git" ]; then
    git -C "$CODE_DIR" fetch --depth 1 origin "$BRANCH"
    # Changed or copied-in copies of Toby's own files would block the update.
    # Keep them rather than lose them: edits go in a git stash, stray copies
    # of files the new version brings go in a backup folder.
    if [ -n "$(git -C "$CODE_DIR" status --porcelain --untracked-files=no)" ]; then
      git -C "$CODE_DIR" stash push -q -m "local changes, saved by install.sh $(date +%F)"
      warn "You had changed some of Toby's files. They're saved: git -C $CODE_DIR stash list"
    fi
    backup="$CODE_DIR/.before-update-$(date +%Y%m%d-%H%M%S)"
    while IFS= read -r f; do
      if [ -n "$f" ] && git -C "$CODE_DIR" cat-file -e "FETCH_HEAD:$f" 2>/dev/null; then
        mkdir -p "$backup/$(dirname "$f")" && mv "$CODE_DIR/$f" "$backup/$f"
      fi
    done < <(git -C "$CODE_DIR" ls-files --others --exclude-standard)
    [ -d "$backup" ] && warn "Moved stray copies of Toby's files out of the way, into $backup"
    git -C "$CODE_DIR" checkout -q -B "$BRANCH" FETCH_HEAD
  else
    mkdir -p "$(dirname "$CODE_DIR")"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$CODE_DIR"
  fi
  exec bash "$CODE_DIR/install.sh" "$@"
fi
REPO="$SELF_DIR"
mkdir -p "$DATA_DIR"

# -- 1. system packages ---------------------------------------------------------
if ! command -v pacman >/dev/null; then
  warn "This installer is for Arch-based systems (pacman). Install these yourself:"
  warn "  python gtk3 python-gobject gtk-layer-shell python-cairo python-requests"
  warn "  espeak-ng ollama ydotool grim tesseract tesseract-data-eng qrencode python-evdev at-spi2-core"
  warn "then run this again with SKIP_SYSTEM_PACKAGES=1."
  [ -n "${SKIP_SYSTEM_PACKAGES:-}" ] || exit 1
fi

if [ -z "${SKIP_SYSTEM_PACKAGES:-}" ] && command -v pacman >/dev/null; then
  step "Installing system packages (asks for your password once)"
  # Deliberately lean: nothing here pulls in scipy, pandas or the like.
  PKGS=(python python-pip gtk3 python-gobject gtk-layer-shell python-cairo python-requests
        espeak-ng ydotool grim tesseract tesseract-data-eng qrencode unzip curl git
        python-evdev at-spi2-core)
  # ollama: use the GPU build if there's an NVIDIA card, plain otherwise
  if lspci 2>/dev/null | grep -qi nvidia; then PKGS+=(ollama-cuda); else PKGS+=(ollama); fi
  [ "$PHONE" = 1 ] && PKGS+=(tailscale)
  sudo pacman -S --needed --noconfirm -q "${PKGS[@]}"

  sudo systemctl enable --now ollama.service >/dev/null 2>&1 || warn "Couldn't start Ollama's service; run: sudo systemctl enable --now ollama"
  [ "$PHONE" = 1 ] && { sudo systemctl enable --now tailscaled >/dev/null 2>&1 || true; }
  if ! id -nG "$USER" | grep -qw input; then
    sudo usermod -aG input "$USER" && note "Added you to the input group (for mouse/keyboard control). Takes effect after you log out and in."
  fi
fi

systemctl --user enable --now ydotool.service >/dev/null 2>&1 || note "ydotool's user service isn't available; mouse/keyboard control will ask you to start ydotoold."

# -- 2. Python environment -------------------------------------------------------
step "Setting up Toby's Python environment"
# --system-site-packages so the GTK, Cairo and layer-shell bindings from
# pacman are visible; Toby's own extras go in here and never touch the
# system Python.
if [ ! -x "$REPO/.venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$REPO/.venv"
fi
VPIP="$REPO/.venv/bin/pip"
"$VPIP" install -q --upgrade pip >/dev/null
"$VPIP" install -q requests pypdf vosk sounddevice || warn "Voice packages didn't install; Voice Mode will say so."
if ! "$VPIP" install -q mediapipe opencv-python-headless >/dev/null 2>&1; then
  warn "Camera Mode's packages (mediapipe) aren't available for this Python yet; everything else works."
fi

# -- 3. models --------------------------------------------------------------------
if [ "$MODELS" = 1 ] && [ "$UPDATE" = 0 ]; then
  # The recommended model is a ~2.5 GB download. If you already have a model
  # Toby can use, installing doesn't wait for it: the download runs in the
  # background, and Toby switches to it by itself once it's there.
  for _ in $(seq 1 15); do curl -s http://localhost:11434/api/tags >/dev/null && break; sleep 1; done
  BEST="qwen3:4b"
  installed="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
  if printf '%s\n' "$installed" | grep -qx "$BEST\|$BEST:latest\|qwen3:4b-instruct.*"; then
    note "The recommended model is already installed."
  elif printf '%s\n' "$installed" | grep -q .; then
    step "Getting the faster AI model in the background"
    nohup ollama pull "$BEST" >"$DATA_DIR/model-download.log" 2>&1 &
    note "You already have $(printf '%s\n' "$installed" | head -n1), so Toby works right away."
    note "$BEST (~2.5 GB) is downloading in the background; Toby switches to it by itself when it's done."
    note "Progress: tail -f ~/linux-agent/model-download.log"
  else
    step "Downloading the AI model (about 2.5 GB — the only big download; it resumes if interrupted)"
    ollama pull "$BEST" || warn "Couldn't download it. Later, run: toby model $BEST"
  fi

  VOSK_DIR="$DATA_DIR/vosk-model-small-en-us-0.15"
  if [ ! -d "$VOSK_DIR" ]; then
    step "Downloading the offline speech model (about 50 MB)"
    tmp="$(mktemp)"
    curl -fL "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip" -o "$tmp" \
      && unzip -q "$tmp" -d "$DATA_DIR" || warn "Couldn't download it; Voice Mode will say so."
    rm -f "$tmp"
  fi
  for f in hand_landmarker face_landmarker; do
    [ -f "$DATA_DIR/$f.task" ] && continue
    kind="${f%_landmarker}"
    curl -fsL "https://storage.googleapis.com/mediapipe-models/${kind}_landmarker/${kind}_landmarker/float16/1/$f.task" \
      -o "$DATA_DIR/$f.task" || rm -f "$DATA_DIR/$f.task"
  done
fi

if [ ! -f "$REPO/.env" ]; then
  # keep the Discord/email settings from an older install if there are any
  if [ "$REPO" != "$DATA_DIR" ] && [ -f "$DATA_DIR/.env" ]; then
    cp "$DATA_DIR/.env" "$REPO/.env"
    sed -i 's/^OLLAMA_MODEL=.*/OLLAMA_MODEL=/' "$REPO/.env"   # let Toby pick the fastest installed model
  else
    cp "$REPO/.env.example" "$REPO/.env"
  fi
fi
chmod +x "$REPO/scripts/"*.py "$REPO/scripts/toby-session.sh" "$REPO/bin/toby"

# -- 4. command and services --------------------------------------------------------
step "Installing the toby command and services"
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
ln -sf "$REPO/bin/toby" "$HOME/.local/bin/toby"
for unit in toby.service toby-fold.service; do
  sed "s|@REPO@|$REPO|g" "$REPO/systemd/$unit" > "$HOME/.config/systemd/user/$unit"
done
started=1
systemctl --user daemon-reload 2>/dev/null || started=0
systemctl --user enable toby.service toby-fold.service >/dev/null 2>&1 || started=0
systemctl --user restart toby.service toby-fold.service 2>/dev/null || started=0
if [ "$started" = 0 ]; then
  warn "Couldn't start the services from here (no user systemd session?)."
  warn "They're installed; they'll start next time you log in, or run: toby start"
fi

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "Add ~/.local/bin to your PATH to use the toby command (fish: fish_add_path ~/.local/bin)" ;;
esac

# An older Toby started from your Hyprland config would run alongside the
# new one (and both would answer Super+G). It's your config, so it's pointed
# out rather than edited.
old_refs="$(grep -rnI "linux_agent_apple.py" "$HOME/.config/hypr" 2>/dev/null | grep -v "toby-session" || true)"
if [ -n "$old_refs" ]; then
  warn "Your Hyprland config still starts or binds the old Toby here:"
  printf '%s\n' "$old_refs" | sed 's/^/      /'
  warn "Delete those lines (Toby now starts itself and binds Super+G), then log out and in."
fi
pkill -f "$DATA_DIR/scripts/linux_agent_apple.py" 2>/dev/null && [ "$REPO" != "$DATA_DIR" ] \
  && note "Stopped the old copy of Toby that was running."

step "Done"
[ "$started" = 1 ] && note "Toby is running and starts by itself when you log in."
note "Press Super+G to summon it, or run: toby"
note "Check everything with: toby doctor"
[ "$PHONE" = 1 ] && note "Pair your phone with: toby phone on"
exit 0
