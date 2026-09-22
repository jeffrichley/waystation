# Waystation

A bare Python library for orchestrating sandboxed AI coding agents against git repositories. Flows are plain Python owned by the user; waystation supplies the primitives.

## Language

**Flow script**:
A plain Python script, owned by the user, that orchestrates agent runs. The hackable surface — waystation never owns or generates it.
_Avoid_: workflow, pipeline, config

**Run**:
One agent execution against a host repo: prepare workspace, start sandbox, execute agent, collect commits, and integrate them when asked.
_Avoid_: job, ticket, session

**Run spec**:
The value a flow script builds to describe a run — prompt, agent, sandbox, integration, hooks. Awaiting it performs a run; awaiting it again performs another, with a new run id.
_Avoid_: run (that's the execution), builder, job definition

**Workspace**:
The run's private copy of the host repo at the base ref — what the agent works in. Exists inside the sandbox for the run's lifetime. It names its own branch and the refs that travel with it, and core removes it; a sandbox backend never does (ADR-0037).
_Avoid_: worktree (a git mechanism, not the concept), checkout

**Refs that travel**:
The refs a workspace carries into the sandbox — the run branch, and whatever else was asked for. The same set on every transport, which is what makes a bound sandbox and a copied one the same repository.
_Avoid_: refspec (git's own word for the mapping, not the set), visible refs

**Primitive**:
A public, composable building block a flow script may use individually instead of the whole loop — a stage of a run, or a cross-run tool such as fan-out or a queue.

**Fan-out**:
Running multiple runs concurrently and consuming each result as it completes.

**Batch**:
The iterable of runs handed to one fan-out — heterogeneous by design: different agents, sandboxes, integration targets, even different flows.
_Avoid_: group, stage, job set

**Queue**:
Fan-out's open-ended peer: a block that stays open, takes a run spec whenever one is submitted, preflights each run as it starts, and yields each result as it completes until it is closed and every submitted run has reported. A batch is the case of it that is submitted and closed at once (ADR-0047).
_Avoid_: pool, scheduler, job queue, worker

**Sandbox**:
The isolated, ephemeral environment an agent executes in — created for a run, destroyed after it.
_Avoid_: container (that's one backend's implementation detail)

**Sandbox backend**:
A pluggable implementation of sandbox isolation (e.g. Docker, user-namespace, none). Each configured instance of a backend is a sandbox spec.
_Avoid_: provider (reserved for agents)

**Sandbox spec**:
An instance of a sandbox backend: the value a flow script supplies describing the sandbox it wants — image, environment, transport. The backend it is an instance of turns it into a live sandbox; two equal specs describe the same sandbox.
_Avoid_: config, settings

**Host runner**:
The public value a sandbox backend drives one host process through — `NoSandbox`'s agent, `DockerSandbox`'s client, a bwrap backend's `bwrap`. It keeps the parts of the exec contract that do not depend on what is being isolated, so a backend is a thin adapter rather than a rewrite.
_Avoid_: executor, driver, process manager

**Conformance suite**:
The sandbox contract written as tests, shipped so that a backend waystation does not ship runs the same ones. Every shipped backend subclasses it.
_Avoid_: compliance tests, contract tests (they name a different thing in Pact's sense)

**Shell**:
What a sandbox answers when asked how to run a command string: the argv a script follows there — `("sh", "-c")` in a container, the host's own absolute sh on a host backend. A caller with a script asks rather than spelling one, because only the sandbox knows.
_Avoid_: sh path, interpreter

**Shell**:
What a sandbox answers when asked how to run a command string: the argv a script follows there — `("sh", "-c")` in a container, the host's own absolute sh on a host backend. A caller with a script asks rather than spelling one, because only the sandbox knows.
_Avoid_: sh path, interpreter

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
The pluggable rule for how a run's patch series reaches the host repo. Two ship: `Integration`, parameterized by target (head, or a named branch) and mechanism (apply linearly, or merge), and `Squash`, which lands the whole series as one commit. Both are policy over the landing steps, as a user's strategy is (ADR-0040).
_Avoid_: branch strategy, merge-back

**Landing step**:
One of the public pieces of plumbing on `GitRepo` a strategy is built from: read the target, rebuild the series at its base, merge trees, commit a tree, move the target. A landing rule is the choice between them, never a rewrite of them.
_Avoid_: helper, primitive (that is a stage of a run)

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

**Stage runner**:
The value a flow script composing the primitives by hand runs each stage through: it applies that stage's bound, holds a cancellation until the stage's own work ends, and records how long the stage took. It owns no policy — which stages run, what happens after a failure, and whether a series is preserved stay with whoever composes, the run orchestrator included.
_Avoid_: pipeline, executor, driver

**Hook**:
A user-supplied function invoked at a named point of a run — a stage boundary, or each line the agent emits.
_Avoid_: callback, listener

**Run channel**:
The `waystation.run` log records: one per lifecycle event of a run, each naming its `event`. The way an observer sees the two events hooks deliberately don't carry — the agent starting, and a cancellation — and the reason a built-in observer is a peer of a user's bundle rather than a privileged one. An adapter of the user's own joins it with `run_logger`.
_Avoid_: event stream (that is `EventLog`'s JSONL), log output
