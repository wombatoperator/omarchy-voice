# GPT-Live backend

The daemon supports GPT-Live with Responses delegation alongside Realtime.
Live handles conversation; a separately configurable Responses model selects
desktop functions, which run locally through the existing Executor and policy.
The audio reader stays responsive while a coordinator executes independent tools
in bounded parallel groups and keeps dependent groups ordered.

## Run and switch back

For an isolated foreground evaluation, stop any existing daemon first, then run:

```sh
omarchy-voice listen quit
./bin/omarchy-voice run --engine live
```

Toggle listening with the existing keybinding or `omarchy-voice listen start`.
The engine starts muted and creates no paid session until listening or a typed
`listen say` request starts. To select Live persistently, set `engine = "live"`
under `[openai]` in your config. See `[live]` in `share/config.example.toml`.
The shipped fallback default is Realtime. This machine is configured explicitly
with `engine = "live"` for QA.

`run --engine realtime` selects the original engine. The complete pre-migration
implementation, including removed prototype files, is preserved on branch
`codex/realtime-backup-2026-09-10`, commit `865cea2`. Live work is on
`codex/live-refactor`.

## QA on this machine

The installed service is configured for Live with `gpt-live-1`, Marin, and
`gpt-5.6-terra`. Listening starts off.

1. Press **Super+Shift+V** and wait for the listening indicator.
2. Ask “What's my shortcut for the scratchpad?” to test system discovery.
3. Ask “Which workspace am I on?” to test current desktop state.
4. Try an ordinary action such as “Open a terminal”, then a follow-up.
5. Press **Super+Shift+V** again. The indicator should disappear and the Live
   session should close. Use `omarchy-voice log` to check `final=true` usage.

`omarchy-voice doctor` identifies the active configured backend and devices.
`omarchy-voice listen say "..."` uses the running Live backend without opening
the microphone; `omarchy-voice say "..."` uses the separate one-shot planner.

For a quick engine rollback, change `[openai] engine` to `"realtime"` in
`~/.config/omarchy-voice/config.toml` and restart `omarchy-voice.service`.
The original installed files and config were also saved in an owner-only
`pre-live-*.tar.gz` archive under `~/.local/state/omarchy-voice/backups/`.
The Git backup branch preserves the original source and removed prototypes.

## Costs

