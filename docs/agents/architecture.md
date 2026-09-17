# Architecture

The lens to design through. An **ADR** records one decision and why it beat the alternatives; this file is the standing preference you apply to every decision, including the ones too small to get an ADR. If you find yourself writing a *decision* here, it wants `docs/adr/` instead — see [the index](../adr/README.md).

## Deep modules, small interfaces

The unit of quality is the ratio of what a module does to how much of it a caller has to know. `run_agent` is the shape to aim for: one call, and behind it two stream readers, three timers, Outcome validation and failure mapping. Prefer one module that hides a hard thing over three that each expose half of it.

A wide interface is the smell, not a long file. Before adding a parameter, ask whether the caller should have had to care.

## The patterns this codebase already runs on

Reach for the one already here before inventing a shape:

- **Strategy** — anything that varies by platform, backend or policy is injected, never branched on inline. `ProcessStrategy` (how a process is spawned and killed, per OS), `SandboxBackend`, `AgentProvider`, `IntegrationStrategy`. A platform `if` inside a method is the signal that a strategy wants to be born.
- **Observer** — anything that watches a run is a hook bundle: an object with `on_<hook>` methods. Built-ins (`RunLog`, and `RunLogFiles`/`EventLog`/`Dashboard` as they land) are *peers* of a user's bundle, not a privileged path (ADR-0001).
- **Null Object** — `HookBundle`'s no-op defaults and the `NullHandler` on the logger hierarchy both mean the caller never checks for absence.
- **Value Object** — results, `Timeouts` and the `Failure` union are frozen dataclasses that hold data and answer questions, with no behaviour worth mocking (ADR-0021). `CommandFailed` redacting itself is the one documented exception, and it has an ADR (ADR-0025).
- **Adapter / Facade** — `GitRepo` wraps a git runner, `configure_logging` wraps rich and stdlib logging, `RunLoggerAdapter` wraps a logger. Each is a thin, honest hop, not a Middle Man.

Notably absent, on purpose: **Template Method**. Extension here is by injection, not by subclassing a base that calls down into you — inheritance couples a user's code to our call order. `HookBundle` is inheritable only as a convenience for signature checking; it is never required.

## When *not* to reach for a pattern

Bareness is the prime directive. A pattern that adds a seam nobody asked for is Speculative Generality, and it costs the user surface they have to read.

The test: **can you name the second implementation?** A `Strategy` with one implementation and no named second is an interface tax. Two known implementations, or a documented extension seam in the spec, and it earns its place. Otherwise write the straight-line version and leave the seam for when the need is real — a rule expressed as a table or a small named function is usually enough, the way the redactor's `_RULES` is.

## Settling a design argument

When two shapes both look defensible and the argument is going in circles, **a named precedent wins over taste.** Point at how a library with the same problem solved it — stdlib, Kubernetes, OpenTelemetry, Go's `httptrace` — and take that shape. "It matches `httptrace`" is a reason; "it feels cleaner" is not.

## Reviewing

`/code-review` runs two axes that are deliberately kept apart: **Standards** (does this follow the repo's documented rules?) and **Spec** (does it do what the ticket asked?). Code can pass one and fail the other, and merging the reports lets one mask the other.

The Standards axis carries a fixed smell baseline — Fowler, *Refactoring* ch.3. The ones that actually bite here: **Duplicated Code** (the same shape in two hunks), **Primitive Obsession** (a `str` where a domain type belongs), **Data Clumps** (the same parameters travelling together), **Shotgun Surgery** (one logical change forcing scattered edits), and **Speculative Generality**. Every one of them is a judgement call, and a documented repo standard overrides the baseline.
