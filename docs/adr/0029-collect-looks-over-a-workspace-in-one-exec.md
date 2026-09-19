---
status: accepted
---

# Collect looks a workspace over in one exec, through git's own shell

Collect's read-only checks and its series are one exec: `git status --porcelain` when salvaging, `rev-list --merges`, `merge-base --is-ancestor`, then `format-patch --stdout`. It runs them as a script in a git alias passed on the command line, `git -c 'alias.waystation-collect=!<script>' waystation-collect`. The script's first line of stdout is its verdict: `dirty`, `nonlinear` or `linear`. Anything after `linear` is `format-patch`'s output. A dirty workspace is salvaged exactly as before, with its `add` and `commit` and the private-index fallback, and then looked over again. A nonlinear one is squashed exactly as before.

Why one exec: on `DockerSandbox` every exec is a `docker exec`. Measured on Docker Desktop for Windows (28.0.4), a whole run of a scripted agent took 3.6 s. Collect's four execs were 0.82 s of that, and one exec doing the same work takes 0.21 s. The research put an exec at 0.5 s on that stack (docs/research/docker-startup-efficiency.md §2.4), which would make the saving 1.5 s. Ticket #37 asked for docker calls batched where possible, and collect was the one place left unbatched.

Why a git alias and not `sh -c`: collect runs over the `Sandbox` protocol, and `NoSandbox` runs it on the host. On Windows, Git's default install puts only `Git\cmd` on `PATH`, so there is no `sh` to name. Windows also finds a program on the host's `PATH`, whatever `PATH` the sandbox is given. Git runs a `!` alias with the shell it was built with: `/bin/sh` on Linux, and its own bundled `sh` on Windows, found wherever git is installed. So the script needs git and nothing else, which is all collect needed before.

Why the failure mapping survives: before each step, the script writes a line to stderr naming it. When the script fails, it exits with the failing git's code. Collect finds the last of those lines and raises `StageError("collect", CommandFailed)` with that step's argv, its exit code, and the stderr written after its line. That is the same failure one exec per step gave. If a failure happens before any step has started, say git could not run its shell, the failure names the whole survey command. As before, any `merge-base` exit other than 0 counts as nonlinear, not as a failure.

## Considered options

- **Leave collect alone.** Rejected: collect was roughly a quarter of a trivial run on Docker Desktop, the largest fixed cost after `docker run` and `docker rm -f`.
- **`sh -c` with the script.** Rejected: `NoSandbox` would stop collecting on a Windows host whose `PATH` has only `Git\cmd`, and this machine's does.
- **Find `sh` the way `ScriptedAgent` does, beside git.** Rejected: collect cannot tell which backend it is talking to, and a host path means nothing inside a container.
- **Batch inside `DockerSandbox`, or add an `exec_many` to the protocol.** Rejected: collect would get a path only Docker has, which is a privileged internal path. A protocol method for one caller is the kind of change ADR-0028 already turned down for bytes stdin (ADR-0010).
- **Plain git: `rev-list --left-right --parents base...HEAD`.** This answers both the merges question and the ancestry question in one command. Rejected as the whole answer: it saves one exec of four, and neither `status` nor `format-patch` can be folded into it.
- **Delimited sections on stdout.** Rejected: the only stdout the script needs is the verdict, and `format-patch` after it. The checks test their output inside the script, so there is nothing to delimit. A delimiter would also be something a patch could contain.
- **Fold salvage and the squash into the script too.** Rejected: the salvage fallback reports the fallback's failure, not the first one, and writing that again in sh would put a second copy of the logic beside the Python. Both are the uncommon paths anyway.

## Consequences

- A committed run's collect is one exec, down from four. A salvaged run's is four, down from six. A nonlinear run pays one exec to find that out, then the squash's own execs as before.
- `NoSandbox` on a Windows host runs a little slower: Git for Windows' `sh`, and the gits it starts, took collect from about 0.15 s to about 0.27 s. That is the price of keeping one path for every backend.
- A sandbox needs a git whose `!` aliases run, which every git build has. `DockerSandbox` already required `sh` in the image.
- The survey's command line carries its script, and ADR-0025's redactor reads `out=$(git` as an assignment. The logged line shows `out=***` where the script captured output. The steps' own lines on stderr, logged at DEBUG, say what ran. `CommandFailed` carries the failing step's argv, which has no `=`.
- `format-patch`'s output reaches collect through the same exec stdout as before, after one verdict line. `test_patch_bytes.py` holds this, CRLF content included.
- A cancelled survey is killed like any exec (ADR-0023). Its `sh` and gits are in the exec's process tree, and in its process group in a container.
- The tests' cancellation gate held collect at `git format-patch`, which a committed run no longer runs alone. It now holds collect's first git exec, since the tests' agents are shell scripts.

Decided on the [#37](https://github.com/jeffrichley/waystation/issues/37) follow-up, in [#67](https://github.com/jeffrichley/waystation/pull/67).
