# Examples

A ladder of flow scripts, each adding one idea to the one before. Read them in
order: every module docstring says what the rung teaches, how to run it, and
what to watch while it does. Each is a plain script meant to be copied into
your own repo and changed, not a framework to configure.

## Before the first rung

- **Docker**, running.
- **The example image**, built once from [`image/Dockerfile`](image/Dockerfile):

  ```sh
  just example-image          # tags waystation-dev
  just example-image cursor   # tags waystation-cursor, for rung 7
  ```

  Its header lists what `DockerSandbox` needs from any image, so you can trim
  it or write your own.
- **A Claude credential** in the environment the rung runs in. `ClaudeCode`
  passes it into the sandbox by name, and nothing else of yours goes in.
  Export it in your shell:

  ```sh
  export ANTHROPIC_API_KEY=...   # or CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`)
  ```

  Or copy [`.env.example`](../.env.example) to `.env` at this repo's root,
  which is gitignored, fill in one key, and add `--env-file .env` to the
  `uv run` commands below. uv loads it into the process; waystation itself
  reads no file.

## Running one

From the root of this repo:

```sh
uv run --group examples python examples/<rung>.py
uv run --env-file .env --group examples python examples/<rung>.py   # with a .env
```

The `examples` dependency group adds what the rungs use beyond the library,
such as typer for the flagship. A rung works on the repo you run it from, so
`cd` into a repo of your own first and point `--project` back at this one:

```sh
cd path/to/your/repo
uv run --project path/to/waystation --group examples \
    --env-file path/to/waystation/.env \
    python path/to/waystation/examples/single_run.py
```

`--env-file` is resolved from where you run, hence the full path.

These call a real agent, which costs money, so CI only lints and type-checks
them and never runs them.

## The ladder

1. [`single_run.py`](single_run.py): one `ClaudeCode` run in a
   `DockerSandbox`, awaited and consumed with `match` over the three result
   types. It lands on the HEAD of the repo you run it from, so read its loud
   note first.
2. `observability`: hooks, `configure_logging`, `on_agent_output`, `ctx.log`
   and `EventLog`. *Coming in #40.*
3. `primitives`: the run loop composed by hand, with a two-line `NoSandbox`
   swap. *Coming in #40.*
4. [`implement_then_review.py`](implement_then_review.py): an implement run
   lands on a branch, a review run reports a `Verdict` without integrating,
   and a rejection gets one bounded fix run and one re-review. The review
   instructions are a reusable prompt file, [`prompts/review.md`](prompts/review.md).
   HEAD is never touched.
5. [`control_room.py`](control_room.py): a hardcoded list of checkouts, one
   `Flow` per checkout, and one `fan_out` over them all, consuming results as
   they finish.
6. `parallel_prompts`: the typer flagship, which fans prompts out onto a shared
   batch branch with a dashboard and resolver runs. *Coming in #42.*
7. [`bring_your_own_agent.py`](bring_your_own_agent.py): a provider of your
   own, for the Cursor agent CLI, reporting its Outcome through
   `outcome_instructions` and `find_outcome`. Needs the `waystation-cursor`
   image and `CURSOR_API_KEY`.

## Outside the ladder

- [`smoke_collect.py`](smoke_collect.py): a scripted agent in a throwaway
  repo, landing on a named branch. It needs no Docker and no credential, so
  `just smoke` checks the library end to end in a second.

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
