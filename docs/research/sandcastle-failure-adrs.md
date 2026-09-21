---
type: reference
---
# Sandcastle's failure-semantics ADRs

Research for [waystation#13](https://github.com/jeffrichley/waystation/issues/13).
Date: 2026-09-15.

**Question.** What do sandcastle's `docs/adr/` decide about failure semantics — granular
per-step timeouts, AbortSignal / cancellation, dirty-preservation cleanup, error
classification — and which of those decisions transfer to waystation's settled model
(ephemeral sandboxes; clone-in workspace + `format-patch` out; `finally` + `docker rm -f`
teardown with run-id labels and an explicit reaper; fan-out with no cancel-on-fail;
orchestrator-owned timeouts)?

**Methodology.** Every file under `docs/adr/` in [mattpocock/sandcastle][sc] at commit
`e99f832` (2026-06-29) was read — 20 files, numbered 0001–0020 with two quirks: two
files share number 0005, and there is no 0013 file although ADR-0014 cites "ADR-0013"
(a `docs/research/` note at the same sha names it as the planned build-arg ADR, so 0014
is that ADR renumbered). Where an ADR was ambiguous or described behaviour that has since
drifted, the source it points at was read at the same sha: `src/errors.ts`,
`src/Orchestrator.ts`, `src/SandboxFactory.ts`, `src/SandboxLifecycle.ts`,
`src/createSandbox.ts`, `src/syncOut.ts`, `src/RecoveryMessage.ts`,
`src/raceAbortSignal.ts`, `src/shutdownRegistry.ts`, `src/sandboxes/docker.ts`,
`src/sandboxes/no-sandbox.ts`, `src/WorktreeManager.ts`, `src/ErrorHandler.ts`, plus
`CONTEXT.md` and `.out-of-scope/provider-error-retry.md`. Each claim below cites the ADR
file or source file at `e99f832`. Transferability is judged against waystation's
settled model as recorded in `CONTEXT.md` and wayfinder tickets #3, #5, #8, #9.

## TL;DR — recommended default

1. **Bound every stage, orchestrator-side, with a typed stage-tagged failure — not ten
   exception classes.** Sandcastle wraps every lifecycle step in its own timeout and its
   own `TaggedError` (ADR-0001; 10 timeout classes in `errors.ts`). Waystation should keep
   the *per-stage* bounding (`asyncio.timeout` around clone-in, agent exec, collect,
   integrate) but express it as one `RunFailed(stage=…, cause="timeout", elapsed=…)`,
   because fan-out must yield results, not raise.
2. **Treat the agent timeout as a silence timer plus a wall-clock cap.** Sandcastle's
   only agent timeout is idle-based (600 s of no output, reset on every line; there is no
   wall-clock cap at all). Waystation's exec already streams line callbacks, so the
   silence timer is cheap — add it, and add the wall-clock cap sandcastle lacks, since
   Claude Code has none of its own.
3. **Adopt the completion-grace idea for the Outcome line.** Sandcastle found that agents
   whose children (MCP servers, `gh`) hold stdout open never deliver EOF, and rather than
   fail the run after the 10-minute idle timeout it now force-completes 60 s after the
   completion signal (ADR-0019). Waystation reverse-scans stdout for the marker-prefixed
   JSON line — same shape: once the Outcome line is seen, start a short silence grace, then
   finish the run and let `docker rm -f` kill stragglers.
4. **Cancellation = asyncio task cancellation + shielded salvage, not a preserved
   sandbox.** Sandcastle binds `AbortSignal` to operations (not resource factories),
   rejects with the caller's own `signal.reason`, and deliberately leaves worktree and
   sandbox alive on abort (ADR-0004). Waystation's sandboxes are ephemeral, so
   "preserve on abort" becomes "on cancel, still run the salvage `WIP` commit +
   `format-patch` under a short shielded timeout, then tear down in `finally`".
   Note sandcastle never actually kills the agent process on abort or idle timeout — the
   Promise race abandons it and container teardown kills it (`docker.ts` `exec` has no
   `proc.kill`; ADR-0019 admits the no-sandbox leak). `rm -f` teardown makes that
   acceptable for waystation too.
5. **Keep the "save before apply, preserve on failure" ordering; drop everything about
   host worktrees.** Sandcastle's sync-out writes patches to `.sandcastle/patches/<ts>/`
   *before* `git am`, keeps them and prints recovery commands on failure, and preserves a
   dirty worktree whether the run succeeded or failed (`SandboxFactory.ts`
   `cleanupWorktree`). Waystation's equivalent is already settled: series lands on
   `waystation/<run-id>` before integration; conflict → `RunConflicted`, host untouched.
   Dirty-worktree reuse, ff-only refresh, PID-file worktree locks, stale-worktree pruning,
   and the `refs/sandcastle/sync-base` ref all exist only because sandcastle's worktrees
   are long-lived host resources; none transfer.
6. **Fail fast, never retry — except exit 126/137 on idempotent sandbox setup execs.**
   Sandcastle refuses to retry provider errors, timeouts, or prompt expansion, and retries
   only transient exec-layer races (exit 126/137) on idempotent git-setup commands, twice,
   250 ms apart (ADR-0020, `SandboxLifecycle.ts`). "Retry belongs at the layer that owns
   the parallelism" — for waystation that is the flow script, which needs typed
   diagnostics (`exit_code`, `elapsed`, stage) on `RunFailed` to decide.

## 1. Inventory: which ADRs touch failure semantics

Eight ADRs decide something about failure handling; four more brush against it; the
remaining eight do not (listed at the end for completeness).

**ADR-0001 — Per-step timeouts with dedicated error types** ([file][adr-0001]).
Before it, only agent idle had a timeout; container start, hooks and git operations could
hang forever. Decision: wrap every lifecycle step in `Effect.timeoutFail` via a
`withTimeout` helper, each step with its own `Data.TaggedError` carrying `message`,
`timeoutMs` and step context; defaults "internal — not user-configurable"; rename
`TimeoutError` → `AgentIdleTimeoutError`. Result: 10 timeout error types in the
`SandboxError` union.

**ADR-0003 — Reuse existing worktree by default** ([file][adr-0003]).
Replaces throw-on-duplicate-worktree with reuse: clean worktree → reuse and
fast-forward from `origin` with `--ff-only`; dirty (uncommitted changes) → reuse with a
warning, no refresh. A failed fetch "is non-fatal; it never breaks the run". `git reset
--hard` was rejected because it clobbers unpushed commits. Failure-relevant because it
defines "dirty" narrowly (uncommitted changes only) and makes an infrastructure failure
(fetch) degrade rather than abort.

**ADR-0004 — AbortSignal on run/interactive, not on factories** ([file][adr-0004]).
`run()`, `interactive()` and their `Worktree.`/`Sandbox.` variants accept
`signal?: AbortSignal`; `createWorktree()`/`createSandbox()` do not, because binding
cancellation to a resource's existence "was rejected as overloading two different
concepts". On abort the promise rejects with `signal.reason` verbatim — no
sandcastle abort class; worktree and sandbox are *not* torn down, "mirroring the existing
error path". Hooks receive the signal; the process-level SIGINT/SIGTERM handler is
"a different concern".

**ADR-0007 — Worktree locking for concurrent access prevention** ([file][adr-0007]).
File lock at `.sandcastle/locks/<name>.lock` holding `{pid, branch, acquiredAt}`, created
atomically (`O_EXCL`); on contention check PID liveness — alive → fail fast, dead →
remove stale lock and reacquire. "There is no wait/retry behavior" — contention is a
caller error, "consistent with the project's fail-fast philosophy". Lock released on
scope close (close, abort, error, process exit) regardless of worktree dirtiness;
`pruneStale()` removes locks for missing worktrees or dead PIDs.

**ADR-0010 — Structured output is orthogonal to the completion signal**
([file][adr-0010]). Missing tag, invalid JSON, or schema failure throws
`StructuredOutputError`; no tolerant JSON parsing ("Loud is better"); last match wins
(self-correction is benign); no auto-cleanup of worktree or branch — "the caller decides
recovery". A discriminated-union return was rejected as inconsistent with `run()`'s
throw-based error model. The amendment adds `sessionId`/`sessionFilePath` to the error
so the failed session can be resumed with feedback — "Resuming the session **is**
recovery"; retry loops stay in the consumer.

**ADR-0017 — Sync-out tracks its patch base in a sandbox-owned ref** ([file][adr-0017]).
A data-loss bug: `git am` rewrites SHAs, so the second sync-out from the same sandbox used
a base the sandbox never had, `format-patch` failed with `Invalid revision range`
"before any recovery artifacts are saved", and teardown lost the commits (#651). Fix:
`refs/sandcastle/sync-base` inside the sandbox, advanced only after the commit step
succeeds, so failed `git am` retries from the same base while diff/untracked failures
don't cause re-emission.

**ADR-0019 — Completion timeout: force-complete a hanging process** ([file][adr-0019]).
An agent that emits the completion signal but whose child (`gh`, an MCP server) keeps
stdout open never delivers EOF; the run used to fail after the full idle timeout,
"discarding the already-committed work". Now, once the signal is in the buffer, a 60 s
silence-based completion timeout replaces the idle timeout and on expiry the iteration
completes *successfully* with a "hanging" warning. Killing immediately on signal was
rejected because useful data trails the signal (usage events, `result` text, structured
output). Consequence admitted: force-completing abandons the process; containers get
killed by `docker rm -f`, but no-sandbox has "no `proc.kill()` anywhere in the codebase",
so it leaks.

**ADR-0020 — Prompt expansion fails fast, never retries or degrades**
([file][adr-0020]). Under contention a `` !`gh api …` `` prompt expansion timed out at
30 s and aborted the run; the issue asked for retry, a `!?` best-effort marker, and better
diagnostics. Retry and degradation rejected: prompt content is not idempotent plumbing,
and running against a knowingly-incomplete prompt in AFK mode "is worse than not running
it". Accepted: typed diagnostics (`elapsedMs`, `exitCode`) so "a downstream orchestrator
can programmatically branch — timeout → retry the whole run; non-zero exit → don't".
Restates the house rule: "retry idempotent infrastructure races, fail fast on hangs and
genuine errors".

Brushing against failure semantics:

- **ADR-0014 — Docker UID alignment via build-arg + pre-flight diagnostic**
  ([file][adr-0014]): a `docker image inspect` pre-flight before `docker run` that throws
  a clear error naming two remedies; "silent `EACCES` is the bug this fixes; early
  detection is essential".
- **ADR-0018 — `.fork()` isolates the session only** ([file][adr-0018]): concurrent
  fan-out is only git-safe with a distinct branch per fork; `generateTempBranchName` was
  second-granularity with no randomness, so forks fired in one tick collided (since
  hardened with a random suffix in `WorktreeManager.ts`). Safe fan-out is the caller's
  responsibility.
- **ADR-0015 — Allow `noSandbox()` in `run()`** ([file][adr-0015]): the trust model is
  explicit ("the caller owns the risk"); no runtime guard. Relevant only as the source of
  ADR-0019's process-leak caveat.
- **`.out-of-scope/provider-error-retry.md`** ([file][sc-retry]; not an ADR but a
  recorded decision the ADRs cite): sandcastle "does not retry on provider errors" —
  rate limits, auth, quota, network — because it shells out to a CLI whose error shapes it
  does not own; "Error handling and retry logic belong in the provider/harness layer".

Not about failure semantics: 0002 (`cwd` option), 0005a (remove runtime chown), 0005b
(usage as raw tokens), 0006 (worktree mounts on Windows), 0008 (inline prompts are
literal), 0009 (templates share no code), 0011 (`.resume()` is one iteration), 0012
(provider-owned session storage), 0016 (resume needs filesystem sessions).

## 2. What sandcastle decided, by theme

### 2.1 Timeouts

**Shape.** One helper, `withTimeout(timeoutMs, onTimeout)` over `Effect.timeoutFail`,
applied at each call site; every timeout has its own tagged error carrying `timeoutMs`
plus step-specific fields (`command`, `path`/`operation`, `paths`, `expression`,
`sourceBranch`/`targetBranch`) ([`errors.ts`][sc-errors]). Defaults, all internal
constants:

| Step | Default | Error | Where |
|---|---|---|---|
| Agent idle (silence, reset on every stdout line) | 600 s | `AgentIdleTimeoutError` (+`preservedWorktreePath`) | [`Orchestrator.ts:247`][sc-orch] |
| Completion grace (silence after signal seen) | 60 s | none — resolves *successfully* | [`Orchestrator.ts:248`][sc-orch] |
| Container start | 120 s | `ContainerStartTimeoutError` | [`startSandbox.ts:73`][sc-start] |
| Sync-in (isolated providers) | 120 s | `SyncInTimeoutError` | [`startSandbox.ts:74`][sc-start] |
| Copy paths into worktree | 60 s | `CopyToWorktreeTimeoutError` (+`paths`) | [`CopyToWorktree.ts:11`][sc-copy] |
| Each `onSandboxReady` hook | 60 s (per-hook `timeoutMs`) | `HookTimeoutError` (+`command`) | [`SandboxLifecycle.ts:20`][sc-lifecycle] |
| Each in-sandbox git setup command | 10 s per attempt; 2 retries, 250 ms apart, only on exit 126/137 | `GitSetupTimeoutError` (+`command`) | [`SandboxLifecycle.ts:19–27`][sc-lifecycle] |
| Prompt shell expansion | 30 s | `PromptExpansionTimeoutError` (+`expression`, `elapsedMs`) | [`PromptPreprocessor.ts:7`][sc-prompt] |
| Commit collection | 30 s | `CommitCollectionTimeoutError` | [`SandboxLifecycle.ts:21`][sc-lifecycle] |
| Merge temp branch to host | 30 s | `MergeToHostTimeoutError` (+branches) | [`SandboxLifecycle.ts:22`][sc-lifecycle] |
| Worktree create / prune | 30 s | `WorktreeTimeoutError` (+`path`, `operation`) | [`WorktreeManager.ts:8`][sc-wt] |

**Two things the ADR does not say that the source does.** First, "not user-configurable"
has drifted: `run()` now takes `timeouts?: Timeouts` overriding four of them
(`copyToWorktreeMs`, `gitSetupMs`, `commitCollectionMs`, `mergeToHostMs`,
[`run.ts:321`][sc-run]), hooks take a per-hook `timeoutMs`, and idle/completion are
public options — the knobs came back one issue at a time. Second, there is **no wall-clock
timeout on the agent at all**: the idle timer is re-armed on every parsed line
(`resetTimer()` in `onLine`, [`Orchestrator.ts:138–189`][sc-orch]), so a chatty agent can
run indefinitely. Teardown is also unbounded: `removeContainer` is `docker stop` then
`docker rm` with errors ignored ([`DockerLifecycle.ts:176`][sc-dlife]) — and, per the
[docker-startup research][ws-docker], `stop` against `sleep infinity` eats 10 s.

**Why idle rather than wall-clock.** The ADRs never argue it; it is inherited. ADR-0019
does explain why the post-signal window is silence-based rather than an immediate kill:
"useful data trails the signal" (usage on `turn.completed`, Claude Code's `result`,
structured-output tags), and there is no reliable cross-provider terminal stream event
(sandcastle synthesizes a `result` event per message for Codex and OpenCode).

**Timeout as failure vs success.** Idle expiry *fails* the iteration
(`AgentIdleTimeoutError`, with the preserved worktree path attached). Completion expiry
*succeeds* with a warning and collects commits normally. The gate is strict: a process
that hangs before any signal "is indistinguishable from an agent genuinely stuck mid-work".
`CONTEXT.md` names the distinction: a **hanging process** (signal seen, EOF missing) vs a
**stuck agent** (no output).

### 2.2 Cancellation

**Surface.** `signal?: AbortSignal` on the operations that do work (`run`, `interactive`,
and their handle-scoped variants), not on the factories that produce long-lived handles
(ADR-0004). Pre-aborted signal → `throwIfAborted()` at entry, before any setup
([`createSandbox.ts:332`][sc-create], [`run.ts:497`][sc-run]). Mid-run abort → the promise
rejects with the caller's own `signal.reason`; there is no sandcastle abort error type, and
callers distinguish abort from failure by `error.name === "AbortError"` or by identity
with their own reason. Hooks get the signal on their argument object and opt in to
cooperative cancellation.

**Mechanism.** `raceAbortSignal` / the orchestrator race the work effect against a
`Deferred` that is *died* (not failed) with `signal.reason`, so the reason propagates as a
defect untouched by the typed error channel ([`raceAbortSignal.ts`][sc-race],
[`Orchestrator.ts:123–231`][sc-orch]). The agent exec, idle timeout, completion timeout
and abort are all one `Effect.raceFirst` chain; the loser is interrupted.

**What "killed" means in practice.** ADR-0004 says the in-flight agent subprocess "is
killed". The docker provider's `exec` is a bare `new Promise` around `spawn("docker",
["exec", …])` with no interruption hook and no `proc.kill()`
([`docker.ts:250–320`][sc-docker]); interrupting the wrapping effect only detaches the
promise. ADR-0019's consequences confirm it: "there is no `proc.kill()` anywhere in the
codebase". So on abort, idle timeout, or completion timeout the `docker exec` child is
*abandoned*; it dies when the container does. For containers that is `close()` →
`stop`+`rm`, or the crash path's `docker rm -f`. For `noSandbox()`, `close()` is a no-op
([`no-sandbox.ts:164`][sc-nosb]) and the agent and its children leak.

**Resources on abort.** Deliberately preserved: "worktree preserved, sandbox handle still
alive on `Sandbox.run()` / `Worktree.run()`", so callers can inspect, resume, or `.close()`
on their own terms. Whole-session cancellation is the caller's job — "wire the same
`AbortController` into each `.run()`".

**Process signals are separate.** A single process-wide registry installs one
`SIGINT`/`SIGTERM`/`exit` listener regardless of sandbox count (more than ~10 per-sandbox
listeners tripped `MaxListenersExceededWarning`), runs every registered *synchronous*
teardown, then exits with code 1 ([`shutdownRegistry.ts`][sc-shutdown]). The Docker
provider registers `execFileSync("docker", ["rm", "-f", name])` ([`docker.ts:236`][sc-docker]);
the worktree layer registers a `console.error` "Worktree preserved at …" hint with review
and cleanup commands ([`createSandbox.ts:1072`][sc-create]). Teardowns must not await —
"a signal handler cannot await before the process exits".

### 2.3 Cleanup and dirty preservation

**The rule.** After every run, success or failure, `cleanupWorktree` checks
`git status --porcelain`; dirty → preserve the worktree and print
"Run succeeded but worktree has uncommitted changes at …" or "Worktree preserved at …";
clean → `git worktree remove --force`, printing "Worktree removed (no uncommitted
changes)" on the failure path ([`SandboxFactory.ts:203–226`][sc-factory]). The preserved
path is attached to `AgentIdleTimeoutError` and `AgentError` (`attachPreservedPath`,
[`SandboxFactory.ts:228–252`][sc-factory]) and to `RunResult.preservedWorktreePath` /
`CloseResult.preservedWorktreePath` on success. Commits are never the concern here —
they already sit on the branch (branch strategy) or were merged (merge-to-head); "dirty"
means uncommitted work only (ADR-0003).

**Resource ordering.** Worktree creation and sandbox start are nested
`acquireUseRelease`s "so the worktree is always cleaned up (outer release) even when a
later step … fails"; the provider handle is always closed by the inner release
([`SandboxFactory.ts:590–596`][sc-factory]). A setup failure in `createSandbox()` removes
the worktree outright ([`createSandbox.ts:1053`][sc-create]) — preservation only applies
once the agent could have written something.

**Sync-out (isolated providers) is save-then-apply.** Phase 1 eagerly writes all three
artifacts — `format-patch` files, `git diff HEAD` as `changes.patch`, untracked files — to
`.sandcastle/patches/<YYYYMMDD-HHMMSS>/` on the host. Phase 2 applies in order (`git am
--3way`, `git apply`, copy); the first failure stops the chain, the patch directory is
kept, and `buildRecoveryMessage` prints copy-pastable commands from the failed step
onward (`git am --continue`, then the remaining steps); on full success the directory is
deleted ([`syncOut.ts`][sc-syncout], [`RecoveryMessage.ts`][sc-recovery]). ADR-0017's
`refs/sandcastle/sync-base` is advanced only when the commit step succeeded, so a failed
`git am` retries from the same base next run while landed commits are not re-emitted.

**Merge-to-head failure.** `git merge` of the temp branch failing throws a `SyncError`
whose message says the temp branch "has been preserved" and gives the retry and cleanup
commands ([`SandboxLifecycle.ts:447–452`][sc-lifecycle]); the branch delete is skipped.

**Locks and stale state.** Worktree locks are released on every exit path independent of
dirtiness — "the lock protects concurrent access to a worktree, not the worktree's
existence on disk" (ADR-0007). `pruneStale()` runs at worktree creation (best-effort) and
removes orphaned `.sandcastle/worktrees/*` directories not known to `git worktree list`
([`WorktreeManager.ts:461`][sc-wt]).

**Containers.** Always removed on `close()`; never preserved for post-mortem. The
preserved unit of state is the host worktree, never the sandbox.

### 2.4 Error classification

**Taxonomy.** A closed union `SandboxError` of 24 `Data.TaggedError` classes
([`errors.ts`][sc-errors]): 13 operational (`ExecError`, `ExecHostError`, `CopyError`,
`DockerError`, `PodmanError`, `SyncError`, `WorktreeError`, `PromptError`, `AgentError`,
`ConfigDirError`, `InitError`, `SessionCaptureError`, `CwdError`), 10 timeouts (§2.1),
and `CopyToWorktreeError`. Discrimination is by `_tag`; `ErrorHandler.formatErrorMessage`
switches on it to produce the CLI message and exits 1 ([`ErrorHandler.ts`][sc-errh]).
Structured fields exist where an ADR needed programmatic branching: `ExecError.exitCode`
(absent when the exec itself failed), `PromptError.exitCode`,
`PromptExpansionTimeoutError.elapsedMs` (ADR-0020), `preservedWorktreePath` on the two
agent-phase errors, `StructuredOutputError` with `tag`, `rawMatched`, `cause`, `commits`,
`branch`, `preservedWorktreePath`, `sessionId`, `sessionFilePath` (ADR-0010).

**What is *not* an error.** Abort is a defect carrying the caller's reason, outside the
union (ADR-0004). Completion-timeout expiry is success plus a warning (ADR-0019). A failed
`fetch` on worktree reuse is a log line (ADR-0003). Non-zero exit from a
`git rev-list` count degrades to 0 (`countCommitsToSync`, [`syncOut.ts:171`][sc-syncout]).

**Agent exit.** Non-zero exit of the agent exec becomes `AgentError` with detail taken
from stderr, else the parsed `result` text, else the last 20 non-empty stdout lines
([`Orchestrator.ts:191–207`][sc-orch]). No parsing of provider error shapes: "Parsing
provider-specific error shapes … means taking responsibility for an interface we don't
control" ([provider-error-retry][sc-retry]).

**Retry policy.** Exactly one retry site: `execOkWithGitTimeout` retries an `ExecError`
whose `exitCode` is 126 or 137 (shell could not exec — overlayfs not ready, or SIGKILL
under load), at most twice, spaced 250 ms, each attempt under its own timeout; timeouts
and genuine git errors are not retried ([`SandboxLifecycle.ts:29–81`][sc-lifecycle]).
ADR-0020 generalises it: "retry is safe for idempotent infrastructure, unsafe for prompt
content", and retry of a whole run belongs to "the layer that owns the parallelism".
Worktree lock contention likewise gets no wait/retry (ADR-0007).

**Throw vs return.** `run()` throws; ADR-0010 explicitly rejected a discriminated-union
return for structured output as "inconsistent with the rest of `run()`'s error model".
Errors carry recovery surface so the *consumer* loops (ADR-0010 amendment); nothing in
sandcastle catches and continues on the caller's behalf.

## 3. Transfer table

Judged against waystation's settled model: ephemeral Docker sandboxes (`run -d` +
`exec`, `finally` + `docker rm -f`, `waystation.run-id` labels, explicit reaper, no
auto-reap); workspace cloned into the sandbox; patch series out via `format-patch` on exec
stdout, applied with `git am --3way` or merged; salvage `WIP` commit; conflicts →
`RunConflicted` with the series kept on `waystation/<run-id>`; fan-out as an as-completed
iterator yielding `RunSucceeded | RunConflicted | RunFailed`, nothing raises mid-gather;
orchestrator-owned timeouts; Outcome as a marker-prefixed, pydantic-validated JSON line,
reverse-scanned.

| # | Sandcastle decision (source) | Verdict | Waystation form / why not |
|---|---|---|---|
| 1 | Bound every lifecycle step, not just the agent (ADR-0001) | **Adapted** | Same principle, fewer stages: clone-in, agent exec, collect (salvage + `format-patch`), integrate, teardown. `asyncio.timeout` per stage inside the run primitive. Also bound teardown, which sandcastle leaves unbounded — `rm -f` makes that ~1 s. |
| 2 | One `TaggedError` per timeout with `timeoutMs` + step context (ADR-0001) | **Adapted** | Fan-out yields results instead of raising, so the taxonomy folds into `RunFailed(stage, cause="timeout", timeout, elapsed, …)`. Keep the *fields*, drop the ten classes. |
| 3 | Timeout defaults are internal, not user-configurable (ADR-0001) | **Does not transfer** | Sandcastle itself walked it back (`Timeouts` overrides, per-hook `timeoutMs`, idle/completion options). Settled: timeouts belong to the orchestrator, i.e. the flow script — expose them as parameters with defaults. |
| 4 | Agent timeout is silence-based, re-armed per output line; no wall-clock cap (Orchestrator) | **Adapted** | Exec already gives live line callbacks, so add the silence timer as-is (600 s is a reasonable default), *and* add a wall-clock cap sandcastle lacks — Claude Code has no flag of its own. |
| 5 | Completion grace: after the completion signal, 60 s of silence → success with "hanging" warning; never kill on signal because trailing data matters (ADR-0019) | **Adapted** | Outcome line is both completion and payload, but the hanging-child problem is identical (`docker exec` stdout held open by MCP servers). Once the marker line is seen, arm a short silence grace, then proceed to collect; `rm -f` kills stragglers. Because the Outcome line is reverse-scanned, trailing output after it costs nothing. |
| 6 | Hanging (signal seen, no EOF) vs stuck (no output) are different outcomes (ADR-0019, CONTEXT.md) | **As-is** | Keep the vocabulary and the split: hanging → `RunSucceeded` with a flag; stuck → `RunFailed(cause="timeout")`. |
| 7 | Cancellation binds to operations, not to resource factories (ADR-0004) | **Adapted** | Python idiom: cancel the run's task; the sandbox is an async context manager whose `__aexit__`/`finally` runs `rm -f`. No cancel-token parameter on anything. |
| 8 | Abort rejects with the caller's own reason; no library abort class (ADR-0004) | **Adapted** | `asyncio.CancelledError` propagates for a directly awaited run. Inside fan-out the settled contract is "every run yields a typed result", so cancelling one run must surface as `RunFailed(cause="cancelled")` in the iterator; cancelling the iterator itself follows asyncio semantics. |
| 9 | Abort preserves the worktree and keeps the sandbox handle alive (ADR-0004) | **Adapted** | Nothing on the host to preserve — the workspace lives in the sandbox and teardown is settled. The preservation *goal* survives as: on cancel, run salvage `WIP` + `format-patch` under a short `asyncio.shield`ed timeout before `rm -f`, so cancelled work still lands on `waystation/<run-id>`. |
| 10 | Abort/timeout "kill" the agent by abandoning the exec; container teardown does the killing; no-sandbox leaks (ADR-0004 vs `docker.ts`, ADR-0019) | **As-is** (Docker) | Same reality: abandon the `docker exec`, let `rm -f` kill. Record the constraint for any future no-isolation backend: it must kill the process group itself. |
| 11 | Hooks receive the abort signal and cancel cooperatively (ADR-0004) | **As-is** | Hooks are awaited coroutines; task cancellation reaches them with no plumbing. |
| 12 | One process-wide SIGINT/SIGTERM registry; synchronous `docker rm -f` teardown; exit 1 (`shutdownRegistry.ts`) | **Adapted** | Install one handler that cancels the in-flight run tasks so `finally` teardown runs. Do *not* add exit-time reaping by label — settled: explicit reaper, no auto-reap; the labels exist so the reaper can find what a hard crash left behind. |
| 13 | Preserve the worktree iff dirty, remove otherwise, on success and failure alike (`cleanupWorktree`) | **Adapted** | "Never lose uncommitted agent work" transfers; the mechanism becomes the settled salvage `WIP` commit riding the patch series, flagged in the integration report. Clean workspace → nothing to do; sandbox is removed either way. |
| 14 | Preserved path attached to the error *and* to the success result (`attachPreservedPath`, `RunResult.preservedWorktreePath`) | **Adapted** | Every typed result carries the `waystation/<run-id>` branch and the salvage flag — the integration report already has both. Applies to `RunFailed` too, when collect got that far. |
| 15 | Sync-out saves all artifacts to the host *before* applying; failure keeps them and prints recovery commands (`syncOut.ts`) | **As-is** | Settled shape already matches: series lands on `waystation/<run-id>` before `git am`/merge touches the target; conflict → abort cleanly, host untouched, typed `RunConflicted`. The printed recovery text becomes fields on that result (branch, failed step). |
| 16 | Advance a sandbox-owned sync base only after `git am` succeeded, so retries re-emit exactly the unlanded commits (ADR-0017) | **Does not transfer** | One sandbox, one sync-out, base ref known: the bug needs a reused sandbox after a SHA-rewriting `am`. Revisit only if resume-in-same-sandbox is ever added. |
| 17 | Merge failure preserves the temp branch with retry/cleanup instructions (`SandboxLifecycle.ts`) | **As-is** | This is `RunConflicted` with the series on `waystation/<run-id>`. |
| 18 | Dirty-worktree reuse; ff-only refresh from origin; fetch failure non-fatal (ADR-0003) | **Does not transfer** | No reuse: every run clones a fresh workspace at the base ref, committed state only. |
| 19 | PID-file worktree locks; fail fast on contention, no wait/retry; stale-lock cleanup (ADR-0007) | **Adapted** | No shared host worktrees, so no file locks. The contention that remains is two runs integrating into the same target branch concurrently — serialize the integrate stage per host repo with an in-process `asyncio.Lock` in fan-out. Cross-process contention is out of scope (one flow script owns the repo). |
| 20 | `pruneStale()` at every worktree creation (ADR-0007, `WorktreeManager.ts`) | **Does not transfer** | Settled: no auto-reap. The explicit reaper helper over `waystation.run-id` labels is the deliberate replacement. |
| 21 | Closed `TaggedError` union; discriminate by tag; CLI maps tag → message (`errors.ts`, `ErrorHandler.ts`) | **Adapted** | Result-typed, not exception-typed, for anything a run can produce: `RunFailed` with a `cause` discriminator (`timeout`, `agent_exit`, `sandbox`, `collect`, `outcome_invalid`, `cancelled`), `stage`, and structured fields. Exceptions stay for programmer errors and preflight. |
| 22 | Agent non-zero exit → one error with stderr / result text / last 20 stdout lines as detail; never parse provider error shapes (Orchestrator, provider-error-retry) | **As-is** | Same CLI-shelling position. `RunFailed(cause="agent_exit", exit_code, tail)` — the buffered exec result already has stdout/stderr. |
| 23 | No retry of provider errors, timeouts, or lock contention; fail fast (provider-error-retry, ADR-0007, ADR-0020) | **As-is** | Retry loops live in the flow script. Give it what it needs: `exit_code`, `elapsed`, `stage` on `RunFailed` (row 24). |
| 24 | Retry only transient exec-layer races (exit 126/137) on idempotent setup commands, twice, 250 ms (`SandboxLifecycle.ts`) | **Adapted** | Adopt the narrow rule for the in-sandbox setup execs (git config, clone-in): same overlayfs races exist, and Docker Desktop on Windows adds ~0.5 s per exec so batching setup into one `exec` shrinks the window anyway. Never retry the agent exec or `git am`. |
| 25 | Typed diagnostics (`elapsedMs`, `exitCode`) so the orchestrator can branch timeout → retry run, non-zero → don't (ADR-0020) | **Adapted** | No prompt-template pipeline in a bare library, so the trigger vanishes, but the principle lands on `RunFailed`: `elapsed` alongside `timeout`, `exit_code` alongside `agent_exit`. |
| 26 | Structured output: throw on missing/invalid; no tolerant JSON; last match wins; carry recovery surface (ADR-0010) | **Adapted** | Reverse-scan = last match wins, pydantic = strict — already settled. The difference: inside fan-out a bad Outcome line is `RunFailed(cause="outcome_invalid", raw_matched, validation_error)` plus the branch, not a raise. |
| 27 | Completion signal and structured output are orthogonal; generalising the signal to carry a payload was rejected (ADR-0010) | **Does not transfer** | Waystation chose the rejected option: the Outcome line is both. Sandcastle's reason was future iteration-loop semantics, which waystation has no equivalent of. |
| 28 | Pre-flight `docker image inspect` diagnostic that fails before `docker run` and names the remedy (ADR-0014) | **As-is** | Settled preflight: "fails fast; never repairs". Same two checks (image present, UID story) plus whatever else the backend needs. |
| 29 | Safe concurrent fan-out is the caller's job: distinct branch per fork (ADR-0018) | **Does not transfer** | Fan-out is a primitive with unique `waystation/<run-id>` branches by construction and no cancel-on-fail; the caller never picks branch names to stay safe. |

**Counts.** Over 29 rows: **8 transfer as-is** (rows 6, 10, 11, 15, 17, 22, 23, 28),
**15 transfer adapted** (rows 1, 2, 4, 5, 7, 8, 9, 12, 13, 14, 19, 21, 24, 25, 26),
**6 do not transfer** (rows 3, 16, 18, 20, 27, 29). Every "does not transfer" row is
either a host-worktree mechanism (16, 18, 20) or a choice waystation has already settled
the other way (3, 27, 29).

## Sources

- sandcastle ADRs @ `e99f832`: [0001][adr-0001], [0003][adr-0003], [0004][adr-0004],
  [0007][adr-0007], [0010][adr-0010], [0014][adr-0014], [0015][adr-0015],
  [0017][adr-0017], [0018][adr-0018], [0019][adr-0019], [0020][adr-0020]; the other nine
  ([0002][adr-0002], [0005a][adr-0005a], [0005b][adr-0005b], [0006][adr-0006],
  [0008][adr-0008], [0009][adr-0009], [0011][adr-0011], [0012][adr-0012],
  [0016][adr-0016]) were read and found not to bear on failure semantics.
- sandcastle source @ `e99f832`: [`src/errors.ts`][sc-errors], [`src/Orchestrator.ts`][sc-orch],
  [`src/SandboxFactory.ts`][sc-factory], [`src/SandboxLifecycle.ts`][sc-lifecycle],
  [`src/createSandbox.ts`][sc-create], [`src/run.ts`][sc-run], [`src/syncOut.ts`][sc-syncout],
  [`src/RecoveryMessage.ts`][sc-recovery], [`src/raceAbortSignal.ts`][sc-race],
  [`src/shutdownRegistry.ts`][sc-shutdown], [`src/sandboxes/docker.ts`][sc-docker],
  [`src/sandboxes/no-sandbox.ts`][sc-nosb], [`src/DockerLifecycle.ts`][sc-dlife],
  [`src/startSandbox.ts`][sc-start], [`src/CopyToWorktree.ts`][sc-copy],
  [`src/PromptPreprocessor.ts`][sc-prompt], [`src/WorktreeManager.ts`][sc-wt],
  [`src/ErrorHandler.ts`][sc-errh], [`CONTEXT.md`][sc-context],
  [`.out-of-scope/provider-error-retry.md`][sc-retry].
- waystation: [`CONTEXT.md`][ws-context] (settled vocabulary),
  [docker-startup-efficiency research][ws-docker] (teardown timings), wayfinder tickets
  [#3](https://github.com/jeffrichley/waystation/issues/3),
  [#5](https://github.com/jeffrichley/waystation/issues/5),
  [#8](https://github.com/jeffrichley/waystation/issues/8),
  [#9](https://github.com/jeffrichley/waystation/issues/9).

[sc]: https://github.com/mattpocock/sandcastle
[adr-0001]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0001-per-step-timeouts.md
[adr-0002]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0002-cwd-option.md
[adr-0003]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0003-reuse-worktree-by-default.md
[adr-0004]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0004-abort-signal-on-run-and-interactive.md
[adr-0005a]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0005-remove-chown-uid-alignment.md
[adr-0005b]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0005-usage-raw-tokens-no-percentage.md
[adr-0006]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0006-git-worktree-mounts-on-windows.md
[adr-0007]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0007-worktree-locking.md
[adr-0008]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0008-inline-prompts-skip-processing.md
[adr-0009]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0009-templates-no-shared-code.md
[adr-0010]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0010-structured-output.md
[adr-0011]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0011-resume-is-one-iteration.md
[adr-0012]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0012-agent-provider-owned-session-storage.md
[adr-0014]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0014-docker-uid-alignment-via-build-arg.md
[adr-0015]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0015-no-sandbox-in-run-and-create-sandbox.md
[adr-0016]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0016-resume-requires-filesystem-backed-sessions.md
[adr-0017]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0017-sandbox-owned-sync-base-ref.md
[adr-0018]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0018-fork-is-session-only.md
[adr-0019]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0019-completion-timeout-for-hanging-process.md
[adr-0020]: https://github.com/mattpocock/sandcastle/blob/e99f832/docs/adr/0020-prompt-expansion-fails-fast.md
[sc-errors]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/errors.ts
[sc-orch]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/Orchestrator.ts
[sc-factory]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/SandboxFactory.ts
[sc-lifecycle]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/SandboxLifecycle.ts
[sc-create]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/createSandbox.ts
[sc-run]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/run.ts
[sc-syncout]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/syncOut.ts
[sc-recovery]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/RecoveryMessage.ts
[sc-race]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/raceAbortSignal.ts
[sc-shutdown]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/shutdownRegistry.ts
[sc-docker]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/sandboxes/docker.ts
[sc-nosb]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/sandboxes/no-sandbox.ts
[sc-dlife]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/DockerLifecycle.ts
[sc-start]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/startSandbox.ts
[sc-copy]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/CopyToWorktree.ts
[sc-prompt]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/PromptPreprocessor.ts
[sc-wt]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/WorktreeManager.ts
[sc-errh]: https://github.com/mattpocock/sandcastle/blob/e99f832/src/ErrorHandler.ts
[sc-context]: https://github.com/mattpocock/sandcastle/blob/e99f832/CONTEXT.md
[sc-retry]: https://github.com/mattpocock/sandcastle/blob/e99f832/.out-of-scope/provider-error-retry.md
[ws-context]: https://github.com/jeffrichley/waystation/blob/main/CONTEXT.md
[ws-docker]: https://github.com/jeffrichley/waystation/blob/research/docker-startup-efficiency/docs/research/docker-startup-efficiency.md
