"""Synthetic navigation requests and inventory; no user data or desktop access."""
from omarchy_voice.navigation import ACTIONS

INVENTORY = {
    "active_window": "0x101",
    "windows": [
        {"address": "0x101", "class": "terminal", "title": "Terminal", "workspace": 1},
        {"address": "0x102", "class": "browser", "title": "Example documentation", "workspace": 2},
        {"address": "0x103", "class": "editor", "title": "Synthetic project", "workspace": 3},
    ],
    "monitors": [{"name": "SYNTH-1", "label": "Left monitor"},
                 {"name": "SYNTH-2", "label": "Right monitor"}],
    "apps": [{"id": "example-calculator", "name": "Example Calculator"},
             {"id": "example-calendar", "name": "Example Calendar"},
             {"id": "example-notes", "name": "Example Notes"}],
    "dispatchers": sorted({a.arguments["lua"].partition("(")[0] for a in ACTIONS.values()
                           if a.tool == "hypr_dispatch"}),
    "routes": ["launch terminal", "launch browser", "launch editor", "launch nautilus",
               "hyprland workspace layout toggle", "hyprland window tiled fullscreen toggle",
               "hyprland window pop", "hyprland window width", "menu", "shell"],
}


def cases():
    rows = []
    def add(request, action, split="development", **slots):
        rows.append({"id": f"case-{len(rows):03}", "request": request,
                     "expected": {"action": action, **slots}, "split": split})
    # Explicit slot cases; the remaining development cases exercise catalogue wording.
    slot_cases = [
        ("Switch to workspace four", "workspace", {"workspace": "4"}),
        ("Move this window to workspace three and follow it", "move_workspace", {"workspace": "3"}),
        ("Send this window to workspace six but keep me here", "send_workspace", {"workspace": "6"}),
        ("Focus the browser window", "focus_window", {"window": "w1"}),
        ("Focus the window on my left", "focus_direction", {"direction": "left"}),
        ("Swap this window with the one on its right", "swap_direction", {"direction": "right"}),
        ("Focus the right monitor", "focus_monitor", {"monitor": "m1"}),
        ("Move this workspace to the left monitor", "move_workspace_monitor", {"monitor": "m0"}),
        ("Move this window into the group on the left", "group_join", {"direction": "left"}),
        ("Focus group window three", "group_index", {"group_index": "3"}),
        ("Open Example Calculator", "launch_app", {"app": "a0"}),
    ]
    for request, action, slots in slot_cases:
        add(request, action, **slots)
    for action, spec in ACTIONS.items():
        if not spec.slots:
            add(spec.description, action)
    for request, action, slots in (
        ("Take me to desktop number eight", "workspace", {"workspace": "8"}),
        ("Put this on desktop two without taking me along", "send_workspace", {"workspace": "2"}),
        ("Bring the documentation browser into focus", "focus_window", {"window": "w1"}),
        ("Select the editor showing Synthetic project", "focus_window", {"window": "w2"}),
        ("Switch to the screen on the left", "focus_monitor", {"monitor": "m0"}),
        ("Bring up Example Calendar", "launch_app", {"app": "a1"}),
        ("Go one window backward within this group", "group_previous", {}),
        ("Exchange this window's position with its upper neighbor", "swap_direction", {"direction": "up"}),
        ("Send the active window into the group below", "group_join", {"direction": "down"}),
        ("Bring the active window above the others", "raise", {}),
        ("Open a file browsing window", "files", {}),
        ("Switch to the first window in this group", "group_index", {"group_index": "1"}),
    ):
        add(request, action, "held_out", **slots)
    for request in (
        "What workspace am I on?", "Summarize the browser article", "Find today's weather",
        "Make my desktop better for writing", "Close the browser window",
        "Switch to workspace two then open a terminal", "Open Example Calculator and Example Notes",
        "Focus the missing music player", "Focus that one", "Delete all my files",
        "Run a shell command in the terminal", "If the browser is busy, close this window",
        "Type hello into the editor", "Close all windows", "Exit fullscreen",
        "Move this window to workspace forty", "Increase the font size in my editor",
        "Click the green button", "Launch an application called Unlisted App",
        "Resize this window to exactly 713 by 419 pixels", "Open a browser at https://example.org",
        "Move the browser to workspace two", "Do not close this window", "Explain how to switch workspaces",
    ):
        add(request, "fallback", "held_out")
    return rows
