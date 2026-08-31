"""Build the capability manifest handed to the model.

The point of this module: never hardcode desktop API syntax. Hyprland's Lua
dispatcher API changed shape in 0.56 (``hyprctl dispatch workspace 1`` is now
``hl.dsp.focus({ workspace = "1" })``), and Omarchy's CLI grows every release.
So the manifest is read off the running system:

  * ``/usr/share/hypr/stubs/hl.meta.lua``      -> the dispatcher tree
  * ``/usr/share/omarchy/default/hypr/bindings/`` -> real, version-correct call syntax
  * ``omarchy commands --json``                -> the CLI surface
  * ``hyprctl`` + desktop entries              -> what exists on *this* machine

It is cached, keyed on the versions of the things it was built from, so a
system update rebuilds it and nothing else has to change.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from .config import CACHE_DIR

OMARCHY_PATH = Path("/usr/share/omarchy")
HL_STUB = Path("/usr/share/hypr/stubs/hl.meta.lua")

# Omarchy command groups a voice assistant actually reaches for.
#
# An allow list, not a skip list. The skip list this replaces named seven groups
# and let the other fifty through, which is how the CLI section grew to 15 KB —
# more than half the manifest — on installer plumbing, hardware probes and
# plugin management, none of which anyone says out loud. Omarchy adds groups
# every release and they are far more often plumbing than speech, so the default
# for an unrecognised group should be "leave it out".
#
# Deliberately absent: `install`, `update`, `pkg`, `refresh`, `restart`,
# `migrate`, `drive`. Those are all held by the confirmation gate anyway, and
# listing them invites the model to reach for them.
VOICE_GROUPS = {
    "audio", "bar", "bluetooth", "brightness", "capture", "display", "file",
    "font", "games", "launch", "menu", "monitor", "network", "notification",
    "osd", "power", "powerprofiles", "reminder", "screensaver", "share", "show",
    "system", "theme", "toggle", "tui", "voxtype", "weather", "webapp",
}

# Long enough to disambiguate two similar routes, short enough that 128 of them
# do not cost more than everything else in the manifest put together.
SUMMARY_CHARS = 44

# Only short routes get a summary. Omarchy's routes are English, and a long one
# has already said what it does: `omarchy audio output volume <raise|lower|...>`
# gains nothing from "Adjust the output volume" after it. A two-word route like
# `omarchy capture qr` has not, so it keeps one.
#
# This is not cosmetic. Every token here is spent again on every single turn —
# cached tokens still count against the API's tokens-per-minute limit — so the
# manifest's size is directly how many things the user can say in a minute.
SUMMARY_MAX_SEGMENTS = 2


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def system_versions() -> dict[str, str]:
    return {
        "omarchy": _run(["omarchy", "version"]) or "unknown",
        "hyprland": (_run(["hyprctl", "version"]).splitlines() or ["unknown"])[0],
    }


def dispatcher_tree() -> str:
    """Parse the hl.dsp namespace out of Hyprland's own LuaLS stub."""
    if not HL_STUB.exists():
        return ""
    text = HL_STUB.read_text(errors="replace")
    namespaces: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        cls = re.match(r"---@class HL\.Dsp(\w*)Namespace", line)
        if cls:
            current = cls.group(1).lower() or "root"
            namespaces.setdefault(current, [])
            continue
        if current is None:
            continue
        field = re.match(r"---@field (\w+) fun\(", line)
        if field:
            namespaces[current].append(field.group(1))
        elif not line.startswith("---@field"):
            current = None

    lines = []
    for name in sorted(namespaces):
        prefix = "hl.dsp." if name == "root" else f"hl.dsp.{name}."
        members = namespaces[name]
        if members:
            lines.append(f"  {prefix}{{{', '.join(sorted(members))}}}")
    return "\n".join(lines)


