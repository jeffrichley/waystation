---
status: stable
type: adr
---

# Cancelling an exec kills its process tree inside the sandbox

The sandbox protocol's `exec` carries a cancellation contract: when the awaiting task is cancelled — a silence or wall bound firing, `completion_grace` running out, a signal, a fan-out left early — the backend terminates the exec's whole process tree inside the sandbox before the cancellation completes. Docker starts each exec in its own process group and kills that group with a second `docker exec`; `NoSandbox` kills the process group on POSIX and the Job Object on Windows. Collect therefore always runs against a workspace nothing is still writing to.

**Amended by [ADR-0042](0042-nothing-escapes-the-job-and-a-kill-is-not-waited-on-for-ever.md).** "Kills the Job Object on Windows" left out *when* the job has to exist: adopting a process after it was already running let its descendants start outside the job, survive the kill, and hold the pipe that a cancelled exec was waiting to close. The child is created suspended and resumed only once it is in the job, and the wait after a kill is bounded rather than endless.

Why: cancelling the `docker exec` client leaves the process inside the container running. Sandcastle accepts that and lets teardown kill the agent, so its salvage races a live agent — it can commit half-written files, and an agent holding `index.lock` makes salvage's `git add` fail, losing the series ADR-0016 promises to preserve.

## Considered options

- Sandcastle's shape: cancel the client only; the agent dies at `rm -f` teardown.
- Killing only the exec's direct child. Rejected: agents spawn shells, MCP servers and build tools that keep writing.

## Consequences

On Docker the group is named by a pid the container knows and the host does not. Docker starts every exec as a session leader, so `DockerSandbox` runs each one under a small `sh` that records its own pid in `/tmp` under a name the exec is given, then `exec`s the argv, taking it as arguments and never as script text. The kill exec marks that name killed before it reads the pid, and the wrapper writes the pid before it looks for the mark. Whichever reaches the container first, the other sees it, so a kill that beats the exec's start still lands, and the exec never begins. The image therefore needs a writable `/tmp`; `--read-only` wants `--tmpfs /tmp` beside it. A kill that fails is logged at ERROR, and `rm -f` at teardown still ends what it missed. The kill itself runs to its end, however many cancellations arrive meanwhile.

Decided in [wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7). How Docker names and kills the group was settled in [#38](https://github.com/jeffrichley/waystation/issues/38).
