---
status: accepted
---

# The host-process runner is public, and one conformance suite states the exec contract

`HostRunner` is public in `waystation.sandbox`, so a backend that drives host processes — `NoSandbox`, `DockerSandbox`'s client, a bwrap backend — is a thin adapter over it rather than a rewrite. `waystation.testing.SandboxConformance` is the sandbox contract written as pytest tests; both shipped backends subclass it, and so can anyone else's. `Sandbox.exec`'s docstring states the contract in prose and names the suite as what a backend is actually held to.

Why: the contract has about seven parts, and only two of them were written down — the working directory, a killed process tree, byte-exact capture against readable callbacks, bounded tails, unbounded line length, a cleared and allowlisted environment, and the removal of a workspace dir Windows will not unlink. A backend keeping six of them fails far from itself: a patch whose bytes were replaced, a line that vanished for being long, an agent still writing while collect reads. Both shipped backends got those parts from the private `sandbox/_host.py`; a third backend could only import a private module or write ~180 lines again, which is the privileged internal path CLAUDE.md forbids and #18's readiness for a bwrap backend rules out.

Prose alone would not have done it. A rule a test enforces survives a refactor; a rule in a docstring erodes, and this one had already eroded to two parts out of seven. The suite is the statement, and the docstring points at it.

This revises [ADR-0010](0010-behavioral-sandbox-protocol.md), which made the protocol behavioral and left "one dumb runner" internal. Behavioral is unchanged and is the reason this works — a suite can only exercise a protocol described by behaviour. What changes is that the runner keeping that behaviour is a public building block instead of an implementation detail. Argv purity stays internal: `plan_create` / `plan_exec` / `plan_destroy` remain DockerSandbox's own, because they are how *that* backend is unit-tested without Docker, not something another backend needs.

`HostRunner.run` gained `on_cancel`, for a backend whose exec outlives the host process it spawned. Killing the `docker` client leaves its work running in the container; which work, and how to reach it, only the backend knows — but shielding that reach from the cancellation already on its way is the part that is easy to get subtly wrong, so the runner holds it and `_cancellation.run_to_end` stays private.

`SandboxConformance` is a base class a user subclasses, which is the one shape `docs/agents/architecture.md` rules out — so say it out loud rather than override it quietly. The ban is on the library's extension seams, where a user's code runs inside a run and inheritance would couple it to our call order. Here the call order is pytest's, not waystation's, and the shape is xUnit's, which SQLAlchemy's `sqlalchemy.testing.suite` and fsspec's `AbstractFileSystemTests` both take for exactly this problem: a pluggable backend, one contract, third parties pointing the suite at their own. A named precedent settles it (`architecture.md`, *Settling a design argument*), and architecture.md now carries the carve-out.

## Considered options

- **Publish a `HostSandbox` — the whole of `NoSandbox`'s live sandbox — instead of the runner.** Rejected twice over. Its only caller would be `NoSandbox`: Docker and bwrap wrap argv, so they need the runner, and a `workspace` that is both "the host cwd" and "the path the protocol reports" is only one thing for the backend that isolates nothing. It would also make the third-backend test vacuous — a backend that is one constructor call proves nothing about whether the contract can be kept from outside.
- **Keep the suite in `tests/`, parametrized over a fixture.** Cheapest, and it would cover the shipped backends. Rejected: a backend author cannot point it at their own, which is the whole reason the contract needed stating.
- **Ship a `check_sandbox_conformance(backend)` coroutine that raises on the first violation.** No pytest in the shipped package. Rejected: one opaque pass/fail, where the first violation hides the rest and a reader gets no named sentence per promise. The suite is meant to be read as the contract.
- **Publish `run_to_end`.** Rejected: it hands every backend with detached work the shield-and-re-raise dance to get right, which is exactly the part worth hiding.
- **Give `DockerSandbox` a `processes` field too, matching `NoSandbox`.** The asymmetry was real, but no user and no test can be named for the knob, and a spec is a value. `HostRunner`'s strategy now defaults to the host's, so nothing hard-codes `host_processes()` — which is what the asymmetry was evidence of.

## Consequences

- `waystation.sandbox` gains `HostRunner`, `LineCallback` and `discard_workspace`; top-level `waystation` gains nothing. `LineCallback` names a parameter of a protocol users implement, and `discard_workspace` is what `start` owes the workspace it was handed. `allowlisted_env` was already there (ADR-0034).
- `waystation.testing.SandboxConformance` is the other surface addition, and the reason is the decision itself: a contract a third party cannot run against is prose again.
- `waystation.testing` needs pytest, which waystation does not otherwise depend on: it is the `waystation[testing]` extra. #18's story 114 names the same package as `ScriptedAgent`'s home; that move is not made here, and whoever makes it will have to import the suite lazily so a `ScriptedAgent` user does not need pytest.
- `discard_workspace` is public because the contract today puts the removal on the backend — `start` "owns `ws` from here". #76 proposes taking that responsibility off backends, and if it does, this name and the conformance test that holds the promise both go with it. The commitment is made with its expiry stated rather than made quietly.
- The suite needs a POSIX `sh` in the sandbox. It first took the argv naming one from a `shell` fixture each subclass filled in, because a host path means nothing inside a container and a Windows host has no `sh` on PATH. **ADR-0036 deleted that fixture**: the sandbox answers now, and the suite reads `sandbox.shell` like any other caller.
- A test proves the third backend imports nothing private. Without it, one private import would quietly make the other proof vacuous.
- Tests that stated a contract part for one backend are gone from that backend's module; what stays in `test_docker_sandbox.py` is about docker.

Decided in [#75](https://github.com/jeffrichley/waystation/issues/75).
