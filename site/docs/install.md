# Install

waystation is not on PyPI yet, so install it from git into your project:

```sh
uv add git+https://github.com/jeffrichley/waystation
```

`waystation.testing` ships a token-free `ScriptedAgent` for testing your own
flows, which needs nothing extra. Its sandbox conformance suite, for people
writing a backend of their own, is pytest tests, so it wants the `testing`
extra:

```sh
uv add "waystation[testing] @ git+https://github.com/jeffrichley/waystation"
```

## What a run needs around it

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/).
- **git**, 2.40 or newer.
- **Docker**, running, for `DockerSandbox`. Waystation never builds or pulls
  an image: you build one and name it. The tutorial's image is
  [`examples/image/Dockerfile`](https://github.com/jeffrichley/waystation/blob/main/examples/image/Dockerfile),
  whose header lists what `DockerSandbox` needs from any image.
  `NoSandbox` runs the agent on the host instead, with no Docker at all.
- **A credential for the agent**, in the environment of the process running
  your flow. `ClaudeCode` passes `ANTHROPIC_API_KEY` or
  `CLAUDE_CODE_OAUTH_TOKEN` into the sandbox by name, and nothing else of
  yours goes in:

  ```sh
  export ANTHROPIC_API_KEY=...   # or CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`)
  uv run --env-file .env ...     # or keep it in a gitignored .env and let uv load it
  ```

  Waystation reads no file of its own: whatever is in the environment is what
  it can pass through.

## Running the tutorial's scripts

The rungs live in the repo, with a `just` recipe that builds their image, so
clone it:

```sh
git clone https://github.com/jeffrichley/waystation
cd waystation
just example-image          # tags waystation-dev
```

The [tutorial](tutorial/index.md) says how to run a rung against a repo of
your own.
