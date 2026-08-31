"""The hands: the tool surface the model drives the desktop with.

A small set of *general* tools rather than one tool per desktop action. The
model already knows the desktop's real API (see capabilities.manifest), so a
tool per verb would go stale on every Omarchy release.

Every call passes through `Policy` first. An open microphone is an untrusted
input channel: a misheard sentence should not be able to reformat a disk.
"""

from __future__ import annotations

import json
import re
import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import capabilities
from .config import Config

QUERY_KINDS = {
    "clients", "workspaces", "monitors", "activewindow", "activeworkspace",
    "devices", "layers", "binds", "animations", "version",
}

# Read-only tools still run under --dry-run so the planner can see the desktop.
READ_ONLY_TOOLS = {"hypr_query", "read_screen", "omarchy_help"}

# Hyprland dispatchers that spawn processes. They bypass allow_shell unless
# we reject them here.
SHELL_DISPATCHERS = {"exec_cmd", "exec_raw", "exec"}

# A single hl.dsp.* call: namespace dots, then one argument list, nothing else.
_DISPATCH_RE = re.compile(
    r"^hl\.dsp(?:\.[A-Za-z_][\w]*)+\s*\(.*\)\s*;?\s*$",
    re.DOTALL,
)
_DESKTOP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class Denied(Exception):
    """The policy refused an action outright."""


class NeedsConfirmation(Exception):
    """The action is allowed, but only after the user says so out loud."""

    def __init__(self, description: str):
        super().__init__(description)
        self.description = description


@dataclass
class Policy:
    config: Config

    def check(self, description: str) -> None:
        for pattern in self.config.deny_patterns:
            if re.search(pattern, description, re.IGNORECASE):
                raise Denied(f"blocked by deny rule /{pattern}/")
        for pattern in self.config.confirm_patterns:
            if re.search(pattern, description, re.IGNORECASE):
                raise NeedsConfirmation(description)


@dataclass
class Result:
    ok: bool
    output: str

    def as_tool_result(self) -> str:
        if self.ok:
            return self.output or "done"
        return f"ERROR: {self.output}"


# --- window composition -----------------------------------------------------
#
# One tool call that builds a whole workspace, because the model is bad at the
# part that is not language. Asked to "open the news", it would launch three
# apps and immediately dispatch move/focus at addresses that did not exist yet:
# a window appears some hundreds of milliseconds *after* the launch command
# returns, so every placement raced the app it was placing. Doing it here means
# one round trip instead of six, and a wait for the window that actually
# arrived rather than a guess at its address.

LAYOUTS = ("columns", "main-and-side", "grid")
PANE_KINDS = ("web", "terminal", "tui", "app")

# How long to wait for one window to map. Chromium web apps are the slow case
# (cold start, profile load); a terminal is up in well under a second.
PANE_TIMEOUT = {"web": 12.0, "app": 10.0, "terminal": 6.0, "tui": 6.0}
# Whole-composition budget. Past this, remaining panes are launched without
# waiting — a slow fourth app must not hold the assistant mute for a minute.
COMPOSE_BUDGET = 32.0
MAX_PANES = 6
# How long to let a launch command run before deciding it is an application
# rather than a hung command. See Executor._shell.
LAUNCH_GRACE = 0.5
# Cap on the text of a command's output handed back to the model. JSON
# queries are read here before anything is cut, so this only ever trims
# prose — see Executor._shell and _query_json.
OUTPUT_LIMIT = 4000
# How much OCR'd text to hand back. The prompt is already ~8k tokens and
# every turn is charged against a per-minute budget, so a screen's worth of
# text has to be bounded.
OCR_LIMIT = 6000
# Words tesseract is less sure of than this are noise, not targets.
MIN_OCR_CONFIDENCE = 45.0
# A phrase has to match this fraction of its words to count as found.
PHRASE_MATCH_FLOOR = 0.5
# ydotool button codes: 0xC0 is press+release, +1 right, +2 middle.
YDOTOOL_BUTTONS = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
CLICK_UNAVAILABLE = (
    "clicking needs ydotool, which is not set up on this machine. Hyprland can "
    "move the pointer but has no click dispatcher. Tell the user to run: "
    "sudo pacman -S ydotool && sudo systemctl enable --now ydotoold. "
    "Until then, drive the app with send_shortcut instead — most things that "
    "can be clicked can also be reached with a key."
)


def _layout_plan(layout: str, count: int) -> list[tuple[str, int]]:
    """Per pane after the first: (preselect direction, which pane to anchor on).

    Dwindle splits the *focused* window, so a layout is expressed as "stand on
    pane N, then open the next one to the right / below". `hl.dsp.layout` is one
    of the few dispatchers taking a positional string — `hl.dsp.layout("preselect r")`
    — which is why the manifest's "always a table" rule now says "almost always".
    """
    if count < 2:
        return []
    if layout == "main-and-side":
        # Pane 1 keeps the left half; everything else stacks down the right.
        return [("r", 0)] + [("d", i) for i in range(1, count - 1)]
    if layout == "grid":
        # 2x2. Beyond four panes a grid stops being a grid, so keep going right.
        plan = [("r", 0), ("d", 0), ("d", 1)][: max(0, count - 1)]
        return plan + [("r", i) for i in range(3, count - 1)]
    # columns: each new pane opens to the right of the one before it.
    return [("r", i) for i in range(count - 1)]


def _window_matches(client: dict, hint: str) -> bool:
    """Whether a window looks like the thing `hint` named.

    Checked against the class and against the *initial* title, because a page
    retitles itself the moment it loads: `omarchy launch webapp https://bbc.com`
    opens a window whose initialTitle is "www.bbc.com_/news" and whose title is
    a headline a second later.
    """
    haystack = " ".join(str(client.get(k) or "") for k in
                        ("class", "initialClass", "initialTitle", "title")).lower()
    return hint.lower() in haystack


def _pane_hint(kind: str, target: str, name: str) -> str:
    """What the window this pane opens should look like."""
    target = (target or "").strip()
    if kind == "web":
        host = (urlparse(target).hostname or "").lower()
        # chrome-www.bbc.com__news-Default contains the host either way, but
        # dropping www. also matches apnews.com against a bare host.
        return host[4:] if host.startswith("www.") else host
    if kind == "tui":
        argv = shlex.split(target) if target else []
        return re.sub(r"[^A-Za-z0-9_.-]", "", name or (argv[0] if argv else "")) or ""
    if kind == "app":
        return (target[:-8] if target.endswith(".desktop") else target).partition(":")[0]
    return ""  # a terminal has no distinguishing mark worth guessing at


