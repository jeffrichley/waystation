---
status: accepted
---

# A workspace owns its branch, the refs that travel with it, and its removal

`Workspace` carries `branch` and `refs`. `prepare_workspace` strips the clone to exactly `refs` — `origin` and every ref not travelling go — so the agent sees the same repository whatever transport got it there. `Workspace.remove()` is the one removal, called by core; a backend tears down what it made and never the workspace.

Three things were scattered, and each was scattered for the same reason: the `Workspace` value held four fields and none of the knowledge about them.

## The branch was spelled in four places

`waystation/<run-id>` was minted in `workspace.py`, re-derived in `sandbox/transport.py`, derived again for the preservation branch in `flow.py`, and a fourth time in the conformance suite. It is on the value now, and preservation reads it off `_RunRecord`, which is set when the workspace is made — preservation runs after the workspace is gone, so it cannot read the workspace itself.

## What the agent could see depended on the backend

The copy transport bundles `refs/heads/<branch>`: one branch, no remote. A bind transport and `NoSandbox` hand the agent the host-side workspace, made by `git clone --local`, which brings **every ref the host has** — its other branches, its tags, and all of them again as `origin/*`, with `origin` itself pointing at the host path. Under `NoSandbox` that is a remote the agent can push to.

So the same flow was a different repository depending on which backend ran it, and #32's `extra_refs` had nothing to extend: there was no definition of what travels, only two code paths that happened to disagree.

`refs` is that definition. `prepare_workspace` strips down to it, `clone_in` bundles it *and* fetches all of it — it previously fetched only the branch HEAD lands on, so a second ref would have been bundled and then dropped on arrival — and the conformance suite holds every backend to showing exactly those refs and no remote.

**`origin` does not survive.** Keeping it would mean the agent could read the host's branches on some backends and not others, which is the inconsistency this removes; and on `NoSandbox` it is a write path into the user's real repository. An agent that needs another ref will ask for it through `extra_refs` (#32), which is a thing the flow script says rather than an accident of transport.

**Only refs go, not objects.** The clone's objects are hardlinks and cost nothing; unreferenced, they are simply never read. This is why stripping beat the obvious alternative of bundling for every transport.

## Removal was everyone's job

`SandboxBackend.start` owned `ws` "from here" and removed it on exit. That put a directory core created on whichever backend happened to be handed it — including a copy backend, which reads the workspace once into its own container and never touches it again. Three backends each carried the call; ADR-0035's conformance suite held them to it; and `flow.py` removed it on two further paths for the cases where no backend ever got that far. Meanwhile `remove_workspace` was not exported, so a composer who prepared a workspace and never started a sandbox had nothing to reach for, and a bare `rmtree` fails on git's read-only objects.

It is `Workspace.remove()` now, called from the one `finally` that spans the workspace's whole life in `_lifecycle`. `_prepare` no longer fires `workspace_ready`; `_workspace_ready` does, one step later, so the hook sits inside that block instead of needing a cleanup path of its own. A backend that forgets the workspace cannot leak it, because forgetting is the contract.

The precedent is ordinary resource ownership: the party that acquires releases. Python's own `tempfile.TemporaryDirectory` does not hand its removal to whoever it is passed to, and pytest's `tmp_path` is not cleaned up by the test.

**Off the event loop.** The removal may have to wait — git writes its objects read-only, Windows refuses to unlink those, and a process that has only just exited can still hold a handle. That wait was a `time.sleep` inside `discard_workspace`, on the loop, stalling every other run in a fan-out. It runs on a thread now.

**The retry stays, and keeps its number.** ADR-0017 says a number nobody asked for does not get to exist, and this one is asked for by Windows: a handle outlives the process that held it by milliseconds. Five tries spanning under a second is the shape of that race, not a policy about wedged daemons, and it is no longer a knob — a caller who wants their own has `remove_workspace` and a loop to put it in. A removal that still fails is logged, never raised: the result is already decided (ADR-0016).

## Considered options

- **Bundle for every transport**, so one code path produces the refs a workspace has. Genuinely the single definition, and rejected on cost: `clone --local` hardlinks, while a bundle copies every object reachable from the base ref. On a repository with real history that is the whole thing, paid on every run, to fix a difference that is only about refs.
- **Keep `origin` and delete only the extra branches and tags.** Cheaper to reason about, and it leaves the agent able to `git fetch origin`. Rejected: it keeps the backend-dependent view (a copied sandbox has no `origin` to fetch from) and keeps the push path into the host repo. A ref the agent needs should be named, not found.
- **Leave removal on the backend and fix only the leak**, by having flow remove the workspace when no backend started. Rejected: it keeps the duplicated duty and the conformance test that makes a copy backend implement a removal it has no reason to perform. The leak was a symptom.
- **`Workspace` as an async context manager**, so `async with` removes it. Rejected: the workspace is prepared in one stage and removed after several others, and a run's own composition is a stage runner (ADR-0032), not a nest of `with` blocks. `remove()` composes with `run.anyway`, which is what a cancelled run needs anyway.
- **Delete the retry outright**, as the ticket allows. Rejected: the race is real and observed, and losing it trades a working cleanup for a rule the comment already satisfies.

## Consequences

- **Surface added:** `Workspace.branch`, `Workspace.refs` and `Workspace.remove()`, and `remove_workspace` in top-level `waystation`. **Surface removed:** `discard_workspace` from `waystation.sandbox`, and `Workspace.host_repo`, which was written and never read.
- **Breaking, twice over.** `Workspace` is a frozen dataclass whose fields changed, so anything constructing one by hand breaks; and `SandboxBackend.start` no longer removes the workspace, so a backend written before this now leaks nothing but *keeps* removing a directory core still expects to remove. The conformance suite is what says so, and it states the new contract directly: a sandbox leaves the workspace where it found it.
- **A hand-composed loop removes its own workspace.** This is the cost of taking the duty off backends: a composer who calls `prepare_workspace` and drives the stages by hand gets no cleanup for free. `await run.anyway("workspace", ws.remove(), bound=None)` is the line, and `test_stage_runner.py` shows it.
- ADR-0035's `discard_workspace` commitment was made with its expiry written down, and this is that expiry. ADR-0035 is amended rather than left describing a name that is gone.
- `_RunRecord` gains `branch`. Preservation reads it instead of re-deriving, which is why the record holds it at all.
- **`Workspace` is the second value object with behaviour**, after `CommandFailed` (ADR-0025). `architecture.md` says so rather than leaving its "one documented exception" line to rot. A workspace is a resource as well as a value; the alternative was a free function that every caller has to remember, which is the shape this decision is removing.
- **`elapsed["workspace"]` now covers the removal too.** Stage times accumulate, and the removal runs under `run.anyway("workspace", …)` — the same way a sandbox teardown already accumulates into `elapsed["sandbox"]` (`flow.py`'s `_teardown`). Consistent rather than surprising, but it is a change in what the number means.
- **The copy transport's ref parity is proved without docker.** `DockerSandbox`'s default transport is `auto`, which chooses bind on Linux, so the conformance suite alone would check copy on no CI leg at all. `test_clone_in.py` holds it on the git tier, including a two-ref workspace stood up by hand — the case that made the bundle/fetch mismatch visible, and the one #32 would have hit first.
- Two test backends had the bug in miniature: `CopyingHost` removed core's workspace and left its own copy behind, and `ThinHost` removed a workspace it only ever read. Both are the mistake the old contract invited.

Decided in [#76](https://github.com/jeffrichley/waystation/issues/76).
