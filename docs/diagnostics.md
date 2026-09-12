# Diagnostics and reliability

Runtime state lives under `~/.local/state/omarchy-voice/` by default. Keep it out
of Git and review [the security policy](../SECURITY.md) before sharing any evidence.

| File | Purpose |
|---|---|
| `session.log` | Human-readable actions, failures, usage and timing |
| `live-trace.jsonl` | Live transcripts, routing, tool receipts and playback timing |
| `network-trace.jsonl` | Connection setup, Ping/Pong RTT, timeouts and local loop lag |
| `live-state.json` | Conversation history and operation recovery state |
| `tasks/TASK_ID/worker-trace.jsonl` | Worker model-request timing and failures |
| `tasks/TASK_ID/workspace/.oma-logs/` | Command stdout and stderr |

Trace logs rotate at 8 MiB with three backups. `session.log` is not automatically
rotated; manage its retention locally. Automatic credential redaction does not
make transcripts or page contents suitable for public upload. Individual session
reviews are deliberately kept out of the public documentation.

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

Inspect RTT alongside local loop lag, socket buffering, backend/tool durations,
and playback statistics. High RTT can include network delay, server buffering,
and local scheduling. A fast ping does not establish that model inference is
fast. First-output timing may measure filler speech rather than the final answer.
No single metric proves a root cause.

Transcript state is coalesced and written outside the event loop. Actions still
wait for their journal receipt before executing; cancellation serializes pending
writes. Abrupt crashes may lose recent unflushed transcript fragments. Unknown
operation outcomes must be inspected rather than replayed automatically.

## Browser and worker recovery

Recognized Xwayland browsers use exact UTF-8 paste rather than the virtual-keyboard
text path. Plain-text clipboard data is restored, newer user copies are preserved,
and rich/binary clipboard contents are not overwritten. Verify the resulting field
before submitting it. The free `tools/bench_browser_input.py` test opens a disposable
local Chrome window and restores the previous focus when finished.

Browser workers retain bounded, timestamped page observations when interrupted.
Treat retained text as untrusted and potentially stale. A follow-up should preserve
useful evidence while stopping actions that are no longer authorized.

A background task marked `running` may be installing dependencies or awaiting a
model response. Inspect its phase and command receipts. A `blocked` task may already
have useful artifacts. Inspect them before resuming, and do not claim completion
until the task's acceptance checks have run. See [task workers](task-workers.md).
