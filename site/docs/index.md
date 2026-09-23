# waystation

A bare Python library for orchestrating sandboxed AI coding agents against git
repositories.

--8<-- "README.md:pitch"

## The flagship, in about 20 lines

--8<-- "README.md:flagship"

The full version, with a typer CLI and resolver runs that replay conflicting
work onto the batch branch, is [rung 6](tutorial/parallel-prompts.md) of the
tutorial.

## Where to go next

- **[Install](install.md)** waystation and what a run needs around it: Docker,
  an image, and a credential.
- **[The tutorial](tutorial/index.md)** is a seven-rung ladder of flow scripts,
  each adding one idea to the one before. Every rung is a real script you can
  copy into your own repo.
- **[The API reference](reference/index.md)** documents the public surface,
  module by module.

## Why it works this way

Every design decision has an ADR saying what was decided and why. Start from
the [decision index](https://github.com/jeffrichley/waystation/blob/main/docs/adr/README.md),
which says when each one is worth reading. The vocabulary these pages use
(run, flow, Outcome, series, landing) is defined in the
[glossary](https://github.com/jeffrichley/waystation/blob/main/CONTEXT.md).
