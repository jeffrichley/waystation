# The docker test tier's image: debian-slim, git and a non-root user.
# Build it with `just test-image`; waystation never builds or pulls one (ADR-0011).
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# On Linux the recipe passes the host's uid and gid, so a bound workspace is
# this user's own. A copied one is cloned into /workspace, so the user owns
# that too (ADR-0028).
ARG UID=1000
ARG GID=1000
RUN groupadd --non-unique --gid "$GID" agent \
 && useradd --non-unique --uid "$UID" --gid "$GID" --create-home agent \
 && install -d -o agent -g agent /workspace

USER agent