def _pane_command(kind: str, target: str, name: str) -> list[str] | None:
    """The argv that opens one pane, or None if the kind/target do not fit.

    Every one of these is a *plain* launch, never launch-or-focus: composing a
    workspace means new windows here, not focus stolen away to a copy of the app
    that is already open on another workspace.
    """
    target = (target or "").strip()
    if kind == "web":
        if urlparse(target).scheme.lower() not in ("http", "https"):
            return None
        return ["omarchy", "launch", "webapp", target]
    if kind == "terminal":
        return ["omarchy", "launch", "terminal", *shlex.split(target)] if target \
            else ["omarchy", "launch", "terminal"]
    if kind == "tui":
        if not target:
            return None
        argv = shlex.split(target)
        app_id = re.sub(r"[^A-Za-z0-9_.-]", "", name or argv[0]) or argv[0]
        return ["omarchy", "launch", "tui", f"--app-id={app_id}", *argv]
    if kind == "app":
        app = target[:-8] if target.endswith(".desktop") else target
        if not _DESKTOP_ID_RE.match(app):
            return None
        launcher = shutil.which("uwsm-app") or shutil.which("gtk-launch")
        return [launcher, f"{app}.desktop"] if launcher else None
    return None


# --- tool schemas -----------------------------------------------------------
# Descriptions are written for the model, and carry the failure modes it would
# otherwise have to discover by trial and error.

