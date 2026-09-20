# Decisions

Each ADR is one decision, why it was made, and the options it beat. Reading one saves you re-walking a road that was already walked.

You are not expected to read them all. Scan the **Read it when** column, open the two or three that touch what you're about to change, and move on. If your change contradicts one, say so out loud rather than quietly overriding it — a decision can be revisited, but not by accident.

## The shape of the library

| ADR | Read it when you're… |
| --- | --- |
| [0003](0003-flows-are-plain-python.md) Flows are plain imperative Python | tempted to add config, templates, or a declarative layer |
| [0004](0004-pure-library-boundary.md) Pure library: no CLI, no forge, no magic directory | adding a CLI, a GitHub call, or a dotfile the library owns |
| [0021](0021-dataclass-values-pydantic-at-the-outcome.md) Values are dataclasses; pydantic validates only the Outcome | reaching for a `BaseModel` anywhere but the Outcome |
| [0022](0022-immutable-runs-no-default-integration.md) A run is immutable, and nothing integrates unless asked | adding mutation to a run spec, or a default that lands commits |
| [0031](0031-builders-own-the-bare-names-on-a-run-spec.md) A run spec's builders own the bare names; its stored values take the glossary's | adding a builder method or a stored value to a run spec |

## Failures, bounds and cancellation

| ADR | Read it when you're… |
| --- | --- |
| [0016](0016-runs-return-primitives-raise.md) Runs return typed failures; primitives raise | adding a failure kind, or deciding whether to raise |
| [0017](0017-nothing-bounded-or-trapped-by-default.md) Nothing is bounded or trapped by default | about to type a number for a timeout, retry or buffer, or deciding what a cancellation may interrupt |
| [0032](0032-a-stage-runner-carries-the-guarantees-to-a-hand-composed-loop.md) A stage runner carries a run's guarantees to a hand-composed loop | composing the primitives by hand, or moving what a run guarantees |
| [0024](0024-first-failure-wins.md) The first failure wins; later failures are logged | handling an error that arrives after the run already failed |
| [0023](0023-cancelled-exec-kills-its-process-tree.md) A cancelled exec kills its process tree | touching cancellation, signals, or how a process is killed |
| [0027](0027-host-git-is-killable-and-a-ref-swap-finishes.md) Host git is killable, and a ref swap, once started, finishes | running git on the host, or deciding what a bound may kill |

## Workspace, commits and integration

| ADR | Read it when you're… |
| --- | --- |
| [0002](0002-clone-in-from-committed-refs.md) Runs clone committed refs from a local checkout | changing how a workspace is built, or what an agent can see |
| [0006](0006-linear-series-with-salvage.md) The patch series is linear and salvaged by default | touching collect, salvage, or what counts as a valid series |
| [0029](0029-collect-looks-over-a-workspace-in-one-exec.md) Collect looks a workspace over in one exec, through git's own shell | adding a git command to collect, or reaching for `sh -c` in a sandbox |
| [0020](0020-integration-lands-with-plumbing.md) Integration lands a series with plumbing, never a working tree | writing git that touches the host repo |
| [0005](0005-integration-strategy-and-conflicts.md) Integration is a pluggable strategy; conflicts abort and preserve | adding a landing rule, or handling a conflict |
| [0033](0033-one-way-in-to-host-git-serialized-at-the-entry-points.md) `GitRepo` is the one way in to host git; landings serialize at the entry points | running git on the host from a strategy, or touching the per-repo lock |
| [0015](0015-conflict-resolution-is-a-run.md) Conflict resolution is a run, not a seam | tempted to add a resolver hook, merger or healer |

## Sandboxes

| ADR | Read it when you're… |
| --- | --- |
| [0010](0010-behavioral-sandbox-protocol.md) The sandbox protocol is behavioral; argv purity is internal | implementing or changing a sandbox backend |
| [0011](0011-never-build-or-pull-images.md) Waystation never builds or pulls images | adding anything that would fetch or build an image |
| [0012](0012-transport-option-copy-on-windows.md) Transport is a backend option, copy by default on Windows | touching how a workspace gets into a sandbox |
| [0013](0013-clearenv-allowlist.md) The sandbox environment is cleared and allowlisted | passing an environment variable into a run |
| [0014](0014-ephemeral-sandboxes-rm-f.md) Sandboxes are ephemeral, `rm -f`, never auto-reaped | changing teardown or adding cleanup |
| [0028](0028-copy-lands-in-a-workspace-the-image-owns.md) A copied workspace rides exec stdin as text, into a `/workspace` the image owns | copying a workspace in, or writing an image for `DockerSandbox` |
| [0030](0030-captured-exec-output-keeps-every-byte.md) Captured exec output keeps every byte; what people read shows U+FFFD | decoding an exec's output, writing a backend's `exec`, or printing a patch |

## Agents and outcomes

| ADR | Read it when you're… |
| --- | --- |
| [0009](0009-agent-in-sandbox-outcome-on-stdout.md) The agent runs in the sandbox and reports on stdout | touching how the agent is executed or reports back |
| [0018](0018-pure-agent-providers-bind-the-cli.md) Agent providers are pure and bind the CLI, not the SDK | writing or changing an agent provider |
| [0019](0019-provider-reports-outcome-core-validates.md) The provider reports the Outcome; core validates it | moving validation, or making a provider smarter |

## Watching a run

| ADR | Read it when you're… |
| --- | --- |
| [0008](0008-hooks-snapshot-observer.md) Hooks are an Observer with snapshot binding | adding a hook point, or changing when hooks bind |
| [0001](0001-observability-as-hook-bundles.md) Observability is opt-in and ships as hook bundles | adding logging, a dashboard, or any way to watch a run |
| [0025](0025-credentials-are-elided-in-the-value-not-at-the-call-site.md) Credentials are elided inside the value | logging a command line, or touching redaction |
| [0026](0026-built-in-observers-guard-themselves-and-hold-the-level.md) A built-in observer guards itself, and a run file holds the level | adding a shipped observer, changing what the console prints, or ending what a cancelled run left open |
| [0007](0007-fan-out-yields-never-raises.md) Fan-out yields typed results and never raises | touching fan-out or how a batch reports |

## Adding one

Next free number, house style: the decision stated flat, then **Why**, then **Considered options** with what each was rejected for, then **Consequences** when the decision costs something. End with where it was decided. Then add a row here — an ADR nobody can find is an ADR nobody reads.
