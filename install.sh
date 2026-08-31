#!/usr/bin/env bash
# omarchy-voice installer. Safe to re-run; every step is idempotent.
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local/share/omarchy-voice}"
BINDIR="${BINDIR:-$HOME/.local/bin}"
PLUGINDIR="$HOME/.config/omarchy/plugins"
CONFIGDIR="$HOME/.config/omarchy-voice"
UNITDIR="$HOME/.config/systemd/user"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\033[1;34m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
ask()  { read -rp "   $1 [y/N] " reply; [[ "$reply" =~ ^[Yy] ]]; }

# Install one package, checking pacman's sync databases first. A fresh or
# long-idle machine can have no databases at all, and then `pacman -S` fails
# with "database file for 'extra' does not exist". Never fatal: a missing
# optional package must not abandon a half-finished install.
pacman_install() {
  local pkg="$1"
  if ! compgen -G "/var/lib/pacman/sync/*.db" >/dev/null 2>&1 \
     || ! pacman -Sp "$pkg" >/dev/null 2>&1; then
    warn "pacman's sync databases are missing or too stale to find $pkg."
    if ask "refresh them with 'sudo pacman -Sy'?"; then
      sudo pacman -Sy || { warn "could not refresh the databases"; return 1; }
    else
      return 1
    fi
  fi
  sudo pacman -S --needed --noconfirm "$pkg"
}

bold "omarchy-voice installer"
echo "OpenAI Realtime speech-to-speech, driving Omarchy."
echo

# --- sanity ----------------------------------------------------------------
if ! command -v hyprctl >/dev/null; then
  warn "hyprctl not found — this add-on drives Hyprland and needs it."
  exit 1
fi
if ! command -v omarchy >/dev/null; then
  warn "the omarchy CLI was not found. Things will mostly work, but the"
  warn "assistant loses half of what it can do."
fi

# --- files -----------------------------------------------------------------
step "installing to $PREFIX"
mkdir -p "$PREFIX" "$BINDIR" "$CONFIGDIR"
rm -rf "$PREFIX/src" "$PREFIX/bin" "$PREFIX/share" "$PREFIX/omarchy" "$PREFIX/.venv"
cp -r "$SOURCE/src" "$SOURCE/bin" "$SOURCE/share" "$SOURCE/omarchy" "$PREFIX/"
chmod +x "$PREFIX/bin/omarchy-voice"
ln -sf "$PREFIX/bin/omarchy-voice" "$BINDIR/omarchy-voice"
echo "   omarchy-voice -> $BINDIR/omarchy-voice"

if [[ ! -f "$CONFIGDIR/config.toml" ]]; then
  cp "$SOURCE/share/config.example.toml" "$CONFIGDIR/config.toml"
  echo "   wrote $CONFIGDIR/config.toml"
else
  echo "   kept your existing $CONFIGDIR/config.toml"
fi

# Empty env file, mode 600. Never copy a key out of the current shell.
ENVFILE="$CONFIGDIR/env"
if [[ ! -f "$ENVFILE" ]]; then
  umask 077
  cat > "$ENVFILE" <<'EOF'
# Keys for the systemd user service and for `omarchy-voice` run from a
# terminal. chmod 600. A key exported in your shell does not reach systemd.
# OPENAI_API_KEY=sk-...
EOF
  chmod 600 "$ENVFILE"
  echo "   wrote $ENVFILE (mode 600) — put OPENAI_API_KEY here"
else
  chmod 600 "$ENVFILE" 2>/dev/null || true
  echo "   kept your existing $ENVFILE"
fi

case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) warn "$BINDIR is not on your PATH — add it, or the keybindings will not work." ;;
esac

# --- realtime --------------------------------------------------------------
echo
step "OpenAI Realtime"
if python3 -c "import websockets" 2>/dev/null; then
  echo "   python-websockets already installed"
elif ask "install python-websockets with pacman?"; then
  pacman_install python-websockets \
    || warn "not installed — the daemon cannot connect without it"
else
  warn "skipped — the daemon cannot connect without it"
fi

