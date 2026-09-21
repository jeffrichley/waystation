---
status: stable
type: adr
---

# `GitRepo` is the one way in to host git, and landings serialize at the entry points

`GitRepo` gets what the shipped strategy bypasses it for: `repo.git(*args, env=...) -> str` stays the ergonomic call that raises and hands back stripped stdout, and `repo.run(*args, check=False, stdin=..., env=...) -> GitResult` hands back `exit_code`, `stdout` and `stderr` for the calls that read an exit code (`rev-parse --verify --quiet`, `merge-tree`) or feed a patch in (`mailinfo`). `GitResult` is a small frozen value beside the others in the integration module, not a reused sandbox `ExecResult`: sandbox exec and host git are different things, and a strategy author should not import a sandbox type to name host git's result. Output is `str` decoded with surrogateescape and never newline-translated; `stdin` is `bytes`, because a patch that goes through a text pipe on Windows comes out changed. Commit points stay a named table inside `GitRepo` — `update-ref` today, `merge --ff-only` when [#25](https://github.com/jeffrichley/waystation/issues/25) lands — rather than a flag at the call site.

Serialization stays where ADR-0005 put it, at the entry points: `integrate()` and `preserve_series()` take the per-repo lock, and preservation now runs inside it instead of writing objects and a ref outside it. The lock table is a `WeakKeyDictionary` keyed by the running event loop, holding one lock per git common dir, and it is reentrant per task through a contextvar. The identity check lives once in `_git`, used by both the workspace and integration paths, and `require_host_git()` stops running on every landing, since preflight owns it.

Why: the shipped `Integration` reached past `GitRepo` into the private `run_git` three times, so a user's strategy could not do what the shipped one does — the privileged internal path CLAUDE.md rules out, and a promise #18 story 113 makes ("a git runner … so that custom landing rules stay small"). The lock had two real defects: an `asyncio.Lock` binds to the loop that first contends for it, so a process that runs a second loop — a service calling `asyncio.run` per request, a test suite with a loop per test — failed the next contended landing with `RuntimeError`, reported as `RunFailed(integrate, Errored)`; and `preserve_series` wrote into the host repo outside the lock, the suspected cause of a one-off Windows CI failure on 2026-09-19 where one of three concurrent runs failed while another was cloning. Reentrancy is not theoretical politeness: with serialization at the entry points, a custom strategy that preserves a series it could not land would otherwise deadlock with no error at all.

## Considered options

- **`GitRepo` locks every write.** Rejected: it would have to classify commands as reading or writing, and read-only calls would either serialize or need a second path.
- **An explicit write scope, `async with repo.writing():`.** Rejected: every strategy has to remember it, and forgetting it is silent.
- **One richer method returning a result for every call.** Rejected: twenty call sites that want a sha would each unwrap it.
- **Named landing steps on `GitRepo` (`read_ref`, `merge_tree`, …).** Deferred to [#81](https://github.com/jeffrichley/waystation/issues/81), which has the second strategy that justifies them; this decision is about transport, not landing semantics.
- **Reusing `ExecResult`.** Rejected: it belongs to the sandbox seam.
- **A flag at the call site for commit points.** Rejected: a strategy author who forgets it gets a half-moved ref, the failure ADR-0027 exists to prevent.
- **Documenting the reentrancy deadlock instead of preventing it.** Rejected: a hang with no message is the worst thing to ship.

## Consequences

A strategy written by a user can now do everything the shipped one does, which is what makes the integration seam real enough for #81's squash to be policy over plumbing. Calling a strategy's `integrate()` directly is still unserialized — it is like running git yourself — and the docs say so; `integrate()` and `preserve_series()` are the serialized ways in. Stage labels are untouched here: `_repo_git` keeps `stage="integrate"` until [#70](https://github.com/jeffrichley/waystation/issues/70) moves attribution to each primitive's edge, so the two tickets do not rewrite the same lines. The import surface gains `GitResult`.

Decided while grilling [#71](https://github.com/jeffrichley/waystation/issues/71), out of the architecture review in [#69](https://github.com/jeffrichley/waystation/issues/69).
