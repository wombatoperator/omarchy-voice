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
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote_plus, urlparse

from . import capabilities
from .config import Config
from .keys import normalise_key, normalise_mods

QUERY_KINDS = {
    "clients", "workspaces", "monitors", "activewindow", "activeworkspace",
    "devices", "layers", "binds", "animations", "version",
}

# Read-only tools still run under --dry-run so the planner can see the desktop.
READ_ONLY_TOOLS = {"hypr_query", "read_screen", "omarchy_help", "system_query",
                   "read_terminal", "list_terminals"}

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
# tesseract page segmentation. 6 means "one uniform block of text", which is
# what omarchy-capture-text uses because there a human has dragged a box around
# a paragraph. A whole screen is not that: it is a bar, a sidebar, tabs and a
# page in columns, and psm 6 read straight past them — it could not see the
# "Files changed" tab on a GitHub pull request at all, while psm 3 (automatic
# segmentation) finds it and reads more of everything else too.
OCR_PAGE_MODE = 3
# Words tesseract is less sure of than this are noise, not targets.
MIN_OCR_CONFIDENCE = 45.0
def _required_hits(word_count: int) -> int:
    """How many of a query's words must be present for a run to count.

    All of them for anything short. A long phrase gets one word of slack,
    because OCR reliably mangles about that many and losing the whole match to
    one misread letter is worse than the occasional near-miss.
    """
    return word_count if word_count <= 3 else word_count - 1
# --- terminals, through tmux ------------------------------------------------
#
# A terminal used to be a picture: grim the window, run tesseract, hope. That
# meant output was garbled, only readable while the window was visible, and
# impossible to see at all with the screen asleep or the session locked. Input
# was worse — wtype into whatever happened to have focus, with no way to tell
# whether it landed.
#
# tmux answers all of it at once, and Omarchy already ships it:
#
#   capture-pane -p        exact scrollback, from a pane on no workspace at all
#   pane_current_command   bash -> sleep -> bash: "is it finished", for free
#   send-keys              input with no focus, no keysym, no window target
#   list-panes -a          structured state across every session
#
# `Work` is the session `omarchy launch terminal tmux` attaches to
# (`tmux attach || tmux new -s Work`), so this shares the terminal the user
# already has rather than hiding one away.
TMUX_SESSION = "Work"
# Window classes that are a terminal. Used to answer "can the user actually see
# this?", because tmux's own `session_attached` cannot: a session is equally
# "attached" whether its client is on the workspace in front of you or on one
# you left an hour ago. Substring-matched, so ghostty's reverse-DNS class and
# wezterm's both land.
TERMINAL_CLASSES = ("foot", "alacritty", "kitty", "ghostty", "wezterm",
                    "term", "console")
# What `pane_current_command` says when nothing is running but the shell. A
# pane sitting at one of these is idle; anything else is a running command.
IDLE_COMMANDS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "nu", "elvish"}
# Scrollback handed back for a read. Generous — this is exact text rather than
# OCR, and the per-minute budget is no longer the binding constraint it was.
TERMINAL_LINES = 200
TERMINAL_OUTPUT_LIMIT = 6000
# How long `run_in_terminal` waits for a quick command before handing back
# "still running" and watching it instead. Long enough for a git status or a
# test run that was going to be fast; short enough not to hold the microphone.
TERMINAL_QUICK_WAIT = 6.0
TERMINAL_POLL = 0.2
# How long to allow for a pane to *start* looking busy after keys are sent.
# `pane_current_command` does not update the instant send-keys returns — the
# shell has not forked yet — so the first poll sees "bash" and, without this, a
# twenty-second command was reported finished in 0.4s with the echoed command
# line handed back as its output. A command that never looks busy inside this
# window really was instant, or was a shell builtin like `cd` that never forks.
#
# Measured rather than guessed: tmux reflected the forked command in 0.024s,
# six times out of six. This is 25x that, and it is the floor on how long an
# instant command appears to take, so it is not worth being generous with —
# 2.5s here meant `echo hello` sat silent for nearly three seconds.
TERMINAL_START_GRACE = 0.6
# How long to give a freshly launched terminal to attach to the session.
TERMINAL_ATTACH_TIMEOUT = 12.0
# A watch that never finishes would sit in the registry forever. Nothing takes
# longer than this that the user would still want announced out of the blue.
WATCH_MAX_SECONDS = 3 * 60 * 60

# --- searching the web ------------------------------------------------------
#
# The query goes in the URL. Nothing is typed.
#
# From a real session: asked to search for something, the assistant pressed
# CTRL+T, typed the query, and pressed Return five different ways before giving
# up — because the window it was driving was opened by `omarchy launch webapp`,
# which is `chrome --app=<url>`. An app window has no tab bar and no address
# bar, so CTRL+T and CTRL+L are no-ops and there was never anywhere for the
# text to go. Twelve tool rounds, no search. `omarchy launch browser <url>`
# does have an omnibox, but it opens a *tab in the window that already exists*,
# so nothing new appears in the window list and the assistant concluded — also
# wrongly — that the launch had failed.
#
# Both problems disappear if the query is part of the URL and the result opens
# as its own window: no typing, and a window that can be waited for, read,
# scrolled and clicked like any other.
SEARCH_SCOPES = {
    "web": "https://www.google.com/search?q={q}",
    "news": "https://www.google.com/search?q={q}&tbm=nws",
    "images": "https://www.google.com/search?q={q}&tbm=isch",
    "videos": "https://www.google.com/search?q={q}&tbm=vid",
    # No consent interstitial and a plainer results page, for when Google's
    # answer panels get in the way of the actual links.
    "duckduckgo": "https://duckduckgo.com/?q={q}",
}
# Scopes whose point is to be looked at. OCR of a wall of thumbnails is noise,
# and the user asked for them because they wanted to see them.
VISUAL_SCOPES = {"images", "videos"}
# A new search replaces the last one's window rather than adding to it, so a
# research session does not end up with nine identical result panes. Only the
# window *this tool* opened is closed — matching on class would also take down
# a duckduckgo pane the user had asked for by name.
# A launched window is mapped well before it has painted anything worth
# reading. Measured: a Google results page needs about two seconds after the
# surface appears, and a slow one is caught by the empty-read retry.
WEB_WINDOW_TIMEOUT = 15.0
WEB_RENDER_SETTLE = 2.0
# Chromium's crash-restore bubble covers the top of the first window it opens
# afterwards, which is exactly where search results are. It ate a whole turn.
RESTORE_BUBBLE = "restore pages"

# Chrome's "Profile error occurred" box. Not corruption — the profile's SQLite
# databases check out `integrity: ok`. It is lock contention: `omarchy launch
# webapp` starts a *new* google-chrome process each time, which is meant to
# hand off to the browser already running and exit. When several are launched
# in a row, or one is launched while a heavy page is still loading, the handoff
# loses the race and the new process opens the profile itself. Chrome's own log
# at that moment:
#
#   ERROR ukm_database_backend.cc:142] Failed to open UKM database: database is locked
#   ERROR top_sites_backend.cc:77]     Failed to initialize database.
#
# The dialog has no window class, so _await_new_window already refuses to
# mistake it for a pane, but it still takes focus and covers the screen. It is
# harmless and transient, so it is closed on sight rather than reported.
PROFILE_ERROR_TITLE = "profile error"

# --- reach, patience, and memory -------------------------------------------
#
# A screen shows what fits; an application answers when it is ready; and a
# session ends when listening is toggled off. Each of those is a wall the
# assistant used to stop at, and each has one tool below.

