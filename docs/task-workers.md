# General task workers

OMA can delegate coding, simulations, benchmarks, data analysis and experiments
to durable workers. The feature has no dataset, architecture, metric or experiment
type baked in. The spoken request supplies the goal and acceptance criteria;
the worker chooses an implementation, executes it and produces evidence.

Two providers use the same task records, artifacts and completion interface:

| Provider | Execution | Best use |
|---|---|---|
| `responses` (default) | A separate Responses conversation with direct file/process tools | OMA conducts the work itself, with explicit local execution receipts |
| `codex` | Installed Codex CLI in non-interactive mode with its workspace sandbox | Reuse an existing coding agent instead of operating its terminal UI |

Grok, Claude and OpenCode may be installed, but currently have no provider adapter.
OMA should say so when one is specifically requested rather than substitute a
different agent. New adapters belong behind the same worker contract; adding an
agent does not require a new scheduler, voice protocol or experiment framework.

**Using it**

Examples of requests:

- “Run a reproducible simulation comparing these three scheduling algorithms.
  Save the code and raw measurements, verify the results, and report back.”
- “Use Codex to build and test a small parser. Include malformed-input cases.”
- “Train three model sizes on a suitable image dataset. Measure CPU/GPU use,
  evaluate held-out accuracy, and save all results even if the target fails.”
- “How is the experiment going?” / “Cancel that experiment.”
- “Resume the blocked task after inspecting its existing output.”

The voice tools are `task_submit`, `task_list`, `task_status`, `task_read`,
`task_cancel`, and `task_resume`. A submission returns immediately with a stable
task ID and a new private workspace. Task status reports job IDs, exact exit
codes, log paths, progress, artifacts and the result of final checks. Reading
an artifact is bounded and supports byte offsets.

A request key identifies one logical submission. Retrying the same key and
specification returns the original task; reusing it with a different specification
is rejected. A genuinely new experiment uses a new key. Mute, workspace changes
and unrelated desktop instructions do not cancel workers. Explicit task cancellation
stops the worker's process group; the legacy `listen cancel` still controls pending
desktop work, so name the experiment when cancelling it by voice.

The CLI accepts JSON rather than shell-escaped prompts:

```json
{
  "request_key": "queue-simulation-01",
  "provider": "responses",
  "network": false,
  "goal": "Compare FIFO and shortest-job-first scheduling on a seeded synthetic workload. Save readable code, raw results and a short report.",
  "criteria": [
    "Both algorithms process the same seeded workload exactly once.",
    "Reported mean waiting times agree with independently recomputed values."
  ]
}
```

```sh
omarchy-voice task submit request.json
omarchy-voice task list
omarchy-voice task status TASK_ID
omarchy-voice task read TASK_ID report.md
omarchy-voice task cancel TASK_ID
omarchy-voice task resume TASK_ID --guidance 'Inspect prior outputs before continuing.'
```

The initial implementation starts from a fresh workspace. It does not give the
direct worker access to existing private project files. Put needed input files
in that workspace before resuming a stopped task, or describe public downloads
in a network-enabled request. Explicit input-import and live task-amendment tools
can be added later without changing the worker lifecycle.

**Lifecycle and persistence**

Tasks live under the private OMA state directory's `tasks` folder, or `[tasks] root`.
SQLite records the specification, attempt, provider, status, jobs, tool-call
receipts and undelivered notifications. Generated files are in a separate
`TASK_ID/workspace` directory. Credentials and task metadata are outside the
direct worker's execution filesystem.

Each task starts a separate systemd user service. Closing a voice connection or
restarting the voice daemon does not own or stop that service. Each attempt has
an exclusive worker lock and a separate service name. An unexpected exit becomes
`interrupted` when reconciled, never an automatic replay. Explicit resume keeps
the same ID and files, starts a new bounded attempt, and supplies previous job
receipts and instructions to inspect partial work. Completed tasks cannot resume;
submit a new key to rerun them.

States are `queued`, `running`, `validating`, `completed`, `failed`, `blocked`,
`cancelled` and `interrupted`. Capacity currently rejects excess active submissions
with a useful error rather than silently queuing them. Default concurrency is one
to avoid unintentional competing GPU workloads; it is configurable.

The worker continues its own program/tool loop after each command finishes. OMA's
Live watcher announces terminal outcomes, or sends a desktop notification when
muted. Realtime currently uses desktop notifications for these workers. Notices
are durable and acknowledged after submission to the output channel; acceptance
by Live is not proof the user heard the audio, and a crash during delivery can
cause a duplicate notice. Task status remains available regardless.

**Execution and verification**

The direct worker has six tools: `write_file`, `read_file`, `list_files`, `run`,
`progress`, and `finish`. File writes accept multiline source directly. `run`
uses an argument vector with a job receipt created before process start, separate
stdout/stderr logs, an exact exit code, and timeout/log limits. Its commands run
inside bubblewrap with only the workspace writable persistently. The user home,
desktop sockets, worker metadata and credentials are not mounted. Environment
variables are explicitly filtered before starting the sandbox. System runtime
files and explicitly selected GPU character devices are available; host disks,
input devices, and terminal devices are not mounted. Workspace file I/O rejects
symlink traversal using directory-relative file descriptors. See
[SECURITY.md](../SECURITY.md) for remaining isolation and resource-limit risks.

