# omarchy-voice

Operate Omarchy by talking to it. Speech goes to OpenAI Realtime; the only
thing that runs on this machine is the policy gate and the Omarchy / Hyprland
tools.

```
you   "put my email on workspace three, then go there"
      → hypr_query(clients)                       looks for the window
      → hl.dsp.window.move({ workspace = "3", window = "address:0x55d4..." })
      → hl.dsp.focus({ workspace = "3" })
it    "Moved HEY to workspace 3."
```

Omarchy already ships **Voxtype** for dictation — speech becomes *text*. This is
the other half: speech becomes *actions*. Voxtype keeps F9; this is on
`SUPER + SHIFT + V`.

This is an OpenAI + Omarchy add-on, packaged so you can install it locally,
drop the bar widget in as an Omarchy shell plugin, or open a PR upstream.

## Why an LLM instead of a phrase grammar

A grammar makes you learn its vocabulary. The interesting part of this add-on
is **what the model is told**. On first run it reads your live system and
builds a capability manifest from it:

| Source | What it contributes |
|---|---|
| `/usr/share/hypr/stubs/hl.meta.lua` | every Hyprland dispatcher, from Hyprland's own type stub |
| `/usr/share/omarchy/default/hypr/bindings/*.lua` | real, version-correct call syntax |
| `omarchy commands --json` | the whole Omarchy CLI, with arguments and summaries |
| `hyprctl -j` + your `.desktop` files | your monitors, workspaces, windows, and installed apps |

Hyprland 0.56 moved to a Lua dispatch API: `hyprctl dispatch workspace 1` is
dead, and a model working from memory writes it anyway. Reading the API off
the machine means the assistant is correct for *your* Hyprland and *your*
Omarchy, and stays correct after an update.

Run `omarchy-voice manifest` to read exactly what it knows.

## How it works

```
microphone ══▶ websocket ══▶ gpt-realtime ══▶ audio ══▶ speakers
 (pw-record)   while live         │             (pw-cat)
                                  ▼  function calls
                            policy gate ──▶ denied / held for confirmation
                                  │
                                  ▼
                    hyprctl · omarchy · wtype · uwsm-app
```

No local transcription step, no wake word: you talk, it stops and answers.
**While listening is on, room audio streams continuously to OpenAI.** Muting
kills the `pw-record` process rather than capturing audio and discarding it.

`omarchy-voice say "..."` is the typed equivalent. It uses the same tools and
the same gate, over Chat Completions, so you can try a command without a
microphone.

## Install

```bash
git clone https://github.com/wombatoperator/omarchy-voice
cd omarchy-voice
./install.sh
```

The installer asks before each step and is safe to re-run. It writes a
mode-600 `~/.config/omarchy-voice/env` for `OPENAI_API_KEY`, puts the
keybindings in `~/.config/hypr/bindings.lua` (backing the file up first),
places the bar widget, and reloads Hyprland.

```bash
# ~/.config/omarchy-voice/env
OPENAI_API_KEY=sk-...
```

A key exported in your shell does not reach the systemd user service. Check
your work:

```bash
omarchy-voice doctor
```

### Requirements

- Omarchy 4.x (Hyprland 0.56+)
- `python-websockets` (from `extra`) — the installer offers to install it
- `OPENAI_API_KEY`
- A microphone PipeWire can see — `doctor` will tell you if the default
  input is a monitor loopback

You get:

| Key | Does |
|---|---|
| `SUPER + SHIFT + V` | turn listening on, and off again |

That is the only way in. There is no hold-to-talk and no always-on mode: the
daemon starts muted, and while it is muted no recorder is running, so there is
nothing to leak. `SUPER + V` (Universal paste) and `SUPER + CTRL + V`
(clipboard manager) are Omarchy's and are left alone.

`./uninstall.sh` reverses all of it, including taking its own block back out of
`bindings.lua` and its widget out of the bar.

## Use

```bash
omarchy-voice run                     # the daemon — or the user service
omarchy-voice say "..."               # one command, typed instead of spoken
omarchy-voice --dry-run say "..."     # decide, but narrate instead of acting
omarchy-voice listen toggle           # what the keybinding calls
omarchy-voice listen confirm          # local confirm of a held action
omarchy-voice listen cancel
omarchy-voice status --json           # for the bar
omarchy-voice log -f
```

`--dry-run` still runs read-only queries (`hypr_query`) so the planner can
see live windows; it only narrates the actions that would change the desktop.

### As an Omarchy command

`install.sh` offers to put the `omarchy voice ...` routes next to the `omarchy`
binary, which is the only directory `omarchy` scans for commands:

```bash
omarchy voice                       # what it is doing
omarchy voice toggle                # same as the key
omarchy voice say "..."             # one request, typed
omarchy voice doctor
```

They are thin wrappers over the same program, with the `# omarchy:summary=`
metadata Omarchy's dispatcher reads, so they appear in `omarchy commands`
alongside everything else. Declining costs you nothing but the spelling —
`omarchy-voice ...` works either way.

### Things to say

> "switch to workspace four" · "close this window" · "put this on the left
> monitor" · "make it full screen" · "float this and centre it"
>
> "move everything off this workspace onto three" · "close the terminal that's
> running the build, not this one" · "open my email and put it beside the
> browser" · "what's on workspace two?" · "switch to a dark theme"
>
> "open a new tab" · "search this page for pipewire" · "save the file"

