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
    # Fresh confirmation set: added after the first two representations were calibrated.
    for request, action, slots in (
        ("Please take me to workspace seven", "workspace", {"workspace": "7"}),
        ("Send the active window to desktop nine and switch there too", "move_workspace", {"workspace": "9"}),
        ("Leave me here and put the current window on workspace five", "send_workspace", {"workspace": "5"}),
        ("I want the Example documentation window in front", "focus_window", {"window": "w1"}),
        ("Give focus to the Synthetic project editor window", "focus_window", {"window": "w2"}),
        ("Select the neighboring window below this one", "focus_direction", {"direction": "down"}),
        ("Trade positions with the neighboring window on the left", "swap_direction", {"direction": "left"}),
        ("Take focus to my right-hand display", "focus_monitor", {"monitor": "m1"}),
        ("Transfer the current workspace to the right display", "move_workspace_monitor", {"monitor": "m1"}),
        ("Go forward one workspace", "workspace_next", {}),
        ("Step back one adjacent workspace", "workspace_previous", {}),
        ("Return to the last workspace I visited", "workspace_back", {}),
        ("Toggle scratchpad visibility for me", "scratchpad", {}),
        ("Put the current window away in scratchpad", "send_scratchpad", {}),
        ("Close the currently focused terminal", "close", {}),
        ("Switch focus to the next window", "cycle_next", {}),
        ("Switch focus to the previous window", "cycle_previous", {}),
        ("Bring this window to the very top", "raise", {}),
        ("Toggle fullscreen mode for the active window", "fullscreen", {}),
        ("Toggle floating mode for this window", "float", {}),
        ("Change the current window split orientation", "split", {}),
        ("Detach the current window from its group", "group_leave", {}),
        ("Select window number four within this group", "group_index", {"group_index": "4"}),
        ("Move the active window into the neighboring group above it", "group_join", {"direction": "up"}),
        ("Launch Example Notes", "launch_app", {"app": "a2"}),
        ("Toggle the desktop sound panel", "panel_audio", {}),
        ("Toggle the wireless network panel", "panel_network", {}),
        ("Bring up a new default terminal", "terminal", {}),
        ("Show the file manager", "files", {}),
        ("Display the keyboard bindings menu", "keybindings", {}),
        ("Show the notification history panel", "notification_history", {}),
        ("Restore the saved width of this window", "restore_width", {}),
    ):
        add(request, action, "confirmation", **slots)
    for request in (
        "Which display would be best for editing?", "Read the text in the terminal",
        "Take me to workspace two and then workspace five", "Close the editor and focus the browser",
        "If this is the terminal, switch to workspace three", "Please leave this window open",
        "Make the browser window float", "Switch to desktop ninety-nine", "Focus the chat window",
        "Open a terminal running an update command", "Resize the window to 800 pixels wide",
        "Bring up a useful website about window management", "Switch off fullscreen mode",
        "Put that thing over there", "Explain the scratchpad", "Open Example Notes and write a shopping list",
    ):
        add(request, "fallback", "confirmation")
    return rows