if grep -q "^OPENAI_API_KEY=.\+" "$ENVFILE" 2>/dev/null || [[ -n "${OPENAI_API_KEY:-}" ]]; then
  echo "   OPENAI_API_KEY is set"
else
  warn "OPENAI_API_KEY is not set. Put it in $ENVFILE as"
  warn "OPENAI_API_KEY=sk-... — a key exported in your shell does not reach"
  warn "the systemd user service."
fi
warn "while listening is on, room audio streams continuously to OpenAI."
warn "Toggling off stops the recorder, so nothing is captured while muted."

# --- desktop integration ---------------------------------------------------
echo
step "desktop integration"
if [[ -d "$HOME/.config/omarchy" ]] && ask "install the bar widget plugin?"; then
  mkdir -p "$PLUGINDIR"
  cp -r "$SOURCE/plugin/voice.indicator" "$PLUGINDIR/"
  echo "   installed to $PLUGINDIR"
  if command -v omarchy >/dev/null && omarchy bar put voice.indicator --section right >/dev/null 2>&1; then
    echo "   placed on the bar, right section"
  else
    warn "could not place it automatically. Add it with:"
    echo "     omarchy bar put voice.indicator --section right"
  fi
fi

# The `omarchy voice ...` routes. Optional and off by default: they need a
# directory that `omarchy` itself scans, which is the one holding the omarchy
# binary — /usr/bin, and therefore root. Without this you still have the
# `omarchy-voice` command; you just do not get the omarchy-native spelling.
OMARCHY_BIN=$(dirname "$(command -v omarchy 2>/dev/null || echo /usr/bin/omarchy)")
if [[ -d $OMARCHY_BIN ]] && ask "also install the 'omarchy voice ...' commands into $OMARCHY_BIN (needs sudo)?"; then
  if sudo install -m 755 "$SOURCE"/omarchy/bin/omarchy-voice* "$OMARCHY_BIN/"; then
    echo "   installed. Try: omarchy voice doctor"
  else
    warn "could not install them; 'omarchy-voice' still works on its own."
  fi
fi

if ask "install the systemd user service (starts with your session)?"; then
  mkdir -p "$UNITDIR"
  cp "$SOURCE/share/omarchy-voice.service" "$UNITDIR/"
  systemctl --user daemon-reload
  systemctl --user enable omarchy-voice.service
  echo "   enabled. Start it now with: systemctl --user start omarchy-voice"
fi

# --- keybindings -----------------------------------------------------------
echo
step "keybindings"
BINDINGS="$HOME/.config/hypr/bindings.lua"
if [[ ! -f "$BINDINGS" ]]; then
  warn "$BINDINGS does not exist. Add this by hand:"
  sed 's/^/     /' "$SOURCE/share/bindings.lua.snippet"
elif grep -q 'omarchy-voice' "$BINDINGS"; then
  echo "   already bound in $BINDINGS"
elif ask "bind SUPER + SHIFT + V in $BINDINGS?"; then
  cp "$BINDINGS" "$BINDINGS.bak-voice"
  cat >> "$BINDINGS" <<'LUA'

-- omarchy-voice ------------------------------------------------------------
-- Avoids SUPER + V (Universal paste) and SUPER + CTRL + V (clipboard manager).
-- Listening is off until this key turns it on, and off again when it does.
if o.cmd_present("omarchy-voice") then
  o.bind("SUPER + SHIFT + V", "Toggle voice control", "omarchy-voice listen toggle")
end
LUA
  echo "   appended, backup at $BINDINGS.bak-voice"
  if hyprctl reload >/dev/null 2>&1; then
    echo "   reloaded — SUPER + SHIFT + V is live"
  else
    warn "could not reload Hyprland; the binding applies on next reload"
  fi
else
  echo "   skipped. The snippet is at $SOURCE/share/bindings.lua.snippet"
fi

echo
bold "next"
echo "   edit $ENVFILE                put OPENAI_API_KEY=sk-... in it"
echo "   omarchy-voice doctor              check every moving part"
echo "   omarchy-voice --dry-run say \"...\"  try a command without a microphone"
echo "   systemctl --user start omarchy-voice"