TOOL_SCHEMAS = [
    {
        "name": "hypr_query",
        "description": (
            "Read desktop state as JSON. Use this before acting whenever the request "
            "refers to something by name ('my browser', 'the terminal on the left') — "
            "it returns window addresses you can target precisely."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": sorted(QUERY_KINDS),
                         "description": "Which hyprctl -j query to run."},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hypr_dispatch",
        "description": (
            "Run one Hyprland dispatcher, given as a Lua expression such as "
            'hl.dsp.focus({ workspace = "3" }). Every dispatcher takes a single table '
            "argument or none; positional strings are rejected. Target a specific window "
            'with the window key and an address from hypr_query, e.g. '
            'hl.dsp.window.move({ workspace = "2", window = "address:0x55..." }). '
            "Do not use hl.dsp.exec_cmd or hl.dsp.exec_raw — those are blocked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lua": {"type": "string", "description": "A single hl.dsp.* call."},
            },
            "required": ["lua"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_shortcut",
        "description": (
            "Press a key combination inside a window — the way to drive an application's "
            "own UI (new browser tab, editor save, close a dialog). Prefer this over "
            "typing text for anything that is a command rather than content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mods": {"type": "string", "description": 'e.g. "CTRL", "CTRL SHIFT", or "" for none.'},
                "key": {"type": "string", "description": 'e.g. "T", "Return", "Escape", "Page_Down".'},
                "window": {"type": "string",
                           "description": 'Target: "activewindow", or "address:0x..." / "class:chromium".'},
            },
            "required": ["mods", "key", "window"],
            "additionalProperties": False,
        },
    },
    {
        "name": "omarchy_cli",
        "description": (
            "Run an omarchy command (see the CLI list in the manifest). This is how you "
            "change themes, volume, brightness, screenshots, night light, reminders, "
            "and the rest of the desktop's own features. Give the command without the "
            "leading 'omarchy'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": 'e.g. "theme set catppuccin" or "audio output volume +5".'},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "omarchy_help",
        "description": (
            "Find the exact omarchy command for something not in the manifest's common "
            "list — themes, bluetooth, night light, notifications, power profiles, and "
            "the hundred-odd other routes this desktop has. Give a word or two "
            "(\"dark theme\", \"night light\", \"bluetooth\"); you get back real routes "
            "with their arguments. Use it instead of guessing a route, then run what it "
            "gives you with omarchy_cli."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": 'A word or two, e.g. "theme" or "night light".'},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "launch_app",
        "description": (
            "Start an application by desktop entry id, for apps with no entry in "
            "the manifest's \"Apps this desktop already knows how to open\" list. "
            "If the app IS in that list, use omarchy_cli with the exact command "
            "shown there — those launch-or-focus correctly and get the right "
            "window rules. Names like 'terminal' or 'browser' are omarchy launch "
            "routes, not desktop ids, and will fail here. "
            "For a SECOND window of an app that is already open, pass "
            "'<desktop-id>:<action>' — e.g. 'google-chrome:new-window', or "
            "'google-chrome:new-private-window' for incognito. Launching an app "
            "normally focuses the window that already exists instead of opening "
            "another one. "
            "Do not pass a shell command line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "A .desktop id from the application list."},
                "url": {"type": "string", "description": "Optional http(s) URL to open instead."},
            },
            "required": ["app"],
            "additionalProperties": False,
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type literal text into the focused window, as if from the keyboard. For "
            "content — a sentence, a search query, a path. Not for key commands; use "
            "send_shortcut for those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_screen",
        "description": (
            "Read the text that is on screen right now, with OCR. This is how you answer "
            "\"what does it say\", \"summarise this\", \"what's in the news\", \"read me the "
            "error\" — questions about CONTENT rather than about which windows exist. "
            "hypr_query tells you a window is called \"BBC News\"; this tells you what it "
            "says. Default target is the whole visible screen, which is usually what you "
            "want after composing a workspace: it reads every pane at once. "
            "Only windows currently visible can be read — a window on another workspace is "
            "not being drawn, so switch to it first. OCR is imperfect on small or stylised "
            "text; quote what you got rather than inventing what you expected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        '"screen" for the whole focused monitor (the default), '
                        '"activewindow" for the focused window, or an "address:0x..." '
                        "from hypr_query for one specific visible window."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "click_text",
        "description": (
            "Click something on screen by the words you can see on it — a headline, a "
            "button, a link, a menu entry. Say the text, not coordinates: "
            "click_text(text=\"Continue\") or click_text(text=\"US and Iran trade "
            "strikes\", double=True). It reads the screen, finds those words, puts the "
            "pointer on them and clicks. "
            "Only what is visible can be clicked, so switch to the right workspace first. "
            "If you are not sure of the exact wording, call read_screen and use words that "
            "actually came back. Prefer send_shortcut when a keyboard shortcut does the "
            "same job — it is faster and cannot miss."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "The visible text to click, e.g. \"Continue\"."},
                "button": {"type": "string", "enum": ["left", "right", "middle"],
                           "description": "Default left."},
                "double": {"type": "boolean",
                           "description": "True to double-click, e.g. to open an item."},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compose_windows",
        "description": (
            "Build a whole workspace for a task in one call: open several windows and "
            "lay them out together. This is the right tool whenever the user asks for a "
            "SUBJECT rather than an application — \"what's going on in the news today\", "
            "\"set me up to work on the budget\", \"I want to watch the game and follow "
            "the chat\". You choose what belongs on screen; pick real, current sites you "
            "know, and prefer the exact commands in the manifest's app list for anything "
            "this desktop already knows how to open. "
            "It waits for each window to actually appear before placing the next one, so "
            "do NOT follow it with your own move/focus calls — that races the layout it "
            "just built. Two to four panes is the useful range; more than that on one "
            "screen is unreadable. Say what you are opening before you call it: this "
            "takes a few seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "panes": {
                    "type": "array",
                    "description": "The windows to open, in order: left to right, then down.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": list(PANE_KINDS),
                                "description": (
                                    "web = a site in its own window (target is an https URL). "
                                    "terminal = a terminal (target is an optional command). "
                                    "tui = a terminal program such as btop or lazygit. "
                                    "app = an installed desktop id from the application list."
                                ),
                            },
                            "target": {
                                "type": "string",
                                "description": 'URL, command, or desktop id — e.g. "https://apnews.com", "btop", "spotify".',
                            },
                            "name": {
                                "type": "string",
                                "description": "Short label for this pane, e.g. \"AP News\". Used in the reply.",
                            },
                        },
                        "required": ["kind", "target"],
                        "additionalProperties": False,
                    },
                },
                "layout": {
                    "type": "string",
                    "enum": list(LAYOUTS),
                    "description": (
                        "columns = equal side by side, best for comparing sources. "
                        "main-and-side = first pane large, the rest stacked beside it, "
                        "best when one thing is the work and the others are reference. "
                        "grid = 2x2, for four peers."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": (
                        'Where to build it: "next" for the first empty workspace (the '
                        'default, and usually right — it does not disturb what is open), '
                        '"current", or a number like "4".'
                    ),
                },
            },
            "required": ["panes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_shell",
        "description": (
            "Run a shell command. Disabled unless the user turned it on in config. "
            "Only reach for it when no other tool can express the request."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]


_BARE_ADDRESS_RE = re.compile(r'(window\s*=\s*")(0x[0-9a-fA-F]+)(")')


def _normalise_window_addresses(lua: str) -> str:
    """Add the `address:` prefix Hyprland needs on a window address.

    `window = "0x55f9..."` does not match anything — Hyprland answers
    "window not found" and, because that arrives as a warning rather than an
    error, the caller was told it worked. A close then silently did nothing
    while the assistant reported success. A bare hex value in `window` can only
    be an address, so fixing it here is unambiguous.
    """
    return _BARE_ADDRESS_RE.sub(r'\1address:\2\3', lua)


XDG_APP_DIRS = (
    Path.home() / ".local/share/applications",
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
)


def _desktop_entry_path(app_id: str) -> Path | None:
    for directory in XDG_APP_DIRS:
        candidate = directory / f"{app_id}.desktop"
        if candidate.is_file():
            return candidate
    return None


def _desktop_entry_exists(app_id: str) -> bool:
    """Whether <app_id>.desktop is installed anywhere the launcher will look."""
    return _desktop_entry_path(app_id) is not None


def desktop_actions(app_id: str) -> list[str]:
    """The extra entry points a .desktop declares, e.g. Chrome's new-window.

    This is how a second window gets opened. Plain `launch` on an app that is
    already running focuses what is there, which is right for "open my browser"
    and wrong for "open another one".
    """
    path = _desktop_entry_path(app_id)
    if path is None:
        return []
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("Actions="):
            return [a for a in line.split("=", 1)[1].split(";") if a]
    return []


# Multi-word omarchy routes the model tends to write with hyphens.
_HYPHENATED_ROUTES = {
    "launch-or-focus": ["launch", "or", "focus"],
    "launch_or_focus": ["launch", "or", "focus"],
    "install-and-launch": ["install", "and", "launch"],
}


def normalise_omarchy(command: str) -> tuple[list[str], str | None]:
    """Split an omarchy command line and repair the two mistakes it arrives with.

    Both are in the session log. `omarchy launch-or-focus webapp ...` writes a
    multi-word route as a hyphenated command name, which is not a route and
    opens nothing. And the manifest lists signatures like
    `omarchy launch or focus webapp <window-pattern> <url>`, whose placeholders
    have been passed through verbatim.

    Shared with `describe`, so the log, the dry run and the confirmation prompt
    all show the command that would actually run.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return [], f"could not parse command: {exc}"
    while argv and argv[0] == "omarchy":
        argv = argv[1:]
    if not argv:
        return [], "empty command"
    if argv[0] in _HYPHENATED_ROUTES:
        argv = _HYPHENATED_ROUTES[argv[0]] + argv[1:]
    placeholders = [a for a in argv if len(a) > 2 and a.startswith("<") and a.endswith(">")]
    if placeholders:
        return argv, (
            f"{', '.join(placeholders)} is a placeholder from the command signature, "
            "not a value. Replace it with a real one — a window pattern is a short "
            'word matching the window, e.g. "x" for x.com.')
    return argv, None


def _misused_change_id(expr: str) -> str | None:
    """Catch `change_id` used as "go to workspace N", which it is not.

    This is the single most repeated mistake in the session log: the model
    reaches for `hl.dsp.workspace.change_id({ id = "5" })` to navigate, and
    change_id RENAMES a workspace — it needs both `workspace` (which one) and
    `id` (its new number). Given one key it does nothing useful, and Hyprland
    says so quietly enough that the assistant then told the user there was no
    workspace 5. Saying it in the manifest did not stop it; refusing the call
    and naming the right one does, and costs one tool round instead of a
    workspace switch that silently never happened.
    """
    if ".workspace.change_id" not in expr:
        return None
    keys = set(re.findall(r"(\w+)\s*=", expr))
    if {"workspace", "id"} <= keys:
        return None  # a genuine rename, with both halves
    return ('hl.dsp.workspace.change_id renames a workspace and needs both '
            '`workspace` and `id`. To SWITCH to a workspace, call '
            'hl.dsp.focus({ workspace = "N" }) instead.')


class Executor:
    """Runs tool calls against the real desktop (or narrates them, in dry-run)."""

    def __init__(self, config: Config, on_action: Callable[[str, str], None] | None = None):
        self.config = config
        self.policy = Policy(config)
        self.on_action = on_action or (lambda name, desc: None)
        self.pending: tuple[str, dict] | None = None
        self.transcript: list[str] = []
        self._lock = threading.Lock()

    # -- dispatch -----------------------------------------------------------
    def call(self, name: str, args: dict) -> Result:
        with self._lock:
            return self._call_locked(name, args)

    def _call_locked(self, name: str, args: dict) -> Result:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return Result(False, f"unknown tool {name!r}")
        description = self.describe(name, args)
        try:
            self.policy.check(description)
        except Denied as exc:
            self.transcript.append(f"DENIED  {description} ({exc})")
            return Result(False, f"refused: {exc}. Tell the user you will not do that.")
        except NeedsConfirmation:
            if self.pending:
                held = self.describe(*self.pending)
                self.transcript.append(f"HOLD    refused second gate; still holding {held}")
                return Result(False,
                              f"another action is already waiting for confirmation: {held}. "
                              "Confirm or cancel it first; do not try a second gated action.")
            self.pending = (name, args)
            self.transcript.append(f"HOLD    {description}")
            return Result(False,
                          "This action needs spoken confirmation. Stop here and ask the user "
                          "to confirm out loud; do not try another route around it.")

        self.transcript.append(f"RUN     {description}")
        self.on_action(name, description)
        if self.config.dry_run and name not in READ_ONLY_TOOLS:
            validator = getattr(self, f"_validate_{name}", None)
            if validator is not None:
                try:
                    error = validator(**args)
                except TypeError as exc:
                    return Result(False, f"bad arguments: {exc}")
                if error:
                    return Result(False, error)
            return Result(True, f"[dry-run] would run: {description}")
        try:
            return handler(**args)
        except TypeError as exc:
            return Result(False, f"bad arguments: {exc}")

    def run_pending(self) -> Result:
        """Execute the action the user just confirmed out loud."""
        with self._lock:
            if not self.pending:
                return Result(False, "nothing was waiting for confirmation")
            name, args = self.pending
            self.pending = None
            handler = getattr(self, f"_tool_{name}")
            description = self.describe(name, args)
            self.transcript.append(f"CONFIRM {description}")
            self.on_action(name, description)
            if self.config.dry_run and name not in READ_ONLY_TOOLS:
                return Result(True, f"[dry-run] would run: {description}")
            try:
                return handler(**args)
            except TypeError as exc:
                return Result(False, f"bad arguments: {exc}")

    def drop_pending(self) -> str | None:
        with self._lock:
            if not self.pending:
                return None
            held = self.describe(*self.pending)
            self.pending = None
            self.transcript.append(f"CANCEL  {held}")
            return held

    @staticmethod
    def describe(name: str, args: dict) -> str:
        if name == "hypr_dispatch":
            return args.get("lua", "")
        if name == "omarchy_cli":
            argv, _ = normalise_omarchy(args.get("command", ""))
            return " ".join(["omarchy", *argv]).rstrip()
        if name == "run_shell":
            return args.get("command", "")
        if name == "launch_app":
            return f'launch {args.get("app", "")}{" " + args["url"] if args.get("url") else ""}'
        if name == "send_shortcut":
            return f'press {args.get("mods", "")}+{args.get("key", "")} in {args.get("window", "")}'
        if name == "type_text":
            return f'type {args.get("text", "")!r}'
        if name == "omarchy_help":
            return f'look up omarchy command {args.get("query", "")!r}'
        if name == "click_text":
            kind = "double-click" if args.get("double") else "click"
            return f'{kind} {args.get("button", "left")} on {args.get("text", "")!r}'
        if name == "read_screen":
            return f'read screen ({args.get("target", "screen")})'
        if name == "compose_windows":
            panes = args.get("panes") or []
            labels = ", ".join(
                str(p.get("name") or p.get("target", ""))[:32]
                for p in panes if isinstance(p, dict))
            where = args.get("workspace", "next")
            return (f'compose {len(panes)} windows on workspace {where} '
                    f'({args.get("layout", "columns")}): {labels}')
        return f'{name} {args}'

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _shell(cmd: list[str], timeout: float = 20.0, grace: float | None = None,
               limit: int = OUTPUT_LIMIT) -> Result:
        """Run a command and read its result.

        `grace` is for commands that start an application. `omarchy launch
        terminal` does not return while the terminal is open, so waiting for it
        blocked the assistant for the full timeout and then reported failure —
        and the model, told the launch had failed, launched again. That is what
        turned one "open a terminal" into terminals appearing every 30 seconds.

        With a grace set, a process still alive after that many seconds is taken
        to be a running application: it is left alone, reaped in the background,
        and reported as started. Callers that need the output (queries) leave
        `grace` unset and wait the full timeout as before.

        LAUNCH_GRACE is measured, not guessed: on this machine every way an
        `omarchy launch` can fail — unknown route, missing argument — returns in
        under 0.35 s. The 1.2 s it used to wait was 0.85 s of silence added to
        every launch, and composition pays that per pane.
        """
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        except FileNotFoundError:
            return Result(False, f"{cmd[0]} is not installed")
        try:
            stdout, stderr = proc.communicate(timeout=grace if grace is not None else timeout)
        except subprocess.TimeoutExpired:
            if grace is None:
                proc.kill()
                proc.communicate()
                return Result(False, f"{cmd[0]} timed out")
            # Still running: a foreground application, not a hung command.
            # Reap it off-thread so the daemon does not collect zombies.
            threading.Thread(target=proc.communicate, daemon=True).start()
            return Result(True, "started")
        out = (stdout or "").strip()
        err = (stderr or "").strip()
        if proc.returncode != 0:
            return Result(False, err or out or f"exit {proc.returncode}")
        # hyprctl reports dispatcher failures on stdout with a zero exit code.
        if out.startswith("error:"):
            return Result(False, out)
        if len(out) > limit:
            # Say so. This used to cut silently at 4000 characters, which is
            # about five windows' worth of `hyprctl -j clients` — past that the
            # model was handed a JSON document with the end sliced off, and the
            # parse failure looked to it like an empty desktop.
            return Result(True, out[:limit] + f"\n… [truncated at {limit} characters]")
        return Result(True, out)

    # -- tools --------------------------------------------------------------
    def _tool_hypr_query(self, kind: str) -> Result:
        if kind not in QUERY_KINDS:
            return Result(False, f"unknown query {kind!r}")
        # Read the whole document, then slim it. Cutting first was a silent
        # correctness bug: `hyprctl -j clients` runs about 750 characters per
        # window, so from roughly the fifth window on, the model was parsing a
        # JSON array with no closing bracket and concluding nothing was open.
        result = self._shell(["hyprctl", "-j", kind], limit=1 << 22)
        if result.ok and kind == "clients":
            try:
                clients = json.loads(result.output)
            except json.JSONDecodeError:
                return result
            slim = [
                {k: c.get(k) for k in
                 ("address", "class", "title", "pid", "floating", "fullscreen")}
                | {"workspace": c.get("workspace", {}).get("name")}
                for c in clients
            ]
            return Result(True, json.dumps(slim))
        return result

    def _validate_hypr_dispatch(self, lua: str) -> str | None:
        expr = lua.strip()
        if not _DISPATCH_RE.match(expr):
            return "expression must be a single hl.dsp.* call"
        method = expr.split("(", 1)[0].split(".")[-1]
        if method in SHELL_DISPATCHERS and not self.config.allow_shell:
            return (f"hl.dsp.{method} is process execution; enable allow_shell "
                    "to use it, or launch apps with launch_app / omarchy_cli")
        if error := _misused_change_id(expr):
            return error
        return None

    def _tool_hypr_dispatch(self, lua: str) -> Result:
        error = self._validate_hypr_dispatch(lua)
        if error:
            return Result(False, error)
        return self._dispatch_lua(_normalise_window_addresses(lua.strip()))

    def _dispatch_lua(self, lua: str) -> Result:
        """Run one dispatcher, treating "not found" as the failure it is.

        hyprctl reports a missing target as `warning: ... not found` on stdout
        with a zero exit code, so it read as success. The model was told a
        window had been closed when nothing had happened, and moved on to the
        next step of a request whose first step had silently failed.
        """
        result = self._shell(["hyprctl", "dispatch", lua])
        if result.ok and "not found" in result.output.lower():
            first = result.output.splitlines()[0] if result.output else "not found"
            return Result(False, first.strip())
        return result

    def _tool_send_shortcut(self, mods: str, key: str, window: str = "activewindow") -> Result:
        lua = (
            f'hl.dsp.send_shortcut({{ mods = {json.dumps(mods)}, '
            f'key = {json.dumps(key)}, window = {json.dumps(window)} }})'
        )
        return self._dispatch_lua(lua)

    def _validate_omarchy_cli(self, command: str) -> str | None:
        return normalise_omarchy(command)[1]

    def _tool_omarchy_cli(self, command: str) -> Result:
        argv, error = normalise_omarchy(command)
        if error:
            return Result(False, error)
        # Only `omarchy launch ...` starts a foreground application; everything
        # else returns promptly and may have output worth reading, so it keeps
        # the full wait.
        grace = LAUNCH_GRACE if argv[0] == "launch" else None
        return self._shell(["omarchy", *argv], timeout=30, grace=grace)

    def _validate_launch_app(self, app: str, url: str = "") -> str | None:
        if url:
            scheme = urlparse(url).scheme.lower()
            if scheme not in ("http", "https"):
                return "url must be http or https"
            return None
        app = (app or "").strip()
        # "<desktop-id>:<action>" is a valid shape; validate the id half. The
        # action itself is checked against the entry's declared Actions later,
        # where a wrong one can name the alternatives.
        app = app.partition(":")[0].strip()
        if app.endswith(".desktop"):
            app = app[:-8]
        if not _DESKTOP_ID_RE.match(app):
            if not self.config.allow_shell:
                # Say what to do next. A bare refusal made the model retry the
                # same shape, and a failing launch loop is how a single "open a
                # terminal" turned into terminals opening every 30 seconds.
                return ("app must be a desktop id, not a command line. If this is "
                        "an app from the manifest's \"Apps this desktop already "
                        "knows how to open\" list, call omarchy_cli with the exact "
                        "command shown there instead.")
            try:
                argv = shlex.split(app)
            except ValueError as exc:
                return f"could not parse command: {exc}"
            if not argv:
                return "empty app"
        return None

    def _tool_launch_app(self, app: str, url: str = "") -> Result:
        error = self._validate_launch_app(app, url)
        if error:
            return Result(False, error)
        if url:
            return self._shell(["xdg-open", url], timeout=10, grace=LAUNCH_GRACE)
        app = (app or "").strip()
        # "google-chrome:new-window" — a desktop entry plus one of the actions
        # it declares. uwsm-app takes this shape directly.
        app, _, action = app.partition(":")
        app = app.strip()
        action = action.strip()
        if app.endswith(".desktop"):
            app = app[:-8]
        if not _DESKTOP_ID_RE.match(app):
            if action:
                return Result(False, f"{app!r} is not a desktop id, so it has no actions")
            return self._shell(shlex.split(app), timeout=10, grace=LAUNCH_GRACE)
        # uwsm-app happily returns success for a .desktop that does not exist,
        # so the model was told "opened" while nothing appeared and then tried
        # again. Check first and hand back the route that does work.
        if not _desktop_entry_exists(app):
            return Result(False,
                          f"no desktop entry named {app!r} on this system. If this app is in "
                          "the manifest's \"Apps this desktop already knows how to open\" "
                          "list, call omarchy_cli with the exact command shown there.")
        if action:
            available = desktop_actions(app)
            if action not in available:
                return Result(False, f"{app!r} has no action {action!r}"
                                     + (f"; it offers {', '.join(available)}" if available
                                        else " and declares none"))
        launcher = shutil.which("uwsm-app") or shutil.which("gtk-launch")
        if not launcher:
            return Result(False, "no desktop launcher (uwsm-app or gtk-launch)")
        target = f"{app}.desktop:{action}" if action else f"{app}.desktop"
        return self._shell([launcher, target], timeout=10, grace=LAUNCH_GRACE)

    def _tool_omarchy_help(self, query: str) -> Result:
        matches = capabilities.search_commands(query)
        if not matches:
            return Result(False, f"no omarchy command matches {query!r}. Try a single "
                                 "plainer word — \"theme\", \"audio\", \"bluetooth\".")
        return Result(True, "\n".join(matches) +
                      "\n\nRun one of these with omarchy_cli, without the leading 'omarchy'.")

    # -- reading the screen -------------------------------------------------
    def _visible_workspaces(self) -> set[str]:
        """Workspace names currently being drawn, one per monitor.

        grim captures what the compositor is painting. A window parked on a
        workspace nobody is looking at has no pixels, so OCR of it would come
        back empty and look like a page with no text on it rather than a window
        that is not on screen.
        """
        names = set()
        for monitor in self._query_json("monitors"):
            name = (monitor.get("activeWorkspace") or {}).get("name")
            if name is not None:
                names.add(str(name))
        return names

    def _ocr_region(self, geometry: str) -> Result:
        """grim the region, pipe it through tesseract, hand back the text."""
        for tool, package in (("grim", "grim"), ("tesseract", "tesseract")):
            if not shutil.which(tool):
                return Result(False, f"{tool} is not installed (pacman -S {package})")
        if asleep := self._screen_is_awake():
            return Result(False, asleep)
        try:
            shot = subprocess.run(["grim", "-g", geometry, "-"],
                                  capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            return Result(False, f"screen capture failed: {exc}")
        if shot.returncode != 0 or not shot.stdout:
            return Result(False, (shot.stderr or b"").decode(errors="replace").strip()
                          or "screen capture produced nothing")
        try:
            ocr = subprocess.run(
                ["tesseract", "stdin", "stdout", "--oem", "1", "--psm", "6",
                 "-l", os.environ.get("OMARCHY_OCR_LANGS", "eng"), "--dpi", "300",
                 "-c", "preserve_interword_spaces=1"],
                input=shot.stdout, capture_output=True, timeout=45)
        except (OSError, subprocess.SubprocessError) as exc:
            return Result(False, f"OCR failed: {exc}")
        text = (ocr.stdout or b"").decode(errors="replace").strip()
        if not text:
            return Result(False, "no readable text in that region")
        # Long enough for three news panes, short enough not to eat the whole
        # token budget for the turn — the prompt is already ~8k.
        if len(text) > OCR_LIMIT:
            text = text[:OCR_LIMIT] + "\n… [more text on screen, not read]"
        return Result(True, text)

    def _screen_is_awake(self) -> str | None:
        """Why the screen cannot be captured right now, or None if it can.

        grim copies frames from the compositor, and a monitor in DPMS off is not
        producing any — the capture does not fail, it blocks until the timeout.
        Reading the screen at 00:30 therefore hung for fifteen seconds and then
        reported an OCR failure, when the honest answer is that the display is
        asleep and nobody is looking at it.
        """
        monitors = self._query_json("monitors")
        if not monitors:
            return None  # cannot tell; let the capture try
        awake = [m for m in monitors
                 if m.get("dpmsStatus") is not False and not m.get("disabled")]
        if awake:
            return None
        return ("the display is asleep, so there is nothing on screen to read. "
                "Wake it first with hypr_dispatch: "
                'hl.dsp.dpms({ state = "on" })')

    def _ocr_words(self, geometry: str) -> tuple[list[dict], str]:
        """OCR a region into positioned words: [{text, x, y, w, h, conf}, ...].

        tesseract's `tsv` output carries a bounding box per word, which is the
        whole reason clicking by text is possible. Coordinates come back
        relative to the captured image, so the region's own origin is added
        back on to get screen coordinates.
        """
        for tool in ("grim", "tesseract"):
            if not shutil.which(tool):
                return [], f"{tool} is not installed"
        if asleep := self._screen_is_awake():
            return [], asleep
        try:
            origin_x, origin_y = (int(v) for v in geometry.split()[0].split(","))
        except (ValueError, IndexError):
            return [], "could not read the capture geometry"
        try:
            shot = subprocess.run(["grim", "-g", geometry, "-"],
                                  capture_output=True, timeout=15)
            if shot.returncode != 0 or not shot.stdout:
                return [], "screen capture produced nothing"
            ocr = subprocess.run(
                ["tesseract", "stdin", "stdout", "--oem", "1", "--psm", "6",
                 "-l", os.environ.get("OMARCHY_OCR_LANGS", "eng"), "--dpi", "300", "tsv"],
                input=shot.stdout, capture_output=True, timeout=45)
        except (OSError, subprocess.SubprocessError) as exc:
            return [], f"OCR failed: {exc}"

        words = []
        for line in (ocr.stdout or b"").decode(errors="replace").splitlines()[1:]:
            cell = line.split("\t")
            if len(cell) < 12 or not cell[11].strip():
                continue
            try:
                conf = float(cell[10])
                if conf < MIN_OCR_CONFIDENCE:
                    continue
                words.append({"text": cell[11].strip(),
                              "x": origin_x + int(cell[6]), "y": origin_y + int(cell[7]),
                              "w": int(cell[8]), "h": int(cell[9]), "conf": conf})
            except ValueError:
                continue
        return words, ""

    @staticmethod
    def _find_phrase(words: list[dict], query: str) -> tuple[int, int] | None:
        """Centre of the best run of words matching `query`, or None.

        Matched as a sliding window over consecutive words rather than a string
        search, because OCR splits a headline into words and drops the odd one.
        A run scores by how many of the query's words it contains, so
        "US and Iran trade strikes" still finds the headline when tesseract read
        "trade" as "frade".
        """
        wanted = [w for w in re.split(r"\W+", query.lower()) if w]
        if not wanted or not words:
            return None
        best_score, best_span = 0.0, None
        span_len = max(1, len(wanted))
        for start in range(len(words)):
            for length in (span_len, span_len + 1, max(1, span_len - 1)):
                run = words[start:start + length]
                if not run:
                    continue
                text = " ".join(w["text"].lower() for w in run)
                hits = sum(1 for w in wanted if w in text)
                if not hits:
                    continue
                # Prefer runs where more of the query landed, then tighter runs.
                score = hits / len(wanted) - 0.01 * abs(len(run) - span_len)
                if score > best_score:
                    best_score, best_span = score, run
        if best_span is None or best_score < PHRASE_MATCH_FLOOR:
            return None
        left = min(w["x"] for w in best_span)
        top = min(w["y"] for w in best_span)
        right = max(w["x"] + w["w"] for w in best_span)
        bottom = max(w["y"] + w["h"] for w in best_span)
        return (left + right) // 2, (top + bottom) // 2

    def _press_button(self, button: str, double: bool) -> Result:
        """The actual click. Hyprland has no click dispatcher, so this is ydotool."""
        if not shutil.which("ydotool"):
            return Result(False, CLICK_UNAVAILABLE)
        code = YDOTOOL_BUTTONS.get(button)
        if code is None:
            return Result(False, f"button must be one of {', '.join(YDOTOOL_BUTTONS)}")
        cmd = ["ydotool", "click"] + (["--repeat", "2"] if double else []) + [code]
        result = self._shell(cmd, timeout=10)
        if not result.ok and "uinput" in result.output.lower():
            return Result(False, CLICK_UNAVAILABLE)
        return result

    def _validate_click_text(self, text: str, button: str = "left",
                             double: bool = False) -> str | None:
        if not (text or "").strip():
            return "text is required — say what is on screen that you want clicked"
        if button not in YDOTOOL_BUTTONS:
            return f"button must be one of {', '.join(YDOTOOL_BUTTONS)}"
        return None

    def _tool_click_text(self, text: str, button: str = "left",
                         double: bool = False) -> Result:
        error = self._validate_click_text(text, button, double)
        if error:
            return Result(False, error)
        monitors = self._query_json("monitors")
        screen = next((m for m in monitors if m.get("focused")), None) \
            or (monitors[0] if monitors else None)
        if not screen:
            return Result(False, "no monitor to look at")
        try:
            geometry = f'{screen["x"]},{screen["y"]} {screen["width"]}x{screen["height"]}'
        except KeyError:
            return Result(False, "could not read the monitor geometry")

        words, error = self._ocr_words(geometry)
        if error:
            return Result(False, error)
        point = self._find_phrase(words, text)
        if point is None:
            return Result(False,
                          f"could not find {text!r} on screen. Read the screen first and "
                          "use wording you can actually see, or scroll it into view.")
        x, y = point
        moved = self._dispatch_lua(f'hl.dsp.cursor.move({{ x = {x}, y = {y} }})')
        if not moved.ok:
            return Result(False, f"could not move the pointer: {moved.output}")
        pressed = self._press_button(button, double)
        if not pressed.ok:
            return pressed
        return Result(True, f'{"double-" if double else ""}clicked {text!r} at {x},{y}')

    def _tool_read_screen(self, target: str = "screen") -> Result:
        target = (target or "screen").strip()

        if target in ("screen", "", "monitor", "all"):
            monitors = self._query_json("monitors")
            focused = next((m for m in monitors if m.get("focused")), None) \
                or (monitors[0] if monitors else None)
            if not focused:
                return Result(False, "no monitor to read")
            try:
                geometry = (f'{focused["x"]},{focused["y"]} '
                            f'{focused["width"]}x{focused["height"]}')
            except KeyError:
                return Result(False, "could not read the monitor geometry")
            return self._ocr_region(geometry)

        clients = self._query_json("clients")
        if target in ("activewindow", "active", "focused"):
            window = next((c for c in clients
                           if c.get("focusHistoryID") == 0), None)
            if window is None:
                return Result(False, "nothing is focused")
        else:
            address = target[8:] if target.startswith("address:") else target
            window = next((c for c in clients if c.get("address") == address), None)
            if window is None:
                return Result(False, f"no window with address {address!r} — "
                                     "call hypr_query(clients) for current addresses")

        workspace = str((window.get("workspace") or {}).get("name"))
        if workspace not in self._visible_workspaces():
            return Result(False,
                          f"that window is on workspace {workspace}, which is not on any "
                          "screen right now, so there is nothing to read. Switch to it "
                          "first with hl.dsp.focus, then read again.")
        try:
            geometry = (f'{window["at"][0]},{window["at"][1]} '
                        f'{window["size"][0]}x{window["size"][1]}')
        except (KeyError, IndexError, TypeError):
            return Result(False, "could not read that window's geometry")
        return self._ocr_region(geometry)

    # -- composition --------------------------------------------------------
    def _await_new_window(self, before: set[str], timeout: float,
                          hint: str = "") -> str | None:
        """Block until the window we just launched is mapped, and return it.

        This is the whole reason composition is a tool and not four dispatches:
        `omarchy launch` returns as soon as the process is started, which is
        long before the surface exists. Anything addressed in that gap silently
        misses.

        Taking simply "the newest window" is not enough. A composition ran while
        Chrome happened to raise a "Profile error occurred" dialog, and that
        dialog was claimed as the first pane: every later pane shifted by one and
        the layout came out 1261/621/626 instead of even columns. So a candidate
        has to look like the thing that was asked for — `hint` is the site's host,
        the app id, or the desktop id, matched against the class and the title the
        window was born with. A window with no class at all is never a launched
        application's own window, and is skipped outright.
        """
        deadline = time.monotonic() + timeout
        fallback: list[dict] = []
        while time.monotonic() < deadline:
            time.sleep(0.15)
            fresh = [c for c in self._query_json("clients")
                     if c.get("address") not in before and c.get("class")]
            if not fresh:
                continue
            fallback = fresh
            matched = [c for c in fresh if _window_matches(c, hint)] if hint else fresh
            if matched:
                # The one just mapped is the one with focus; focusHistoryID 0 is
                # the focused window. Ties fall back to whatever came back first.
                matched.sort(key=lambda c: c.get("focusHistoryID", 999))
                return matched[0].get("address")
        # The hint never matched but something did appear. Better to place that
        # than to report nothing opened, so long as it is not an unclassed dialog.
        if fallback:
            fallback.sort(key=lambda c: c.get("focusHistoryID", 999))
            return fallback[0].get("address")
        return None

    def _target_workspace(self, workspace: str) -> tuple[str | None, str]:
        """Resolve "next" / "current" / "4" to a workspace name, or an error."""
        workspace = (workspace or "next").strip().lower()
        if workspace == "current":
            return None, ""
        if workspace == "next":
            used = {w.get("id") for w in self._query_json("workspaces")
                    if (w.get("windows") or 0) > 0}
            for candidate in range(1, 11):
                if candidate not in used:
                    return str(candidate), ""
            return None, "every workspace from 1 to 10 already has windows on it"
        if not re.fullmatch(r"\d{1,2}", workspace):
            return None, f"{workspace!r} is not a workspace number, \"next\", or \"current\""
        return workspace, ""

    def _query_json(self, kind: str) -> list[dict]:
        """A hyprctl query parsed here, never truncated on the way in."""
        result = self._shell(["hyprctl", "-j", kind], timeout=5, limit=1 << 22)
        if not result.ok:
            return []
        try:
            data = json.loads(result.output)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def _equalize_columns(self, addresses: list[str | None],
                          workspace: str | None) -> None:
        """Even out a columns layout that dwindle halved instead of divided.

        Dwindle splits the *focused* window, so opening three panes to the right
        of each other gives 1/2, 1/4, 1/4 — the third pane is half the width of
        the first, which does not read as columns. Shrinking each pane in turn to
        one nth of the span hands the difference to the subtree holding the rest,
        which then splits it evenly, so the last two come out right on their own:
        on a 2560-wide monitor this takes 1261/621/626 to 845/829/834.

        Only `columns` wants this. `main-and-side` is unequal on purpose, and the
        grid comes out even from the preselects alone.
        """
        def row() -> list[dict]:
            """Every tiled window sharing the column row on the target workspace.

            Not just the panes we placed. A composition landed on an empty
            workspace at the same moment Chrome re-raised a "Profile error"
            dialog onto it; balancing only our own three left them at 420 px
            each beside a 1261 px intruder. Whatever is actually tiled there is
            what has to add up to the width of the screen.
            """
            here = [c for c in self._query_json("clients")
                    if not c.get("floating") and (c.get("fullscreen") or 0) == 0
                    and (workspace is None
                         or str(c.get("workspace", {}).get("name")) == str(workspace))]
            try:
                here.sort(key=lambda c: c["at"][0])
            except (KeyError, IndexError, TypeError):
                return []
            return here

        live = [a for a in addresses if a]
        if len(live) < 2:
            return
        boxes = row()
        if len(boxes) < 3:
            return  # a single split is already even
        for step in range(len(boxes) - 2):
            boxes = row()
            if len(boxes) < 3:
                return  # a window went away mid-layout; leave the rest alone
            try:
                left = boxes[0]["at"][0]
                span = boxes[-1]["at"][0] + boxes[-1]["size"][0] - left
                delta = int(span / len(boxes)) - boxes[step]["size"][0]
            except (KeyError, IndexError, TypeError):
                return
            if abs(delta) < 12:  # already within a gap's width of even
                continue
            # x and y are both required, and `relative` is what makes them a
            # delta rather than an absolute size.
            self._dispatch_lua(
                f'hl.dsp.window.resize({{ x = {delta}, y = 0, relative = true, '
                f'window = "address:{boxes[step]["address"]}" }})')

    def _validate_compose_windows(self, panes: list, layout: str = "columns",
                                  workspace: str = "next") -> str | None:
        if not isinstance(panes, list) or not panes:
            return "panes must be a non-empty list"
        if len(panes) > MAX_PANES:
            return f"at most {MAX_PANES} panes; more than that is unreadable on one screen"
        if layout not in LAYOUTS:
            return f"layout must be one of {', '.join(LAYOUTS)}"
        for index, pane in enumerate(panes, 1):
            if not isinstance(pane, dict):
                return f"pane {index} is not an object"
            kind = str(pane.get("kind", ""))
            if kind not in PANE_KINDS:
                return f"pane {index}: kind must be one of {', '.join(PANE_KINDS)}"
            if _pane_command(kind, str(pane.get("target", "")),
                             str(pane.get("name", ""))) is None:
                return (f"pane {index}: {str(pane.get('target',''))!r} is not usable as a "
                        f"{kind} target (web needs an http(s) URL, app needs a desktop id)")
        return None

    def _tool_compose_windows(self, panes: list, layout: str = "columns",
                              workspace: str = "next") -> Result:
        error = self._validate_compose_windows(panes, layout, workspace)
        if error:
            return Result(False, error)

        target, error = self._target_workspace(workspace)
        if error:
            return Result(False, error)
        if target is not None:
            move = self._dispatch_lua(f'hl.dsp.focus({{ workspace = "{target}" }})')
            if not move.ok:
                return Result(False, f"could not switch to workspace {target}: {move.output}")

        plan = _layout_plan(layout, len(panes))
        placed: list[str | None] = []
        opened: list[str] = []
        slow: list[str] = []
        deadline = time.monotonic() + COMPOSE_BUDGET

        for index, pane in enumerate(panes):
            kind = str(pane.get("kind", ""))
            label = str(pane.get("name", "")).strip() or str(pane.get("target", ""))[:40]
            argv = _pane_command(kind, str(pane.get("target", "")), str(pane.get("name", "")))
            assert argv is not None  # _validate_compose_windows already proved this

            # Defence in depth: a pane is built from a fixed set of shapes, but
            # the deny list is the thing that is allowed to have the last word.
            try:
                self.policy.check(" ".join(argv))
            except (Denied, NeedsConfirmation):
                return Result(False, f"pane {index + 1} ({label}) is not allowed by policy")

            if index > 0 and index - 1 < len(plan):
                direction, anchor = plan[index - 1]
                anchor_address = placed[anchor] if anchor < len(placed) else None
                if anchor_address:
                    self._dispatch_lua(f'hl.dsp.focus({{ window = "address:{anchor_address}" }})')
                # Positional string, not a table: `hl.dsp.layout` is the exception
                # to the one-table-argument rule. Best effort — a failed preselect
                # costs a tidy layout, not the window.
                self._dispatch_lua(f'hl.dsp.layout("preselect {direction}")')

            before = {c.get("address") for c in self._query_json("clients")}
            self.on_action("compose_windows", f"open {label} ({' '.join(argv)})")
            started = self._shell(argv, timeout=30, grace=LAUNCH_GRACE)
            if not started.ok:
                placed.append(None)
                slow.append(f"{label} (failed: {started.output[:60]})")
                continue

            budget = min(PANE_TIMEOUT.get(kind, 8.0), max(1.0, deadline - time.monotonic()))
            address = self._await_new_window(
                before, budget, _pane_hint(kind, str(pane.get("target", "")),
                                           str(pane.get("name", ""))))
            placed.append(address)
            if address is None:
                # Still coming, probably. Say so rather than claiming it is up.
                slow.append(label)
                continue
            opened.append(label)
            if target is not None:
                self._dispatch_lua(
                    f'hl.dsp.window.move({{ workspace = "{target}", '
                    f'window = "address:{address}" }})')

        if layout == "columns":
            self._equalize_columns(placed, target)

        first = next((a for a in placed if a), None)
        if first:
            self._dispatch_lua(f'hl.dsp.focus({{ window = "address:{first}" }})')

        # A window that is on the target workspace but is not one of ours. On a
        # workspace picked *because* it was empty this is something that turned
        # up mid-build — a Chrome profile dialog, in the case that prompted this
        # — and it is sharing the row, so the panes are narrower than asked for.
        # Reported separately from `slow`: it is not a pane that failed to open.
        others = 0
        if target is not None:
            ours = {a for a in placed if a}
            others = len([c for c in self._query_json("clients")
                          if str(c.get("workspace", {}).get("name")) == str(target)
                          and not c.get("floating")
                          and c.get("address") not in ours])
        where = f"workspace {target}" if target else "this workspace"
        if not opened and not slow:
            return Result(False, "nothing opened")
        summary = f"Composed {where} in a {layout} layout: {', '.join(opened)}." if opened \
            else f"Nothing came up on {where}."
        if slow:
            summary += (f" Still opening or did not appear: {', '.join(slow)} — "
                        "tell the user that, do not claim it is on screen.")
        if others:
            summary += (f" {others} other window(s) were already on {where} and are "
                        "sharing the row, so the panes are narrower than planned. "
                        "Mention that only if the user asks why.")
        return Result(True, summary)

    def _tool_type_text(self, text: str) -> Result:
        if not shutil.which("wtype"):
            return Result(False, "wtype is not installed")
        return self._shell(["wtype", "--", text])

    def _tool_run_shell(self, command: str) -> Result:
        if not self.config.allow_shell:
            return Result(False, "shell access is disabled in config (allow_shell = false)")
        return self._shell(["bash", "-lc", command], timeout=30)
