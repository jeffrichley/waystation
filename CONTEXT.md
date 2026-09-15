# Waystation

A bare Python library for orchestrating sandboxed AI coding agents against git repositories. Flows are plain Python owned by the user; waystation supplies the primitives.

## Language

**Flow script**:
A plain Python script, owned by the user, that orchestrates agent runs. The hackable surface — waystation never owns or generates it.
_Avoid_: workflow, pipeline, config

**Run**:
One agent execution against a host repo: prepare workspace, start sandbox, execute agent, collect commits, integrate them back.
_Avoid_: job, ticket, session

**Primitive**:
A public, composable building block of a run that a flow script may use individually instead of the whole loop.

**Sandbox**:
The isolated, ephemeral environment an agent executes in — created for a run, destroyed after it.
_Avoid_: container (that's one backend's implementation detail)

**Sandbox backend**:
A pluggable implementation of sandbox isolation (e.g. Docker, user-namespace, none).
_Avoid_: provider (reserved for agents)

**Agent**:
The AI coding tool executed inside a sandbox (e.g. Claude Code).

**Agent provider**:
The adapter that lets waystation run a specific agent: builds its command line and environment, parses its output.

**Branch strategy**:
The rule for how a run's commits reach the host repo: `head`, `merge-to-head`, or a named branch.

**Base ref**:
The ref in the host repo a run's workspace starts from; defaults to the host repo's HEAD. Only committed state — a run never sees the host repo's uncommitted changes.

**Outcome**:
The structured result an agent reports back to the flow script at the end of a run.
_Avoid_: result, response

**Host repo**:
The git repository a run targets. Waystation core speaks only git, the VCS.
_Avoid_: any forge name — GitHub/Bitbucket (forges are out of core; forge operations belong to flow scripts)

**Hook**:
A user-supplied function invoked at a named lifecycle point of a run.
_Avoid_: callback, listener
