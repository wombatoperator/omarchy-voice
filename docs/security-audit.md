# Security hardening and publication review

This review covered source, installers, local control, desktop dispatch, worker
isolation, workspace file operations, diagnostics, and files intended for GitHub.
It is a manual review with regression tests and lightweight credential scanning,
not a penetration-test certification or a guarantee that no vulnerabilities remain.

Fixed findings:

- **Executable Lua inside dispatcher arguments:** replaced permissive validation
  with a bounded literal-only parser. Nested functions, variable evaluation,
  concatenation, comments, and additional statements are rejected before dispatch.
- **Excess host device access:** task sandboxes now create a private device tree
  and bind only available GPU compute/render character devices. Host input devices,
  terminals, and disks are not inherited as a device directory.
- **Workspace symlink races:** reads, atomic writes, command logs, and artifact
  verification use directory-relative descriptors and refuse symlink traversal.
- **Control-socket denial of service:** malformed UTF-8 and idle clients no longer
  terminate or indefinitely occupy the server loop.
- **Credential propagation and logging:** generic desktop child commands omit
  credential-like environment variables; traces, session logs, and saved Live
  state redact recognized credential strings in free-form output as well as
  structured secret fields.
- **Destructive install paths:** installer/uninstaller reject root execution,
  dangerous prefixes, and unrelated nonempty target directories. Normal uninstall
  preserves runtime evidence and task artifacts; explicit purge removes them.
- **Public documentation privacy:** local conversation reviews moved to ignored
  `docs/private/`. Public documentation describes configuration, diagnostics, and
  limitations without production conversation identifiers or personal paths.
- **Publication checks:** a pinned, read-only GitHub Actions workflow scans indexed
  source for common credential patterns/private artifacts and runs security tests.

The initial credential-pattern scan found no exposed credentials in publishable
working files or the 23 local commits examined. These patterns are incomplete;
review findings and enable GitHub's secret scanning/push protection. If a secret
is later found in history, revoke it first and coordinate history cleanup.
Local development commits contain conversation reviews. The publication branch
is based on the existing public main branch with the reviewed snapshot applied,
so those unpublished development commits are not introduced into public history.
The original local development history is preserved.

Read [SECURITY.md](../SECURITY.md) for the threat model, privacy boundaries, residual
risks, and private disclosure process. In particular, desktop actions remain
user-privileged, network-enabled workers are not destination-filtered, GPU/kernel
isolation is not equivalent to a VM, and the optional external provider has a
different filesystem read boundary.

Validation: **583 tests passed**, including security regressions. A real CUDA
smoke check also verified that the private device tree retains GPU availability
while host input and disk device paths are absent. No paid API calls were used.
