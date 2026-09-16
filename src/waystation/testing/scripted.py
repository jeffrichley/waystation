"""ScriptedAgent: token-free provider that plays back canned output via sh."""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from waystation.agents.outcome import OUTCOME_MARKER, find_outcome
from waystation.agents.protocol import (
    AgentCommand,
    AgentEvent,
    AgentText,
    OutcomeReported,
)


def _find_sh() -> str:
    found = shutil.which("sh")
    if found:
        return found
    if sys.platform == "win32":
        candidates: list[Path] = [
            Path(r"C:\Program Files\Git\bin\sh.exe"),
            Path(r"C:\Program Files\Git\usr\bin\sh.exe"),
            Path(r"C:\Program Files (x86)\Git\bin\sh.exe"),
            Path(r"E:\Program Files\Git\bin\sh.exe"),
            Path(r"E:\Program Files\Git\usr\bin\sh.exe"),
        ]
        git = shutil.which("git")
        if git is not None:
            git_path = Path(git).resolve()
            # .../Git/cmd/git.exe → .../Git/bin/sh.exe
            root = git_path.parent.parent
            candidates.extend(
                (
                    root / "bin" / "sh.exe",
                    root / "usr" / "bin" / "sh.exe",
                )
            )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    msg = "POSIX sh not found (install Git Bash on Windows)"
    raise FileNotFoundError(msg)


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _outcome_payload(outcome: Any) -> str:
    if outcome is None:
        return ""
    if isinstance(outcome, str):
        return outcome
    if isinstance(outcome, BaseModel):
        return json.dumps(outcome.model_dump(mode="json"))
    if isinstance(outcome, Mapping):
        return json.dumps(dict(outcome))
    return json.dumps(TypeAdapter(type(outcome)).dump_python(outcome, mode="json"))


@dataclass(frozen=True, slots=True)
class ScriptedAgent:
    """Play back canned lines and an Outcome through ``sh -c``."""

    lines: Sequence[str] = ()
    outcome: Any = None
    exit_code: int = 0
    env: Mapping[str, str] = field(default_factory=dict)
    pass_env: Sequence[str] = ()

    def preflight(self) -> None:
        _find_sh()

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        del prompt, outcome_schema  # scripted playback ignores prompt/schema
        parts: list[str] = ["set -e"]
        for line in self.lines:
            parts.append(f"printf '%s\\n' {_shell_single_quote(line)}")
        payload = _outcome_payload(self.outcome)
        if payload:
            marker_line = f"{OUTCOME_MARKER} {payload}"
            parts.append(f"printf '%s\\n' {_shell_single_quote(marker_line)}")
        parts.append(f"exit {int(self.exit_code)}")
        script = "\n".join(parts)
        return AgentCommand(
            argv=(_find_sh(), "-c", script),
            stdin=None,
            env=self.env,
            pass_env=self.pass_env,
        )

    def parse(self, line: str) -> Sequence[AgentEvent]:
        events: list[AgentEvent] = []
        raw = find_outcome(line)
        if raw is not None:
            events.append(OutcomeReported(raw=raw))
        elif line:
            events.append(AgentText(text=line))
        return events
