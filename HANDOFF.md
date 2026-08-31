# omarchy-voice — 0.3.0

Composition, and the speed work that came out of reading the session log.

## Composing a workspace

New tool, `compose_windows`. Ask for a subject rather than an application —
"what's going on in the news today" — and it opens a set of windows for the
task and lays them out on the first empty workspace.

It exists because the sequencing cannot be done from the model's side. A launch
command returns before the window maps, so the assistant's own move/focus calls
raced the applications they were placing. `compose_windows` waits for each
window to appear, moves it if a window rule sent it elsewhere, and preselects
the split for the next one. Three layouts: `columns`, `main-and-side`, `grid`.

`hl.dsp.layout("preselect r")` is what steers dwindle, and it is the one
dispatcher taking a positional string rather than a table — the manifest used to
say they all take tables. Equal columns then need one resize per pane beyond the
second, because dwindle halves recursively: 1261/621/626 becomes 845/829/834.

## Speed

Measured, not guessed. The numbers are for this machine at 40,000 TPM.

- **Nothing was reported when a turn failed.** `response.done` with status
  `failed` was dropped silently — no log line, no notification, no speech. The
  log has the user asking to switch workspace four times in a row and getting
  silence each time. Those were rate-limited responses.
- **The org's limit is 40,000 tokens/minute and a turn costs ~8,200**, so about
  five turns a minute. Cached tokens count in full. The daemon now waits the
  interval the server names and retries twice before saying anything, which
  turns most of those silences into a pause.
- **The desktop snapshot no longer rewrites `instructions`.** It arrives as a
  conversation item instead, and the previous one is deleted. Rewriting the
  instructions re-prefilled ~9k unchanged tokens per turn; appending without
  deleting would have accumulated a stale window list per turn.
- **The manifest is ~1,500 tokens smaller.** The Omarchy CLI section was 15 KB
  of 26 KB, most of it installer and hardware plumbing. It is now an allow list
  of groups, and only short routes carry a summary.
- **The persona no longer asks for a spoken preamble before every action.** It
  cost a second of speech before anything moved, on actions that are instant.
- **Launch grace 1.2 s → 0.5 s.** Every way an `omarchy launch` can fail returns
  in under 0.35 s on this machine, so the rest was silence added to every launch.

## Toggle-only

`SUPER + SHIFT + V` turns listening on and off, and that is the whole story.
Removed: the `F10` hold-to-talk bindings and the `always` listening mode. The
`mode` config key is retired rather than unknown — every config written before
this says `mode = "push"`, and `doctor` explains why it stopped mattering
instead of reporting it as a typo.

`RealtimeSession.active` is now hardcoded to start false. There is no
configuration that opens the microphone at boot.

## Fits where Omarchy puts things

`omarchy/bin/omarchy-voice*` — ten one-line wrappers carrying the
`# omarchy:summary=` / `# omarchy:args=` / `# omarchy:group=voice` metadata the
dispatcher reads, so the feature registers as `omarchy voice ...` alongside
every other command. Verified against Omarchy's own dispatcher: all ten routes
resolve with the right args and summaries.

`omarchy` only scans the directory holding the `omarchy` binary — `/usr/bin`,
resolved from `${BASH_SOURCE[0]}` — so `install.sh` offers this as an optional
sudo step. Declining costs the spelling, nothing else.

The wrappers go through `python3 -m omarchy_voice`, never `exec omarchy-voice`:
the wrapper for the bare `omarchy voice` route *is* named `omarchy-voice`, so a
PATH lookup would find it again and loop.

## Installs without a key

A missing `OPENAI_API_KEY` used to be a hard start failure, so a fresh install
gave you a service that failed, restarted, failed, and hit `StartLimitBurst`.
It now prints what to do, publishes an `unconfigured` state for the bar widget,
and exits 0 so systemd leaves it down.

## The daemon used to die quietly

Three bugs, one symptom ("it stopped working"), found when it did:

1. A dropped websocket ended the run. `_send` caught
   `keepalive ping timeout`, set the stop flag, and wound the session down. It
   now reconnects with backoff — six tries, doubling from 2 s to 30 s — and
   comes back listening if it was listening.
2. That path returned **0**, so `Restart=on-failure` left it down. A dropped
   socket now sets a non-zero exit, and giving up after the reconnect cap does
   too.
3. The process then would not exit at all. Tool calls run through
   `asyncio.to_thread`, and `asyncio.run` waits for the default executor to
   drain, so one tool still blocked on a subprocess kept the process alive
   after its control socket was gone: `ps` showed a healthy daemon, the CLI
   said no daemon was running, and systemd never restarted it. The loop now
   owns its executor and abandons it on the way out.

Verified against real sockets: three connections, two forced closes, clean
recovery each time.

## The prompt is looked up, not pasted

The manifest used to carry all 128 Omarchy CLI routes on every turn — ~2,270
tokens, resent against a per-minute budget, so the model could reach for
`omarchy notification dismiss` roughly never. They live in a cached index on
disk now, behind `omarchy_help`, which searches them on demand. The fifteen
things people actually say stay inline.

Prompt: 9,350 → 6,919 tokens. At the 40,000 TPM being enforced that is 4.3 →
5.8 turns a minute.

Search matches *any* query word, ranked by how many hit, with route matches
outranking summary matches. Requiring every word meant "dark theme" found
nothing, because no route says "dark".

## Reading the screen

`read_screen` OCRs what is visible — `grim` on a resolved geometry piped through
`tesseract`. This is how "what does it say", "summarise this", "what's in the
news" get answered; the window list tells you a window is called "BBC News",
this tells you the headlines.

`omarchy capture text` cannot do this job: it shells out to `slurp` for an
interactive drag and writes to the clipboard rather than stdout.

Only visible windows can be read — grim captures what is being painted — so a
window on another workspace is refused with the fix in the message.

**It sends a picture of the screen to OpenAI.** That is a step past streaming
audio, and whatever else is on screen goes with it.

## Bugs found while reading the log

- **`hyprctl -j clients` was truncated at 4,000 characters** — about five
  windows — and the model got JSON with the end cut off. Silent. JSON queries
  are now parsed whole and slimmed afterwards.
- `hl.dsp.workspace.change_id` used to navigate is refused with the right call
  named. It renames a workspace; used with one key it did nothing, and the
  assistant then told the user workspace 5 did not exist.
- `<window-pattern>` and other signature placeholders passed through verbatim
  are refused with an explanation.
- `launch-or-focus` is rewritten to the real route, `launch or focus`.
- Workspaces 1–10 always exist. The persona says so; the assistant had been
  refusing to switch to an empty one.

## Verify

```bash
cd omarchy-voice
python3 -m unittest discover -s tests      # 111 tests
./bin/omarchy-voice doctor
./bin/omarchy-voice --dry-run say "what's going on in the news today"
python3 tools/bench_realtime.py            # costs API tokens
```

`say` needs `OPENAI_API_KEY` in the environment or in
`~/.config/omarchy-voice/env`.
