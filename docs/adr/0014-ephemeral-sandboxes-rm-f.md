---
status: stable
type: adr
---

# Sandboxes are ephemeral, torn down with `rm -f`, never auto-reaped

Every run gets a fresh sandbox — on Docker, `run -d` with an idle entrypoint, work through `exec`, teardown in `finally` with `docker rm -f`. Bare `docker stop` is never used: on a `sleep infinity` PID 1 it burns the full 10 s SIGTERM grace (measured 10.65 s against 1.1 s for `rm -f`). Every sandbox carries a `waystation.run-id=<id>` label so orphans are findable and an explicit reaper helper exists, but preflight never auto-reaps — it would kill a concurrent flow's sandboxes. Warm pools and `docker commit` snapshots were rejected: cold start is ~1.2 s, commit does not speed startup, and bind mounts are fixed at create time.

## Consequences

Long-lived or reusable sandboxes are out of scope for the public API; startup efficiency is handled internally.

Decided in [wayfinder tickets 5](https://github.com/jeffrichley/waystation/issues/5) and [10](https://github.com/jeffrichley/waystation/issues/10).
