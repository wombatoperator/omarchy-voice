# Diagnostics and reliability

Persistent logs and history live under `~/.local/state/omarchy-voice/` by default
(`$XDG_STATE_HOME/omarchy-voice` when set). Keep them out of Git and review
[the security policy](../SECURITY.md) before sharing any evidence.

| File | Purpose |
|---|---|
| `session.log` | Human-readable actions, failures, usage and timing |
| `live-trace.jsonl` | Live transcripts, routing, tool receipts and playback timing |
| `network-trace.jsonl` | Connection setup, Ping/Pong RTT, timeouts and local loop lag |
| `vision-trace.jsonl` | Camera startup, request stages/timing, bounded FFmpeg/FFplay error tails and shutdown reasons |
| `live-state.json` | Conversation history and operation recovery state |
| `tasks/TASK_ID/worker-trace.jsonl` | Worker model-request timing and failures |
| `tasks/TASK_ID/workspace/.oma-logs/` | Command stdout and stderr |

Task paths in this table assume the default root; `[tasks] root` can change it.
Control sockets, the indicator's `state.json` and `level`, and the camera's
`vision/control.sock` and `vision/companion.log` live under
`$XDG_RUNTIME_DIR/omarchy-voice`. Without `XDG_RUNTIME_DIR`, the application uses
`run/` inside its persistent state directory. These paths are defined in
[`config.py`](../src/omarchy_voice/config.py).

Live, network and worker trace logs rotate at 8 MiB with three backups.
`session.log` and `vision/companion.log` are not automatically rotated; manage
their retention locally. Automatic credential redaction does not
make transcripts or page contents suitable for public upload. Individual session
reviews are deliberately kept out of the public documentation.

The vision trace rotates at 1 MiB with two backups and does not record images,
questions or model observations. In `vision_inspect_finished`, `ready_ms` and
`prepared_ms` are elapsed milestones from the request's start; `model_ms` is the
model duration and `total_ms` covers the full inspection. `vision_inspect_error`
identifies the failed stage. `vision_stopped` distinguishes idle/session limits, a closed preview,
feed failures and session lock. Error tails are bounded to 2,048 characters per
child process. Empty or incomplete JPEG bodies with valid multipart framing may
be discarded until one second since the last usable frame (or reader startup),
also capped at 10,000 consecutive bad records. A stalled frame read has a separate
three-second timeout; framing errors stop capture immediately.
`vision_frame_dropped` and `vision_frames_recovered` describe each burst without
logging every empty record. No stale frame is submitted as a fresh observation.

## Latency

```toml
[network]
enabled = true
interval_seconds = 20.0
timeout_seconds = 5.0
```

The monitor uses control-frame pings on the existing voice socket. It does not
open another paid session or request model inference. Each connection records
`endpoint_host`; localhost measurements are not OpenAI latency. Older untagged
entries may include development tests and should not be used as WAN benchmarks.

For a local timing summary from a clone, run:

```sh
python3 tools/trace_summary.py /path/to/live-trace.jsonl
```

The summary includes session identifiers and error messages; review it before
sharing. It makes no API calls.

Inspect RTT alongside local loop lag, socket buffering, backend/tool durations,
and playback statistics. High RTT can include network delay, server buffering,
and local scheduling. A fast ping does not establish that model inference is
fast. First-output timing may measure filler speech rather than the final answer.
No single metric proves a root cause.

Live transcript state is coalesced and written outside the event loop. Live
actions wait for their journal receipt before executing; cancellation serializes pending
writes. Abrupt crashes may lose recent unflushed transcript fragments. Unknown
operation outcomes must be inspected rather than replayed automatically.

## Browser and worker recovery

`browser_window` events in the Live trace record creation, reuse and navigation.
`browser_finished` includes `windows_created` and `navigations`, making accidental
window accumulation measurable. URL-based research opens a normal browser window
with an address bar and navigates it in place. Later URL requests can reuse it
when no explicit window target was supplied, its last observed title, process,
address, class and workspace are unchanged, and it is on the current workspace.
An explicit target selects an existing window when no starting URL is supplied;
supplying a URL starts the research-window path. Unrelated windows are not
automatically closed or rearranged. New tiled windows still follow the
compositor's layout, and reuse is deliberately refused after an identity change.

Recognized Xwayland browsers use exact UTF-8 paste rather than the virtual-keyboard
text path. Plain-text clipboard data is restored, newer user copies are preserved,
and rich/binary clipboard contents are not overwritten. Verify the resulting field
before submitting it. Automated clipboard and targeting regressions live in
`tests/test_browser.py`, `tests/test_page_text.py`, and `tests/test_web.py`.

Browser workers retain bounded, timestamped page observations when interrupted.
Treat retained text as untrusted and potentially stale. A follow-up should preserve
useful evidence while stopping actions that are no longer authorized.

