# Recipes mirrored by CI. Local green ⇒ CI green.

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]
set shell := ["bash", "-cu"]

# Material prints a banner about MkDocs 2 on every build; the `site` group pins
# mkdocs<2, so it says nothing this repo can act on.
export NO_MKDOCS_2_WARNING := "true"

default:
    @just --list

# Lint, type-check, test, and build the site. Fails if any step fails.
check:
    uv run ruff check src tests examples site
    uv run ruff format --check src tests examples site
    uv run --group examples mypy
    uv run pytest
    just site-build

# Build the documentation site, failing on any warning: a broken include,
# reference or link fails it (#127).
site-build:
    uv run --group site mkdocs build --strict -f site/mkdocs.yml

# Serve the documentation site with live reload, at http://127.0.0.1:8000.
site:
    uv run --group site mkdocs serve -f site/mkdocs.yml

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

# Build the examples' image (examples/image/Dockerfile): `waystation-dev`, or
# `waystation-cursor` with `just example-image cursor`.
[unix]
example-image target="dev":
    docker build --target {{target}} --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" -t waystation-{{target}} examples/image

# Build the examples' image (examples/image/Dockerfile): `waystation-dev`, or
# `waystation-cursor` with `just example-image cursor`.
[windows]
example-image target="dev":
    docker build --target {{target}} -t waystation-{{target}} examples/image
