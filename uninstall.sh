#!/usr/bin/env bash
# Removes everything install.sh put down. Leaves your config.toml alone
# unless you pass --purge.
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local/share/omarchy-voice}"
BINDIR="${BINDIR:-$HOME/.local/bin}"

systemctl --user disable --now omarchy-voice.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/omarchy-voice.service"
systemctl --user daemon-reload 2>/dev/null || true
rm -rf "$PREFIX" "$HOME/.config/omarchy/plugins/voice.indicator"
rm -f "$BINDIR/omarchy-voice"

# The `omarchy voice ...` routes, if they were installed next to the omarchy
# binary. Only these exact names, never a glob that could take omarchy's own.
OMARCHY_BIN=$(dirname "$(command -v omarchy 2>/dev/null || echo /usr/bin/omarchy)")
for route in "" -start -stop -toggle -confirm -cancel -say -doctor -log -manifest; do
  target="$OMARCHY_BIN/omarchy-voice$route"
  [[ -f $target ]] && sudo rm -f "$target"
done
rm -rf "$HOME/.cache/omarchy-voice" "$HOME/.local/state/omarchy-voice"

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$HOME/.config/omarchy-voice"
  echo "removed config too"
fi

# --- keybindings -----------------------------------------------------------
# Removes exactly the block install.sh appended, matched from its marker
# comment to the `end` that closes the `if o.cmd_present` guard. Anything you
# added yourself around it is left alone.
BINDINGS="$HOME/.config/hypr/bindings.lua"
if [[ -f "$BINDINGS" ]] && grep -q 'omarchy-voice' "$BINDINGS"; then
  cp "$BINDINGS" "$BINDINGS.bak-voice-uninstall"
  python3 - "$BINDINGS" <<'PYEOF'
import re, sys
path = sys.argv[1]
text = open(path).read()
block = re.compile(
    r'\n*-- omarchy-voice -+\n(?:--[^\n]*\n)*'
    r'if o\.cmd_present\("omarchy-voice"\) then\n(?:.*?\n)*?end\n',
    re.MULTILINE)
new, n = block.subn('\n', text)
if n:
    open(path, 'w').write(new)
    print(f"   removed the binding block from {path}")
else:
    print(f"!! could not find the block automatically — check {path} by hand")
PYEOF
  hyprctl reload >/dev/null 2>&1 || true
fi

# --- bar widget ------------------------------------------------------------
# There is no `omarchy bar remove`, so the entry comes out of shell.json here.
SHELL_JSON="$HOME/.config/omarchy/shell.json"
if [[ -f "$SHELL_JSON" ]] && grep -q 'voice.indicator' "$SHELL_JSON"; then
  cp "$SHELL_JSON" "$SHELL_JSON.bak-voice-uninstall"
  python3 - "$SHELL_JSON" <<'PYEOF'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
removed = 0
for section in data.get("bar", {}).get("layout", {}).values():
    if isinstance(section, list):
        before = len(section)
        section[:] = [w for w in section
                      if not (isinstance(w, dict) and w.get("id") == "voice.indicator")]
        removed += before - len(section)
if removed:
    json.dump(data, open(path, "w"), indent=2)
    print(f"   took the widget off the bar ({removed} entry)")
PYEOF
fi

echo "omarchy-voice removed."