A background task marked `running` may be installing dependencies or awaiting a
model response. Inspect its phase and command receipts. A `blocked` task may already
have useful artifacts. Inspect them before resuming, and do not claim completion
until the task's acceptance checks have run. See [task workers](task-workers.md).

## Jev desktop navigation trials

`tools/bench_navigation.py` compares a pinned Jev decision model with the configured
trial OpenAI model on synthetic Omarchy requests. It never captures the desktop,
records audio, executes tools, or changes the running voice service. The default
is offline validation; network usage requires `--connect`.

The experimental catalogue in `src/omarchy_voice/navigation.py` covers 61 action
variants: numbered workspace switching, moving the focused window with or without
following, workspace history, named-window and directional focus, window cycling
and swapping, monitor focus and workspace movement, scratchpad, fullscreen and
floating toggles, split/pseudo layouts, groups, packaged resize steps, installed
application launches, default applications, panels, and navigation menus. It uses
existing tool names and literal command templates; Jev selects only action and
argument labels. Dispatcher and command availability are explicit inputs, not
assumed from a model's knowledge. Arbitrary application IDs and window addresses
from model output are never interpolated into commands.

This is a decision experiment, **not an enabled voice backend**. Workspaces 1–10,
group positions 1–5, and the packaged resize increments are currently enumerated.
Named nonfocused window mutations, arbitrary dimensions, free text, URLs,
conditional/compound requests, and interpretive tasks defer to GPT. A request to
set a particular fullscreen/floating state cannot safely become a toggle without
state evidence. Missing or uncertain arguments also defer.
`--representation slots` asks independent action and argument questions; the action and required arguments must pass the
threshold. `--representation candidates` expands templates into complete action/target
choices and asks Jev to select one. It gates selection certainty and a separate
whole-request eligibility question. More than 254 complete candidates is an explicit
error, never silent truncation. Confidence does not establish permission or correctness.

Run from the repository root:

```sh
# Validate the catalogue and development case count without reading credentials.
python3 tools/bench_navigation.py

# Explicit paid calls using only synthetic fixtures; no desktop operations.
python3 tools/bench_navigation.py --connect --provider jev \
  --split development --env-file .env \
  --output benchmarks/jev/development.json

# Interleave providers on paraphrases and requests that should defer.
python3 tools/bench_navigation.py --connect --provider both \
  --split held_out --repeat 2 --env-file .env \
  --output benchmarks/jev/comparison.json

# Confirm a calibrated complete-candidate strategy on a separate case set.
python3 tools/bench_navigation.py --connect --provider both \
  --representation candidates --threshold 0.8 --split confirmation --repeat 2 \
  --env-file .env --output benchmarks/jev/confirmation.json
```

The key names default to `JEV_API_KEY` and `OPENAI_API_KEY`; `--jev-key-env` can
select `TYPESAFE_API_KEY`. Only those keys are read from the specified file, which
is parsed as data and never executed. Results require a new file under ignored
`benchmarks/` or `docs/private/`, with owner-only file permissions. Do not commit or
upload reports. No key, request text, or provider error body is printed. Requests
use fixed HTTPS provider destinations, reusable connections, bounded responses,
and no automatic retries. A provider failure stops the run and retains partial
results. A run is limited to 200 requests, with a 15-second per-request timeout.
This bounds calls, not account-wide spend; backend model prices vary.

The report separates raw selection accuracy, accepted decisions, wrong accepted
actions, eligible commands correctly handled, and HTTP median/p95 latency. Unused
argument slots do not affect correctness or acceptance. Cold and reused-connection
samples are reported separately. Development cases intentionally cover catalogue
wording. The original `held_out` paraphrases and rejection cases were subsequently
used to calibrate the complete-candidate experiment, so they are now calibration
data. The separate `confirmation` set was authored after calibration and before
running the revised strategy. It includes 32 eligible commands and 16 requests
that should defer, without copying user speech. Once any set informs prompt or
threshold changes, create fresh cases before claiming generalization.

The OpenAI comparison uses one Responses decision with low reasoning, standard
processing, the same inventory and action/argument choices, and forced structured
function output. It is not the existing Live session's end-to-end latency. Neither
provider's measurements include speech completion, delegation, desktop execution,
verification, fallback work, or spoken confirmation. Faster decision requests alone
do not prove that the complete voice workflow is faster.

Before enabling execution, integrate through the existing Executor policy and
confirmation path, recheck target identity and active focus, discard stale or
cancelled decisions, and verify action outcomes. GPT-Live client delegation can
route supported commands to this path and interpretive work to GPT; the trial does
not alter the existing managed Responses delegation. Avoid inserting Jev ahead of
all the same OpenAI calls, which adds a round trip rather than replacing one.

References: [TypeSafe models](https://docs.typesafe.ai/models),
[TypeSafe function selection](https://docs.typesafe.ai/cookbooks/function_calling),
and [GPT-Live delegation](https://developers.openai.com/api/docs/guides/live-delegation).
