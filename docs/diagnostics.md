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

Automatic detail inspection emits `vision_inspection_selected` with the normalized
crop, rotation, enhancement preset and original frame sequence. Each
`vision_model_finished` records its step, timing and usage; `vision_inspect_finished`
includes `model_calls` and `detail_image_bytes`. The two steps share one inference
timeout. `vision_detail_preview_unavailable` means analysis continued with the
live preview because the labeled snapshot could not be rendered. No images,
questions or answer text are added to these events.

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

### Experimental Jev navigation replay

[`tools/eval_navigation.py`](../tools/eval_navigation.py) evaluates explicitly
selected requests with TypeSafe's Jev model. It is a development experiment and
does not change either voice engine or execute desktop actions. Jev selects
among supplied candidates; the application would still need to discover current
windows/controls, construct complete actions, apply the existing policy gate,
verify outcomes, and fall back to its planner.

The [TypeSafe quickstart](https://docs.typesafe.ai/introduction/quickstart),
[function-calling cookbook](https://docs.typesafe.ai/cookbooks/function_calling),
and [confidence guide](https://docs.typesafe.ai/confidence) describe this model.
It cannot generate arbitrary tool arguments, search queries, shell commands,
or spoken responses. Confidence is derived from the option distribution and
is not a guarantee that a proposed action is correct.

Create a reviewed JSON array under ignored `benchmarks/` or `docs/private/`.
This example is synthetic:

```json
[
  {
    "state": {"request": "Show workspace two"},
    "candidates": {
      "workspace_2": "Switch to workspace 2; do nothing else.",
      "workspace_3": "Switch to workspace 3; do nothing else.",
      "fallback": "No single listed action fully satisfies the request."
    },
    "expected": "workspace_2"
  }
]
```

Each case must include `fallback`. Put only necessary evidence in `state`, such
as the completed request and observed window/control descriptions. Label cases
before evaluation; the `expected` label is never sent to the model. Reconstruct
transcript fragments in session/utterance order and review their boundaries.
Missing historical desktop state cannot be repaired by inventing a target and
then treating it as ground truth. Include ambiguous targets, missing candidates,
corrections, compound requests, and instruction-like page text.

```sh
# Validate locally; no API calls and no credential file read.
python3 tools/eval_navigation.py benchmarks/navigation-cases.json

# Explicit paid probe: sends the selected cases to TypeSafe.
# Configure JEV_API_KEY privately; --key-env TYPESAFE_API_KEY is also supported.
python3 tools/eval_navigation.py benchmarks/navigation-cases.json \
  --connect --env-file ~/.config/omarchy-voice/env --limit 20 \
  --output benchmarks/navigation-results.json
```

Never commit case files, historical transcripts, or generated reports. Obtain
permission before sending private historical inputs to an additional provider.
The tool sends a Choice and an independent Noul eligibility question in one
request. It validates distributions and accepts a shadow decision only when
selection probability, confidence, and eligibility all reach `--threshold`
(initially 0.90). This threshold is experimental, not calibrated for OMA.
Invalid output and transport errors stop the run without retries. Requests use
a fixed HTTPS endpoint, reuse the connection, and never follow redirects. The
default socket timeout is five seconds; it is not an end-to-end deadline.
`--limit` and `--repeat` cap each invocation at 100 calls; they are not dollar caps.

Reports are created without overwriting existing files, with owner-only file
permissions inside an ignored directory. They contain numeric case indices,
timings, decision scores, returned model identifiers when available, and token
usage, but no request text or raw provider bodies. Keep the reviewed case file
locally to interpret those indices. `jev-latest` is mutable; pin `--model` to a
provider-supported version when comparing runs.

Compare raw selection accuracy, accepted coverage, wrong accepted decisions,
fallback accuracy, and cold/warm median and p95 request latency. An error is not
counted as a correct fallback. High raw accuracy with every request falling back
does not produce a speedup. Use separate development and held-out cases when
choosing thresholds. Repeated requests are not independent accuracy samples.
Historical backend timings are context only: a fair speed comparison needs the
same inputs and candidate set, plus discovery, execution, verification, fallback,
and speech costs. Measure end-of-speech to first correct action before enabling
any runtime routing.

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
