---
type: adr
status: stable
supersedes: adr/0012-transport-option-copy-on-windows
---

# Workspace transport is a backend option, and `auto` binds only on Linux

How a workspace gets into a sandbox stays an option on the backend, `DockerSandbox(transport="auto"|"copy"|"bind")`, not a pluggable seam, for every reason ADR-0012 gave. The protocol still promises only that every exec runs with its working directory at the workspace root, and commits still leave via `git format-patch` on exec stdout. What changes is `auto`: it **binds on Linux and copies everywhere else**, macOS included. `"bind"` stays available on every host for anyone who asks for it by name.

Why: only a Linux bind keeps the host's owner. Docker Desktop shows a bound `/workspace` as root's, whatever the files inside it belong to. On Windows that is the 9p share ADR-0028 already recorded; on macOS it is the virtiofs share (checked on Docker Desktop with engine 29.8, kernel 7.0.12-linuxkit). Git in the image's non-root user then refuses the repository as dubious ownership, and the run fails at collect after the agent has done its work. On macOS it is intermittent: single runs often got through, while a three-run batch failed at collect on every run, and the docker tier failed about six bind tests per run. A failure that depends on timing is worse than one that always happens, because it passes the first try and fails in production. Copy has no foreign owner to refuse: it clones into a `/workspace` the image's own user owns (ADR-0028), and it was already built and tested on every host. With `auto` copying, the docker tier passed three runs in a row on macOS, where it had never passed.

## Considered options

- **`safe.directory` for `/workspace`**, passed to git inside the sandbox. Rejected: the library would be switching off a git safety check on the user's behalf. ADR-0028 rejected it for Windows too, for CRLF churn.
- **Chase the virtiofs behaviour**: find when the mount root shows its real owner, and bind only then. Rejected: it is Docker Desktop's behaviour to change, not ours, and it would still be intermittent.
- **Bind on macOS and document the failure.** Rejected: the default would fail in a way that looks like the agent's fault.

## Consequences

Every macOS run now spends one clone of the workspace into the container, where it spent a mount before. ADR-0028 measures that as one exec, and a `--local` clone on the host was already made for every transport. A `workspace_ready` hook's uncommitted writes no longer reach a macOS sandbox, as they already did not on Windows (ADR-0028): only commits travel. The docker tier runs its bind cases only on Linux, where CI runs them.

Supersedes [ADR-0012](0012-transport-option-copy-on-windows.md), whose decision this restates with the new default. Decided in [#135](https://github.com/jeffrichley/waystation/issues/135).
