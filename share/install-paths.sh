# Sourced by install/uninstall before any destructive filesystem operation.
validate_install_prefix() {
  if [[ $EUID -eq 0 ]]; then
    echo 'Run this script as your desktop user, not as root.' >&2
    return 1
  fi
  local resolved home_resolved source_resolved
  resolved=$(realpath -m -- "$1")
  home_resolved=$(realpath -m -- "$HOME")
  source_resolved=$(realpath -m -- "$2")
  case "$resolved" in
    /|/usr|/usr/local|/home|/tmp|/var|/opt|"$home_resolved"|"$source_resolved")
      echo 'Refusing an unsafe install prefix.' >&2
      return 1 ;;
  esac
  if [[ -d $resolved ]] && [[ -n $(find "$resolved" -mindepth 1 -maxdepth 1 -print -quit) ]] \
     && [[ ! -f "$resolved/.omarchy-voice-install" ]] \
     && [[ ! -f "$resolved/src/omarchy_voice/__init__.py" || ! -f "$resolved/bin/omarchy-voice" ]]; then
    echo 'Refusing to overwrite/remove a nonempty directory that is not an OMA installation.' >&2
    return 1
  fi
}
