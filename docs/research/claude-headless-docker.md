# Research: running Claude Code headless inside a Linux Docker container

Ticket: [#8](https://github.com/jeffrichley/waystation/issues/8)
Date: 2026-09-15

Question: what are the facts a Python orchestration library needs to launch
Claude Code non-interactively as a subprocess inside a Linux Docker container?

Sources: the [mattpocock/sandcastle](https://github.com/mattpocock/sandcastle)
repo (shallow clone at tag v0.12.0) and official Claude Code docs at
code.claude.com. File paths below are relative to the sandcastle repo root.

---

## 1. How sandcastle does it

### Image (`.sandcastle/Dockerfile`)

- Base: `FROM node:22-bookworm` plus `git`, `curl`, `jq`, and the GitHub CLI.
- Renames the base image's `node` user to `agent` (home `/home/agent`) and
  aligns its UID/GID to the host user via build args `AGENT_UID`/`AGENT_GID`
  (default 1000), so bind-mounted files share an owner with the host —
  no runtime `chown`.
- Installs Claude Code **as the non-root `agent` user** via the native
  installer: `RUN curl -fsSL https://claude.ai/install.sh | bash`, then
  `ENV PATH="/home/agent/.local/bin:$PATH"`.
- `ENTRYPOINT ["sleep", "infinity"]` — the container is a long-lived shell
  host; every agent run is a `docker exec` into it.
- The repo worktree is bind-mounted at `/home/agent/workspace` and the
  container workdir is set there.

### Container lifecycle (`src/DockerLifecycle.ts`, `src/sandboxes/docker.ts`)

- `docker run -d --name sandcastle-<uuid> -e KEY=VAL ... -v host:container
  -w <workdir> --user <uid>:<gid> <image>` — env vars (including credentials)
  are injected at container start with `-e`; `HOME` is explicitly forced to
  `/home/agent` in that env set (`docker.ts` line ~187).
- Commands run via `docker exec [-i] [-w cwd] <name> sh -c '<command>'`.
  `-i` is added only when stdin content is supplied; the prompt is written to
  the child's stdin and the pipe is closed (`docker.ts` `exec`).
- stdout is consumed **line-by-line** (Node `readline` over the exec pipe);
  each line goes to the provider's `parseStreamLine`. stderr is buffered
  separately. Non-zero exit → error surfaced with stderr, falling back to the
  parsed result text, then the last 20 stdout lines (`src/Orchestrator.ts`).
- A pre-flight `docker image inspect --format {{.Config.User}}` checks the
  image UID matches the expected host UID.

### Claude Code argv (`src/AgentProvider.ts`, `claudeCode` provider, ~line 1190)

```
claude --print --verbose [--permission-mode <mode> | --dangerously-skip-permissions]
       --output-format stream-json --model '<model>' [--effort <level>]
       [--resume '<session-id>'] [--fork-session] -p -
```

with the prompt piped over **stdin** (`-p -`), not argv — deliberately, to
avoid Linux's ~128 KiB per-argument limit (see the comments on the Cursor and
Copilot providers, which are argv-bound and capped at 120 KB).

- Default AFK behavior is `--dangerously-skip-permissions`; an explicit
  `permissionMode` option (`default | acceptEdits | plan | auto | dontAsk |
  bypassPermissions`) replaces it — the two flags are mutually exclusive on
  Claude's CLI.
- `--fork-session` is only meaningful alongside `--resume`; it writes the
  continuation as a new session instead of mutating the resumed one.

### Output parsing (`parseStreamJsonLine`, `src/AgentProvider.ts` ~line 67)

Each stdout line starting with `{` is JSON-parsed; everything else is skipped.
Three event types are consumed:

| stream-json event | condition | mapped to |
| --- | --- | --- |
| `{"type":"system","subtype":"init",...}` | has `session_id` | session id (first event of the run) |
| `{"type":"assistant","message":{"content":[...]}}` | text / `tool_use` blocks | streaming text + tool-call display |
| `{"type":"result","result":"..."}` | terminal event | final result text (last write wins) |

Token usage is **not** taken from the stream; it is parsed post-run from the
captured session JSONL (last `assistant` event's `message.usage`:
`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`,
`output_tokens`) — `parseSessionUsage`.

Structured output is a separate layer: prompts instruct the agent to emit
`<tag>...</tag>`; `src/extractStructuredOutput.ts` takes the **last**
occurrence in accumulated stdout, unwraps optional Markdown fences,
JSON-parses, and validates against a schema. Run completion is likewise
signalled in-band by a magic string (default `<promise>COMPLETE</promise>`)
plus idle/completion timeouts (default 600 s idle, 60 s post-signal grace) —
`src/Orchestrator.ts`.

### `.env` handling (`src/EnvResolver.ts`, `src/mergeProviderEnv.ts`)

- Only keys **declared** in `.sandcastle/.env` are resolved; values fall back
  to `process.env` when blank in the file. Repo-root `.env` is ignored.
- Precedence at launch: agent-provider `env` > sandbox-provider `env` >
  resolved `.env` (agent/sandbox key overlap is a hard error).
- `sandcastle init` scaffolds `CLAUDE_CODE_OAUTH_TOKEN=` with a commented
  `# ANTHROPIC_API_KEY=` fallback for the Claude Code agent
  (`src/InitService.ts` ~line 418), and its next-steps copy points at
  `claude setup-token`.

### Session capture (`src/SessionStore.ts`, `makeClaudeSessionStorage`)

Sessions live at `<projects-dir>/<encoded-cwd>/<session-id>.jsonl`, where the
encoded cwd replaces `/` and `\` with `-` (Claude Code's own convention);
sandbox side defaults to `/home/agent/.claude/projects`. Sandcastle copies the
JSONL out with `docker cp`, rewrites `cwd` fields host↔sandbox, and copies it
back in to resume — this is what makes `--resume <id>` work across container
recreations.

## 2. Auth options

Primary source: <https://code.claude.com/docs/en/authentication>.

Credential precedence (abridged; full list on the auth page): cloud-provider
vars > `ANTHROPIC_AUTH_TOKEN` > `ANTHROPIC_API_KEY` > `apiKeyHelper` >
`CLAUDE_CODE_OAUTH_TOKEN` > profiles > `/login` OAuth.

| | `CLAUDE_CODE_OAUTH_TOKEN` | `ANTHROPIC_API_KEY` |
| --- | --- | --- |
| Provisioning | run `claude setup-token` on a host with a browser; approve; copy the printed token (it is not saved anywhere) | create a key in the Claude Console (platform.claude.com) |
| Bills against | Claude subscription (Pro/Max/Team/Enterprise) and its rate limits | Console API billing, per token |
| Lifetime | one year | until revoked |
| Restrictions | model requests only — no Remote Control, no claude.ai connectors; **not read in `--bare` mode** | none of those; the documented credential for `--bare`; in `-p` mode "the key is always used when present" |
| Header | OAuth bearer | `X-Api-Key` |

Other facts:

- Interactive `/login` credentials on Linux land in
  `~/.claude/.credentials.json` (mode 0600) — a volume-mounted `~/.claude`
  plus `CLAUDE_CONFIG_DIR` can persist a browser login across container
  rebuilds, but for unattended containers env-var injection is the documented
  path (<https://code.claude.com/docs/en/devcontainer>).
- `apiKeyHelper` (settings) supports rotating credentials from a vault;
  re-run every 5 min by default (`CLAUDE_CODE_API_KEY_HELPER_TTL_MS`).
- Trade-off summary: OAuth token = flat-rate subscription billing but expires
  yearly, can't be scoped, and breaks under `--bare`; API key = per-token
  spend (`--max-budget-usd` can cap a run) and works everywhere.

## 3. Headless invocation

Primary sources: <https://code.claude.com/docs/en/headless>,
<https://code.claude.com/docs/en/cli-reference>.

- `-p` / `--print` = non-interactive mode. Prompt via argv, or piped on
  stdin (`cat f | claude -p "..."` or `-p -`); **stdin is capped at 10 MB**.
  Exit code 0 on success, non-zero on failure; SIGTERM → exit 143 with the
  in-flight turn unfinished (SIGINT ends the turn cleanly first).
- `--output-format`: `text` (default) | `json` (single object: `result`,
  `session_id`, `total_cost_usd`, usage, `structured_output`,
  `permission_denials`) | `stream-json` (NDJSON, one event per line;
  final line is the `result` event). `stream-json` with `-p` is used with
  `--verbose` (all doc examples pair them; sandcastle passes `--verbose`
  unconditionally). `--include-partial-messages` adds token-level
  `stream_event` deltas.
- `--json-schema '<schema>'` with `--output-format json` returns validated
  structured output in the `structured_output` field — the first-class
  alternative to sandcastle's tag-scraping.
- Permission modes for unattended runs (`-p` starts in Manual by default):
  - `--permission-mode` `default|acceptEdits|plan|auto|dontAsk|bypassPermissions|manual`.
  - `--dangerously-skip-permissions` ≡ `bypassPermissions`; **rejected when
    launched as root** — hence sandcastle's non-root `agent` user. Documented
    as acceptable inside containers for trusted repos
    (<https://code.claude.com/docs/en/devcontainer>, "Run without permission
    prompts"), with the warning that a malicious repo can exfiltrate anything
    in the container including `~/.claude` credentials.
  - Finer-grained: `--allowedTools "Bash(git diff *),Read,Edit"` /
    `--disallowedTools`; `--permission-prompts none` (v2.1.259+) denies
    anything that would prompt instead of hanging.
- Limits: `--max-turns <n>` and `--max-budget-usd <n>` (both print-mode
  only). There is no wall-clock flag — enforce time limits from the
  orchestrator (sandcastle uses its own idle + completion timers and kills
  the exec).
- System prompt: `--append-system-prompt` / `--append-system-prompt-file`
  (keep defaults, add instructions); `--system-prompt[-file]` replaces
  entirely; `--append-subagent-system-prompt` for subagents (`-p` only).
- Sessions: `--continue` (most recent), `--resume <id|jsonl-path>`,
  `--session-id <uuid>` (pre-pick the id — handy for an orchestrator that
  wants to know the id before the run), `--fork-session`,
  `--no-session-persistence`.
- `--bare` (recommended for scripted calls, future default for `-p`): skips
  hooks, skills, plugins, MCP, CLAUDE.md, auto memory — reproducible CI runs,
  faster startup. Caveat: no OAuth/keychain — needs `ANTHROPIC_API_KEY` or
  `apiKeyHelper`. Without `--bare`, a `-p` run executes project hooks and
  `.mcp.json` servers **without any trust prompt** — a real consideration when
  running untrusted repos in a container.

## 4. Container environment requirements

Primary sources: <https://code.claude.com/docs/en/setup>,
<https://code.claude.com/docs/en/devcontainer>,
<https://code.claude.com/docs/en/env-vars>.

- **Node is not required at runtime.** The native installer
  (`curl -fsSL https://claude.ai/install.sh | bash`) and the npm package both
  install a self-contained native binary (npm needs Node 22+ only to run the
  install). Debian 10+/Ubuntu 20.04+/Alpine 3.19+ are supported; Alpine needs
  `libgcc libstdc++ ripgrep` plus `USE_BUILTIN_RIPGREP=0`.
- Installer drops a launcher at `~/.local/bin/claude` (versions under
  `~/.local/share/claude/`) — so `HOME` must be set and writable, and
  `~/.local/bin` on `PATH`. Sandcastle forces `HOME=/home/agent` on
  `docker run` because `docker exec --user` does not set it.
- State/config: `~/.claude/` (settings, sessions under
  `~/.claude/projects/<encoded-cwd>/<id>.jsonl`, credentials on Linux) plus
  `~/.claude.json` (OAuth account, per-project trust). `CLAUDE_CONFIG_DIR`
  relocates all of it — mount a volume there to persist across rebuilds.
- Run as **non-root** (bypassPermissions is refused as root).
- Version pinning for reproducible images: `install.sh | bash -s 2.1.89`, or
  `npm install -g @anthropic-ai/claude-code@X.Y.Z`; set
  `DISABLE_AUTOUPDATER=1` (native installs self-update in the background
  otherwise). `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and
  `DISABLE_TELEMETRY` cut non-inference traffic.
- Useful knobs: `ANTHROPIC_MODEL`, `ANTHROPIC_BASE_URL` (proxy/gateway),
  `API_TIMEOUT_MS` (default 600000), `MCP_TIMEOUT`,
  `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (cap on `-p` waiting for background
  subagents; default 10 min).

## 5. Getting structured JSON back on stdout — reliable patterns

1. **One-shot:** `claude -p ... --output-format json [--json-schema ...]` →
   single JSON object on stdout; parse `.result` / `.structured_output`;
   branch on exit code. Simplest for a Python `subprocess.run`.
2. **Streaming:** `--output-format stream-json --verbose` → NDJSON; read
   line-by-line, `json.loads` each line that starts with `{`, ignore the
   rest (sandcastle's exact approach). Grab `session_id` from the
   `system/init` event, text/tool calls from `assistant` events, and treat
   the final `result` event as the answer. Claude Code waits (≤30 s) for a
   slow consumer to drain before exiting, so a blocking reader won't lose
   the tail.
3. **In-band tags** (sandcastle's extra layer): instruct the model to emit
   `<tag>...</tag>`, take the last occurrence, unwrap fences, parse,
   validate. Works on any output format but is prompt-discipline-dependent;
   `--json-schema` supersedes it for single-shot runs.
4. Failure detail discipline (from sandcastle): on non-zero exit prefer
   stderr, then the last parsed `result` (CLIs emit auth/rate-limit errors
   as stdout events), then the stdout tail. Keep only a bounded tail of raw
   stdout to avoid unbounded memory on long runs.

## Decision-relevant summary for waystation

1. Proven argv shape: `claude --print --verbose --dangerously-skip-permissions
   --output-format stream-json --model <m> -p -` with the prompt on stdin,
   run as a non-root user via `docker exec -i` into a `sleep infinity`
   container.
2. Auth is just env: inject `CLAUDE_CODE_OAUTH_TOKEN` (from
   `claude setup-token`, 1-year, subscription-billed) or `ANTHROPIC_API_KEY`
   (Console-billed, required for `--bare`) at `docker run -e`; no dotfiles
   needed in the image.
3. Parsing contract: NDJSON lines — `system/init` → session id, `assistant`
   → text/tool_use, final `result` → answer; or skip streaming and use
   `--output-format json` + `--json-schema` for schema-validated one-shots.
4. Image needs: any supported Linux base, `HOME` set, `~/.local/bin` on
   `PATH`, non-root user, `DISABLE_AUTOUPDATER=1` + pinned version for
   reproducibility; Node only if the workload itself needs it.
5. Guardrails the CLI gives you: `--max-turns`, `--max-budget-usd`,
   `--allowedTools`/`--disallowedTools`, `--permission-prompts none`,
   `--bare`; wall-clock timeouts stay the orchestrator's job.
