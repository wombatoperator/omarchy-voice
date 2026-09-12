# OMA's Omarchy system map

OMA discovers the installed machine on demand through `omarchy_help`. The same
map is available locally through `bin/omarchy-voice map`, without opening an
OpenAI session or making an API request. The common desktop actions stay in the
voice prompt; the full inventories are retrieved only when needed.

## How the desktop fits together

| Layer | Responsibility | Discovery and control |
| --- | --- | --- |
| Arch Linux | Packages, hardware, system services | `system_query` for supported status checks; Omarchy's package, hardware, setup and update command groups |
| Hyprland | Monitors, workspaces, windows, input, bindings | `hypr_query` for current state; `hypr_dispatch` for operations; `dispatchers` and `shortcuts` map topics |
| Omarchy CLI | Public interface to desktop features and system utilities | `commands` searches every public group; `command_details` supplies exact arguments, examples when provided, and the installed source path |
| Omarchy shell | One Quickshell process hosting bar, menus, panels, overlays and services | `plugins` reports discovered manifests and enabled/active state from the shell registry when available |
| User configuration | Overrides loaded after packaged defaults | `configuration` gives paths, ownership and reload behavior; file contents are not sent to the model |
| Applications | Desktop entries and additional launch actions | `applications` finds desktop IDs and actions such as new windows, respecting user overrides and XDG search paths |
| Automation | Event hooks, startup commands, shell plugins | `hooks` inventories event directories and scripts; `configuration` locates startup and plugin files |
| OMA | Voice/typed requests translated into local tool calls | Realtime or Live backend -> shared executor -> existing execution policy -> desktop interfaces |

Hyprland starts the Omarchy shell as part of the graphical session. The shell's
plugins share services; the bar, notifications and settings panels are not
separate independently configured desktop environments.

Packaged defaults live under `/usr/share/omarchy/`. User Hyprland files load
after those defaults, and shell overrides live in `~/.config/omarchy/shell.json`.
Customizations belong in user configuration: package updates replace packaged
files. Most Hyprland Lua changes auto-reload, but require validation with
`hyprctl reload` and `hyprctl configerrors`. Shell configuration hot-reloads;
night-light and portal configuration belong to separate processes.

## Explore it

Run these from the repository (after installation, use `omarchy-voice`):

```sh
bin/omarchy-voice map
bin/omarchy-voice map commands wifi
bin/omarchy-voice map commands plugin
bin/omarchy-voice map command_details 'omarchy plugin clone'
bin/omarchy-voice map shortcuts scratchpad
bin/omarchy-voice map shortcuts 'SUPER SHIFT V'
bin/omarchy-voice map applications browser
bin/omarchy-voice map dispatchers 'workspace move'
bin/omarchy-voice map configuration 'night light'
bin/omarchy-voice map plugins clock
bin/omarchy-voice map hooks
bin/omarchy-voice map commands --offset 12
```

The assistant uses the same lookup, for example:

```json
{"query":"scratchpad","topic":"shortcuts"}
```

An empty query browses a topic. Results include inventory counts and a next
offset; `limit` ranges from 1 to 30. Plain-word search ignores common filler and
recognizes terms such as “wifi”, “sound”, “microphone” and “wallpaper”. Exact
command details accept only a catalogue route, never arbitrary command text.

Useful voice questions include “How do I move a window to another monitor?”,
“What's my shortcut for the scratchpad?”, “Where do I change when the screen
locks?”, “Which clock plugin am I using?” and “What hooks can I customize?”.
OMA can explain these answers instead of restricting every reply to a brief
action acknowledgement.

## Evidence and limits

On September 10, 2026, this machine's map found **367 public commands across
63 groups**, **51 dispatcher names**, **61 visible applications**, and **39 shell
plugin manifests**. These are an inventory snapshot, not hardcoded limits.

- Commands come from `omarchy commands --json`, including public groups previously
  excluded from voice discovery. Administrator requirements remain visible.
  Discovery does not change execution permissions or confirmation rules.
- Current shortcuts come from `omarchy menu keybindings --print`. This uses
  Omarchy's handling of Lua/custom bindings and avoids malformed shortcut JSON
  in the installed Hyprland version. A missing graphical session reports
  unavailable data instead of presenting defaults as active shortcuts.
- Dispatcher names come from the installed Lua stub; examples come from packaged
  bindings. A `fun(...)` stub proves a name exists, not which arguments it accepts.
  Missing examples are explicitly flagged. Dynamic Lua is not evaluated to build
  the map, so this is not a complete dispatcher argument reference.
- Application discovery follows XDG directory precedence, nested desktop IDs,
  hidden overrides, desktop visibility and declared actions. It does not execute
  desktop entry contents. The prompt's small app shortlist remains cached; the
  applications topic reads the current inventory.
- Plugin files prove installation. The shell registry supplies enabled/active
  state separately; if inaccessible, that state is unknown. Sample hook files
  are labeled as samples and are not executed by discovery or Omarchy's hook runner.
- Configuration guidance is a maintained architecture map grounded in installed
  templates and shell documentation. It checks path existence without reading
  private configuration values. It does not automatically edit or validate them.

This improves navigation and discovery. It does not add an unrestricted
configuration editor or guarantee every discovered command can run unattended.
Interactive setup, authentication and privileged actions still need their
appropriate execution workflow.

## Local sources

- `/usr/share/omarchy/config/hypr/hyprland.lua`: defaults and override load order.
- `/usr/share/omarchy/default/hypr/bindings/`: version-specific dispatcher examples.
- `/usr/share/hypr/stubs/hl.meta.lua`: dispatcher namespaces.
- `/usr/share/omarchy/shell/README.md` and `shell/plugins/README.md`: shell/plugin architecture.
- `/usr/share/omarchy/bin/omarchy-menu-keybindings`: resolved shortcut discovery.
- `/usr/share/omarchy/bin/omarchy-plugin-list`: live shell plugin registry.
- `/usr/share/omarchy/bin/omarchy-hook`: event and sample-file handling.

Validation covers catalogue changes, search ranking, pagination, exact-route
lookup, unavailable IPC, namespace verification, XDG overrides/actions, plugin
state and read-only hook/configuration discovery. No paid API test is needed to
exercise this map.
