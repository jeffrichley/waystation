# Waystation

A bare Python library for orchestrating sandboxed AI coding agents against git repositories. Flows are plain Python owned by the user; waystation supplies the primitives.

## Language

**Flow script**:
A plain Python script, owned by the user, that orchestrates agent runs. The hackable surface — waystation never owns or generates it.
_Avoid_: workflow, pipeline, config

**Run**:
One agent execution against a host repo: prepare workspace, start sandbox, execute agent, collect commits, integrate them back.
_Avoid_: job, ticket, session

**Workspace**:
The run's private copy of the host repo at the base ref — what the agent works in. Exists inside the sandbox for the run's lifetime.
_Avoid_: worktree (a git mechanism, not the concept), checkout

**Primitive**:
A public, composable building block a flow script may use individually instead of the whole loop — a stage of a run, or a cross-run tool such as fan-out.

**Fan-out**:
Running multiple runs concurrently and consuming each result as it completes.

**Batch**:
The iterable of runs handed to one fan-out — heterogeneous by design: different agents, sandboxes, integration targets, even different flows.
_Avoid_: group, stage, job set

**Sandbox**:
The isolated, ephemeral environment an agent executes in — created for a run, destroyed after it.
_Avoid_: container (that's one backend's implementation detail)

**Sandbox backend**:
A pluggable implementation of sandbox isolation (e.g. Docker, user-namespace, none). Each configured instance of a backend is a sandbox spec.
_Avoid_: provider (reserved for agents)

**Sandbox spec**:
An instance of a sandbox backend: the value a flow script supplies describing the sandbox it wants — image, environment, transport. The backend it is an instance of turns it into a live sandbox; two equal specs describe the same sandbox.
_Avoid_: config, settings

**Transport**:
How a workspace gets into a sandbox — copied in, or bound in place. An option of the sandbox backend, chosen per host by default.
_Avoid_: mount (one mechanism, not the concept)

**Preflight**:
Validation a sandbox backend or agent provider performs before any run starts — proving the host can create sandboxes, every spec in a batch is satisfiable, and each agent's credentials are present. Fails fast; never repairs.
_Avoid_: setup, bootstrap

**Agent**:
The AI coding tool executed inside a sandbox (e.g. Claude Code).

**Agent provider**:
The adapter that lets waystation run a specific agent: builds its command line and environment, parses its output.

**Prompt**:
The instructions a run hands its agent. Belongs to the run, not the agent provider — one provider serves many prompts.
_Avoid_: task, message, query

**Integration strategy**:
The pluggable rule for how a run's patch series reaches the host repo. The shipped strategy is parameterized by target (head, or a named branch) and mechanism (apply linearly, or merge).
_Avoid_: branch strategy, merge-back

**Integration report**:
What integration did for a run — the target, where commits landed, conflict state. Returned alongside the Outcome when the run integrates.

**Base ref**:
The ref in the host repo a run's workspace starts from; defaults to the host repo's HEAD. Only committed state — a run never sees the host repo's uncommitted changes.

**Patch series**:
The ordered, linear sequence of commits a run produced atop its base ref — the run's collected product. Cannot contain merge commits.
_Avoid_: diff, changeset

**Salvage commit**:
The automatic final commit capturing work the agent left uncommitted at run end; it rides the patch series and is flagged on the run's result.

**Preservation branch**:
The branch that keeps a run's patch series whenever the series does not reach a target — integration conflicted, the run failed, or the run had no integration — so nothing the agent produced is lost. Created only when the series is non-empty; waystation never deletes it.
_Avoid_: backup branch, conflict branch

**Resolver run**:
An ordinary run whose prompt asks the agent to replay a preservation branch onto a target, resolving conflicts commit by commit. Nothing distinguishes it from any other run but its prompt.
_Avoid_: merger, fixer, healer, conflict handler

**Outcome**:
The structured result an agent reports back to the flow script at the end of a run.
_Avoid_: result, response

**Host repo**:
The git repository a run targets. Waystation core speaks only git, the VCS.
_Avoid_: any forge name — GitHub/Bitbucket (forges are out of core; forge operations belong to flow scripts)

**Stage**:
One of the ordered phases a run passes through: workspace, sandbox, agent, collect, integrate. A failure is attributed to the stage it happened in.
_Avoid_: step, phase

**Hook**:
A user-supplied function invoked at a named point of a run — a stage boundary, or each line the agent emits.
_Avoid_: callback, listener
