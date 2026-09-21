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

# Install the secret-scanning hooks (.pre-commit-config.yaml), once per clone.
hooks:
    uv run pre-commit install

# Runnable demo: ScriptedAgent → Integration onto a named branch.
smoke:
    uv run python examples/smoke_collect.py

# On unix the image's user takes your uid and gid, so a workspace bound in from
# this host is its own to write. Windows copies workspaces in, so the image's
# default uid will do there.

# Build the docker test tier's image (tests/sandbox/Dockerfile).
[unix]
test-image:
    docker build --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" -t waystation-test tests/sandbox

# Build the docker test tier's image (tests/sandbox/Dockerfile).
[windows]
test-image:
    docker build -t waystation-test tests/sandbox