def dispatch_examples(limit: int = 16) -> str:
    """Harvest real dispatcher calls from Omarchy's own keybindings.

    These are guaranteed-correct for the installed Hyprland: they are what the
    running desktop binds to keys right now.
    """
    bindings = OMARCHY_PATH / "default/hypr/bindings"
    if not bindings.is_dir():
        return ""
    seen: dict[str, str] = {}
    for path in sorted(bindings.glob("*.lua")):
        for line in path.read_text(errors="replace").splitlines():
            match = re.search(r'o\.bind\([^,]+,\s*"([^"]+)"\s*,\s*(hl\.dsp\.[^\n]+?)\)\s*$', line)
            if match:
                desc, call = match.group(1), match.group(2).rstrip(")") + ")"
                seen.setdefault(desc, call)
            else:
                shell = re.search(r'o\.bind\([^,]+,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)', line)
                if shell:
                    seen.setdefault(shell.group(1), f'shell: {shell.group(2)}')
    rows = [f"  {desc}  →  {call}" for desc, call in list(seen.items())[:limit]]
    return "\n".join(rows)


def omarchy_commands(limit: int = 120) -> str:
    """The Omarchy CLI surface, straight from `omarchy commands --json`.

    Filtered to what a voice assistant should reach for: no dev/hardware
    plumbing, no hidden commands, and nothing needing a sudo password (a
    background daemon has no terminal to type one into).
    """
    raw = _run(["omarchy", "commands", "--json"], timeout=20)
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    entries = data if isinstance(data, list) else data.get("commands", [])
    rows = []
    for entry in entries:
        route = entry.get("route", "")
        if not route or entry.get("hidden") or entry.get("requires_sudo"):
            continue
        if entry.get("group") not in VOICE_GROUPS:
            continue
        signature = f'{route} {entry.get("args", "")}'.strip()
        # No column padding. Aligning summaries at column 62 spent roughly a
        # fifth of this section on spaces, and the model does not read columns.
        summary = ""
        if len(route.split()) - 1 <= SUMMARY_MAX_SEGMENTS:
            summary = (entry.get("summary") or "")[:SUMMARY_CHARS].strip()
        rows.append(f'  {signature}' + (f'  — {summary}' if summary else ""))
        if len(rows) >= limit:
            break
    return "\n".join(rows)


COMMAND_INDEX = "command-index.tsv"