# How far one wheel notch scrolls, in pixels. Measured against a browser on this
# machine: ten notches moved a tracked word from y=547 to y=141, so 40.6 px a
# notch — which is the 40 px Chromium and most GTK apps use. It is the
# application's number, not the compositor's, so a terminal (three lines) or a
# PDF viewer will differ; `amount` is documented as approximate for that reason.
SCROLL_PIXELS_PER_CLICK = 40
# A screen with a couple of lines of overlap, because reading down a page wants
# continuity rather than a clean cut between screenfuls. Clamped so a tiny pane
# still moves and a 4K window does not fire a burst big enough for the
# application's own momentum scrolling to run away with it.
SCROLL_MIN_CLICKS, SCROLL_MAX_CLICKS = 4, 30
SCROLL_PAGE_OVERLAP = 0.85
SCROLL_MAX_PAGES = 10
# REL_WHEEL counts up when the wheel turns away from you, which scrolls the page
# up, so "down" is negative. Measured, not taken from the header: scrolling
# "down" moved tracked words 406 px *up* the screen, twice, on a live browser.
SCROLL_SIGN = {"down": -1, "up": 1, "right": 1, "left": -1}


def _scroll_clicks(height: int, pages: int) -> int:
    """Wheel notches for `pages` screenfuls of a window `height` px tall.

    Fixed at ten notches this used to move 406 px of a 1030 px window — 40% of
    a screen while telling the model it had moved one, which is how you read
    half an article and believe you read all of it.
    """
    per_page = round(height * SCROLL_PAGE_OVERLAP / SCROLL_PIXELS_PER_CLICK)
    per_page = max(SCROLL_MIN_CLICKS, min(per_page, SCROLL_MAX_CLICKS))
    return per_page * pages

# wait_for. The cap is short on purpose: the assistant is mute while it waits,
# and silence is the one thing a voice interface cannot afford much of.
WAIT_DEFAULT = 8.0
WAIT_MAX = 25.0
# A window either exists or it does not, so ask often. OCR costs a screen
# capture and a tesseract run, so ask rarely — the poll gap is on top of that.
WAIT_POLL_WINDOW = 0.35
WAIT_POLL_TEXT = 0.6

# Clipboards hold whole documents. This is handed to the model, so it is
# bounded like OCR output is.
CLIPBOARD_LIMIT = 4000

