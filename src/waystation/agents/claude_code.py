"""ClaudeCode: the Claude Code CLI as a pure agent provider (ADR-0018, ADR-0019)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from waystation.agents.protocol import AgentCommand, AgentEvent
from waystation.errors import PreflightError
from waystation.observability import AGENT

__all__ = ["ClaudeCode"]

# Both are passed through, and whichever the host has set reaches the agent.
# With both set, the CLI's own precedence picks the API key in print mode:
# https://code.claude.com/docs/en/authentication#authentication-precedence
_CREDENTIALS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


@dataclass(frozen=True, slots=True)
class ClaudeCode:
    """Run the real Claude Code CLI as a run's agent."""

    model: str | None = None
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
        return ()