def command_index(refresh: bool = False) -> list[tuple[str, str]]:
    """Every voice-relevant omarchy route, as (signature, summary).

    This used to be pasted into the manifest — 128 routes, ~2,270 tokens, resent
    on every single turn against a per-minute budget, so that the assistant could
    reach for `omarchy notification dismiss` about once a week. It is now looked
    up on demand by the omarchy_help tool instead. The fifteen things anyone
    actually says out loud stay inline in ESSENTIALS.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{_cache_key()}-{COMMAND_INDEX}"
    if cached.exists() and not refresh:
        rows = []
        for line in cached.read_text().splitlines():
            signature, _, summary = line.partition("\t")
            if signature:
                rows.append((signature, summary))
        return rows

    raw = _run(["omarchy", "commands", "--json"], timeout=20)
    rows = []
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    for entry in (data if isinstance(data, list) else data.get("commands", [])):
        route = entry.get("route", "")
        if not route or entry.get("hidden") or entry.get("requires_sudo"):
            continue
        if entry.get("group") not in VOICE_GROUPS:
            continue
        rows.append((f'{route} {entry.get("args", "")}'.strip(),
                     (entry.get("summary") or "").strip()))
    for stale in CACHE_DIR.glob(f"*-{COMMAND_INDEX}"):
        stale.unlink(missing_ok=True)
    cached.write_text("\n".join(f"{sig}\t{summ}" for sig, summ in rows))
    return rows


def search_commands(query: str, limit: int = 12) -> list[str]:
    """Routes matching `query`, best first. Every word has to appear somewhere."""
    words = [w for w in re.split(r"\W+", query.lower()) if w]
    if not words:
        return []
    scored = []
    for signature, summary in command_index():
        haystack = f"{signature} {summary}".lower()
        # Any word, not every word. The model asks the way a person would —
        # "dark theme", "turn off the night light" — and requiring all of them
        # returned nothing for "dark theme" because no route says "dark".
        # Matching any, then ranking by how many hit, finds `theme set` first.
        hits = [w for w in words if w in haystack]
        if not hits:
            continue
        # A hit in the route itself beats a hit in the prose describing it.
        score = sum(2 if w in signature.lower() else 1 for w in hits)
        scored.append((-score, len(signature), signature, summary))
    scored.sort()
    return [f"  {sig}" + (f"  — {summ}" if summ else "")
            for _, _, sig, summ in scored[:limit]]


def installed_apps(limit: int = 28) -> str:
    """Desktop entries, so the model launches things that actually exist."""
    names: dict[str, str] = {}
    roots = [
        Path.home() / ".local/share/applications",
        Path("/usr/share/applications"),
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.glob("*.desktop")):
            name = exec_line = ""
            no_display = False
            for line in entry.read_text(errors="replace").splitlines():
                if line.startswith("Name=") and not name:
                    name = line[5:].strip()
                elif line.startswith("Exec=") and not exec_line:
                    exec_line = line[5:].strip()
                elif line.startswith("NoDisplay=true"):
                    no_display = True
            if name and not no_display:
                names.setdefault(name, entry.stem)
    rows = [f"{name} ({desktop_id})" for name, desktop_id in list(names.items())[:limit]]
    return "  " + "\n  ".join(rows) if rows else ""


def live_state() -> str:
    """A snapshot of the desktop right now — refreshed on every request."""
    def query(what: str):
        raw = _run(["hyprctl", "-j", what])
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None

    parts = []
    monitors = query("monitors") or []
    parts.append("Monitors: " + ", ".join(
        f'{m["name"]} {m["width"]}x{m["height"]} (workspace {m.get("activeWorkspace", {}).get("name")})'
        for m in monitors
    ))
    workspaces = query("workspaces") or []
    parts.append("Workspaces in use: " + ", ".join(
        f'{w["name"]} ({w.get("windows", 0)} windows)' for w in sorted(
            workspaces, key=lambda w: w.get("id", 0)) if w.get("id", 0) > 0
    ))
    active = query("activewindow") or {}
    if active.get("class"):
        parts.append(f'Focused window: {active.get("class")} — "{active.get("title")}"')
    clients = query("clients") or []
    if clients:
        rows = [
            f'    {c.get("class","?")} — "{(c.get("title") or "")[:70]}" '
            f'[workspace {c.get("workspace", {}).get("name")}, address {c.get("address")}]'
            for c in clients if not c.get("hidden")
        ]
        parts.append("Open windows:\n" + "\n".join(rows[:25]))
    return "\n".join(parts)


# The fifteen actions a voice assistant reaches for most, and the exact command
# for each. This exists because the two generated sections below both miss them:
# `dispatch_examples` scrapes only `hl.dsp.*` and bare-string bindings, so every
# app binding in applications.lua is invisible to it (they pass a *table*, e.g.
# `o.bind("SUPER + SHIFT + RETURN", "Browser", { omarchy = "browser" })`), and
# `omarchy_commands` truncates long before reaching most of these.
#
# Every route here was checked against `omarchy commands --json`; `verify_essentials`
# re-checks them, and `doctor` reports any that a system update has broken.
ESSENTIALS = [
    ("Open a terminal",            "omarchy launch terminal"),
    ("Open the browser",           "omarchy launch browser [url]"),
    ("Open the editor",            "omarchy launch editor"),
    ("Open the file manager",      "omarchy launch nautilus"),
    ("Open a web app, or focus it if already open",
                                   "omarchy launch or focus webapp <window-pattern> <url>"),
    ("Open any app, or focus it if already open",
                                   "omarchy launch or focus <window-pattern> <launch-command>"),
    ("Open a terminal app (TUI)",  "omarchy launch or focus tui <command> [args...]"),
    ("Take a screenshot",          "omarchy capture screenshot [smart|region|windows|fullscreen]"),
    ("Read text off the screen (OCR)", "omarchy capture text"),
    ("Open a menu",                "omarchy menu [keybindings|clipboard|emoji|file|images|input]"),
    ("Lock the screen",            "omarchy system lock"),
    ("Change the volume",          "omarchy audio output volume <raise|lower|mute-toggle|+N|-N>"),
    ("Mute the microphone",        "omarchy audio input mute"),
    ("Change the theme",           "omarchy theme set <theme-name>   (omarchy theme list)"),
    ("Dismiss a notification",     "omarchy notification dismiss <summary>"),
]

# A second window of an already-running app. `omarchy launch ...` and a plain
# launch_app both focus what is already open, which is right for "open my
# browser" and wrong for "open another one".
SECOND_WINDOW = (
    "For ANOTHER window of an app that is already open, use the launch_app tool "
    "with '<desktop-id>:<action>', e.g. launch_app(app=\"google-chrome:new-window\") "
    "or 'google-chrome:new-private-window' for incognito. Launching normally "
    "focuses the existing window instead of opening a second one."
)


def app_bindings() -> str:
    """The apps and web apps this desktop binds to keys, with how to open each.

    applications.lua passes a *table* as the third argument to o.bind — e.g.
    `{ omarchy = "browser" }`, `{ webapp = "https://chatgpt.com" }` — which the
    `hl.dsp.*`/bare-string scraper in dispatch_examples cannot see. Without this
    the model knows these apps exist but not how to open them, and invents URLs:
    asked for ChatGPT it guessed the long-dead chat.openai.com.
    """
    path = OMARCHY_PATH / "default/hypr/bindings/applications.lua"
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = re.search(r'o\.bind\(\s*"[^"]+"\s*,\s*"([^"]+)"\s*,\s*\{([^}]*)\}', line)
        if not match:
            continue
        label, table = match.group(1), match.group(2)
        if (app := re.search(r'omarchy\s*=\s*"([^"]+)"', table)):
            # nautilus-cwd -> "nautilus cwd", but leave "browser --private"
            # alone: only a hyphen *between* word characters is a route separator.
            route = re.sub(r"(?<=\w)-(?=\w)", " ", app.group(1))
            rows.setdefault(label, f"launch {route}")
        elif (url := re.search(r'webapp\s*=\s*"([^"]+)"', table)):
            pattern = label.lower().replace(" ", "")
            rows.setdefault(label, f"launch or focus webapp {pattern} {url.group(1)}")
        elif (tui := re.search(r'tui\s*=\s*"([^"]+)"', table)):
            rows.setdefault(label, f"launch or focus tui {tui.group(1)}")
        elif (cmd := re.search(r'launch\s*=\s*"([^"]+)"', table)):
            rows.setdefault(label, f"launch or focus {cmd.group(1)} {cmd.group(1)}")
    return "\n".join(f"  {label:<22} omarchy {how}" for label, how in rows.items())


# The window and workspace calls a voice assistant needs constantly.
#
# These are NOT in the scraped examples below, and that gap is the reason they
# are written out here: Omarchy generates its "Switch to workspace 1..10"
# bindings in a Lua loop, and dispatch_examples only reads literal o.bind(...)
# lines. So the single most common spoken command — "go to workspace four" —
# had no worked example anywhere in the manifest, and the model guessed. It
# reached for hl.dsp.workspace.change_id, which is a *rename* and needs both
# `workspace` and `id`, so workspace navigation silently did nothing.
HYPR_ESSENTIALS = [
    ("Switch to workspace N",            'hl.dsp.focus({ workspace = "4" })'),
    ("Next / previous workspace",        'hl.dsp.focus({ workspace = "e+1" })   -- or "e-1"'),
    ("Back to the previous workspace",   'hl.dsp.focus({ workspace = "previous" })'),
    ("Move this window to workspace N",  'hl.dsp.window.move({ workspace = "4", follow = true })'),
    ("Focus a specific window",          'hl.dsp.focus({ window = "address:0x55..." })'),
    ("Focus left/right/up/down",         'hl.dsp.focus({ direction = "l" })'),
    ("Close the focused window",         'hl.dsp.window.close()'),
    ("Fullscreen the focused window",    'hl.dsp.window.fullscreen({ mode = "fullscreen" })'),
    ("Float / unfloat it",               'hl.dsp.window.float({ action = "toggle" })'),
]

HYPR_WARNING = (
    "Switching workspaces is hl.dsp.focus, never hl.dsp.workspace.change_id — "
    "change_id RENAMES a workspace and requires both `workspace` and `id`. "
    "If a dispatch returns an error, read it and fix the call; do not repeat it."
)


def hypr_essentials() -> str:
    return "\n".join(f"  {what:<34} {how}" for what, how in HYPR_ESSENTIALS)


def essentials() -> str:
    return "\n".join(f"  {what:<44} {how}" for what, how in ESSENTIALS)


def verify_hypr_essentials() -> list[str]:
    """Which HYPR_ESSENTIALS name a dispatcher this Hyprland does not have.

    Parse-only — running them would move the user's windows. It catches the
    failure that mattered here: a call written against an API that has since
    changed, which shows up as silence rather than an error the user can see.
    """
    tree = dispatcher_tree()
    if not tree:
        return []
    broken = []
    for what, how in HYPR_ESSENTIALS:
        match = re.match(r"(hl\.dsp(?:\.[a-z_]+)*)\.([a-z_]+)\(", how)
        if not match:
            continue
        namespace, leaf = match.group(1), match.group(2)
        parent = namespace.rsplit(".", 1)[-1] if namespace != "hl.dsp" else "dsp"
        if leaf not in tree and f"{parent}.{leaf}" not in tree:
            broken.append(f"{what} -> {how}")
    return broken


def _omarchy_routes() -> set[str]:
    """Every `omarchy` route this machine actually has."""
    try:
        data = json.loads(_run(["omarchy", "commands", "--json"], timeout=15) or "{}")
    except json.JSONDecodeError:
        return set()
    return {c["route"] for c in data.get("commands", []) if c.get("route")}


def verify_essentials() -> list[str]:
    """Which ESSENTIALS no longer resolve to a real route. Empty is good.

    Prefix-matched: the table carries argument placeholders, and a route is
    stored without them.
    """
    routes = _omarchy_routes()
    if not routes:
        return []
    broken = []
    for what, how in ESSENTIALS:
        words = how.split()
        if not any(" ".join(words[:n]) in routes for n in range(len(words), 1, -1)):
            broken.append(f"{what} -> {how}")
    return broken


TEMPLATE = """\
# The machine you are operating

