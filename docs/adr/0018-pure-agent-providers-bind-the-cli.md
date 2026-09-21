---
status: stable
type: adr
---

# Agent providers are pure and bind the agent CLI, not its SDK

An agent provider is a stateless, frozen value with three methods: `preflight()` checks the host only (an agent's credentials are resolvable) and raises `PreflightError`; `command(prompt, outcome_schema)` returns an `AgentCommand(argv, stdin, env, pass_env)`; `parse(line)` turns one stdout line into agent events. It never touches the sandbox: core runs the single agent exec and owns the silence, wall and completion-grace timers, the exit code, bounded output tails, Outcome validation (ADR-0019), hooks and logging. The prompt (`str | Path`, read when the agent stage starts) and the outcome schema belong to the run, not the provider, so one provider instance set as a `Flow` default serves every concurrent run of a fan-out — which is why a provider holds no per-run state. Claude Code is bound through its CLI's `stream-json` output, not the Claude Agent SDK: the CLI inside the sandbox loads the Claude Code system prompt, every settings source, plugins, skills, MCP servers and tools by default, while the SDK starts from a minimal system prompt and would own the `claude` process itself. `ClaudeCode` forces `-p --verbose --output-format stream-json` with the prompt on stdin, because the silence timer resets per line and a one-shot `json` run emits nothing until it ends, and defaults `permission_mode` to `bypassPermissions`: an unattended agent cannot answer a prompt, and the sandbox is the trust boundary.

Why: Bazel rules declare actions and the executor runs them; a Kubernetes pod spec declares `command`, `args`, `env` and `stdin` and the kubelet supervises. A behavioural provider would re-implement ADR-0016 and ADR-0017 once per agent, each slightly differently; a pure one unit-tests on canned lines with no sandbox.

## Considered options

- A behavioural provider, `async run(sandbox, prompt, ...)`. Rejected: timer, tail and Outcome semantics duplicated per provider. Its one gain — several execs, or files written before launch — is covered by an `sh -c` wrap (Codex's file-only `--output-schema`) or by the image.
- The Claude Agent SDK on the host with a custom `Transport` over the sandbox exec. Rejected: two supervisors for one process, bidirectional streaming stdin on the sandbox protocol, every SDK default reset to CLI parity — and its headline extra, host-side `@tool` functions the sandboxed agent calls, is a hole through the sandbox.
- The SDK inside the image, foreman's wrapper process. Rejected: every image carries Python, the SDK and a runner matched to the waystation version.
- Prompt and schema on the provider, `ClaudeCode(prompt=..., outcome_schema=...)` as prototyped in ticket 4. Rejected: every provider re-implements the plumbing and an agent configuration cannot be reused across prompts — the shape of the Claude Agent SDK's `query(prompt, options)` and OpenAI Agents' `Runner.run(agent, input)`.

## Consequences

The sandbox exec (ADR-0010) takes `stdin`, and the agent exec keeps bounded tails rather than a whole-run buffer. Whatever an agent can use comes from the image (user scope: `~/.claude` plugins, skills, settings, MCP servers, binaries) or the workspace's committed `.claude/` and `.mcp.json` (project scope); waystation injects no host paths per run and never mounts the host's `~/.claude`, which carries credentials (ADR-0013). Preflight widens from sandbox backends to agent providers. Provider exceptions need no new type: `command` or `parse` raising becomes `RunFailed(stage="agent", Errored)`.

Decided in [wayfinder ticket 15](https://github.com/jeffrichley/waystation/issues/15).