### Asking for a task, not an application

Name a subject rather than a program and it builds a workspace for it:

> "what's going on in the news today" · "set me up to watch the match and
> follow the chat" · "I want to work on the budget"

```
you   "what's going on in the news today"
      → compose_windows({ layout = "columns", workspace = "next",
                          panes = [ AP News, Reuters, BBC News ] })
it    "I opened AP News and Reuters side by side in a new workspace."
```

It picks the sources, opens them on the first empty workspace, and lays them
out — waiting for each window to actually map before placing the next, which
is the part that cannot be done from the outside. `columns` puts equal panes
side by side, `main-and-side` keeps one large with the rest stacked beside it,
`grid` is a 2x2. Whatever did not come up is named in the reply rather than
reported as open.

## Speed

Every turn re-sends the whole system prompt, and OpenAI counts those tokens
against a tokens-per-minute limit whether or not they were served from cache.
So the size of the prompt *is* how many things you can say in a minute:

```bash
omarchy-voice manifest | wc -c     # the biggest part of it
```

At the default 40,000 TPM and roughly 8,200 tokens a turn, that is about five
turns a minute. Past that the API starts refusing responses; the daemon waits
the interval the server names and asks again rather than going quiet, but it
cannot make the budget bigger. If Oma feels like it is pausing between
sentences, that is what is happening — check your tier at
[platform.openai.com/account/rate-limits](https://platform.openai.com/account/rate-limits).

`tools/bench_realtime.py` measures time-to-first-action across realtime models
on this machine.

## Safety

An open microphone is an untrusted input channel. The model's decisions are
not trusted blindly:

- **Denied outright**: `rm -rf`, `dd`, `mkfs`, `sudo`, `pkexec`, `ssh`,
  `passwd`, piping curl into a shell, `git push`.
- **Held for confirmation**: shutdown, reboot, suspend, package installs,
  `omarchy update`, config resets, closing every window.
- **Blocked as process execution**: `hl.dsp.exec_cmd` / `exec_raw`, and
  `launch_app` command lines. Apps launch by desktop id; URLs must be
  `http(s)`. `allow_shell = true` is the only way around that.
- **Off by default**: the shell tool.

Confirmation is not the model's to grant:

1. A gated tool and `confirm_last` in the **same** response is rejected.
2. `confirm_last` only runs after a **new user turn** (speech or `listen say`).
3. The phrase is matched as a whole utterance, so "don't confirm" does not
   confirm.
4. Clicking the bar widget while it says "waiting", or
   `omarchy-voice listen confirm`, releases the hold **locally** — that path
   never asks the model.

The lists live in `~/.config/omarchy-voice/config.toml`. Extra
`confirm_patterns` / `deny_patterns` are *added* to the built-in lists unless
you set `confirm_patterns_replace = true`.

The control socket lives under `$XDG_RUNTIME_DIR` (mode 700, socket 600). The
daemon refuses to start if that directory is not owner-only.

## Releasing as an Omarchy plugin / opening a PR

Two pieces, on purpose:

| Piece | Where it lives | How to ship it |
|---|---|---|
| Bar widget | `plugin/voice.indicator/` | Copy to `~/.config/omarchy/plugins/` and `omarchy bar put voice.indicator --section right`. This is a normal Omarchy shell plugin (`kinds: ["bar-widget"]`). |
| Daemon | `src/`, `bin/`, `share/` | `install.sh` puts it in `~/.local/share/omarchy-voice` and wires the user service + keybindings. |

To open a PR against Omarchy itself you would typically:

1. Keep the bar widget as a plugin under the shell plugin layout.
2. Keep the daemon as a community add-on (this repo) rather than putting a
   Python service in `/usr/share/omarchy/` — that tree is owned by the omarchy
   package and is overwritten on `omarchy update`.
3. Point the plugin's `onPressed` at `omarchy-voice` on `PATH`.

`python3 -m unittest discover -s tests` is the gate before you push.

## Bar widget

Shows idle / listening / thinking / acting / waiting-for-confirm. Click
toggles listening; click while waiting **confirms** the held action.

```bash
omarchy plugin add https://github.com/wombatoperator/omarchy-voice.git
omarchy bar put voice.indicator --section right
```

Or from a clone, without the plugin manager:

```bash
cp -r plugin/voice.indicator ~/.config/omarchy/plugins/
omarchy bar put voice.indicator --section right
```

There is no `omarchy bar remove`; `uninstall.sh` edits `shell.json` for you.

## Configuration

`~/.config/omarchy-voice/config.toml` — see `share/config.example.toml`.

## Layout

```
bin/omarchy-voice              launcher
src/omarchy_voice/
  capabilities.py              builds the manifest from the live system
  persona.py                   shared instructions for Realtime and `say`
  planner.py                   OpenAI Chat Completions loop for `say`
  tools.py                     the eight tools, and the policy gate
  realtime.py                  speech-to-speech engine and confirm gate
  session.py                   control socket (toggle / confirm / cancel)
  feedback.py                  notifications, bar state, TTS
  cli.py                       say / run / listen / status / doctor / manifest
plugin/voice.indicator/        Omarchy shell bar widget
share/                         systemd unit, keybindings, example config
```

MIT.
