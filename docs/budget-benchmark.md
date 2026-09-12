# $3 production optimization experiment — September 10–11, 2026

Accounted spend: **$1.694262 of $3**, leaving **$1.305738**. This includes 71 paid
Live sessions (35 synthetic, 35 real desktop, one installed-service smoke test)
and a conservative $0.06 allowance for three generated speech clips. Every Live
session received final usage and closed. There are no outstanding reservations.
This is an application-side estimate, not an invoice or an account-wide spend cap.

## What was measured

The same Live implementation was exercised with fixed requests derived from the
user's logs: parallel machine-health checks, shortcut discovery, two addressed
window closes, exact page reading, audio-panel recovery, Stocks plus Premier
League, AP/Reuters/NYT composition, spoken corrections, and close-two-then-open
replacement workflows. Real desktop tests were run after explicit authorization
for sending desktop context and tool readings to OpenAI.

We compared the original backend prompt against a compact candidate, Terra against
Luna, low against disabled reasoning, and standard against priority processing.
Synthetic tests fabricated all desktop data and tool outputs. Real desktop tests
used the production tools and actual browser windows, with isolated conversation
history. The final smoke test used the installed daemon, its actual saved history,
control socket, audio playback, and automatic session shutdown.

Backend completion is timed from request/delegation to the last backend response.
It excludes initial connection setup, spoken playback, and idle shutdown. Speech
tests stream generated PCM at conversational speed without recording the room.
They do not measure microphone acoustics or interruption over speaker playback.
The correction scenario deliberately delays one query for seven seconds so a
spoken correction can arrive while work is active. Negative first-action speech
latencies mean a tool began before the recorded sentence ended; they are not
negative network latency. First audio may be filler, not a useful result.

## Findings and decisions

| Measurement | Result | Decision |
| --- | --- | --- |
| Real health task, standard Terra | 3.580 s median completion, n=4 | Keep Terra and low reasoning as defaults |
| Same task requesting priority | 2.698 s median completion, n=3 | Expose explicit fast-tier configuration; retain standard pricing pending preference |
| Four local health reads | 36.35 ms serial vs 27.99 ms parallel, five batches each | Keep bounded parallel reads; model round trips dominate |
| Confirmed parallel window closes | 9.3–9.4 ms each in the post-fix trial | Verify removal before dependent launches |
| Close two fixtures, then launch Stocks | Both real trials passed, 5.384 and 6.134 s backend completion | Preserve close/open dependency ordering |
| Installed-service smoke test | Four parallel health reads, two backend responses, 20 billed voice seconds, final close | Installed runtime verified with microphone off |

The priority sample was roughly 25% faster at the median, but these are small,
non-randomized samples with variable caching and server load. They do not establish
an SLA or a p95. The server explicitly reported `service_tier=priority` in the
final verification run. Priority roughly doubles Terra backend token prices while
Live voice remains $0.05/minute; see [official pricing](https://developers.openai.com/api/docs/pricing).
The selected tier is now an explicit `[live] service_tier` setting (`default` or
`priority`), and returned tiers are included in performance logs.

The compact prompt reduced input tokens, and Luna was cheaper, but neither gave
a consistent real-desktop latency advantage. Disabling reasoning also failed to
produce a consistent improvement. Those experiments remain in the benchmark;
they were not promoted into the installed model or backend prompt.

## Fixes installed

Targeted closes now verify that the window leaves Hyprland's inventory before
returning success. Hyprland can briefly list a dying surface and then reuse its
address. Returning only a dispatch acknowledgement allowed a dependent launch's
initial inventory to include that dying address. The new check polls only while
needed, stops after 1.5 seconds, and reports an unresolved close rather than
continuing dependent actions through a possible unsaved-work dialog. Independent
addressed closes still run together.

Correction handoff instructions now reach the voice layer when an update starts,
and a current completed backend answer is explicitly marked for direct relay.
Tests ensure stale answers cannot mark the current update complete. **This is a
mitigation, not a complete duplicate-delegation fix:** each of the two three-run
real speech batches still contained one repeated time lookup. In all six trials,
the revised time request reached the backend and no memory query started after
the correction began. Already running reads can finish. The latest batch's
correction-to-backend-completion times were 5.828, 4.750 and 9.289 seconds, with the
slowest trial repeating the lookup. Suppressing all new delegations by a timeout
would risk dropping legitimate speech, so that shortcut was not introduced.

The Live API's [delegation documentation](https://developers.openai.com/api/docs/guides/live-delegation)
explains that voice and backend work continue independently and delegation events
do not carry the task text. A stronger single-owner correction design remains
future work; the results do not justify claiming that duplication is solved.

## Benchmark corrections and reproducibility

Early app/news scores exposed two benchmark flaws: browser class names can settle
after a launcher returns, and cleanup acknowledgements can precede window removal.
The harness now reconciles all requested hosts after the response, records before
and after inventories, and waits for its windows to disappear before another run.
Final app/news reruns passed for both configurations. Earlier Boolean scores are
not comparable to the corrected reruns. One early correction score also initially
counted an already-started read as a cancellation failure; the final criterion
checks that no superseded read starts after the spoken correction begins.

`tools/bench_desktop.py` records request-to-first-tool, completion, individual tool
timings, response counts, parallel calls, token usage, final session usage, and
outcomes. `--synthetic` fabricates data and actions; omitting it runs real desktop
tools. `--audio` streams the fixed health/correction clips. `--connect` is mandatory
for paid runners. `tools/bench_service.py` checks the actual installed service.
`tools/bench_report.py` reads the ledger without making API calls.

The owner-only ledger and detail files are under ignored `benchmarks/`. A process
lock prevents concurrent experiment runners from spending the same allowance.
Runs reserve conservative cost before connecting; uncertain final usage retains
that reservation, and unfinished entries block further spending. Unknown model
pricing is rejected. Live sessions are bounded to 45 seconds and six backend
responses, with an additional $0.10 safety margin. These controls apply to these
experiment runners, not unrelated account activity or ordinary user sessions.

The complete 460-test regression run passed, including new window-removal and
handoff cases. The eight budget tests also passed after an additional unknown-model
pricing guard was added. Existing Realtime mock coroutine warnings remain.
See [the measurement tables](benchmark-results.md) for grouped results.

## Rollback and current state

Installed source was backed up to
`~/.local/state/omarchy-voice/backups/pre-budget-20260911-001512.tar.gz`.
Git branch `codex/live-before-budget-bench-2026-09-10` preserves the previous source.
The running service uses Terra, low reasoning, and standard processing. The
microphone is off and the paid smoke-test session is closed. Ordinary Super+Shift+V
session control is unchanged. No experimental compact prompt or Luna migration
was enabled.
