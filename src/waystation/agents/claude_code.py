"""ClaudeCode: the Claude Code CLI as a pure agent provider (ADR-0018, ADR-0019)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from waystation.agents.protocol import (
    AgentCommand,
    AgentEvent,
    AgentText,
    AgentToolUse,
    OutcomeReported,
)
from waystation.errors import PreflightError
from waystation.observability import AGENT
from waystation.results import AgentUsage

__all__ = ["ClaudeCode"]

# Both are passed through, and whichever the host has set reaches the agent.
# With both set, the CLI's own precedence picks the API key in print mode:
# https://code.claude.com/docs/en/authentication#authentication-precedence
_CREDENTIALS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


@dataclass(frozen=True, slots=True)
class ClaudeCode:
    """Run the real Claude Code CLI as a run's agent, with its full behaviour.

    ``claude`` runs inside the sandbox in print mode, loading its system
    prompt, settings, plugins, skills and MCP servers from the image and the
    workspace, never from the host. The run's Outcome schema goes in as
    ``--json-schema``, so the CLI itself re-prompts until the output matches.
    Frozen and stateless: one instance serves every run of a fan-out.

    Attributes:
        model: ``--model``, an alias such as ``"sonnet"`` or a full model
            name; ``None`` leaves it to the CLI's settings.
        permission_mode: ``--permission-mode``. An unattended agent cannot
            answer a permission prompt, and the sandbox is the trust boundary.
        max_turns: ``--max-turns``; ``None`` is unbounded.
        max_budget_usd: ``--max-budget-usd``; ``None`` is unbounded.
        env: variables set in the agent's environment.
        pass_env: host variables passed through to the agent, beside the two
            credentials, which always are.
        args: extra CLI arguments, appended after every flag above.
    """

    model: str | None = None
    # A str, not a Literal: the CLI owns its modes and adds to them.
    permission_mode: str = "bypassPermissions"
    max_turns: int | None = None
    max_budget_usd: float | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    pass_env: Sequence[str] = ()
    args: Sequence[str] = ()

    def preflight(self) -> None:
        """Fail unless a credential will reach the agent; log its name, never it."""
        found = [
            name for name in _CREDENTIALS if self.env.get(name) or os.environ.get(name)
        ]
        if not found:
            names = " or ".join(reversed(_CREDENTIALS))
            msg = (
                f"ClaudeCode needs {names}: set one on the host or pass it in "
                "env= (`claude setup-token` makes an OAuth token)"
            )
            raise PreflightError(msg)
        AGENT.info("Claude Code will authenticate with %s", found[0])

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        # stream-json, not json: the silence timer resets per line, and a
        # one-shot json run prints nothing until it ends (ADR-0018).
        argv = [
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--json-schema",
            json.dumps(outcome_schema),
            "--permission-mode",
            self.permission_mode,
        ]
        if self.model is not None:
            argv += ["--model", self.model]
        if self.max_turns is not None:
            argv += ["--max-turns", str(self.max_turns)]
        if self.max_budget_usd is not None:
            argv += ["--max-budget-usd", str(self.max_budget_usd)]
        # Last, so an extra flag can override one a typed setting passed.
        argv += self.args
        return AgentCommand(
            argv=tuple(argv),
            stdin=prompt,
            env=dict(self.env),
            pass_env=tuple(dict.fromkeys((*_CREDENTIALS, *self.pass_env))),
        )

    def parse(self, line: str) -> Sequence[AgentEvent]:
        """One stream-json line as events; a line it can't read yields none.

        Assistant messages yield their text and tool calls, and the final
        ``result`` event yields the Outcome and the run's usage. A raising
        parse would fail the run, so an unexpected shape is skipped, not
        trusted.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return ()
        if not isinstance(event, dict):
            return ()
        if event.get("type") == "assistant":
            return _assistant_events(event)
        if event.get("type") == "result":
            return _result_events(event)
        return ()


def _assistant_events(event: dict[str, Any]) -> list[AgentEvent]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    events: list[AgentEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind, name, text = block.get("type"), block.get("name"), block.get("text")
        if kind == "text" and isinstance(text, str):
            events.append(AgentText(text))
        elif kind == "tool_use" and isinstance(name, str):
            tool_input = block.get("input")
            events.append(
                AgentToolUse(name, tool_input if isinstance(tool_input, dict) else {})
            )
    return events


def _result_events(event: dict[str, Any]) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    # --json-schema makes the CLI re-prompt until the output validates. An
    # error result (retries exhausted, a turn or budget limit) carries none,
    # and the CLI's non-zero exit fails the run as AgentExited (ADR-0019).
    output = event.get("structured_output")
    succeeded = event.get("subtype") == "success" and not event.get("is_error")
    if succeeded and output is not None:
        events.append(OutcomeReported(output))
    usage = event.get("usage")
    if isinstance(usage, dict):
        events.append(_usage(usage, event))
    return events


def _usage(usage: dict[str, Any], result: dict[str, Any]) -> AgentUsage:
    """The run's cumulative usage, from the ``result`` event's own totals."""
    read = _count(usage.get("cache_read_input_tokens"))
    written = _count(usage.get("cache_creation_input_tokens"))
    return AgentUsage(
        # OpenTelemetry GenAI counts cached input as input; Anthropic's
        # input_tokens leaves out both cache counters, so they are summed.
        input_tokens=sum(
            n or 0 for n in (_count(usage.get("input_tokens")), read, written)
        ),
        output_tokens=_count(usage.get("output_tokens")) or 0,
        cache_read_tokens=read,
        cache_write_tokens=written,
        cost_usd=_amount(result.get("total_cost_usd")),
        turns=_count(result.get("num_turns")),
    )


def _count(value: object) -> int | None:
    # bool is an int to Python, never a count to JSON.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _amount(value: object) -> float | None:
    count = _count(value)
    if count is not None:
        return float(count)
    return value if isinstance(value, float) else None