# The notebook. Small enough that reading it back is never the expensive part
# of a turn.
NOTES_LIMIT = 40
NOTE_LENGTH_LIMIT = 240

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
            "typing text for anything that is a command rather than content. Key names "
            "are X keysyms; common spoken names ('enter', 'esc', 'page down') are "
            "translated, and a name that is not a key is refused rather than pressed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mods": {"type": "string", "description": 'e.g. "CTRL", "CTRL SHIFT", or "" for none.'},
                "key": {"type": "string",
                        "description": 'e.g. "T", "Return" (the enter key), "Escape", "Page_Down".'},
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
            "Start an application by desktop entry id, for apps NOT in the manifest's "
            'app list — for ones that are, use omarchy_cli with the command shown '
            "there. 'terminal' and 'browser' are omarchy routes, not desktop ids, and "
            "fail here. For a SECOND window of an app already open, pass '<desktop- "
            "id>:<action>', e.g. 'google-chrome:new-window'. Not a shell command "
            'line, and for a web page use open_page instead — this hands off to the '
            'browser, which opens an invisible tab.'
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
        "name": "web_search",
        "description": (
            'Search the web and put the results on screen, in a window on the '
            'workspace the user is already looking at. This is the tool for a '
            'QUESTION — a price, a score, a date, news, who someone is, whether '
            'something is true — and for "show me" (scope images/videos). Do not open '
            "a site's home page and hope; search for the answer. The query goes in "
            'the URL, so there is nothing to type: never CTRL+T or CTRL+L, the web '
            'panes here are app windows with no address bar. Results come back as '
            'text AND stay on screen. Follow up with scroll, click_text or '
            'read_screen on the window it names.'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for, in plain words."},
                "scope": {
                    "type": "string",
                    "enum": ["web", "news", "images", "videos", "duckduckgo"],
                    "description": (
                        '"web" (default) — Google, whose answer panel often answers the '
                        'question outright. "news" for what happened recently, "images" / '
                        '"videos" when the user wants to SEE something, "duckduckgo" for a '
                        "plain list of links when Google's panels are in the way."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "open_page",
        "description": (
            "Open a URL as its own window and read it. Use this rather than launch_app "
            "with a url: that one hands off to the browser, which opens a tab inside a "
            "window that already exists — nothing new appears, and you cannot tell "
            "whether it worked. This gives you a window with an address you can read, "
            "scroll and click."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "An http(s) URL."},
                "read": {"type": "boolean",
                         "description": "Read the page once it loads. Default true."},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_terminal",
        "description": (
            "Read a terminal's output as EXACT text, through tmux. Use this instead of "
            "read_screen for anything in a terminal: it is not OCR, it works on panes "
            "that are on another workspace or not on screen at all, and it works with "
            "the display asleep. Empty target picks the pane with something running in "
            "it. Tells you whether the pane is idle or still busy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string",
                           "description": 'A tmux target like "Work:1.1", or empty for the '
                                          "most interesting pane. list_terminals shows them."},
                "lines": {"type": "integer", "description": "Scrollback lines. Default 200."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_terminals",
        "description": (
            "What tmux panes exist, what each is running, and whether anyone can see "
            "them. Call it when the user says \"the terminal\" and more than one is open, "
            "or to find something that is still going."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "run_in_terminal",
        "description": (
            "Run a shell command in a terminal the user can see, and read what it "
            "printed. Goes through tmux, so it needs no focus and no keypresses. Only "
            "runs in panes that are on screen — never a hidden one — and opens a "
            "terminal if none is up. A command still going after a few seconds is left "
            "running and watched; you are told, and should say so and move on rather "
            "than waiting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "One shell command."},
                "target": {"type": "string",
                           "description": "Optional tmux target. Empty picks a visible idle pane."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "watch_terminal",
        "description": (
            "Tell me when the command in a pane finishes. Returns immediately — the "
            "daemon watches in the background and interrupts with the result, even if "
            "the user has moved to another workspace. Use it for anything long: a "
            "build, a test run, a download. Do not poll read_terminal in a loop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Empty for the busy pane."},
                "note": {"type": "string",
                         "description": "What it is, in the user's words — \"the test run\"."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_screen",
        "description": (
            'OCR the text on screen — CONTENT, where hypr_query gives you window '
            'names. The default reads the whole visible screen, which is what you '
            'want after composing a workspace. Only visible windows can be read; '
            'switch workspace first. OCR is imperfect on small or stylised text, so '
            'quote what you got rather than what you expected.'
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
                "query": {
                    "type": "string",
                    "description": (
                        "Optional words to look for. With it you get the matching lines "
                        "rather than the whole screenful — use it for one fact (a price, "
                        "an error, whether a setting is on), leave it out to summarise."
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
            'Open several windows and lay them out together, to SET UP a workspace to '
            'work or watch in — "set me up to work on the budget", "I want the game '
            'and the chat". Two to four panes, and it goes to an empty workspace, so '
            'it takes the user away from what they were doing. It is NOT how you '
            'answer a question: for that, and for anything that only needs one '
            'window, use web_search or open_page. It waits for each window to appear '
            'before placing the next, so do NOT follow it with your own move/focus '
            'calls — that races the layout it just built. Takes a few seconds; say '
            'what you are opening first.'
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
        "name": "scroll",
        "description": (
            "Scroll a window, to bring what is below the fold into view for read_screen "
            "or click_text. Read the screen again afterwards; the text changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["down", "up", "left", "right"]},
                "amount": {"type": "integer",
                           "description": ("Roughly how many screenfuls. Default 1, "
                                           "max 10; how far a notch goes is the "
                                           "application's choice, so read to check.")},
                "target": {
                    "type": "string",
                    "description": (
                        '"activewindow" (default) or an "address:0x...". The pointer is '
                        "moved there first, so with panes side by side this picks which "
                        "one moves."
                    ),
                },
            },
            "required": ["direction"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_for",
        "description": (
            "Block until something has happened, then carry on. Use it between doing a "
            "thing and depending on it — a page loading, a window appearing. Returns as "
            "soon as the condition holds; a timeout comes back as a fact to report, not "
            "as an error."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    "enum": ["text", "window", "window_gone"],
                    "description": ('"text": those words on screen. "window"/"window_gone": '
                                    "a window whose class or title contains the value."),
                },
                "value": {"type": "string", "description": "Words, class or title."},
                "timeout": {"type": "number", "description": "Seconds. Default 8, max 25."},
            },
            "required": ["what", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "clipboard",
        "description": (
            "Read or write the system clipboard. Reading gives exact characters where "
            "read_screen only guesses at pixels: have the application copy something "
            "(CTRL+C, or CTRL+A then CTRL+C for a page) and read it here. Writing leaves "
            "text for the user to paste."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "write"]},
                "text": {"type": "string", "description": "What to copy (write only)."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "system_query",
        "description": (
            "Ask the machine about itself — disk, memory, battery, network, bluetooth, "
            "audio, uptime, temperature, time, OS version, what is using the CPU. "
            "Read-only and always allowed; it needs no shell."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["disk", "memory", "battery", "network", "bluetooth",
                             "audio", "uptime", "processes", "temperature", "time", "os"],
                },
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remember",
        "description": (
            "The notebook, and the only memory that outlives a session — when listening "
            "is toggled off the conversation is gone. Note a goal when you take one on "
            "and each step as it lands; list it back when the user picks the thread up "
            "again (\"where were we\")."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["note", "list", "forget"]},
                "text": {
                    "type": "string",
                    "description": ("note: one line that still makes sense tomorrow. "
                                    "forget: words identifying it, or \"all\"."),
                },
            },
            "required": ["action"],
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


# What "ask the machine about itself" is allowed to run. Fixed argv, no shell,
# nothing that writes: this is a reference table, not a command builder, so a
# misheard sentence cannot steer it anywhere. Anything not installed is skipped
# rather than reported as an error — `sensors` and `nmcli` are both optional.
SYSTEM_QUERIES: dict[str, object] = {
    "disk": [("", ["df", "-h", "--output=target,size,used,avail,pcent",
                   "-x", "tmpfs", "-x", "devtmpfs", "-x", "efivarfs"])],
    "memory": [("", ["free", "-h"])],
    "battery": None,  # filled in below; it reads /sys rather than shelling out
    "network": [("connections", ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE",
                                 "connection", "show", "--active"]),
                ("state", ["nmcli", "-t", "-f", "STATE,CONNECTIVITY", "general"])],
    # --timeout is not optional. With no controller present bluetoothctl waits
    # for one forever: on this machine `bluetoothctl show` was still running at
    # five seconds, which on a voice channel is five seconds of silence.
    "bluetooth": None,  # filled in below; it checks for an adapter first
    "audio": [("default sink", ["pactl", "get-default-sink"]),
              ("volume", ["pactl", "get-sink-volume", "@DEFAULT_SINK@"]),
              ("muted", ["pactl", "get-sink-mute", "@DEFAULT_SINK@"])],
    "uptime": [("", ["uptime", "-p"]), ("booted", ["uptime", "-s"])],
    # ps lists every process on the machine. The question is "what is making
    # the fan spin", and the answer is the top of that list, not all of it.
    "processes": [("busiest (%cpu %mem)", ["ps", "-eo", "pcpu,pmem,comm",
                                           "--sort=-pcpu", "--no-headers"], 10)],
    "temperature": [("", ["sensors"])],
    "time": [("", ["date", "+%A %-d %B %Y, %H:%M %Z"]),
             ("timezone", ["timedatectl", "show", "-p", "Timezone", "--value"])],
    "os": [("", ["uname", "-sr"]),
           ("distribution", ["sh", "-c", "true"])],  # replaced below
}


def _os_release() -> str:
    for line in Path("/etc/os-release").read_text().splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.partition("=")[2].strip().strip('"')
    return ""


def _os_report(executor) -> "Result":
    parts = []
    try:
        if pretty := _os_release():
            parts.append(pretty)
    except OSError:
        pass
    for argv in (["uname", "-sr"], ["hyprctl", "version", "-j"]):
        got = executor._shell(argv, timeout=8, limit=800)
        if not got.ok:
            continue
        if argv[0] == "hyprctl":
            try:
                parts.append("Hyprland " + json.loads(got.output).get("tag", "").lstrip("v"))
            except (json.JSONDecodeError, AttributeError):
                pass
        else:
            parts.append(got.output.strip())
    return Result(True, "\n".join(p for p in parts if p) or "could not read the OS version")


def _bluetooth_report(executor) -> "Result":
    if not Path("/sys/class/bluetooth").exists():
        return Result(True, "this machine has no bluetooth adapter")
    parts = []
    for label, argv in (("adapter", ["bluetoothctl", "--timeout", "3", "show"]),
                        ("connected", ["bluetoothctl", "--timeout", "3",
                                       "devices", "Connected"])):
        got = executor._shell(argv, timeout=8, limit=1200)
        if got.ok and got.output.strip():
            parts.append(f"{label}:\n{got.output.strip()}")
    return Result(True, "\n\n".join(parts) or "the bluetooth adapter did not answer")


SYSTEM_QUERIES["battery"] = lambda executor: executor._battery()
SYSTEM_QUERIES["bluetooth"] = _bluetooth_report
SYSTEM_QUERIES["os"] = _os_report


def tools_for(config: Config) -> list[dict]:
    """The schemas this configuration can actually run.

    `run_shell` is off by default, and a tool that is always going to be
    refused is worse than a tool that is not offered: it costs its schema on
    every turn, and when the model reaches for it — which it does, it is the
    one tool that can express anything — the refusal costs a whole round trip
    before it tries the tool it should have used.
    """
    if config.allow_shell:
        return list(TOOL_SCHEMAS)
    return [schema for schema in TOOL_SCHEMAS if schema["name"] != "run_shell"]


_BARE_ADDRESS_RE = re.compile(r'(window\s*=\s*")(0x[0-9a-fA-F]+)(")')
# `key = "..."` inside a raw hl.dsp.send_shortcut, so a chord written by hand
# through hypr_dispatch gets the same keysym check as the send_shortcut tool.
_LUA_KEY_RE = re.compile(r'(\bkey\s*=\s*")([^"]*)(")')
_LUA_MODS_RE = re.compile(r'(\bmods\s*=\s*")([^"]*)(")')


def _wait_description(what: str, value: str) -> str:
    return {
        "text": f"{value!r} appeared on screen",
        "window": f"a window matching {value!r} opened",
        "window_gone": f"the window matching {value!r} closed",
    }[what]


def _wait_timeout(what: str, value: str) -> str:
    return {
        "text": f"{value!r} still is not on screen",
        "window": f"no window matching {value!r} has opened",
        "window_gone": f"the window matching {value!r} is still open",
    }[what]


def _matching_lines(text: str, query: str, context: int = 1) -> str:
    """The lines of `text` that answer `query`, with a line either side.

    A screenful of OCR is a couple of thousand tokens charged against a
    per-minute budget. When the question is "what is the price" the answer is
    one line, and sending the other ninety costs the user a turn.
    """
    wanted = [w for w in re.split(r"\W+", query.lower()) if w]
    lines = text.splitlines()
    if not wanted or not lines:
        return ""
    keep: set[int] = set()
    for index, line in enumerate(lines):
        lowered = line.lower()
        hits = sum(1 for w in wanted if w in lowered)
        if hits >= _required_hits(len(wanted)):
            keep.update(range(max(0, index - context),
                              min(len(lines), index + context + 1)))
    if not keep:
        return ""
    out, previous = [], None
    for index in sorted(keep):
        if previous is not None and index > previous + 1:
            out.append("…")
        out.append(lines[index])
        previous = index
    return "\n".join(out).strip()


def _chord(mods: str, key: str) -> str:
    """"CTRL SHIFT" + "Return" -> "CTRL+SHIFT+Return"; a bare key stays bare."""
    parts = [p for p in (mods or "").replace("+", " ").split() if p]
    return "+".join([*parts, key])


def _normalise_shortcut_lua(lua: str) -> tuple[str, str | None]:
    """Fix the key and mods in a hand-written send_shortcut, or say why not.

    hypr_dispatch is the back door to every dispatcher, send_shortcut included,
    so the keysym check has to live on this path too — otherwise "Enter" is
    still a silent no-op as long as the model spells the Lua out itself.
    """
    if "send_shortcut" not in lua:
        return lua, None
    error: str | None = None

    def replace(pattern, normalise):
        nonlocal error, lua
        match = pattern.search(lua)
        if match is None:
            return
        value, problem = normalise(match.group(2))
        if problem:
            error = error or problem
            return
        lua = lua[:match.start(2)] + value + lua[match.end(2):]

    replace(_LUA_MODS_RE, normalise_mods)
    replace(_LUA_KEY_RE, normalise_key)
    return lua, error



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
    if error := _misused_launch_browser(argv):
        return argv, error
    return argv, None


def _misused_launch_browser(argv: list[str]) -> str | None:
    """Refuse `omarchy launch browser <url>`, naming the tool that works.

    The manifest lists `omarchy launch browser [url]`, read off the live
    system, so the model reaches for it — and it is the wrong shape for an
    assistant. It hands the URL to the running browser, which opens a TAB in a
    window that already exists: nothing new appears in hyprctl, so there is no
    window to wait for, read, scroll, move or close. In the session log that is
    exactly what happened — "I don't see the Google results; the active window
    is still the OpenAI usage page. The search page didn't appear."

    Persona wording did not beat the manifest here; a refusal that names the
    right call at the moment of the mistake does, which is the same fix already
    used for hl.dsp.workspace.change_id.
    """
    if argv[:2] != ["launch", "browser"]:
        return None
    urls = [a for a in argv[2:] if urlparse(a).scheme.lower() in ("http", "https")]
    if not urls:
        return None  # "open my browser" is a perfectly good request
    url = urls[0]
    query = parse_qs(urlparse(url).query).get("q", [""])[0]
    if query:
        return (f"that is a search URL. Use web_search with query={query!r} — it opens "
                "the results as their own window, which this does not: `omarchy launch "
                "browser <url>` opens a tab inside an existing window, so no new window "
                "appears and you cannot read or verify it.")
    return (f"use open_page with url={url!r} instead. `omarchy launch browser <url>` "
            "opens a tab inside a window that already exists, so nothing new appears in "
            "the window list and you cannot wait for it, read it, or tell if it worked.")


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
        # The window the last web_search opened, so the next one can replace it.
        self._last_search_window: str | None = None
        # tmux panes being watched for a command to finish, by target.
        self._watches: dict[str, dict] = {}
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
            # Report the keysym that will actually be pressed, not the word the
            # model said: the log is the only record of what hit the window.
            mods = normalise_mods(args.get("mods", ""))[0]
            keysym = normalise_key(args.get("key", ""))[0]
            chord = _chord(mods if mods is not None else args.get("mods", ""),
                           keysym or args.get("key", ""))
            return f'press {chord} in {args.get("window", "")}'
        if name == "type_text":
            return f'type {args.get("text", "")!r}'
        if name == "hypr_query":
            return f'query hyprctl {args.get("kind", "")}'
        if name == "omarchy_help":
            return f'look up omarchy command {args.get("query", "")!r}'
        if name == "click_text":
            kind = "double-click" if args.get("double") else "click"
            return f'{kind} {args.get("button", "left")} on {args.get("text", "")!r}'
        if name == "read_screen":
            where = args.get("target", "screen")
            if query := (args.get("query") or "").strip():
                return f'read screen ({where}) looking for {query!r}'
            return f'read screen ({where})'
        if name == "scroll":
            return (f'scroll {args.get("target", "activewindow")} '
                    f'{args.get("direction", "")} x{args.get("amount", 1)}')
        if name == "wait_for":
            return f'wait for {args.get("what", "")} {args.get("value", "")!r}'
        if name == "clipboard":
            if args.get("action") == "write":
                return f'copy to clipboard: {str(args.get("text", ""))[:60]!r}'
            return "read the clipboard"
        if name == "web_search":
            scope = args.get("scope", "web")
            return f'search the {scope} for {args.get("query", "")!r}'
        if name == "open_page":
            return f'open {args.get("url", "")}'
        if name == "read_terminal":
            return f'read terminal {args.get("target", "") or "(busiest pane)"}'
        if name == "list_terminals":
            return "list the terminal panes"
        if name == "run_in_terminal":
            return f'run in terminal: {args.get("command", "")}'
        if name == "watch_terminal":
            return f'watch terminal {args.get("target", "") or "(busy pane)"}'
        if name == "system_query":
            return f'look up system {args.get("topic", "")}'
        if name == "remember":
            action = args.get("action", "")
            if action == "list":
                return "read the notebook"
            return f'{action} note {str(args.get("text", ""))[:60]!r}'
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
        if error := _normalise_shortcut_lua(expr)[1]:
            return error
        return None

    def _tool_hypr_dispatch(self, lua: str) -> Result:
        error = self._validate_hypr_dispatch(lua)
        if error:
            return Result(False, error)
        expr, error = _normalise_shortcut_lua(_normalise_window_addresses(lua.strip()))
        if error:
            return Result(False, error)
        return self._dispatch_lua(expr)

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

    def _validate_send_shortcut(self, mods: str, key: str,
                                window: str = "activewindow") -> str | None:
        return normalise_mods(mods)[1] or normalise_key(key)[1]

    def _tool_send_shortcut(self, mods: str, key: str, window: str = "activewindow") -> Result:
        """Press a chord, having first checked the keys exist.

        Hyprland answers `ok` for a keysym it cannot resolve and presses
        nothing, so `key = "Enter"` — the word a person actually says — was a
        silent no-op that the model then reported as done. Both halves are
        resolved here, and an unresolvable one is an error the model can read.
        """
        clean_mods, error = normalise_mods(mods)
        if error:
            return Result(False, error)
        keysym, error = normalise_key(key)
        if error:
            return Result(False, error)
        lua = (
            f'hl.dsp.send_shortcut({{ mods = {json.dumps(clean_mods)}, '
            f'key = {json.dumps(keysym)}, window = {json.dumps(window)} }})'
        )
        result = self._dispatch_lua(lua)
        # Say so when the name was translated, but not for a mere case fold —
        # "read 'T' as t" is noise the model would repeat out loud.
        if result.ok and keysym.lower() != (key or "").strip().lower():
            return Result(True, f"pressed {_chord(clean_mods, keysym)} "
                                f"(read {key!r} as {keysym})")
        return result

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
        if blocked := self._screen_unavailable():
            return Result(False, blocked)
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
                ["tesseract", "stdin", "stdout", "--oem", "1", "--psm", str(OCR_PAGE_MODE),
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

    def _screen_unavailable(self) -> str | None:
        """Why the screen cannot be read or pointed at, or None if it can.

        Two ways to get pixels that are not the desktop:

        * A monitor in DPMS off produces no frames at all. grim does not fail on
          one, it blocks until the timeout — a read at half past midnight hung
          for fifteen seconds and then blamed OCR.
        * A locked session paints the lock screen over everything. That one is
          worse, because the capture succeeds: grim returns the wallpaper and a
          password box, so "read me the news" came back with whatever OCR made
          of a blurred photograph, reported as the news. Nothing in hyprctl
          shows it — an ext-session-lock surface is not a layer, no locker
          process is running under a recognisable name, and logind's LockedHint
          stays "no" — but Omarchy's own shell, which draws the lock, will say.

        Neither check is allowed to be the reason nothing works: if the query
        does not answer, the capture is attempted anyway.
        """
        if self._session_is_locked():
            return ("the session is locked, so the only thing on screen is the "
                    "lock screen. Ask the user to unlock it; do not try to read "
                    "or click through it, and do not report the lock screen as "
                    "the contents of their desktop.")
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

    def _session_is_locked(self) -> bool:
        """Whether the lock screen is covering the desktop.

        Only Omarchy's shell knows. It draws the lock itself through
        ext-session-lock, which is not a layer surface, runs under no process
        named for locking, and leaves logind's LockedHint at "no" — so hyprctl,
        ps and loginctl all report an ordinary unlocked desktop while a
        password box is the only thing being painted.

        Not `-q`: that is omarchy-shell's best-effort mode and it suppresses the
        answer along with the errors, which reads as "not locked".
        """
        if not shutil.which("omarchy-shell"):
            return False
        answer = self._shell(["omarchy-shell", "lock", "isLocked"], timeout=4)
        return answer.ok and answer.output.strip().lower() == "true"

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
        if blocked := self._screen_unavailable():
            return [], blocked
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
                ["tesseract", "stdin", "stdout", "--oem", "1", "--psm", str(OCR_PAGE_MODE),
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
                # Every word has to be there, give or take one for a long phrase
                # that OCR has mangled. Accepting half of them meant a two-word
                # target passed on a single word: asked for "Files changed" on a
                # pull request it matched the words "changed files" in the body
                # prose and clicked that, confidently, in the wrong place.
                if hits < _required_hits(len(wanted)):
                    continue
                # Among runs that qualify, prefer the tightest one.
                score = hits / len(wanted) - 0.05 * abs(len(run) - span_len)
                if score > best_score:
                    best_score, best_span = score, run
        if best_span is None:
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

    def _tool_read_screen(self, target: str = "screen", query: str = "") -> Result:
        result = self._read_screen_text(target)
        if not result.ok or not (query or "").strip():
            return result
        found = _matching_lines(result.output, query)
        if not found:
            return Result(True, f"nothing on screen matches {query!r}. It may be below "
                                "the fold — scroll and look again — or simply not there.")
        return Result(True, found)

    def _read_screen_text(self, target: str = "screen") -> Result:
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
        if len(panes) == 1:
            # A layout tool asked to lay out one window is a tell: the request
            # was a question, not a workspace. The model kept reaching here for
            # "who won the race" and "show me pictures of a duck" because this
            # is the habitual route to anything on the web, and persona wording
            # did not move it. Refusing at the point of the mistake does.
            pane = panes[0] if isinstance(panes[0], dict) else {}
            target = str(pane.get("target", ""))
            return ("compose_windows lays several windows out together; for one window "
                    "it is the wrong tool. If this is a question — a price, a result, a "
                    "date, or \"show me\" — call web_search, which puts the answer on the "
                    "workspace the user is already looking at. If you want this exact "
                    f"page, call open_page{f' with url={target!r}' if target else ''}.")
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
            # Chrome raises its profile-error box when a second browser process
            # races the first for the profile's databases, which is exactly what
            # launching panes back to back does. Clear it between panes so it
            # cannot steal the focus the next preselect depends on.
            if kind == "web":
                self._dismiss_browser_error_dialogs()
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

    # -- terminals, through tmux --------------------------------------------
    def _tmux(self, *args: str, timeout: float = 8.0) -> Result:
        if not shutil.which("tmux"):
            return Result(False, "tmux is not installed (pacman -S tmux)")
        return self._shell(["tmux", *args], timeout=timeout, limit=1 << 20)

    def _tmux_panes(self) -> list[dict]:
        """Every pane in every session, whether or not anyone is looking at it."""
        fmt = ("#{session_name}\t#{session_attached}\t#{window_index}\t"
               "#{pane_index}\t#{pane_current_command}\t#{pane_title}")
        listed = self._tmux("list-panes", "-a", "-F", fmt)
        if not listed.ok:
            return []
        panes = []
        for line in listed.output.splitlines():
            cell = line.split("\t")
            if len(cell) < 6:
                continue
            panes.append({
                "target": f"{cell[0]}:{cell[2]}.{cell[3]}",
                "session": cell[0],
                "attached": cell[1] not in ("", "0"),
                "command": cell[4],
                "title": cell[5],
                "idle": cell[4] in IDLE_COMMANDS,
            })
        return panes

    def _resolve_pane(self, target: str) -> tuple[dict | None, str]:
        """Which pane `target` means, or (None, why not).

        An empty target is the common case — the user said "the terminal", not
        "Work:1.2". Prefer a pane someone is actually attached to, and prefer a
        busy one, because the pane worth reading is nearly always the one with
        something running in it.
        """
        panes = self._tmux_panes()
        if not panes:
            return None, ("no tmux session is running. Start one with omarchy_cli "
                          '"launch terminal tmux", which opens a terminal attached to '
                          "the Work session; commands run there can be read exactly, "
                          "from any workspace.")
        target = (target or "").strip()
        if target:
            exact = [p for p in panes if p["target"] == target]
            if exact:
                return exact[0], ""
            loose = [p for p in panes
                     if target.lower() in (p["target"] + " " + p["title"]).lower()]
            if len(loose) == 1:
                return loose[0], ""
            if not loose:
                return None, (f"no tmux pane matches {target!r}. Open ones: "
                              + ", ".join(p["target"] for p in panes))
            return None, (f"{target!r} matches several panes: "
                          + ", ".join(p["target"] for p in loose))
        busy = [p for p in panes if p["attached"] and not p["idle"]]
        attached = [p for p in panes if p["attached"]]
        return (busy or attached or panes)[0], ""

    def _capture_pane(self, target: str, lines: int = TERMINAL_LINES) -> Result:
        got = self._tmux("capture-pane", "-p", "-J", "-S", f"-{int(lines)}", "-t", target)
        if not got.ok:
            return got
        # capture-pane pads to the height of the pane; the blank tail is not
        # output, it is empty screen.
        text = "\n".join(got.output.splitlines()).rstrip()
        if not text.strip():
            return Result(True, "(that pane is empty)")
        if len(text) > TERMINAL_OUTPUT_LIMIT:
            text = "… [earlier output not shown]\n" + text[-TERMINAL_OUTPUT_LIMIT:]
        return Result(True, text)

    def _validate_read_terminal(self, target: str = "", lines: int = TERMINAL_LINES) -> str | None:
        try:
            int(lines)
        except (TypeError, ValueError):
            return "lines must be a whole number"
        return None

    def _tool_read_terminal(self, target: str = "", lines: int = TERMINAL_LINES) -> Result:
        pane, why = self._resolve_pane(target)
        if pane is None:
            return Result(False, why)
        captured = self._capture_pane(pane["target"], min(int(lines), 2000))
        if not captured.ok:
            return captured
        running = ("idle at the shell" if pane["idle"]
                   else f"still running {pane['command']!r}")
        return Result(True, f"{pane['target']} ({running}):\n{captured.output}")

    def _tool_list_terminals(self) -> Result:
        panes = self._tmux_panes()
        if not panes:
            return Result(False, "no tmux session is running")
        rows = [f"  {p['target']:<16} {'running ' + p['command'] if not p['idle'] else 'idle':<22}"
                f"{'' if p['attached'] else '(not on screen) '}{p['title'][:40]}"
                for p in panes]
        return Result(True, "tmux panes:\n" + "\n".join(rows))

    def _terminal_on_screen(self) -> bool:
        """Whether a terminal window is being drawn on a workspace in view.

        tmux's `session_attached` is not this. It says a client exists, not that
        anyone can see it — the client may be in a window on a workspace nobody
        has looked at since this morning. Running a command somewhere invisible
        is exactly what this tool must not do, so the compositor gets the last
        word on what "visible" means.
        """
        visible = self._visible_workspaces()
        for client in self._query_json("clients"):
            klass = (client.get("class") or "").lower()
            if not any(name in klass for name in TERMINAL_CLASSES):
                continue
            if str((client.get("workspace") or {}).get("name")) in visible:
                return True
        return False

    def _ensure_visible_session(self) -> tuple[dict | None, str]:
        """A pane the user can watch, opening a terminal if there is not one.

        Two conditions, and both are needed: tmux has a client (so keys sent to
        the pane are being rendered somewhere at all), and a terminal window is
        on a workspace currently being drawn (so that somewhere is in front of
        the user).
        """
        panes = [p for p in self._tmux_panes() if p["attached"]]
        if panes and self._terminal_on_screen():
            idle = [p for p in panes if p["idle"]]
            return (idle or panes)[0], ""
        started = self._shell(["omarchy", "launch", "terminal", "tmux"],
                              timeout=20, grace=LAUNCH_GRACE)
        if not started.ok:
            return None, f"could not open a terminal: {started.output}"
        deadline = time.monotonic() + TERMINAL_ATTACH_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.4)
            fresh = [p for p in self._tmux_panes() if p["attached"]]
            if fresh and self._terminal_on_screen():
                time.sleep(0.5)  # let the shell finish drawing its prompt
                return fresh[0], ""
        return None, "opened a terminal but tmux never attached to it"

    def _validate_run_in_terminal(self, command: str, target: str = "") -> str | None:
        if not (command or "").strip():
            return "command is required"
        if "\n" in command:
            # Naming the way that works matters more than the refusal. Given
            # only "newlines are not sent", the model retried the same heredoc
            # five times and then took thirteen commands and forty-five seconds
            # to write a two-line file.
            return ("newlines are not sent, so heredocs (cat > f <<EOF) cannot work "
                    "here. To write a file, use printf with escapes in ONE line: "
                    "printf '#!/bin/sh\\necho hi\\n' > f  — and note \\n inside single "
                    "quotes is the two characters backslash-n, which printf turns into "
                    "a newline. Append more with >>.")
        return None

    def _tool_run_in_terminal(self, command: str, target: str = "") -> Result:
        if error := self._validate_run_in_terminal(command, target):
            return Result(False, error)
        command = command.strip()
        if target:
            pane, why = self._resolve_pane(target)
            if pane is not None and not (pane["attached"] and self._terminal_on_screen()):
                return Result(False,
                              f"{pane['target']} is not on screen. Commands only run in "
                              "panes the user can see; read that one instead, or leave "
                              "target empty to use a visible terminal.")
        else:
            pane, why = self._ensure_visible_session()
        if pane is None:
            return Result(False, why)
        if not pane["idle"]:
            return Result(False,
                          f"{pane['target']} is busy running {pane['command']!r}; typing "
                          "into it would go to that program. Wait for it with "
                          "watch_terminal, or pick another pane.")

        sent = self._tmux("send-keys", "-t", pane["target"], "--", command, "Enter")
        if not sent.ok:
            return sent
        started_at = time.monotonic()
        deadline = started_at + TERMINAL_QUICK_WAIT
        seen_busy = False
        while time.monotonic() < deadline:
            time.sleep(TERMINAL_POLL)
            current = next((p for p in self._tmux_panes()
                            if p["target"] == pane["target"]), None)
            if current is None:
                return Result(False, "that pane went away while the command was running")
            if not current["idle"]:
                seen_busy = True
                continue
            # Idle. Either it finished, or it has not started yet — and telling
            # those apart is the whole reason for the grace period.
            if seen_busy or time.monotonic() - started_at > TERMINAL_START_GRACE:
                out = self._capture_pane(pane["target"])
                return Result(True, f"ran {command!r} in {pane['target']}:\n{out.output}")

        self.watch(pane["target"], command, seen_busy=seen_busy)
        return Result(True,
                      f"{command!r} is still running in {pane['target']} after "
                      f"{TERMINAL_QUICK_WAIT:.0f}s, so I am watching it and will say when "
                      "it finishes. Tell the user that, and carry on with something else "
                      "rather than waiting.")

    # -- watching a pane ----------------------------------------------------
    def watch(self, target: str, label: str = "", seen_busy: bool = False) -> None:
        self._watches[target] = {"label": label or "the command",
                                 "started": time.monotonic(),
                                 "seen_busy": seen_busy}

    def _validate_watch_terminal(self, target: str = "", note: str = "") -> str | None:
        return None

    def _tool_watch_terminal(self, target: str = "", note: str = "") -> Result:
        pane, why = self._resolve_pane(target)
        if pane is None:
            return Result(False, why)
        if pane["idle"]:
            out = self._capture_pane(pane["target"], 40)
            return Result(False,
                          f"{pane['target']} is already idle — nothing is running there to "
                          f"wait for. What it last showed:\n{out.output}")
        # Busy was just confirmed above, so idle from here means finished —
        # this watch does not need the start-up grace.
        self.watch(pane["target"], note or pane["command"], seen_busy=True)
        return Result(True,
                      f"watching {pane['target']} ({pane['command']}). I will say when it "
                      "finishes, even if the user has moved to another workspace. Do not "
                      "wait here — say that it is being watched and carry on.")

    def poll_watches(self) -> list[dict]:
        """Watches that are over, and why. Called from the daemon's background loop.

        Three ways to be over, and the middle one is the reason this is not a
        one-liner. A pane that is idle has either finished or *not started yet*:
        `pane_current_command` still says "bash" for a moment after the keys are
        sent, because the shell has not forked. Reporting that as finished
        handed back a twenty-second command as done in under half a second.
        So a watch has to see the pane busy before idle means anything — unless
        the grace period passes without it ever looking busy, which is what an
        instant command or a shell builtin like `cd` looks like.

        Returns and forgets, so a finished job is announced exactly once.
        """
        if not self._watches:
            return []
        panes = {p["target"]: p for p in self._tmux_panes()}
        finished, now = [], time.monotonic()
        for target, watch in list(self._watches.items()):
            pane = panes.get(target)
            age = now - watch["started"]
            if pane is None:
                reason = "vanished"
            elif not pane["idle"]:
                watch["seen_busy"] = True
                if age <= WATCH_MAX_SECONDS:
                    continue
                reason = "timed_out"
            elif not watch["seen_busy"] and age <= TERMINAL_START_GRACE:
                continue  # keys are sent but the shell has not forked yet
            else:
                reason = "finished"
            del self._watches[target]
            finished.append({
                "target": target,
                "label": watch["label"],
                "seconds": age,
                "vanished": reason == "vanished",
                "timed_out": reason == "timed_out",
                "tail": "" if reason == "vanished" else self._capture_pane(target, 30).output,
            })
        return finished

    # -- the web ------------------------------------------------------------
    def _open_web_window(self, url: str, hint: str,
                         timeout: float = WEB_WINDOW_TIMEOUT) -> tuple[dict | None, str]:
        """Open `url` as its own window and hand back the client, or say why not.

        `omarchy launch webapp` is deliberate: it is `chrome --app=<url>`, which
        makes a real window rather than a tab in one that already exists. A tab
        is invisible to hyprctl, so there is no way to wait for it, read it,
        move it or close it — the assistant that opened one was left guessing
        whether anything had happened, and guessed wrong.
        """
        before = {c.get("address") for c in self._query_json("clients")}
        launched = self._shell(["omarchy", "launch", "webapp", url],
                               timeout=20, grace=LAUNCH_GRACE)
        if not launched.ok:
            return None, f"could not open the browser: {launched.output}"
        address = self._await_new_window(before, timeout, hint)
        if address is None:
            return None, ("the browser did not open a window within "
                          f"{timeout:.0f}s. Say so rather than assuming it worked.")
        self._dismiss_browser_error_dialogs()
        window = next((c for c in self._query_json("clients")
                       if c.get("address") == address), None)
        if window is None:
            return None, "the window opened and then went away again"
        return window, ""

    def _read_web_window(self, window: dict, settle: float = WEB_RENDER_SETTLE) -> Result:
        """OCR a freshly opened page, once it has had a moment to paint.

        A window is mapped well before it has drawn anything. Reading straight
        away returns a blank page, which is indistinguishable from a page with
        nothing on it — so an empty or very short read is retried once.
        """
        time.sleep(settle)
        try:
            geometry = (f'{window["at"][0]},{window["at"][1]} '
                        f'{window["size"][0]}x{window["size"][1]}')
        except (KeyError, IndexError, TypeError):
            return Result(False, "could not read that window's geometry")
        result = self._ocr_region(geometry)
        if not result.ok or len(result.output) < 200:
            time.sleep(settle)
            retry = self._ocr_region(geometry)
            if retry.ok and len(retry.output) > len(result.output if result.ok else ""):
                result = retry
        if result.ok and RESTORE_BUBBLE in result.output.lower():
            return Result(True, result.output + (
                "\n\n[Chromium is showing its \"Restore pages\" crash prompt over this "
                "page. Dismiss it with click_text on \"Restore\" or \"No thanks\", or "
                "send_shortcut Escape, then read again — what is above is partly that "
                "prompt, not the page.]"))
        return result

    def _dismiss_browser_error_dialogs(self) -> int:
        """Close Chrome's "Profile error occurred" boxes. See PROFILE_ERROR_TITLE.

        Matched on an empty class *and* the title, so this can only ever take
        down an unclassed dialog — never a real window, whatever it is called.
        """
        closed = 0
        for client in self._query_json("clients"):
            if client.get("class"):
                continue
            if PROFILE_ERROR_TITLE in (client.get("title") or "").lower():
                self._dispatch_lua(
                    f'hl.dsp.window.close({{ window = "address:{client["address"]}" }})')
                closed += 1
        return closed

    def _close_last_search(self) -> bool:
        """Take down the window the previous search opened, if it is still up.

        Tracked by address rather than matched by class: the class of a Google
        results pane is indistinguishable from one the user asked for by name,
        and closing a window somebody wanted is a worse failure than leaving a
        stale one behind.
        """
        address = self._last_search_window
        self._last_search_window = None
        if not address:
            return False
        if not any(c.get("address") == address for c in self._query_json("clients")):
            return False
        self._dispatch_lua(f'hl.dsp.window.close({{ window = "address:{address}" }})')
        time.sleep(0.4)
        return True

    def _validate_web_search(self, query: str, scope: str = "web") -> str | None:
        if not (query or "").strip():
            return "query is required — say what to search for"
        if scope not in SEARCH_SCOPES:
            return f"scope must be one of {', '.join(SEARCH_SCOPES)}"
        return None

    def _tool_web_search(self, query: str, scope: str = "web") -> Result:
        if error := self._validate_web_search(query, scope):
            return Result(False, error)
        query = " ".join(query.split())
        url = SEARCH_SCOPES[scope].format(q=quote_plus(query))
        self._close_last_search()
        window, why = self._open_web_window(url, urlparse(url).hostname or "")
        if window is None:
            return Result(False, why)
        address = window["address"]
        self._last_search_window = address

        if scope in VISUAL_SCOPES:
            return Result(True, f"{scope} for {query!r} are on screen now "
                                f"(window address:{address}). They are pictures, so tell "
                                "the user to look rather than describing them from OCR.")
        read = self._read_web_window(window)
        if not read.ok:
            return Result(True, f"the results for {query!r} are on screen "
                                f"(window address:{address}) but could not be read: "
                                f"{read.output}")
        return Result(True,
                      f"results for {query!r} (window address:{address}, and on screen "
                      f"for the user to see):\n\n{read.output}\n\n"
                      "This is OCR of a results page, so quote it rather than embroidering "
                      "it, and scroll or click_text on the window to go further.")

    def _validate_open_page(self, url: str, read: bool = True) -> str | None:
        if urlparse(url or "").scheme.lower() not in ("http", "https"):
            return "url must be an http or https address"
        return None

    def _tool_open_page(self, url: str, read: bool = True) -> Result:
        if error := self._validate_open_page(url, read):
            return Result(False, error)
        host = urlparse(url).hostname or ""
        window, why = self._open_web_window(url, host[4:] if host.startswith("www.") else host)
        if window is None:
            return Result(False, why)
        address = window["address"]
        if not read:
            return Result(True, f"opened {url} (window address:{address})")
        got = self._read_web_window(window)
        if not got.ok:
            return Result(True, f"opened {url} (window address:{address}) but could not "
                                f"read it: {got.output}")
        return Result(True, f"opened {url} (window address:{address}):\n\n{got.output}")

    # -- reach: scrolling ---------------------------------------------------
    def _window_geometry(self, target: str) -> tuple[dict | None, str]:
        """The client `target` names, or (None, why not)."""
        clients = self._query_json("clients")
        if not clients:
            return None, "nothing is open"
        if target in ("", "activewindow", "active", "focused"):
            window = next((c for c in clients if c.get("focusHistoryID") == 0), None)
            if window is None:
                return None, "nothing is focused"
            return window, ""
        address = target[8:] if target.startswith("address:") else target
        window = next((c for c in clients if c.get("address") == address), None)
        if window is None:
            return None, (f"no window with address {address!r} — call "
                          "hypr_query(clients) for current addresses")
        return window, ""

    def _validate_scroll(self, direction: str, amount: int = 1,
                         target: str = "activewindow") -> str | None:
        if direction not in SCROLL_SIGN:
            return f"direction must be one of {', '.join(SCROLL_SIGN)}"
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return "amount must be a whole number of screens"
        if amount < 1:
            return "amount must be at least 1"
        return None

    def _tool_scroll(self, direction: str, amount: int = 1,
                     target: str = "activewindow") -> Result:
        """Turn the wheel over a window.

        Pointing at the window first is not decoration: a wheel event goes to
        whatever is under the cursor, so without the move, "scroll the article"
        scrolled whichever pane the mouse happened to be resting on — and in a
        composed workspace that is usually the wrong one.
        """
        if error := self._validate_scroll(direction, amount, target):
            return Result(False, error)
        amount = min(int(amount), SCROLL_MAX_PAGES)
        window, why = self._window_geometry(target)
        if window is None:
            return Result(False, why)

        if blocked := self._screen_unavailable():
            return Result(False, blocked)
        workspace = str((window.get("workspace") or {}).get("name"))
        if workspace not in self._visible_workspaces():
            return Result(False, f"that window is on workspace {workspace}, which is not "
                                 "on screen, so there is nothing to scroll. Switch to it "
                                 "first with hl.dsp.focus.")
        try:
            x = window["at"][0] + window["size"][0] // 2
            y = window["at"][1] + window["size"][1] // 2
        except (KeyError, IndexError, TypeError):
            return Result(False, "could not read that window's geometry")

        title = (window.get("title") or window.get("class") or "the window")[:40]
        if not shutil.which("ydotool"):
            # Keys reach further than nothing. Page_Down only works where the
            # page itself has focus, so say which route was taken — if it did
            # not move, that is the reason.
            key = {"down": "Page_Down", "up": "Page_Up",
                   "right": "Right", "left": "Left"}[direction]
            pressed = self._tool_send_shortcut("", key,
                                               f'address:{window["address"]}')
            if not pressed.ok:
                return pressed
            return Result(True, f"pressed {key} {amount}x on {title} (ydotool is not "
                                "installed, so this used keys rather than the wheel; "
                                "it only scrolls if the page itself has focus)")

        moved = self._dispatch_lua(f'hl.dsp.cursor.move({{ x = {x}, y = {y} }})')
        if not moved.ok:
            return Result(False, f"could not point at the window: {moved.output}")
        span = window["size"][0] if direction in ("left", "right") else window["size"][1]
        clicks = _scroll_clicks(span, amount) * SCROLL_SIGN[direction]
        axis = "-x" if direction in ("left", "right") else "-y"
        other = "-y" if axis == "-x" else "-x"
        turned = self._shell(["ydotool", "mousemove", "--wheel",
                              axis, str(clicks), other, "0"], timeout=10)
        if not turned.ok:
            if "uinput" in turned.output.lower():
                return Result(False, CLICK_UNAVAILABLE)
            return turned
        screens = "a screen" if amount == 1 else f"{amount} screens"
        return Result(True, f"scrolled {title} {direction} about {screens}. "
                            "Read it again to see what is there now.")

    # -- patience: waiting for something to happen --------------------------
    def _validate_wait_for(self, what: str, value: str, timeout: float = WAIT_DEFAULT) -> str | None:
        if what not in ("text", "window", "window_gone"):
            return 'what must be "text", "window" or "window_gone"'
        if not (value or "").strip():
            return "value is required — say what you are waiting for"
        try:
            float(timeout)
        except (TypeError, ValueError):
            return "timeout must be a number of seconds"
        return None

    def _tool_wait_for(self, what: str, value: str,
                       timeout: float = WAIT_DEFAULT) -> Result:
        """Poll until the condition holds, and say how long it took.

        A timeout here is a finding, not a failure: "the page did not load in
        ten seconds" is something the user wants said out loud, and it is much
        better than reading a stale screen and reporting its contents as new.
        """
        if error := self._validate_wait_for(what, value, timeout):
            return Result(False, error)
        limit = max(0.5, min(float(timeout), WAIT_MAX))
        started = time.monotonic()
        gap = WAIT_POLL_TEXT if what == "text" else WAIT_POLL_WINDOW
        last_error = ""

        while True:
            if what == "text":
                monitors = self._query_json("monitors")
                screen = next((m for m in monitors if m.get("focused")), None) \
                    or (monitors[0] if monitors else None)
                if screen is None:
                    return Result(False, "no monitor to look at")
                geometry = (f'{screen.get("x", 0)},{screen.get("y", 0)} '
                            f'{screen.get("width", 0)}x{screen.get("height", 0)}')
                words, last_error = self._ocr_words(geometry)
                if last_error:
                    return Result(False, last_error)
                found = self._find_phrase(words, value) is not None
            else:
                clients = self._query_json("clients")
                exists = any(_window_matches(c, value) for c in clients)
                found = exists if what == "window" else not exists

            waited = time.monotonic() - started
            if found:
                return Result(True, f"{_wait_description(what, value)} after "
                                    f"{waited:.1f}s. Carry on.")
            if waited + gap >= limit:
                return Result(True, f"waited {limit:.0f}s and {_wait_timeout(what, value)}. "
                                    "That is what happened — say so, or look with "
                                    "read_screen before deciding what to do next.")
            time.sleep(gap)

    # -- exact text: the clipboard ------------------------------------------
    def _validate_clipboard(self, action: str, text: str = "") -> str | None:
        if action not in ("read", "write"):
            return 'action must be "read" or "write"'
        if action == "write" and not (text or "").strip():
            return "text is required to write to the clipboard"
        return None

    def _tool_clipboard(self, action: str, text: str = "") -> Result:
        if error := self._validate_clipboard(action, text):
            return Result(False, error)
        if action == "write":
            if not shutil.which("wl-copy"):
                return Result(False, "wl-copy is not installed (pacman -S wl-clipboard)")
            # Not one pipe between here and wl-copy. It forks a process that
            # holds the selection until something else takes it, and that child
            # inherits our file descriptors: with stderr on a pipe, reading to
            # EOF meant waiting for a process designed to outlive us, so a copy
            # that had already worked was reported as a ten-second timeout.
            # A file has no such problem — the parent exits, we read it after.
            with tempfile.TemporaryFile() as errors:
                try:
                    done = subprocess.run(["wl-copy", "--", text],
                                          stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=errors,
                                          timeout=10)
                except (OSError, subprocess.SubprocessError) as exc:
                    return Result(False, f"could not write the clipboard: {exc}")
                if done.returncode != 0:
                    errors.seek(0)
                    detail = errors.read().decode(errors="replace").strip()
                    return Result(False, detail or "wl-copy failed")
            return Result(True, f"copied {len(text)} characters to the clipboard")

        if not shutil.which("wl-paste"):
            return Result(False, "wl-paste is not installed (pacman -S wl-clipboard)")
        got = self._shell(["wl-paste", "--no-newline", "--type", "text/plain"],
                          timeout=10, limit=CLIPBOARD_LIMIT)
        if not got.ok:
            # wl-paste exits non-zero on an empty selection and on one holding
            # only an image, and the two read very differently to a user. Its
            # own words for empty are "Nothing is copied".
            lowered = got.output.lower()
            if "empty" in lowered or "nothing is copied" in lowered:
                return Result(True, "the clipboard is empty")
            return Result(True, "the clipboard does not hold any text "
                                f"({got.output.strip() or 'no text/plain offer'})")
        if not got.output.strip():
            return Result(True, "the clipboard is empty")
        return got

    # -- the machine about itself -------------------------------------------
    def _tool_system_query(self, topic: str) -> Result:
        """Read-only facts, from a fixed list of commands.

        Deliberately not run_shell. Every one of these is a question people ask
        out loud — "how much space is left", "am I still on wifi" — and none of
        them is worth making someone open the shell tool for, which would hand
        an open microphone the whole command line at the same time.
        """
        recipe = SYSTEM_QUERIES.get(topic)
        if recipe is None:
            return Result(False, f"unknown topic {topic!r}. Choose one of: "
                                 + ", ".join(sorted(SYSTEM_QUERIES)))
        if callable(recipe):
            return recipe(self)
        parts = []
        for label, argv, *cap in recipe:
            if not shutil.which(argv[0]):
                continue
            got = self._shell(argv, timeout=10, limit=1200)
            if not got.ok or not got.output.strip():
                continue
            body = got.output.strip()
            if cap:
                body = "\n".join(body.splitlines()[:cap[0]])
            parts.append(f"{label}:\n{body}" if label else body)
        if not parts:
            return Result(False, f"nothing on this machine could answer {topic!r}")
        return Result(True, "\n\n".join(parts))

    def _battery(self) -> Result:
        """/sys rather than upower, because the answer is often "there isn't one"."""
        root = Path("/sys/class/power_supply")
        try:
            supplies = sorted(root.iterdir())
        except OSError:
            supplies = []
        rows = []
        for entry in supplies:
            def read(name: str) -> str:
                try:
                    return (entry / name).read_text().strip()
                except OSError:
                    return ""
            if read("type").lower() == "battery":
                percent, status = read("capacity"), read("status")
                rows.append(f"{entry.name}: {percent or '?'}% ({status or 'unknown'})")
            elif read("type").lower() == "mains" and read("online") == "1":
                rows.append(f"{entry.name}: on mains power")
        if not rows:
            return Result(True, "this machine has no battery — it is a desktop, "
                                "always on mains power")
        return Result(True, "\n".join(rows))

    # -- the notebook -------------------------------------------------------
    def _notes_path(self) -> Path:
        from .config import STATE_DIR
        return STATE_DIR / "notes.json"

    def _read_notes(self) -> list[dict]:
        try:
            data = json.loads(self._notes_path().read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return [n for n in data if isinstance(n, dict) and n.get("text")] \
            if isinstance(data, list) else []

    def _write_notes(self, notes: list[dict]) -> str | None:
        path = self._notes_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(notes[-NOTES_LIMIT:], indent=1))
            path.chmod(0o600)
        except OSError as exc:
            return f"could not write the notes: {exc}"
        return None

    def _validate_remember(self, action: str, text: str = "") -> str | None:
        if action not in ("note", "list", "forget"):
            return 'action must be "note", "list" or "forget"'
        if action == "note" and not (text or "").strip():
            return "text is required — say what to write down"
        if action == "forget" and not (text or "").strip():
            return 'text is required — words identifying the note, or "all"'
        return None

    def _tool_remember(self, action: str, text: str = "") -> Result:
        if error := self._validate_remember(action, text):
            return Result(False, error)
        notes = self._read_notes()

        if action == "list":
            if not notes:
                return Result(True, "the notebook is empty")
            return Result(True, "\n".join(
                f'{n.get("when", "?")}  {n["text"]}' for n in notes))

        if action == "forget":
            if text.strip().lower() == "all":
                kept, dropped = [], len(notes)
            else:
                wanted = [w for w in re.split(r"\W+", text.lower()) if w]
                kept = [n for n in notes
                        if not all(w in n["text"].lower() for w in wanted)]
                dropped = len(notes) - len(kept)
            if not dropped:
                return Result(False, f"no note matches {text!r}; nothing was forgotten")
            if error := self._write_notes(kept):
                return Result(False, error)
            return Result(True, f"forgot {dropped} note(s)")

        line = " ".join(text.split())[:NOTE_LENGTH_LIMIT]
        notes.append({"when": time.strftime("%Y-%m-%d %H:%M"), "text": line})
        if error := self._write_notes(notes):
            return Result(False, error)
        return Result(True, f"noted. {len(notes[-NOTES_LIMIT:])} note(s) in the notebook")