Network is off for task programs unless the submission enables it. Network-enabled
work may acquire the dependencies and public inputs needed for its goal. The
worker's model connection is separate from program networking. The runtime does
not preinstall an ML stack: workers must prepare an environment in their workspace
and perform a real device smoke test, or report a precise blocker. Long programs
must stay in the foreground; each command's descendant processes are cleaned up
when it exits or times out.

Codex runs with `--sandbox workspace-write`, `approval_policy="never"`, an explicit
working directory, ephemeral sessions, and user configuration disabled. It uses
the CLI's default model and existing local authentication. No approval/sandbox
bypass flag is used. Its native sandbox has different read access from the direct
worker's filesystem isolation; it is not the same privacy boundary. Provider
authentication, available model choices and native sandbox support must work on
the host. [Official non-interactive Codex documentation](https://learn.chatgpt.com/docs/non-interactive-mode)

Both providers return the same result structure: outcome, summary, artifact paths,
and a runnable argument vector for each zero-based acceptance criterion. The
supervisor re-runs those checks in the direct worker's sandbox and records their
exit status. `completed` requires all criteria represented, all checks passing,
and existing artifacts whose hashes remain unchanged through verification.
Failed checks produce `failed`, even if the agent claims success. Missing evidence
or an unsupported environment produces a useful incomplete outcome.

These are worker-executed, agent-authored checks. They are stronger than accepting
a prose claim but are not an independent scientific audit: a poorly designed
check can still miss leakage, an unsuitable dataset or a wrong metric. Save raw
measurements and use domain-specific validators where correctness matters.

The Responses adapter rejects an incomplete response or malformed call batch
before executing any call from that response. It permits one bounded attempt to
produce a smaller complete call and records incomplete details. Completed calls
have persisted IDs and argument fingerprints. Uncertain operations are not
automatically replayed. For the previously recorded Live handoff error, the voice
layer now invalidates the incomplete exchange while retaining the connection and
durable tasks; other protocol errors retain their existing pause behavior.

**Configuration and costs**

See `[tasks]` in `share/config.example.toml`. Defaults: enabled, Responses/Astra,
one active worker, 30 minutes per attempt, 24 model calls, 8,192 output tokens per
call, ten minutes per command and 8 MiB of combined command logs. Codex has the
wall-time/log limits but its native loop does not expose this adapter's model-call
limit. Each explicit resume creates a new bounded attempt.

These limits are not a dollar cap or a disk quota for generated artifacts. The
Responses worker has separate API usage from Live; Codex uses its configured
authentication/account. No task/model request starts at daemon boot. Usage totals
for completed Responses requests are saved; a terminated or failed HTTP request
can have unreported billed usage. The artifact/log directory persists until the
user removes it.

**Validation**

```sh
python3 -m unittest discover -s tests -p test_tasks.py -v
python3 tools/bench_tasks.py
python3 -m unittest discover -s tests
```

The task tests cover concurrent duplicate submissions, capacity, cancellation
races, restart reconciliation, resume, bounded file reads, path traversal,
uncertain-call receipts, incomplete responses, late results after cancellation,
and required verification. Real sandbox tests exercise numerical analysis, text
processing and a simulation with standard-library Python, plus failed assertions,
nonzero exits and timeouts.

`bench_tasks.py` launches real temporary systemd workers with a **fake coding CLI**
and real sandboxed verification. It checks client reconstruction while work runs,
idempotent submission, cancellation and resumption. It makes no API calls and
does not invoke an installed coding agent. Its output is saved in the private
benchmark directory. Nested execution sandboxes can prevent bubblewrap's network
namespace setup; run these checks in an environment that permits that feature,
not by weakening the worker's sandbox.

The initial verification establishes the toolkit mechanics. It does not establish
that a paid model will autonomously complete an arbitrary experiment, that provider
authentication succeeds, or that the original three-model GPU experiment passes.

Back up installed source before upgrading. To roll back, stop or cancel active
tasks using the current task CLI, restore the matching source version, and restart
the idle voice service. Preserve the task database and workspaces.

### Diagnostic phases and recovered results

Task status now includes the current `phase` (`model`, `command`, or a terminal
status), `current_job_id`, and bounded command descriptions with job timestamps.
A running worker does not necessarily mean training is underway. Model request
start/completion/error timings are in the task directory's `worker-trace.jsonl`.
A timeout preserves completed jobs and files; inspect those before resuming.

Voice connection diagnostics are configured in `[network]` and recorded in
`session.log`, `network-trace.jsonl`, and, for Live, `live-trace.jsonl`. See the
[diagnostics guide](diagnostics.md) for interpretation.
