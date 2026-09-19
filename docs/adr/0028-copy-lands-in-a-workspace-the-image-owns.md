---
status: accepted
---

# A copied workspace rides exec stdin as text, into a `/workspace` the image owns

`clone_in(sandbox, ws)` bundles the workspace branch on the host and sends it base64 over one exec's stdin. Inside, the same exec clones it into the workspace root as the sandbox's own user and sets the host's git identity. That root must be an empty directory the user can write. On Docker it is `/workspace`, and the image provides it: `RUN install -d -o <user> /workspace`. Waystation runs no root exec to create it.

Why text: `Sandbox.exec` takes `stdin: str` (ADR-0010). Widening it to bytes would change a contract every backend implements, all for one caller. Base64 costs a third more bytes on a path that is already one exec, and needs only `base64`, which coreutils and busybox both ship.

Why the image: `docker run --workdir` creates a missing directory owned by root (checked: `root:root 755` on Docker 28), so the image's user cannot clone into it. Fixing that from waystation's side means a `docker exec -u 0` on every copied start. That is one more call, about 0.5 s on Docker Desktop, and a root path into a sandbox that runs as the image's non-root user. The image is already a purpose-built artefact that has to supply git and that user, so it supplies the directory too.

Why one exec: every docker call costs about 0.5 s on Docker Desktop. Decoding, cloning and setting the identity are one script.

## Considered options

- **Bytes stdin on `Sandbox.exec`.** Rejected: a protocol change for one caller.
- **`docker cp` of the tree or the bundle.** Rejected: the files arrive root-owned, and it is Docker's alone, which ADR-0012 already moved away from.
- **A root exec that makes `/workspace`.** Rejected: an extra call on every start, and a root path.
- **Clone into `$HOME/workspace`.** Rejected: `HOME` varies by image, and a container backend's workspace root is `/workspace`.
- **A tmpfs at `/workspace`.** Rejected: it is bounded by RAM, and it is root-owned, so git refuses it as dubious ownership.
- **`safe.directory` for a bind mount from Windows.** Rejected: it gets past the ownership check but not the CRLF churn that host `core.autocrlf` leaves in the checkout (ADR-0012).

## Consequences

- An image used for copy transport must own `/workspace`. Without it, the sandbox stage fails with `CommandFailed`, and its stderr says the directory is not writable by the image's user.
- The exec is the setup ADR-0016 retries on exit 126 or 137, so it must be idempotent, and `git clone` is not: it refuses a directory an earlier try wrote to. The exec therefore makes a repository, fetches the branch from the bundle and resets onto it, all of which a second try can do over the first. It leaves a marker in `.git` saying the directory holds its own work, and any other content is still refused. There is no remote to drop, since nothing was cloned ([#38](https://github.com/jeffrichley/waystation/issues/38)).
- Only commits travel. A `workspace_ready` hook's uncommitted writes reach a bound sandbox but not a copied one.
- The workspace stage still clones into a host temp dir for every transport, and `clone_in` bundles from that clone. #18's lifecycle says copy skips the host clone. Keeping it means `prepare_workspace` stays blind to transport, the `workspace_ready` hook sees the same workspace either way, and a `--local` clone costs next to nothing.
- `clone_in` bundles the workspace branch alone. Extra refs (#32) must join the bundle when they arrive, or a copied sandbox will lack them. Until then a copied workspace has no remote, and a bound one keeps its clone's `origin`.
- The bundle is held in memory, a third larger once base64-encoded. Streaming it would take the protocol change rejected above.
- Bind needs the image's user to have the host user's uid on Linux. `just test-image` passes it as a build arg, as sandcastle's reference image does.
- Binding from a Windows host fails: git in the image's user refuses the root-owned 9p mount as dubious ownership (checked). `auto` never binds there. The docker tier skips bind on Windows, and the Linux CI job runs it.

Decided in [#37](https://github.com/jeffrichley/waystation/issues/37).
