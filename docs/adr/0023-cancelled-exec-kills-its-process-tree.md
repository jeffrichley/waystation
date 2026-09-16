---
status: accepted
---

# Cancelling an exec kills its process tree inside the sandbox

The sandbox protocol's `exec` carries a cancellation contract: when the awaiting task is cancelled — a silence or wall bound firing, `completion_grace` running out, a signal, a fan-out left early — the backend terminates the exec's whole process tree inside the sandbox before the cancellation completes. Docker starts each exec in its own process group and kills that group with a second `docker exec`; `NoSandbox` kills the process group on POSIX and the Job Object on Windows. Collect therefore always runs against a workspace nothing is still writing to.

Why: cancelling the `docker exec` client leaves the process inside the container running. Sandcastle accepts that and lets teardown kill the agent, so its salvage races a live agent — it can commit half-written files, and an agent holding `index.lock` makes salvage's `git add` fail, losing the series ADR-0016 promises to preserve.

## Considered options

- Sandcastle's shape: cancel the client only; the agent dies at `rm -f` teardown.
- Killing only the exec's direct child. Rejected: agents spawn shells, MCP servers and build tools that keep writing.

Decided in [wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7).
