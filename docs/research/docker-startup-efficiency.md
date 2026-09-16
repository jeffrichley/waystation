# Docker startup efficiency for ephemeral agent sandboxes

Research for [waystation#10](https://github.com/jeffrichley/waystation/issues/10).

**Question.** Sandboxes are ephemeral (created per run, destroyed after), but startup must
be efficient. What are the facts on fast Docker cold-starts for an agent container
(node + Claude Code + git), and what patterns can hide behind an ephemeral-sandbox API?

**Methodology.** Claims below are sourced from (a) official Docker documentation,
(b) the [mattpocock/sandcastle](https://github.com/mattpocock/sandcastle) source at commit
`e99f832` (2026-06-29), and (c) local measurements taken 2026-09-15 on Docker Desktop
4.40.0 (engine 28.0.4, linux/amd64) on Windows 11 + WSL2 — the exact environment waystation
targets. The benchmark image was built from sandcastle's Claude Code reference Dockerfile
(`node:22-bookworm` + git/curl/jq + Claude Code CLI, `ENTRYPOINT ["sleep","infinity"]`,
final size 1.37 GB). Timings are wall-clock around the `docker` CLI call; each includes
roughly 0.5 s of fixed Windows-client-to-WSL2-daemon round-trip overhead (see §2.4).

## TL;DR — recommended default

1. **Pre-build one image per flow, reuse it across every run.** Build is the only
   expensive step (~47 s cold, ~1 s fully cached). Never build or pull on the run path;
   fail fast with "image missing — build it first", exactly as sandcastle does.
2. **Start per run with a single `docker run -d` of a long-lived idle process, then
   `docker exec` the work in.** Cold container ready-to-work in **~1.2–1.3 s** measured.
   There is no gain from `create`+`start` split (same total).
3. **Tear down with `docker rm -f` (~1.1 s), never bare `docker stop`.** With
   `sleep infinity` as PID 1, `docker stop`'s default 10 s SIGTERM grace always expires:
   measured **10.65 s** per teardown. This is a real trap sandcastle's `removeContainer`
   falls into.
4. Warm pools and `docker commit` snapshotting are compatible with the ephemeral API but
   **not worth it at ~1.2 s cold-start** — keep them as future options behind the interface.

## 1. Image strategy

### 1.1 Layer caching and ordering

Docker rebuilds a layer when its instruction (or, for `COPY`/`ADD`, the copied files'
checksums) changes, and "if a layer changes, all other layers that come after it are also
affected" — everything downstream rebuilds ([Docker docs: build cache][cache]).
Consequently the docs recommend putting expensive, stable steps first: "try to make
expensive steps appear near the beginning of the Dockerfile" and frequently-changing steps
(project files, config) last ([Docker docs: cache optimization][cache-opt]). For an agent
image the natural order is:

```
FROM node:<ver>            # pulled once, shared across all local images on that base
RUN apt-get ... git ...    # OS deps — changes ~never
RUN install claude-code    # agent CLI — changes on version bump
COPY / per-flow config     # changes most often — keep last
```

Other cache levers from the same docs: keep the build context minimal via
`.dockerignore`, and use `RUN --mount=type=cache` for package-manager caches so version
bumps re-download only deltas ([Docker docs: cache optimization][cache-opt]).

Measured effect (local, Dockerfile above):

| Build scenario | Time |
|---|---|
| Cold `--no-cache` build, incl. pulling `node:22-bookworm` (~400 MB compressed) | **47.4 s** (~23 s of that was the base-image pull) |
| Rebuild, fully cached | **1.1 s** |
| Rebuild after appending a trailing `ENV` layer | **1.0 s** |

So the image is the *durable warm artifact*: pay ~1 min once per machine (or per
agent-version bump), and every later "is the image up to date?" check is ~1 s.

### 1.2 Pull policy

`docker run --pull` accepts `missing` (default), `always`, `never`: by default Docker
pulls "if it was not found in the image cache" ([Docker docs: docker run][run]). For a
locally-built image there is nothing to pull — the risk is the opposite: a typo'd or
missing image name triggers a slow, doomed registry lookup. Two clean options:

- pre-flight `docker image inspect <img>` and fail with a build hint (sandcastle's
  choice, see §4); or
- pass `--pull=never` so `docker run` errors immediately instead of hitting the registry.

### 1.3 Pre-pull / pre-build

Because base-image pull dominated the cold build (~23 s of 47 s), any "setup" or "doctor"
command that runs `docker build` ahead of first use removes the entire multi-second cost
from the first real run. There is no daemon-side scheduled pre-pull in plain Docker; it is
just "run the build/pull early, from a CLI step or CI" — which is what sandcastle
formalizes as `sandcastle docker build-image` / an opt-in build during `init`.

## 2. Container lifecycle costs (measured)

All numbers: Docker Desktop 4.40.0 / engine 28.0.4 on Windows 11 + WSL2, 1.37 GB local
image, `sleep infinity` entrypoint, 3–5 reps each.

### 2.1 Startup

| Operation | Measured |
|---|---|
| `docker run -d` (returns once container is running) | 0.6–1.0 s |
| `docker exec <c> echo hi` (container already running) | 0.5–0.7 s |
| **`docker run -d` + first `exec` (cold sandbox, ready to work)** | **1.2–1.3 s** |
| `docker create` | 0.5 s |
| `docker start` | 0.7–0.8 s |

`docker create` "creates a writeable container layer over the specified image and
prepares it for running the specified command" without starting it; it is "similar to
`docker run -d` except the container is never started", useful "when you want to set up a
container configuration ahead of time so that it's ready to start when you need it"
([Docker docs: docker create][create]). Measured, the split buys nothing on this stack
(0.5 s + 0.8 s ≈ the 0.9 s combined `run`), because each extra CLI call costs ~0.5 s of
round-trip (§2.4). Pre-`create`-ing at most saves the ~0.5 s create half — only worth it
inside a warm-pool design (§3.1).

### 2.2 Teardown

| Operation | Measured |
|---|---|
| `docker stop` (default grace) on `sleep infinity` PID 1 | **10.65 s** |
| `docker stop -t 0` | 1.0 s |
| `docker rm -f` (running container) | **1.1 s** |
| `docker kill` then `docker rm` | 0.9 s + 0.4 s |
| `--rm` auto-remove observed complete after `docker kill` | 1.4 s |

Why the 10.65 s: `docker stop` sends SIGTERM, and "if the container doesn't stop within
the grace period" — default "10 seconds for Linux containers" — it sends SIGKILL
([Docker docs: docker stop][stop]). `sleep` as PID 1 has no SIGTERM handler (and PID 1
gets no kernel default handlers), so the grace period always runs out. Any
idle-entrypoint sandbox hits this unless teardown uses `-t 0`, `kill`, or `rm -f`
(`rm --force`: the main process "will receive SIGKILL, then the container will be
removed" — [Docker docs: docker rm][rm]). Since an ephemeral sandbox has nothing to shut
down gracefully — results live on bind mounts — **`docker rm -f` is the right teardown**.

### 2.3 `--rm` semantics

`--rm` makes the daemon "automatically remove the container and its associated anonymous
volumes when it exits"; named volumes are kept, and combining `--rm` with `--restart`
errors ([Docker docs: docker run][run]). It is a daemon-side guarantee — cleanup happens
even if the orchestrating process dies — which makes it good leak insurance for an
ephemeral API. Caveats: the container's filesystem (logs, diagnostics) is gone the moment
the entrypoint exits, and the orchestrator still needs `rm -f`-style code for the "kill a
still-running sandbox" path. Belt-and-braces: `--rm` *plus* an explicit `rm -f` on close
(the second call is a no-op race, ignore its error).

### 2.4 Fixed per-command overhead on Docker Desktop (Windows/WSL2)

Every `docker` CLI call measured — even `docker exec <c> echo hi` at 0.5 s and
`docker run --rm alpine true` at 1.1 s — carries roughly 0.5 s that is not container
work: Windows client → named pipe → WSL2 VM daemon round-trip. Two implications:

- batching matters more than on native Linux: one `exec` running a shell script beats
  five small `exec`s (~2.5 s of pure overhead);
- sub-second "startup" claims from native-Linux benchmarks do not transfer;
  on this stack the practical floor for run+exec is ~1 s.

## 3. Patterns that hide behind an ephemeral-sandbox API

The public contract "create → use → destroy" doesn't constrain what the provider does
underneath. Options, in order of increasing complexity:

### 3.1 Warm pool

Keep N containers pre-`run` (or pre-`create`d, per [Docker docs: docker create][create])
against the flow's image; `create_sandbox()` pops one and `docker exec`s into it,
`destroy()` removes it and tops the pool back up in the background. Saves the 0.9 s
run-cost, at the price of: reset guarantees between users of a pooled container (an
ephemeral API promises a *fresh* filesystem — a popped container must never have been
used), bind-mounts being fixed at create time (a pooled container can't mount a
run-specific worktree; this alone rules pools out for bind-mount sandboxes), and leaked
containers when the orchestrator crashes. Verdict: not worth it to save ~1 s; revisit
only if a future isolated-mode (no bind mounts, `docker cp` in/out) needs sub-second starts.

### 3.2 Snapshot a warmed container with `docker commit`

`docker commit` creates "a new image from a container's changes"; the container "will be
paused while the image is committed" by default, and "commits do not include any data
contained in mounted volumes" ([Docker docs: docker commit][commit]). Measured: commit of
a warmed 1.37 GB container took 0.5 s, and `docker run -d` from the committed image took
0.66 s — identical to the base image. So commit does **not** make startup faster; its use
is capturing *state* produced at runtime (e.g. run `npm install` or agent first-run setup
once in a throwaway container, commit, start all future sandboxes from the warmed image).
The cleaner equivalent for anything expressible as a command is an extra `RUN` layer in
the Dockerfile — reproducible and cache-friendly — so commit is a niche tool for
state that's awkward to script.

### 3.3 One container per flow vs per run

Per-flow (sandcastle's model, §4): container starts once, every agent turn is a
`docker exec`, teardown at flow end. Follow-up cost within a flow drops from ~1.2 s to
~0.5 s (one exec). Per-run: stronger isolation (each run gets a fresh filesystem), ~1.2 s
each. Given the measured gap is ~0.7 s, choose per-run (simpler invariants, no
cross-run contamination) and treat per-flow reuse as an optimization the provider could
adopt later without changing the API — the API never exposed the container identity.

## 4. How sandcastle's Docker provider does it

Source: [`src/sandboxes/docker.ts`][sc-docker], [`src/DockerLifecycle.ts`][sc-lifecycle],
[`src/InitService.ts`][sc-init], [`src/cli.ts`][sc-cli] at `e99f832`.

- **Image is pre-built, never built or pulled at run time.** `docker()` pre-flights
  `docker image inspect <img>`; on failure it throws
  `"Image '<name>' not found locally. Build it first with 'sandcastle docker build-image'."`
  (`docker.ts`, `checkImageUid`). The build happens in `sandcastle init` (optional
  prompt) or the explicit `build-image` command, which runs plain
  `docker build -t <img> .sandcastle/` with UID/GID build-args and default layer caching —
  no `--pull`, no `--no-cache` (`DockerLifecycle.ts` `buildImage`, `cli.ts`).
- **Default image name is derived from the repo directory**: `sandcastle:<sanitized-dir>`
  (`mountUtils.ts` `defaultImageName`) — one image per project, reused across all runs.
- **Reference Dockerfile** (Claude Code variant, `InitService.ts`): `FROM node:22-bookworm`;
  `apt-get install git curl jq`; rename the base image's `node` user to `agent` with
  host-matching UID/GID build-args (so bind-mounted files share an owner "without runtime
  chown"); install Claude Code via `curl -fsSL https://claude.ai/install.sh | bash`;
  `ENTRYPOINT ["sleep", "infinity"]`.
- **Lifecycle**: one container per sandbox via a single
  `docker run -d --name sandcastle-<uuid> ...` (create+start combined, detached, **no**
  `--rm`), with bind mounts for the worktree; all agent work goes through `docker exec`
  (`DockerLifecycle.ts` `startContainer`, `docker.ts` `exec`).
- **Teardown**: `close()` runs `docker stop` then `docker rm`
  (`DockerLifecycle.ts` `removeContainer`) — which, per §2.2, eats the full 10 s SIGTERM
  grace against the `sleep infinity` entrypoint. Its crash-path cleanup (a process-exit
  shutdown registry) uses `docker rm -f`, the fast path (`docker.ts`).

Takeaways for waystation: copy the pre-built-image + fail-fast + `run -d`/`exec` shape;
fix the teardown (`rm -f` everywhere); keep `--rm` off only if post-mortem inspection of
dead sandboxes is wanted, otherwise add it as leak insurance.

## Sources

- [Docker docs — `docker container run` reference][run] (`--rm`, `--pull`, `-d`)
- [Docker docs — `docker container create` reference][create]
- [Docker docs — `docker container stop` reference][stop] (SIGTERM, 10 s default grace)
- [Docker docs — `docker container rm` reference][rm] (`--force` sends SIGKILL)
- [Docker docs — `docker container commit` reference][commit]
- [Docker docs — build cache][cache] and [cache optimization][cache-opt]
- [sandcastle `src/sandboxes/docker.ts`][sc-docker], [`src/DockerLifecycle.ts`][sc-lifecycle],
  [`src/InitService.ts`][sc-init], [`src/cli.ts`][sc-cli] @ `e99f832`
- Local measurements, 2026-09-15: Docker Desktop 4.40.0, engine 28.0.4 (linux/amd64),
  Windows 11 + WSL2; benchmark script and raw output preserved in the session scratchpad
  (methodology described above; numbers in §1.1, §2).

[run]: https://docs.docker.com/reference/cli/docker/container/run/
[create]: https://docs.docker.com/reference/cli/docker/container/create/
[stop]: https://docs.docker.com/reference/cli/docker/container/stop/
[rm]: https://docs.docker.com/reference/cli/docker/container/rm/
[commit]: https://docs.docker.com/reference/cli/docker/container/commit/
[cache]: https://docs.docker.com/build/cache/
[cache-opt]: https://docs.docker.com/build/cache/optimize/
[sc-docker]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/sandboxes/docker.ts
[sc-lifecycle]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/DockerLifecycle.ts
[sc-init]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/InitService.ts
[sc-cli]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/cli.ts
