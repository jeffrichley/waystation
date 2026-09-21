# Research

Each is a dated record of what was found, linked from the ticket that asked. The decisions they fed are in [the ADRs](../adr/README.md).

* [Running Claude Code headless in Docker](claude-headless-docker.md) (#8): argv, auth, stream-json parsing, and what the image needs.
* [Docker startup efficiency](docker-startup-efficiency.md) (#10): measured start and teardown costs, and why `rm -f` beats `docker stop`.
* [Git worktrees and Docker bind mounts on Windows](windows-worktrees-docker.md) (#9): why a bind-mounted worktree breaks on a Windows host, and what clone-in avoids.
* [Sandcastle's failure-semantics ADRs](sandcastle-failure-adrs.md) (#13): sandcastle's timeouts, cancellation and error taxonomy, and which of them transfer.
* [Docs generators](docs-generators.md) (#16): MkDocs Material, Zensical, Sphinx and pdoc against the site waystation wants.
