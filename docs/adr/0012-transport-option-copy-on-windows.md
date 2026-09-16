---
status: accepted
---

# Workspace transport is a backend option, copy by default on Windows

How a workspace gets into a sandbox is an option on the backend — `DockerSandbox(transport="auto"|"copy"|"bind")`, where `auto` means copy on Windows and bind on Linux and macOS — not a pluggable seam. The protocol promises only that every exec runs with its working directory at the workspace root — `/workspace` on container backends, a host temp directory under `NoSandbox` (narrowed in [wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7): a host path cannot promise `/workspace`) — and commits always leave via `git format-patch` on exec stdout.

Why not a seam: every mechanism is backend-specific (`docker cp`, `-v`, bwrap `--bind`, nothing) and the axis is ragged (NoSandbox has none, bwrap binds only), so a transport protocol would be an enum wearing a protocol's clothes. Promote it only if a backend-agnostic mechanism such as tar over stdin appears. (That trigger was met in [wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7) — copy became a git bundle streamed over exec stdin and cloned inside the sandbox as the image user, which works on any backend — and answered with a public helper, `waystation.sandbox.clone_in`, that copying backends call, not a seam: bind stays backend-specific and `NoSandbox` still has no transport, so the axis is as ragged as before.)

Why copy on Windows: a worktree's `.git` pointer holds an absolute Windows path that a Linux container reads as relative, so host and container can never both be valid; bind mounts arrive over 9p at roughly 60× the cost of overlayfs for git-shaped small-file writes; and shared files pick up root ownership and CRLF churn. All experiment-verified on Windows 11 with Docker Desktop.

## Considered options

- A `Transport` protocol.
- `git worktree repair` in-container (breaks the host), `GIT_DIR`/`GIT_WORK_TREE` overrides, sandcastle's overlay-mounted `.git` pointer.

Decided in [wayfinder tickets 5](https://github.com/jeffrichley/waystation/issues/5) and [9](https://github.com/jeffrichley/waystation/issues/9).
