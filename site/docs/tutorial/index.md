# Tutorial

A ladder of flow scripts, each adding one idea to the one before. Read them in
order. Every rung is a plain script meant to be copied into your own repo and
changed, not a framework to configure, and each page shows the script exactly
as it sits in the repo, where CI lints and type-checks it. The docstring at the
top of each says what the rung teaches, how to run it, and what to watch while
it does.

1. [A single run](single-run.md): one agent in Docker, awaited, and its result
   matched on.
2. [Observability](observability.md): hooks, logging, and an event log.
3. [The primitives](primitives.md): the run loop, composed by hand.
4. [Implement, then review](implement-then-review.md): one run's Outcome
   decides the next.
5. [The control room](control-room.md): one prompt across several repos.
6. [Parallel prompts](parallel-prompts.md): the flagship, with conflicts
   resolved by another run.
7. [Bring your own agent](bring-your-own-agent.md): a provider of your own.

## Before the first rung

[Install](../install.md) covers what a run needs: Docker, the tutorial's image
(`just example-image`, from a clone of the repo) and a credential.

## Running one

From the root of the waystation clone:

```sh
uv run --group examples python examples/<rung>.py
uv run --env-file .env --group examples python examples/<rung>.py   # with a .env
```

The `examples` dependency group adds what the rungs use beyond the library,
such as typer for the flagship. A rung works on the repo you run it from, so
`cd` into a repo of your own first and point `--project` back at the clone:

```sh
cd path/to/your/repo
uv run --project path/to/waystation --group examples \
    --env-file path/to/waystation/.env \
    python path/to/waystation/examples/single_run.py
```

These call a real agent, which costs money. CI only lints and type-checks
them, and never runs them.

## Two conventions the rungs follow

**Reusable instructions are files; situational prompts are inline strings.**
Instructions an agent gets on every run, such as how to review or how to
resolve a conflict, live in a `.md` file passed as a `Path`, so they can be
read, diffed and reviewed like code. A prompt written for this one run is a
string in the script.

**An Outcome can be any object-shaped type.** The rungs use pydantic models,
but a dataclass or a `TypedDict` works too: waystation builds the JSON Schema
the agent reports against from whichever you pass as `outcome=`, and validates
the agent's report against it before you see it.
