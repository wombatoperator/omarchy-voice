"""Turning a spoken key name into a keysym Hyprland will actually press.

`hl.dsp.send_shortcut({ key = "Enter" })` returns `ok` and presses nothing.
There is no keysym called "Enter" — the Return key is `Return`, and xkbcommon
resolves the name to NoSymbol — but Hyprland does not report that, so the
dispatch looks like a success from the outside. The persona (correctly) tells
the model that a tool which returned without an error did what it says, so it
then reported "pressed Enter" to the user, out loud, while nothing had
happened. "Enter" is also the word a person says, so this was not a rare path.

Two jobs here:

* Translate what a person says into what xkb calls it — "enter", "esc",
  "page down", "space bar", "dot", "up arrow".
* Refuse anything that still does not resolve, instead of letting Hyprland
  swallow it. A named failure the model can read beats a silent one it cannot.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import difflib
import functools
import re

# Hyprland's own modifier names. `send_shortcut` takes these space- or
# plus-separated; anything else is dropped as quietly as a bad keysym is.
MODIFIERS = {
    "SUPER": "SUPER", "SHIFT": "SHIFT", "CTRL": "CTRL", "ALT": "ALT",
    # What people say, and what other desktops call the same key.
    "CONTROL": "CTRL", "CONTROLKEY": "CTRL", "CMD": "SUPER", "COMMAND": "SUPER",
    "WIN": "SUPER", "WINDOWS": "SUPER", "META": "SUPER", "MOD": "SUPER",
    "OPTION": "ALT", "ALTGR": "ALT", "MOD1": "ALT", "MOD4": "SUPER",
}

# Spoken name -> keysym. Only entries xkb would otherwise get wrong: plain
# `Escape`, `Tab`, `Home` and the rest already resolve on their own.
ALIASES = {
    # The bug this file exists for.
    "enter": "Return", "returnkey": "Return", "enterkey": "Return",
    "newline": "Return", "carriagereturn": "Return",
    "numpadenter": "KP_Enter", "keypadenter": "KP_Enter",
    # Abbreviations.
    "esc": "Escape", "del": "Delete", "ins": "Insert", "backspace": "BackSpace",
    "bksp": "BackSpace", "back": "BackSpace", "capslock": "Caps_Lock",
    "numlock": "Num_Lock", "scrolllock": "Scroll_Lock",
    "printscreen": "Print", "prtsc": "Print", "prntscrn": "Print",
    "sysrq": "Print", "pausebreak": "Pause", "context": "Menu",
    "contextmenu": "Menu", "rightclickkey": "Menu",
    # Spelled out with a space, which arrives here with the space removed.
    "pagedown": "Page_Down", "pgdn": "Page_Down", "pagedn": "Page_Down",
    "pageup": "Page_Up", "pgup": "Page_Up",
    "spacebar": "space", "spacekey": "space",
    # "up arrow" and "arrow up" are both said; both collapse to one of these.
    "uparrow": "Up", "arrowup": "Up", "downarrow": "Down", "arrowdown": "Down",
    "leftarrow": "Left", "arrowleft": "Left",
    "rightarrow": "Right", "arrowright": "Right",
    # Punctuation, which has a *word* for a keysym name. Said out loud far
    # more often than it is spelled: "press slash", "control minus".
    "dot": "period", "fullstop": "period", "point": "period",
    "dash": "minus", "hyphen": "minus", "underscore": "underscore",
    "plus": "plus", "equals": "equal", "equalsign": "equal",
    "forwardslash": "slash", "backslash": "backslash",
    "star": "asterisk", "asterisk": "asterisk", "hash": "numbersign",
    "pound": "numbersign", "hashtag": "numbersign", "at": "at",
    "tilde": "asciitilde", "backtick": "grave", "graveaccent": "grave",
    "caret": "asciicircum", "ampersand": "ampersand", "pipe": "bar",
    "questionmark": "question", "exclamationmark": "exclam",
    "exclamationpoint": "exclam", "quote": "apostrophe",
    "singlequote": "apostrophe", "doublequote": "quotedbl",
    "openbracket": "bracketleft", "closebracket": "bracketright",
    "leftbracket": "bracketleft", "rightbracket": "bracketright",
    "openbrace": "braceleft", "closebrace": "braceright",
    "openparen": "parenleft", "closeparen": "parenright",
    "lessthan": "less", "greaterthan": "greater",
    # Media keys, said by their function rather than their XF86 name.
    "playpause": "XF86AudioPlay", "play": "XF86AudioPlay",
    "pausemedia": "XF86AudioPause", "stopmedia": "XF86AudioStop",
    "nexttrack": "XF86AudioNext", "previoustrack": "XF86AudioPrev",
    "prevtrack": "XF86AudioPrev", "volumeup": "XF86AudioRaiseVolume",
    "volumedown": "XF86AudioLowerVolume", "mutevolume": "XF86AudioMute",
    "brightnessup": "XF86MonBrightnessUp",
    "brightnessdown": "XF86MonBrightnessDown",
}

# A literal character the model may pass instead of the keysym's name. These
# are the ones whose keysym name is not simply the character itself.
LITERALS = {
    " ": "space", ".": "period", ",": "comma", ";": "semicolon",
    ":": "colon", "'": "apostrophe", '"': "quotedbl", "/": "slash",
    "\\": "backslash", "-": "minus", "_": "underscore", "=": "equal",
    "+": "plus", "*": "asterisk", "&": "ampersand", "|": "bar",
    "!": "exclam", "?": "question", "@": "at", "#": "numbersign",
    "$": "dollar", "%": "percent", "^": "asciicircum", "~": "asciitilde",
    "`": "grave", "(": "parenleft", ")": "parenright",
    "[": "bracketleft", "]": "bracketright",
    "{": "braceleft", "}": "braceright", "<": "less", ">": "greater",
}

# Said around the key rather than as part of its name: "the enter key",
# "hit the escape button".
_NOISE = re.compile(r"^(?:the|a)\s+|\s+(?:key|button|keys)$", re.IGNORECASE)
_XKB_KEYSYM_CASE_INSENSITIVE = 1
_NO_SYMBOL = 0


@functools.lru_cache(maxsize=1)
def _xkb():
    """libxkbcommon, or None where it is not installed.

    Hyprland links it, so on any machine this actually runs on it is present.
    Absent (a container, another OS running the tests), name resolution falls
    back to the alias table and nothing is rejected — refusing every key on a
    machine we cannot ask would be worse than the bug being fixed.
    """
    try:
        path = ctypes.util.find_library("xkbcommon")
        lib = ctypes.CDLL(path or "libxkbcommon.so.0")
        lib.xkb_keysym_from_name.restype = ctypes.c_uint32
        lib.xkb_keysym_from_name.argtypes = [ctypes.c_char_p, ctypes.c_uint32]
        lib.xkb_keysym_get_name.restype = ctypes.c_int
        lib.xkb_keysym_get_name.argtypes = [ctypes.c_uint32, ctypes.c_char_p,
                                            ctypes.c_size_t]
        return lib
    except (OSError, AttributeError):
        return None


def canonical_keysym(name: str) -> str | None:
    """The keysym's own spelling of `name`, or None if there is no such keysym.

    Resolution is case-insensitive so "return" and "ESCAPE" are accepted, but
    what comes back is what xkb calls it — Hyprland is handed `Return`, never
    `return`, so a case-folding difference between versions cannot reintroduce
    a silent miss.
    """
    lib = _xkb()
    if lib is None:
        return name  # unverifiable; pass it through rather than refuse
    keysym = lib.xkb_keysym_from_name(name.encode(), _XKB_KEYSYM_CASE_INSENSITIVE)
    if keysym == _NO_SYMBOL:
        return None
    buffer = ctypes.create_string_buffer(64)
    written = lib.xkb_keysym_get_name(keysym, buffer, len(buffer))
    if written <= 0:
        return name
    return buffer.value.decode()


def _suggest(name: str) -> str:
    """A "did you mean" drawn from the names we know, for the error message."""
    pool = sorted({*ALIASES.values(), *ALIASES, *LITERALS.values()})
    close = difflib.get_close_matches(name.lower(), pool, n=1, cutoff=0.72)
    if not close:
        return ""
    fixed = ALIASES.get(close[0], close[0])
    return f' Did you mean "{fixed}"?'


def normalise_key(key: str) -> tuple[str | None, str | None]:
    """(keysym, error). Exactly one of the two is set.

    The error is written for the model: it says the key was not pressed, which
    is the fact Hyprland's `ok` hides.
    """
    raw = (key or "").strip()
    if not raw:
        return None, "key is required — name the key to press, e.g. \"Return\"."
    if raw in LITERALS:
        return LITERALS[raw], None
    # "the Page Down key" -> "pagedown". Spaces, hyphens and underscores all
    # come out of speech-to-text interchangeably.
    stripped = _NOISE.sub("", raw).strip() or raw
    folded = re.sub(r"[\s_\-]+", "", stripped).lower()
    if folded in ALIASES:
        return ALIASES[folded], None
    if stripped in LITERALS:
        return LITERALS[stripped], None
    resolved = canonical_keysym(stripped)
    if resolved is not None:
        return resolved, None
    return None, (
        f"{raw!r} is not a key name on this system, so nothing was pressed."
        f"{_suggest(stripped)} Key names are X keysyms: Return, Escape, Tab, "
        "space, Page_Down, F5, a single letter or digit."
    )


def normalise_mods(mods: str) -> tuple[str | None, str | None]:
    """(mods, error). Hyprland ignores a modifier it does not recognise.

    Same failure shape as the keysym: `CMD + T` presses a bare T and reports
    success, so the tab opens in the wrong application or not at all.
    """
    raw = (mods or "").strip()
    if not raw:
        return "", None
    parts = [p for p in re.split(r"[\s+,]+", raw) if p]
    out: list[str] = []
    for part in parts:
        name = MODIFIERS.get(re.sub(r"[\s_\-]+", "", part).upper())
        if name is None:
            return None, (
                f"{part!r} is not a modifier, so the shortcut was not pressed. "
                f"Use one or more of: {', '.join(sorted(set(MODIFIERS.values())))}."
            )
        if name not in out:
            out.append(name)
    return " ".join(out), None