Omarchy {omarchy} — Arch Linux + Hyprland ({hyprland}), Wayland.

## Start here — the common actions

Run these with the omarchy_cli tool. Square brackets are optional, angle
brackets are yours to fill in.

{essentials}

## Apps this desktop already knows how to open

Use these exact commands rather than guessing a URL or a binary name.

{app_bindings}

{second_window}

## Hyprland dispatchers (read from this machine's Lua API stub)

Almost every dispatcher takes ONE table argument, or none. Positional strings
are rejected: `hl.dsp.cursor.move("400 300")` errors, `hl.dsp.cursor.move({{ x = 400, y = 300 }})`
works. The exception is `hl.dsp.layout`, which takes a layout message as a
plain string: `hl.dsp.layout("preselect r")`. Available:

{dispatchers}

### The calls you will need most

{hypr_essentials}

{hypr_warning}

## Dispatcher calls this desktop actually binds to keys

Copy these shapes. They are correct for the installed Hyprland version.

{examples}

## The rest of the Omarchy CLI

Not listed here. There are over a hundred more routes — themes, audio, network,
notifications, screenshots, toggles. Call `omarchy_help` with a word or two to
find the exact one ("dark theme", "bluetooth", "night light"), then run what it
gives you with omarchy_cli. Do not guess a route you have not seen.