As of September 10, 2026, [official pricing](https://developers.openai.com/api/docs/pricing)
lists GPT-Live at **$0.05 per session minute**, billed by the second. Backend
model and hosted tool usage are separate. This means 10 connected minutes cost
$0.50 and an hour costs $3 before backend work. Treat connected silence as paid
time; server input muting is not a session shutdown.

The configured backend defaults to GPT-5.6 Terra with standard service tier,
low reasoning effort, and a 2,048-token output cap. Its short-context rates are
$2 per million input tokens, $0.20 cached input, and $12 output. For example,
10,000 uncached input tokens plus 1,000 output tokens cost $0.032 per backend
response. A multi-step request can need several responses. These are estimates,
not an account-specific quote or a guarantee that Live is cheaper than Realtime.

Controls:

- Toggle off closes the Live session and stops capture and playback.
- `live.max_session_seconds` defaults to 1,800 seconds. Reaching it pauses until
  the next toggle; it does not silently open another paid session.
- Typed requests synthesize silent input with no recorder and close after
  `live.typed_idle_seconds` of inactivity, once work and playback finish.
- `max_turns` caps local tool rounds; `live.max_output_tokens` caps each backend
  response. These are workload bounds, not a dollar spending limit.
- Session logs record cumulative voice seconds and finalization status, plus
  separate backend token usage. Voice snapshots are not added together.
  Disconnections without `session.closed` leave final voice usage unconfirmed.

## Audio and action behavior

`live.max_parallel_tools` defaults to four (range 1–8). The backend can return
multiple calls in one response. Independent read-only lookups, distinct desktop
app launches, standard terminal/browser/editor/file-manager launches, and closes
targeting distinct explicit window addresses can overlap within their own groups.
Focus, typing, scrolling, layout, general commands, and unknown call shapes are
barriers. Duplicate targets are serialized. If a group fails, later groups are
reported as unexecuted so the model can replan; started peers still finish.
Confirmation and cancellation checks remain in the execution path.

When new speech arrives during backend work, OMA pauses unstarted calls and
forwards the accumulated update to the backend after outstanding results drain.
The backend reconciles it with unfinished work, prioritizing new desktop actions.
Already-started operations finish and retain their results. This is task steering,
not unrestricted parallel execution across tasks. Transcript timestamps help group
updates; a 1,200 ms delivery-coalescing delay is not an authoritative speech-end
signal. Later fragments remain eligible to update the request. Real microphone
QA is still needed for hesitations and overlapping speech. Each settled new
request gets a bounded step budget; continued fragments cannot replenish it.

`read_page_text` reads selectable browser text before resorting to OCR. Page
opening/search also tries this fast path automatically. It uses Wayland's primary
selection and restores prior plain selection text. If Chromium does not republish
an already selected document, explicit copy provides a fresh fallback; the regular
plain-text clipboard is restored afterward. Rich/binary selections are left
untouched and use OCR instead. Exact addresses, visible workspaces and focus checks protect against
reading another window. A focused input field may still yield only that field;
returned text explicitly identifies that limitation.

`reveal_window` focuses an addressed window, optionally hides one named Omarchy
panel, and reads the result in one ordered tool call. The screen result must
support any visibility claim; an accepted focus/hide command alone is insufficient.
It does not blindly dismiss browser restore prompts or other application dialogs.

QA these changes by starting a lookup, asking to close a named window while it
works, then asking for text from an open page. To test panel recovery, open the
audio panel and ask OMA to reveal the page and dismiss the panel. Toggle off
afterward. `steering_paused` and `steering_forwarded` events show the update path.

The assistant receives current installed app names at startup, so personal app
requests should avoid web-search detours. `[navigation] news_sources` defines
the URLs opened together for “my news”; configure your own preferred sources.
See the [diagnostics guide](diagnostics.md) for interpreting measurements.

`barge_in = false` retains speaker echo protection. Live still receives a paced
stream, with microphone samples replaced by silence while audible output and its
echo tail play. Use headphones or PipeWire echo cancellation before enabling
`barge_in = true` for simultaneous conversation. The recorder is killed on mute.
Live has no Realtime input-buffer commit or audio-done event; playback state is
tracked locally and Live handles the conversation's turn-taking.

Completed function items are collected from nested `response.output_item.done`
events, because Live terminal snapshots deliberately have empty `output` lists.
All required function outputs are submitted before continuing a backend response.
Tool execution does not block the socket reader. New delegations supersede queued
actions from earlier requests. Mute, disconnect, and cancellation invalidate
queued work; an action already executing cannot be undone by closing the socket.
Its eventual result is recorded locally and is never used to continue an old
session. Check desktop state before retrying an interrupted action.

Spoken confirmations require actual user transcript received after a hold;
model-reported wording alone cannot release an action. Local `listen confirm`
continues to work independently of the voice service. Transcript fragments are
not authoritative completed utterances, so ambiguous confirmation should use
the local control. `listen cancel` cancels queued work and pending confirmation;
it does not claim to reverse actions already running.

Recent text history and action outcomes are stored owner-only in
`$XDG_STATE_HOME/omarchy-voice/live-state.json` (normally
`~/.local/state/omarchy-voice/live-state.json`). New sessions receive bounded
history and recent outcomes. On restart, unfinished actions are marked uncertain
and never automatically replayed. Pending confirmations are not restored across
daemon restarts. Server recording storage is disabled (`store = false`).

## Validation

```sh
python3 -m unittest discover -s tests -p 'test_live.py'
```

The tests use fake sockets and audio; no API tokens or desktop actions. They
cover startup/close ordering, billing lifecycle, function-result continuation,
concurrency, stale work, confirmations, and recovery records. Real-device echo,
speech timing, and API availability must also be checked before making Live the
default.

An opt-in API probe is available with `python3 tools/bench_live.py --connect`.
It sends synthetic silence and exposes only a harmless `probe` function. It
uses no microphone or desktop data. On September 10, the real API probe passed
startup, function results, continuation, and graceful close: 1 second of voice,
1,600 backend input tokens, and 31 output tokens (about $0.0044 at Terra's
published rates). This validates the protocol, not live microphone quality.

Protocol references: [WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets?api=live),
[delegation](https://developers.openai.com/api/docs/guides/live-delegation),
[session lifecycle](https://developers.openai.com/api/docs/guides/live-conversations).


## Budgeted latency experiments and explicit fast processing

The [budgeted benchmark review](budget-benchmark.md) records measured latency,
quality, spending and remaining limitations. Reusable runners in `tools/bench_*`
require explicit `--connect` for paid tests and share a conservative $3 ledger.

`[live] service_tier` accepts `"default"` (the default) or `"priority"`. Priority
requests faster backend processing at higher token prices; it does not change
Live voice-minute pricing. Returned backend tiers appear in performance logs.
Restart the idle user service after changing the setting. The installed model
remains Terra with low reasoning and standard processing.

Addressed window closes now wait for verified removal before dependent launches.
An unresolved close blocks dependent work and reports that the window is still
present. Correction routing includes early ownership and completed-answer notices,
but repeated backend lookups still occurred in measured spoken trials. The review
records those failures; this is not a claim of exactly-once delegated execution.

### Conversation recovery and diagnostics

After the application forwards a spoken correction or typed request, it keeps
routing subsequent utterances explicitly for that session. Fresh settled requests
receive a new bounded tool budget; later fragments of the same utterance do not.
Skipped calls do not consume tool rounds. A limit allows one final summary of
completed and unfinished work, while rejecting additional tool execution.

Detailed local diagnostics are written to `live-trace.jsonl` in the state directory,
with owner-only permissions and 8 MiB rotation (three backups). They include
transcripts, forwarded requests, tool arguments/results, backend answers, routing
state, usage and timing, but no raw audio or transport credentials. Page text and
window titles are private local data. See
[the diagnostics guide](diagnostics.md) for interpretation and safe sharing.

## Astra browser work and buffered audio

Complex browser requests now use an Astra computer-use worker through the
`browser_task` function. Routine desktop operations continue through Terra and
native tools. Live speech stays connected throughout the handoff. Browser
screenshots and actions use a separate bounded Responses conversation; API token
charges for Astra are additional to Live and Terra usage.

Live playback uses a 120 ms jitter buffer and paced 20 ms frames. See
[the diagnostics guide](diagnostics.md) for timing interpretation and known limits.
