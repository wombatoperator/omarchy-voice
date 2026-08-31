# omarchy-voice — 0.3.0

**Status as of 2026-08-31.** 341 tests passing. Published at
<https://github.com/wombatoperator/omarchy-voice> (MIT, public).

## Where things stand

| | |
|---|---|
| Daemon | running as a systemd user service, `gpt-realtime-2.1` |
| Prompt | ~9,700 tokens a turn. No longer the constraint — see below |
| Tests | 341 |
| Clicking | working end to end, verified against a live browser |

### Open pull requests to Omarchy

* **[basecamp/omarchy#9319](https://github.com/basecamp/omarchy/pull/9319)** —
  gives the Secret portal a provider so Chromium can open its password store.
  +51, 3 files. This is the stronger of the two: it completes a fix Omarchy
  already started (their migration `1784508556` says "on Hyprland the
  xdg-desktop-portal Secret backend has no provider and fails"), and fixes and
  hardening are what they actually merge from outside contributors.
* **[basecamp/omarchy#9320](https://github.com/basecamp/omarchy/pull/9320)** —
  adds `omarchy install service voice`. +54, 1 file, opt-in, nothing in the base
  install. Lower odds by the evidence: every existing service installer was
  written by DHH, and only one outsider feature PR merged in the last hundred.

Neither is a prerequisite for anything here. Both stay on the public record
whether or not they merge.

### Clicking

Works. `ydotool` needed `/dev/uinput` group access, which its own packaged udev
rule grants — the `uinput` module simply was not loaded and udev had never
applied the rule. Arch ships a **user** service called `ydotool`, not a root
`ydotoold`.

```bash
sudo modprobe uinput
sudo udevadm control --reload-rules && sudo udevadm trigger --name-match=uinput
systemctl --user enable --now ydotool
```

Two bugs only showed up once it could actually run, both of which made it click
confidently in the wrong place:

* tesseract was on `--psm 6`, "one uniform block of text", inherited from
  `omarchy-capture-text` where a human has dragged a box around a paragraph. A
  screen is not that, and psm 6 read straight past the tab row of a GitHub pull
  request. `--psm 3` finds it.
* A run qualified on half its words, so a two-word target passed on one: asked
  for "Files changed" it matched "changed files" in the body prose and clicked
  there. Every word is required now, with one word of slack for long phrases.

Verified: `click_text("Discussions")` navigated the browser.

**Known limit.** It clicks text that looks like the query, and cannot tell a
link from prose that happens to read the same. On a text-heavy page, prefer
wording that appears once, or drive the app with `send_shortcut` instead.

`ydotool mousemove --absolute` lands at twice the requested coordinates on this
display, so positioning stays with `hl.dsp.cursor.move` and ydotool is only
asked to press the button.

## She was hearing herself

The first long spoken session on **speakers** rather than headphones. It worked
well, and one thing was quietly wrong throughout. The machine's microphone and
line out are the same audio interface — a Focusrite Scarlett Solo, `HiFi__Mic1__
source` in and `HiFi__Line__sink` out — both at 100%, with no echo cancellation
loaded. Her voice left the room and came straight back into an open condenser
mic, and the server's turn detection has no idea it is hers.

Three separate proofs in one minute of log:

```
13:23:06  reply   'OH-mah, OH-mah, OH-mah.'
13:23:06  error   response cancelled: turn_detected
13:23:07  heard   '어마'                      <- her own name, transcribed as Korean
13:23:08  reply   'Yes, I'm here.'            <- she answered herself
```

```
13:23:54  reply   'I closed the terminal and reopened AP News, Reuters, and BBC News.'
13:23:54  error   response cancelled: turn_detected   (x2)
13:23:56  heard   'Workspace closed the terminal.'    <- her own sentence, as two turns
13:23:57  heard   'and re...'
13:23:59  reply   'I didn't catch the rest of that.'
```

```
13:28:29  heard   'Бела.'
13:28:30  action  press CTRL+r in activewindow        <- a phantom utterance ran something
```

That last one is why this is not merely untidy: once a fragment transcribes as
something imperative, it is executed.

**The fix is to stop sending microphone frames while she is speaking**, plus
350 ms for the room to go quiet. `Speaker.write` books the real duration of
each chunk it queues (PCM16 mono: `len/2/rate` seconds), so the gate knows when
sound stops rather than when the queue empties — the model sends a reply far
faster than it is spoken. `_drop_queued` clears the booking, so a genuine
barge-in reopens the mic at once instead of waiting out audio that will never
play.

This costs the ability to interrupt her mid-sentence, which on speakers did not
work anyway: she was the one doing the interrupting. `barge_in = true` gives it
back for headphones or for PipeWire's echo canceller, and `doctor` warns if it
is on with both ends on the same box. `share/echo-cancel.conf` is a working
`libpipewire-module-echo-cancel` config, with `webrtc.analog_gain_control` off
because it fights a hardware preamp.

Counted in the log as `mic held N frames while speaking`, so this is visible
rather than a silent drop.

## Writing a two-line file took thirteen commands

Also from that session, and this one is `run_in_terminal`'s fault. It refuses
newlines — send-keys would deliver them as Enter presses — and said only "one
command at a time; newlines are not sent". So the model reached for a heredoc,
was refused, and **retried the same heredoc five times** before working its way
to `printf` one fragment at a time: thirteen commands and forty-five seconds
for a shebang and an echo, with a broken intermediate file along the way.

The refusal now names the pattern that works (`printf '...\n...' > f`, append
with `>>`), which is the same "refuse with the right call named" fix that got
`web_search` used over `omarchy launch browser`. The persona rule about not
retrying a failing thing twice was already live and did not fire; a refusal
that says what to do instead is worth more than a rule saying what not to do.

Unresolved from the same session: "discard it" for a file she had just created
was blocked by the `rm` deny pattern. She offered to rename or move it instead,
which is a reasonable recovery, but deleting a file the assistant made a minute
earlier is arguably not what that rule is for.

## The rate limit was real, and is gone

`rate_limits.updated` is logged now, which it never was — the reason "tier 3
but enforced at 40,000" took a whole session to diagnose. The server states the
ceiling on every turn, so write it down:

```
limits  tokens: 797790/800000 left, resets in 0.165s
```

800,000, not 40,000. The legacy `gpt-4o-realtime` alias bucket that was capping
this account has been fixed on OpenAI's side. Every "trim the prompt to buy
turns" argument in the sections below was true when written and is now mostly
moot — spend tokens on capability.

## Terminals are text now

A terminal was a picture: `grim` the window, `tesseract` it, hope. That made
output garbled, readable only while the window was visible, and invisible with
the display asleep or the session locked. Input was worse — `wtype` into
whatever had focus, with no way to tell whether it landed.

tmux answers all of it, and Omarchy already ships it:

```
capture-pane -p        exact scrollback, from a pane on no workspace at all
pane_current_command   bash -> sleep -> bash: "is it finished", for free
send-keys              input with no focus, no keysym, no window target
list-panes -a          structured state across every session
```

`send-keys` taking the key by *name* means the entire keysym problem that cost
the last session does not exist on this path.

Four tools: `read_terminal`, `list_terminals`, `run_in_terminal`,
`watch_terminal`. Reading and listing are read-only, so they work under
`--dry-run` and are strictly safer than `read_screen`, which ships a picture of
the screen to OpenAI.

**Running is restricted to panes the user can see.** Two conditions, both
required: tmux has a client, *and* a terminal window is on a workspace the
compositor is currently drawing. `session_attached` alone is not enough — it
says a client exists, not that anyone is looking at it, and the client may be
in a window on a workspace nobody has visited since this morning. An open
microphone should not be able to run commands in a window with no view of it.

## Saying something without being asked

`_watch_loop` polls `Executor.poll_watches` every two seconds and, when a
watched command ends, puts an item in the conversation and asks for a response
— the same move the desktop snapshot already makes every turn, so the model
speaks about it in its own voice rather than a canned string being read out.
What is new is the trigger, not the plumbing.

The rules on interrupting: never across a reply in flight; never closer
together than eight seconds; a desktop notification instead of speech when
listening is off, because talking into a room that is not listening is noise;
and the item says explicitly that the user did **not** just speak, or the model
answers as though it had been asked something.

### The race that made it wrong twice

`pane_current_command` does not update the instant `send-keys` returns — the
shell has not forked yet. So the first poll saw `bash`, concluded the command
had finished, and handed back the echoed command line as its output: a
twenty-second job reported done in 0.4 seconds. `watch_terminal` had the same
bug from the other end.

A watch now has to observe the pane *busy* before idle means anything. If it
never looks busy inside a grace period, the command really was instant — or was
a shell builtin like `cd`, which never forks at all.

Measured rather than guessed: **tmux reflected the forked command in 0.024s,
six times out of six.** The grace is 0.6s, 25x that, and it is the floor on how
long an instant command appears to take — the first draft's 2.5s meant `echo
hello` sat silent for nearly three seconds.

A second bug fell out of the same rewrite: a watch on a pane that stayed busy
forever never timed out, because the "still running" branch `continue`d before
the age check. It would have been held until the daemon restarted.

## Chrome's "Profile error occurred" box

It had been showing up since the first composition and was written off as
noise. It is not corruption — every database in the profile checks out:

```
History     425984 bytes  integrity: ok
Top Sites    20480 bytes  integrity: ok
Favicons    229376 bytes  integrity: ok
```

It is **lock contention**. `omarchy launch webapp` starts a *new*
`google-chrome` process every time, which is supposed to hand off to the
browser already running and exit. Launch several in a row — which is exactly
what `compose_windows` does — or launch one while a heavy page is still
loading, and the handoff loses the race: the new process opens the profile
itself, collides with the first on SQLite, and Chrome raises the dialog. Caught
in Chrome's own log with `--enable-logging=stderr --v=1`:

```
ERROR ukm_database_backend.cc:142] Failed to open UKM database: 0 database is locked
ERROR top_sites_backend.cc:77]     Failed to initialize database.
```

Two long-lived browser processes on one profile were observed directly, with
`SingletonLock` absent at the time.

It is transient and harmless in itself, but it takes focus and covers the
screen, which breaks the preselect the *next* pane depends on. So it is closed
on sight — after every web window opens, and between panes during a
composition. Matched on an empty window class **and** the title, so it can only
ever take down an unclassed dialog, never a page that happens to be about
profile errors. `_await_new_window` already refused to mistake it for a pane.

Verified: a three-pane news composition now leaves zero dialogs on screen and
reads all three panes.

Not fixed, and probably not worth fixing here: the underlying race is in how
Omarchy launches web apps. Making it go away entirely would mean either
launching all panes through one already-running browser, or serialising launches
behind a settle that costs a second a pane.

## Searching: why typing was never going to work

The reported gap was "Oma needs a search engine". The session log says why the
obvious route kept failing, and it is not the one anybody guessed.

```
11:54:01  action  press CTRL+t in activewindow
11:54:02  action  type 'SpaceX stock price and earnings calendar'
11:54:02  action  press Return in activewindow
11:54:03  action  press Return in activewindow
11:54:04  action  press KP_Enter in activewindow
11:54:04  action  press Return in activewindow
11:54:05  action  press Escape in activewindow
11:54:07  action  click left on 'spacex stock price earnings report'   (x4)
11:54:12  guard   stopped after 12 tool rounds with no new user turn
```

The key was never the problem — `Return` is what was dispatched, correctly.
**The window has nowhere to type.** `omarchy launch webapp` is
`chrome --app=<url>`, and a Chromium app window has no tab bar and no address
bar, so `CTRL+T` and `CTRL+L` are no-ops. Every pane `compose_windows` opens is
one of these. Verified four ways — `send_shortcut` with and without a window
target, `send_key_state` down/up, and `ydotool key` at the uinput level — none
of which navigated, because there was no omnibox for any of them to reach.

The other half, from the same minute:

```
11:54:36  action  omarchy launch browser https://www.google.com/search?q=...
11:54:37  action  wait for window 'SpaceX ... - Google Search'
11:54:50  reply   'I don't see the Google results; the search page didn't appear.'
```

`omarchy launch browser <url>` hands the URL to the running browser, which
opens a **tab in a window that already exists**. Nothing new appears in
`hyprctl`, so there is no window to wait for, read, move or close, and the
assistant concluded — wrongly — that the launch had failed.

Both problems vanish if the query goes in the URL and the result opens as its
own window. `web_search(query, scope)` does exactly that: `omarchy launch
webapp` with the query already in the address, so nothing is typed, and the
window is a real one that can be waited for, OCR'd, scrolled and clicked.
`open_page(url)` is the same for one address. Scopes are `web`, `news`,
`images`, `videos`, `duckduckgo`; the two visual ones are deliberately not read
back, because the user asked to look at them.

A new search closes the window the previous one opened — tracked by address,
not matched by class, so a duckduckgo pane the user asked for by name is never
taken down.

Verified against the live realtime model:

```
action  search the web for 'current Bitcoin price in USD'
reply   'It shows Bitcoin at about 79,148 dollars and 79 cents.'      (8s)
action  search the news for 'who won the Formula One race this weekend'
reply   'Kimi Antonelli won the Belgian Grand Prix, but it looks like an older result.'
action  search the images for 'rubber duck'
reply   'They're on screen now—have a look at the rubber duck pictures.'
```

## Getting the model to actually use it

Adding the tool was the easy half. The model kept routing around it, and each
thing that finally moved it is worth recording, because persona wording was the
weakest of them:

1. **The manifest was teaching the mistake.** It listed
   `omarchy launch browser [url]`, read off the live system, and that beat every
   sentence of persona text telling the model not to. The row now reads
   `omarchy launch browser` with no URL, followed by a note naming the tools.
2. **Refusals at the point of the mistake work where prose does not.**
   `omarchy launch browser <url>` is refused, naming `web_search` with the query
   already extracted from the URL, or `open_page` with the address. Same pattern
   as the `hl.dsp.workspace.change_id` refusal.
3. **`compose_windows` with one pane is refused.** A layout tool asked to lay
   out a single window is a tell that the request was a question, not a
   workspace. It was the habitual route to anything web-shaped and it swallowed
   every search.
4. **Order in `TOOL_SCHEMAS` matters.** `web_search` and `open_page` moved ahead
   of `read_screen` and `compose_windows`.
5. Persona: a "Finding things out" section, and the composing section rewritten
   to be about *settling in* rather than about "a subject rather than an
   application", which had been firing on every question.

**The two models route differently, and only one of them ships.** All of the
above was tuned against `gpt-4.1` through `--dry-run say`, which still prefers
`compose_windows` for questions. `gpt-realtime-2.1` — the one the daemon
actually runs — gets it right, as the transcript above shows. Test routing with
`omarchy-voice listen say` against the running daemon, not with `say`.

## The manifest cache ignored the manifest's source

An hour went into wondering why a corrected essentials table was not reaching
the model. `_cache_key` hashed the system inputs — Omarchy and Hyprland
versions, the Lua stub, the bindings directory — and nothing else, so editing
`capabilities.py` left the daemon serving a manifest built before the change.
`Path(__file__)`'s mtime is in the key now.

## "Enter" pressed nothing, and said it had

The one reported bug, and the worst kind. `hl.dsp.send_shortcut({ key =
"Enter" })` returns **`ok`** and presses nothing: there is no keysym called
`Enter` — the key is `Return`, and xkbcommon resolves the name to NoSymbol —
but Hyprland does not report that. Measured on this machine:

```
$ hyprctl dispatch 'hl.dsp.send_shortcut({ mods="", key="Enter",  window="address:0x…" })'
ok
$ hyprctl dispatch 'hl.dsp.send_shortcut({ mods="", key="Return", window="address:0x…" })'
ok
```

Identical answers; only one of them did anything. And the persona says — quite
rightly, it was the fix for the last session's bug — that a tool which returned
without an error did what it says. So Oma said "pressed Enter" out loud, every
time, while nothing happened. "Enter" is also the word a person actually says,
so this was not a corner.

`keys.py` now resolves every key name through xkbcommon before it is
dispatched, and **refuses** anything that does not resolve rather than letting
Hyprland swallow it. On top of that a table of what people say: `enter`, `esc`,
`del`, `page down`, `space bar`, `up arrow`, `dot`, `pgdn`, `play/pause`, bare
punctuation, and the modifier names other desktops use (`cmd`, `command`,
`win`, `option`, `control`). Names resolve case-insensitively but are dispatched
in xkb's own spelling, so `CTRL + T` goes out as the `t` keysym — which is what
a shortcut wants — and a future case-folding change cannot quietly reintroduce
the miss. `hypr_dispatch` gets the same check, since it is the back door to
every dispatcher including this one.

The persona's "a tool that returned without an error did what it says" is now
*true* for keys, which it was not when it was written.

## Reach, patience and memory

Five tools, chosen from what a goal-directed session actually ran into.

* **`scroll`** — the screen shows one screenful, and what is below the fold does
  not exist to `read_screen` or `click_text`. The pointer is moved onto the
  target window first, because a wheel event goes to whatever is under the
  cursor and in a composed workspace that is usually the wrong pane. Falls back
  to `Page_Down` where ydotool is not installed, and says which route it took.
* **`wait_for`** — text on screen, a window appearing, a window closing. This is
  the sequencing primitive the loop needs: without it you read the screen a
  moment too early, see the old one, and report it as the new one. Capped at 25
  seconds, because the assistant is mute while it waits.
* **`clipboard`** — `read_screen` guesses at pixels; the clipboard is exact.
  CTRL+A, CTRL+C, read, and you have the page's real text instead of tesseract's
  opinion of it.
* **`system_query`** — disk, memory, battery, network, bluetooth, audio,
  uptime, processes, temperature, time, OS. A fixed argv table, not a command
  builder, so a misheard sentence cannot steer it. Read-only, so it works under
  `--dry-run`, and it means "how much space is left" no longer needs the shell
  tool turned on.
* **`remember`** — a dated notebook at `$XDG_STATE_HOME/omarchy-voice/notes.json`,
  mode 600. When listening is toggled off the conversation is gone, so this is
  the only memory a goal spanning two sittings has.

And `read_screen` takes an optional `query`: matching lines and a line either
side, instead of a screenful. A screen of OCR is a couple of thousand tokens
charged against the per-minute budget, and "is night light on" is one line.

`max_turns` 8 → 12. A goal worked properly is a loop — act, wait, look, act —
and eight rounds ran out halfway through anything with three steps in it.

The persona gained a **Going after a goal** section: say what you are doing and
start rather than presenting a plan; act then *look*; treat an unexpected screen
as information rather than repeating the step; write down what has to outlive
the turn; and stop for one of three named reasons, saying which. Plus: take
several steps before checking in — asking after every action means the user says
"carry on" five times to get one thing done.

### Scrolling, measured

Both numbers in `scroll` come off this machine, against a live browser, by
OCR'ing the window and tracking where individual words moved.

* **Direction.** "down" moved tracked words 406 px *up* the screen. REL_WHEEL
  counts up when the wheel turns away from you, so down is negative — the
  kernel convention, now confirmed rather than assumed.
* **Distance.** Ten notches moved 406 px, so 40.6 px a notch — which is the
  40 px Chromium and most GTK apps use. A fixed ten notches was therefore
  **40% of a 1030 px window while telling the model it had moved a screen**,
  which is how you read half an article and believe you read all of it. The
  notch count is derived from the window height now (85%, so there is overlap
  to read across), clamped to 4..30 so a small pane still moves and a 4K window
  does not fire a burst big enough for the application's own momentum scrolling
  to run away with. One call now turns over 64% of the words on screen; the
  remainder is X's sticky sidebar, which does not scroll at all.

How far a notch goes is the *application's* number, not the compositor's — a
terminal moves three lines — so `amount` is documented as approximate and the
persona says to read the screen again afterwards.

**Do not measure this against x.com.** Half its viewport is a sticky panel, so
a median over all shared words comes out as zero movement; it cost three runs
to notice. Track only words that leave the screen, or use a page that scrolls
as a whole.

## The lock screen was being read as the desktop

Found while trying to measure the scroll direction against a browser: `grim`
came back with a blurred photograph and a password box, and `read_screen`
reported it as the page. A locked session is the one way to capture pixels that
are not the desktop *and have the capture succeed* — DPMS only covers a display
that is off.

Nothing standard sees it. `hyprctl layers` shows only the background and the
bar (an `ext-session-lock` surface is not a layer), no process is running under
a name containing "lock", and logind's `LockedHint` stays `no`. Omarchy's own
shell draws the lock and will say so:

```bash
omarchy-shell lock isLocked     # -> true
```

Not `-q`. That is omarchy-shell's best-effort mode and it suppresses the answer
along with the errors, which reads as "not locked" — the first version of this
check was written with it and silently never fired. `read_screen`, `click_text`
and `scroll` all refuse now, with a message telling the model to ask for an
unlock rather than to report the lock screen as the user's desktop. Confirmed
against the live model: asked to open a page and read it, it launched, waited,
read, and answered *"the screen is locked, please unlock it"* instead of
inventing headlines.

## Two flaky tests that were passing by luck

Both reached the real machine through `subprocess.Popen`, which `mock.patch(
"subprocess.run")` does not cover. `test_a_screenful_of_text_is_capped` failed
the moment the display went to sleep, and the DPMS tests failed the moment the
session locked — neither having anything to do with what they were testing. The
screen-state probes are stubbed now.

## What the reach costs

Measured, not guessed:

| | Before | After |
|---|---|---|
| Persona | ~1,740 | ~2,330 |
| Realtime persona | ~890 | ~970 |
| Tool schemas | ~2,520 | ~3,320 |

About **+1,500 tokens a turn**, or roughly one turn a minute off the ~5 the
40,000 TPM bucket allows. That is the price of finishing a multi-step job
rather than stopping at the first thing that is not on screen. Two things were
given back: schema descriptions say what a tool does and its one failure mode
while the persona says when to reach for it, with the duplication between them
removed; and **`run_shell` is no longer sent at all unless `allow_shell = true`**
— a tool that will only ever be refused costs its schema every turn and a whole
round trip when the model reaches for it, which it does, being the one tool that
can express anything.

## Clicking, and not inventing failures

Two things came out of a real spoken session.

Every `Return`, `Down` and `Tab` was reported as "that key didn't register" and
retried three ways. The dispatcher was never broken — `hl.dsp.send_shortcut` and
`wtype` both deliver those keys, verified byte for byte against a terminal in raw
mode. What was wrong is that the model cannot feel a keypress land, so it
narrated a failure it had not observed. The persona now says a tool that returned
without an error did what it says, and to call `read_screen` when the effect
actually matters. One press, one accurate sentence.

And: *"double-click into this US and Iran trade strikes"*. Clicking by coordinate
is useless to someone talking, so `click_text` takes the words instead.
tesseract's TSV output carries a bounding box per word; the phrase is matched as
a sliding window over consecutive words, so one misread word does not lose the
headline. Hyprland moves the pointer, ydotool presses the button.

`omarchy capture text` cannot do this job — it shells out to `slurp` for an
interactive drag and writes to the clipboard rather than stdout.

`read_screen` and `click_text` check DPMS first. grim does not fail on a sleeping
monitor, it blocks until the timeout, so a read at half past midnight hung for
fifteen seconds and then blamed OCR.

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

Measured, not guessed. Figures are for this machine at the 40,000 TPM
actually being enforced — see the tier note below.

- **Nothing was reported when a turn failed.** `response.done` with status
  `failed` was dropped silently — no log line, no notification, no speech. The
  log has the user asking to switch workspace four times in a row and getting
  silence each time. Those were rate-limited responses.
- **A turn costs ~8,200 tokens and the enforced limit is 40,000/minute**, so
  about five turns a minute. Cached tokens count in full. The daemon now waits
  the interval the server names and retries twice before saying anything, which
  turns most of those silences into a pause.
- **The account is on tier 3 and is not being given tier 3 for realtime.**
  `rate_limits.updated` reports `limit=800,000`, and the errors name a different
  bucket than the model called: "Rate limit reached for gpt-realtime-2.1 **(for
  limit gpt-4o-realtime)**", capped at 40,000. A deliberate 8-turn burst had 3
  succeed and 5 fail, with `remaining` reported against 40,000. Both readings
  reproduce. This looks like a legacy alias bucket that never got the tier
  upgrade, and it is OpenAI's to fix — worth a support ticket quoting that
  exact string. Until then the retry logic is load-bearing, and trimming the
  prompt is the only lever on this side.
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
git clone https://github.com/wombatoperator/omarchy-voice
cd omarchy-voice
python3 -m unittest discover -s tests      # 341 tests
./bin/omarchy-voice doctor
./bin/omarchy-voice --dry-run say "what's going on in the news today"
python3 tools/bench_realtime.py            # costs API tokens
```

`say` needs `OPENAI_API_KEY` in the environment or in
`~/.config/omarchy-voice/env`.