## Applications installed here

{apps}
"""


def _cache_key() -> str:
    versions = system_versions()
    stamp = json.dumps(versions, sort_keys=True)
    for path in (HL_STUB, OMARCHY_PATH / "default/hypr/bindings"):
        try:
            stamp += str(path.stat().st_mtime_ns)
        except OSError:
            pass
    return hashlib.sha256(stamp.encode()).hexdigest()[:16]


def manifest(refresh: bool = False) -> str:
    """The stable half of the system prompt. Cached, and cache-friendly:
    identical bytes across requests so the API prefix cache can hold it."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"manifest-{_cache_key()}.md"
    if cached.exists() and not refresh:
        return cached.read_text()

    versions = system_versions()
    text = TEMPLATE.format(
        omarchy=versions["omarchy"],
        hyprland=versions["hyprland"],
        dispatchers=dispatcher_tree() or "  (Lua stub not found — use hyprctl syntax with care)",
        essentials=essentials(),
        app_bindings=app_bindings() or "  (none found)",
        second_window=SECOND_WINDOW,
        hypr_essentials=hypr_essentials(),
        hypr_warning=HYPR_WARNING,
        examples=dispatch_examples() or "  (none found)",
        apps=installed_apps() or "  (no desktop entries found)",
    )
    for stale in CACHE_DIR.glob("manifest-*.md"):
        stale.unlink(missing_ok=True)
    cached.write_text(text)
    return text


def missing_tools() -> list[str]:
    return [t for t in ("hyprctl", "omarchy", "wtype", "pw-record") if not shutil.which(t)]
