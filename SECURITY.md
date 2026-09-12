# Security policy

OMA is a desktop automation agent. Use it only in a user account whose desktop,
files, applications, and connected accounts you intend to let an agent operate.
This project does not promise that arbitrary model-generated actions are safe.

## Reporting a vulnerability

If GitHub private vulnerability reporting is available, use the repository's
Security tab to report privately. Otherwise open an issue asking for a private
contact channel **without** including exploit details, credentials, transcripts,
or private files. Do not post production logs publicly. Include the affected
commit, a minimal synthetic reproduction, expected behavior, and impact through
the private channel. No response-time SLA is currently offered; fixes target the
latest source revision.

## Trust boundaries

- Voice audio is sent to OpenAI while listening is enabled. Tool results, selected
  page text, screenshots, and task inputs can also leave the machine. Muting stops
  recording; it does not cancel previously submitted background tasks.
- Desktop tools run with your user privileges. The regex deny/confirmation policy
  reduces mistakes; it is **not** a sandbox or a complete authorization system.
  Terminal commands, application launchers, typing, and browser actions can alter
  data. Disabling `allow_shell` hides the dedicated shell tool; it does not turn
  the rest of desktop automation into read-only access.
- The local control socket is in an owner-only directory with mode 600. Other
  processes running as the same user remain trusted; there is no same-user
  authentication boundary.
- Direct task commands use bubblewrap namespaces and a restricted filesystem.
  Host home directories, credentials, control sockets, and task metadata are not
  mounted. A private `/dev` replaces the host device tree; available NVIDIA compute
  devices and DRM render nodes are explicitly exposed for GPU experiments. GPU
  drivers and the host kernel remain part of the trusted computing base. This is
  not VM-grade isolation for actively hostile code.
- Task networking is off unless requested. When enabled it allows general outbound
  and local-network access, not a destination allowlist. Do not enable it for
  untrusted workloads on sensitive networks. Resource bounds include wall time,
  model-call counts for the direct worker, and logs; disk, RAM, and API dollar
  quotas are not fully enforced.
- The optional Codex provider uses its own workspace-write sandbox and local
  authentication. Its read permissions differ from the direct worker; do not
  assume it hides your home directory or private files. Agent-authored acceptance
  checks establish only what those checks actually test.
- Model input and web pages can contain prompt injection. Treat tool permissions
  and application sessions as the real authority; prompts alone cannot prevent
  unauthorized actions.

## Credentials and diagnostics

Keep keys in `~/.config/omarchy-voice/env` with mode 600, outside the repository.
Never commit environment files, private keys, copied browser profiles, model
checkpoints containing private data, or runtime logs. Rotate any credential that
has been exposed; deleting it in a later Git commit does not remove Git history.

Logs and saved conversation state are private data even after automatic credential
redaction. Redaction recognizes common token forms and configured credential-like
environment variables; it cannot identify every secret, personal detail, or
password spoken in natural language. Review and sanitize a copy before sharing.
Private reports belong under ignored `docs/private/`; runtime benchmark artifacts
belong under ignored `benchmarks/`.

## Contributor checks

Run `python3 tools/check_public_files.py --staged` before committing. CI scans
tracked files for common credential shapes and accidental local runtime artifacts.
This is a lightweight guard, not a comprehensive secret scanner. GitHub secret
scanning and push protection should remain enabled. Run the unit suite and inspect
any security-sensitive change to execution, file access, logging, or permissions.
