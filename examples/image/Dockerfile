# The examples' sandbox image. Build it with `just example-image`, which tags
# it `waystation-dev`; `just example-image cursor` builds the `cursor` stage
# below as `waystation-cursor`. Waystation never builds or pulls an image
# (ADR-0011): this one is yours to copy, trim, and extend.
#
# ── Don't remove these ──────────────────────────────────────────────────────
# DockerSandbox needs every one of them, whatever else you change:
#   - git and sh: the workspace is a git repo, and collect is one sh script.
#   - `sleep infinity` and `base64` (coreutils): the container idles on the
#     first, and a copied workspace rides in on the second (ADR-0028).
#   - a writable /tmp: each exec records its process group there, so a
#     cancelled exec is killed with everything it started (ADR-0023).
#   - a non-root user that owns /workspace: the agent runs as that user, and
#     a copied workspace is cloned into /workspace (ADR-0028). On Linux the
#     recipe gives it your uid and gid, so a bound workspace is its own.
# ────────────────────────────────────────────────────────────────────────────

FROM python:3.13-slim-bookworm AS dev

# curl and ca-certificates are for the installers below. No Node and no gh:
# Claude Code's native build needs neither, and a forge CLI belongs to a flow
# script that wants one, not to every sandbox.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# uv, for agents working on Python projects. Pinned, like everything here: an
# image that changes under you makes two runs of one flow disagree.
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /usr/local/bin/

ARG UID=1000
ARG GID=1000
RUN groupadd --non-unique --gid "$GID" agent \
 && useradd --non-unique --uid "$UID" --gid "$GID" --create-home agent \
 && install -d -o agent -g agent /workspace

USER agent
WORKDIR /home/agent
ENV PATH="/home/agent/.local/bin:$PATH"

# Claude Code from the native installer, as the agent user, into its home.
# The version is pinned and the autoupdater is off, so every run of a flow
# uses the Claude Code this image was built with.
ARG CLAUDE_CODE_VERSION=2.1.278
ENV DISABLE_AUTOUPDATER=1
RUN curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" \
 && claude --version

# The agent loads its skills, plugins and settings from here, never from your
# host. A tiny skill of this repo's shows the shape; copy yours in the same way.
COPY --chown=agent:agent skills/ /home/agent/.claude/skills/

# A plugin installs the same way, at build time:
# RUN claude plugin marketplace add anthropics/claude-plugins-official \
#  && claude plugin install <plugin>@claude-plugins-official

# ── cursor ──────────────────────────────────────────────────────────────────
# Everything above, plus the Cursor agent CLI, for rung 7's provider. Its
# installer serves one release and takes no version, so this stage is pinned
# only as far as the day you build it.
FROM dev AS cursor

RUN curl -fsS https://cursor.com/install | bash \
 && cursor-agent --version
