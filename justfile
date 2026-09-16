# Recipes mirrored by CI. Local green ⇒ CI green.

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]
set shell := ["bash", "-cu"]

default:
    @just --list

# Lint, type-check, and test. Fails if any step fails.
check:
    uv run ruff check src tests examples
    uv run ruff format --check src tests examples
    uv run mypy
    uv run pytest

# Runnable demo: ScriptedAgent → Integration onto a named branch.
smoke:
    uv run python examples/smoke_collect.py
