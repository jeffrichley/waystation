# Git worktrees + Docker bind mounts on Windows hosts

Research for [#9](https://github.com/jeffrichley/waystation/issues/9), part of the
wayfinder map ([#1](https://github.com/jeffrichley/waystation/issues/1)).

**Question.** Establish the facts that gate the workspace model on a Windows 11 +
Docker Desktop host: waystation creates git worktrees of a host repo and lets an
agent inside a Linux container commit to them. Do worktree `.git` pointer files
break inside a bind mount? What are the workarounds? Where does `git clone
--local` degrade? What are the bind-mount facts (paths, UID/permissions, I/O,
line endings)? How does sandcastle split bind-mount vs isolated providers?

**Method.** Local experiments on this exact host class (Windows 11 Home
10.0.26200, Git for Windows 2.43.0.windows.1, Docker Desktop engine 28.0.4 on
the WSL2 backend, git 2.54.0 inside an `alpine/git` container), plus git docs,
Docker Desktop docs, and the sandcastle source (shallow clone of
`mattpocock/sandcastle`, 2026-09-15). Facts verified by experiment are marked
**[verified]**; everything else cites its source.

---

## 1. Git worktree mechanics

### 1.1 The pointer files

A linked worktree's `.git` is a **file**, not a directory. **[verified]** —
created a scratch repo + worktree and inspected:

```
# wt1/.git (file)
gitdir: C:/Users/jeffr/.../scratchpad/wt-exp/mainrepo/.git/worktrees/wt1

# mainrepo/.git/worktrees/wt1/gitdir
C:/Users/jeffr/.../scratchpad/wt-exp/wt1/.git

# mainrepo/.git/worktrees/wt1/commondir
../..
```

So there are **two absolute host paths**, one in each direction
(worktree → repo, repo → worktree); only `commondir` is relative. Per
[git-worktree docs](https://git-scm.com/docs/git-worktree) (DETAILS): within a
linked worktree `$GIT_DIR` points to the private directory
(`.git/worktrees/<name>`) and `$GIT_COMMON_DIR` back to the main `$GIT_DIR`,
and "these settings are made in a `.git` file located at the top directory of
the linked worktree." If a worktree is moved manually, "you need to update the
`gitdir` file in the entry's directory."

If the main repo is missing from the expected path, any git command in the
worktree dies: `fatal: not a git repository: <host path>` **[verified]** (moved
the main repo aside and ran `git status` in the worktree; exit 128).

### 1.2 Inside a Linux container: broken either way

**Mounting only the worktree fails. [verified]** `docker run -v <wt>:/wt1` →
`git status` in `/wt1` gives `fatal: not a git repository: (null)`.

**Mounting both repo and worktree also fails. [verified]** Same error. The
pointer says `gitdir: C:/Users/...`; on Linux that string does not start with
`/`, so git treats it as a *relative* path and resolution fails regardless of
where the repo is mounted. (Same analysis in sandcastle ADR-0006, below.)

### 1.3 Workarounds, ranked

**(a) `git worktree repair` inside the container — works but is destructive to
the host. [verified]** With both mounted (`/mainrepo`, `/wt1`),
`git worktree repair /wt1` rewrote `/wt1/.git` to
`gitdir: /mainrepo/.git/worktrees/wt1` and the reverse `gitdir` file; the agent
could then `git status`, `add`, `commit` normally. But the pointer files are
*shared mutable state on the host disk*: after the container repair, host-side
`git status` in the worktree failed (`fatal: not a git repository:
/mainrepo/.git/worktrees/wt1`) until `git worktree repair <path>` was re-run on
the host. Host and container can never both be valid at the same time.
[git-worktree docs — REPAIR](https://git-scm.com/docs/git-worktree) confirms
repair "reestablish[es] the connection" after manual moves, in both directions.

**(b) `GIT_DIR` + `GIT_WORK_TREE` env vars — works, non-destructive.
[verified]** With both mounted and *no* file edits:

```sh
export GIT_DIR=/mainrepo/.git/worktrees/wt1 GIT_WORK_TREE=/wt1
```

`git status`, `git add`, `git commit` all worked inside the container; the host
worktree stayed valid throughout. This works because setting `GIT_DIR` skips
repository discovery entirely (the `.git` pointer file is never read;
`commondir` is relative and resolves). Bonus **[verified]**: skipping discovery
also skips the `safe.directory` ownership check — the same commands ran clean
as `--user 1000:1000` even though the mount is root-owned, whereas normal
discovery as uid 1000 fails (see §3.2). Cost: every git invocation in the
container needs the env (or a wrapper), and `GIT_DIR` leaking into a
subdirectory operation can confuse tools that spawn their own git.

**(c) Overlay-mount a corrected `.git` pointer file — sandcastle's answer,
non-destructive.** Write a *temp file on the host* containing
`gitdir: /<container-path>/.git/worktrees/<name>`, then bind-mount it **over**
the worktree's `.git` file (a single-file bind mount shadows the original
inside the container only), and mount the parent `.git` dir at a fixed POSIX
path. Host files are never modified; no env vars needed; git works from
container start. This is sandcastle ADR-0006's design (see §4.2).

**(d) Commits flow back through the shared `.git` automatically. [verified]**
Because branch refs and objects live in the *main repo's* `.git` (bind-mounted),
a commit made inside the container was immediately visible on the host via
`git -C mainrepo log feature1` — no copy-out step. This is the payoff that any
bind-mount strategy (a–c) buys.

**(e) Full clone into the container / copy-in.** Sidesteps pointer paths
entirely; commits must then be shipped back explicitly (bundle/format-patch —
see §4.3). No pointer, ownership, or line-ending coupling with the host, at the
cost of a sync protocol.

---

## 2. `git clone --local` and hardlinks

Per [git-clone docs](https://git-scm.com/docs/git-clone): with `--local` "the
files under `.git/objects/` directory are hardlinked to save space when
possible"; `--local` is the default for path-form sources; `--no-hardlinks`
forces copying. The docs also warn the operation "can race with concurrent
modification to the source repository, similar to running `cp -r`", and that it
refuses repos owned by other users.

Where it actually degrades **[verified on this host]**:

| Scenario | Result |
| --- | --- |
| Same NTFS volume (C: → C:), `--local` | Real NTFS hardlinks — `fsutil hardlink list` shows each loose object linked into both repos |
| Cross-volume (C: → E:), explicit `--local` | **Hard failure**, exit 128: `fatal: failed to create link '...': Improper link` — no fallback |
| Cross-volume (C: → E:), plain `git clone <path>` | Succeeds; objects copied (link count 1) — silent fallback |
| Inside container, source on 9p bind mount → container overlayfs, explicit `--local` | **Hard failure**: `fatal: failed to create link '...': Cross-device link` |
| Inside container, same source, plain clone or `--no-hardlinks` | Succeeds (copy) |

Takeaways: the "hardlink when possible, else copy" fallback belongs to *default*
local-path clones; **passing `--local` explicitly turns hardlink failure into a
fatal error** (observed on both git 2.43.0.windows.1 and 2.54.0). A clone from
a bind-mounted host repo into the container filesystem always crosses
filesystems (9p → overlayfs), so hardlinks are never possible there; use plain
path clone or `--no-hardlinks` and treat it as a full copy. Hardlink savings are
only real for host-side worktree/clone pools kept on one NTFS volume.

---

## 3. Docker Desktop bind-mount facts (Windows, WSL2 backend)

### 3.1 Path syntax

[Docker Desktop troubleshooting docs](https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/topics/):
"Unlike Linux, Windows requires explicit path conversion for volume mounting" —
both `C:\Users\user\work:/work` and Unix-style `/c/Users/user/work:/work` are
accepted. From Git Bash/MSYS, the shell's own path mangling corrupts `-v`
arguments; the docs' fix is `MSYS_NO_PATHCONV=1`. **[verified]** `-v
"C:\...\wt1:/wt1"` worked as-is from PowerShell. Drive letters map to `/mnt/c`,
`/mnt/e`, … inside the Docker Desktop VM; drives must be covered by Docker
Desktop's file-sharing settings.

### 3.2 What the mount is, ownership, permissions

- The Windows drive arrives in the container over **9p** (Plan 9 protocol) —
  **[verified]**: `df -T` inside the container shows filesystem type `9p` for
  the mount, `overlay` for `/`.
- Default view: everything is `root:root` with mode `0777`/`drwxrwxrwx`.
  **[verified]** via `ls -lan`. Docs (same troubleshooting page): "Docker
  Desktop sets permissions on shared volumes to a default value of 0777 … The
  default permissions on shared volumes are not configurable."
- `chmod`/`chown` inside the container **succeed and persist across container
  runs** on this stack (WSL2 metadata on the 9p share) — **[verified]**:
  `chmod 600` + `chown 1000:1000` in one container, re-read `mode=600
  owner=1000:1000` from a fresh container. Do not rely on this for security;
  the host sees NTFS ACLs, not these bits.
- **Git ownership check**: a non-root container user (`--user 1000:1000`)
  running git *by discovery* on a root-owned bind mount gets `fatal: detected
  dubious ownership in repository at '/mainrepo'` and needs
  `git config --global --add safe.directory <path>`. **[verified]** Running as
  root, or with explicit `GIT_DIR` set (§1.3b), avoids it. **[verified]**
- `core.filemode`: Git for Windows writes `core.filemode=false` into repos it
  creates (NTFS has no executable bit), and that per-repo config rides along in
  the bind-mounted `.git` — so the all-777 modes on 9p do *not* spam `git
  status` with mode changes. **[verified]** (`git config --show-origin` inside
  the container: `file:/mainrepo/.git/config core.filemode=false`.) A repo
  *created inside* the container won't have that safety net.

### 3.3 I/O performance

[Docker WSL best-practices docs](https://docs.docker.com/desktop/features/wsl/best-practices/):
"Performance is much higher when files are bind-mounted from the Linux
filesystem, rather than accessed from the Windows host filesystem" —
specifically "avoid `docker run -v /mnt/c/users:/users`". Also: inotify file
events only fire for Linux-filesystem files, so watchers don't work on
Windows-backed mounts.

Measured on this host **[verified]**: writing 400 small files took **1.25 s on
the 9p bind mount vs 0.02 s on container overlayfs — ~60x slower**. Git is
exactly this workload (many small files under `.git/objects`, index rewrites,
status stats), so `npm install`, `git status`, and object churn on a
Windows-backed bind mount pay this tax on every operation. Named volumes and
the container's own filesystem live in the WSL2 ext4 VM and run at native
speed ([bind-mounts docs](https://docs.docker.com/engine/storage/bind-mounts/):
on Docker Desktop "the daemon runs inside a Linux VM"; mount propagation is
unsupported there).

### 3.4 Line endings

Host global `core.autocrlf=true` (the standard Git-for-Windows setup — present
on this host) checks files out with CRLF while the index stores LF. `autocrlf`
is user-global, so it does **not** travel into the container: git there sees it
unset (=false), compares CRLF working-tree bytes against LF index content, and
reports the file dirty. **[verified]**: a file committed clean on the host
showed `modified` inside the container with every line changed;
`git ls-files --eol` reported `i/lf w/crlf`. Consequences for a bind-mounted
worktree: a container agent running `git add -A` re-stages the CRLF bytes
(line-ending churn commits), and tools like `sh` choke on CRLF scripts.
Mitigations: set `core.autocrlf=input` or, better, commit a `.gitattributes`
with `* text=auto` so normalization is repo-owned rather than
machine-dependent ([gitattributes docs](https://git-scm.com/docs/gitattributes),
[git-config docs](https://git-scm.com/docs/git-config)); or avoid sharing
host-checked-out files with the container at all (clone-in checks out fresh
with container-side settings — the hazard evaporates).

---

## 4. How sandcastle splits its providers

Source: `mattpocock/sandcastle` @ `main` (shallow clone, 2026-09-15),
`src/SandboxProvider.ts`, `src/sandboxes/*`, `src/mountUtils.ts`,
`src/syncIn.ts`, `src/syncOut.ts`, `docs/adr/0006-git-worktree-mounts-on-windows.md`.

### 4.1 The split

`SandboxProvider` is a tagged union of three provider kinds
(`src/SandboxProvider.ts`):

- **`bind-mount`** (docker, podman): sandcastle creates the worktree on the
  host and hands the provider `worktreePath`, `hostRepoPath`, and a list of
  host↔sandbox `mounts`; the handle exposes `exec`/`copyFileIn`/`copyFileOut`.
  "Sandcastle handles worktree creation, git mount resolution, and commit
  extraction."
- **`isolated`** (daytona, vercel): `create` receives *only env* — no host
  paths at all; the handle adds `copyIn` (files/dirs) for transfer.
- **`none`**: run directly on the host.

Branch strategies encode the consequence: bind-mount providers support
`head` (agent writes straight into the host working dir), `merge-to-head`, and
`branch`; **isolated providers exclude `head`** — the type alias is annotated
"no head — can't write to host".

The docker provider (`src/sandboxes/docker.ts`) bind-mounts "the worktree and
git directories", runs the container `--user <uid>:<gid>` with a **pre-flight
check that the image's baked-in UID matches** (UID alignment via build arg,
their ADR-0014) — their answer to §3.2's ownership problem on Linux hosts.

### 4.2 Their Windows fix = workaround (c)

ADR-0006 ("Git worktree mounts on Windows") documents exactly the two failures
verified in §1.2 — the parent `.git` has no valid sandbox path, and the `.git`
file's `C:\...` value "treats `C:\Users\...` as a relative path (since it
doesn't start with `/`)". Their decision, implemented in
`patchGitMountsForWindows` (`src/mountUtils.ts`):

1. mount the parent `.git` dir at the fixed path `/.sandcastle-parent-git`;
2. write a corrected pointer file (`gitdir:
   /.sandcastle-parent-git/worktrees/<name>`) to a host temp file and
   bind-mount it **over** `<sandboxRepoDir>/.git`.

Both happen before container start ("no post-start patching"); rejected
alternatives include post-start exec patching (timing window) and "fall back to
isolated mode on Windows … works but sacrifices bind-mount performance". Note
their perf rationale predates weighing §3.3: on a Windows host the bind mount
is itself the slow path, which weakens that rejection.

### 4.3 Collecting commits back (isolated)

- **Sync-in** (`src/syncIn.ts`): `git bundle create --all` on the host →
  `copyIn` the bundle → `git clone <bundle>` inside the sandbox → verify HEAD.
- **Sync-out** (`src/syncOut.ts`): three-prong, saved to
  `.sandcastle/patches/<timestamp>/` before applying so failures are
  recoverable: committed work via `git format-patch` + host-side
  `git am --3way`; uncommitted via `git diff HEAD` + `git apply`; untracked via
  `git ls-files --others` + per-file `copyFileOut`. A sandbox-owned ref
  (`refs/sandcastle/sync-base`, ADR-0017) tracks the last-synced commit;
  "sync-out ships commits, not refs."

For bind-mount providers there is no sync-out: commits land in the shared
`.git` directly (§1.3d).

---

## 5. What this gates for waystation

1. **Bind-mounting host worktrees into Linux containers on Windows is workable
   but never free.** The pointer files break by default (§1.2); the only
   host-safe fixes are the overlay pointer file (§1.3c, sandcastle-proven) or
   per-invocation `GIT_DIR` env (§1.3b). `git worktree repair` inside the
   container silently breaks the host side (§1.3a) and must not be the
   mechanism.
2. **Even fixed, the Windows bind mount carries three ongoing taxes**: ~60x
   small-file I/O penalty on every git/npm operation (§3.3), CRLF dirty-state
   and churn hazards from host-checked-out files (§3.4), and root-ownership /
   `safe.directory` friction for non-root container users (§3.2). None of
   these exist for a repo that lives in the container filesystem.
3. **Clone-in should be plain `git clone <path>` or bundle-based — never
   explicit `--local`.** Container-side clones always cross filesystems, where
   explicit `--local` is a hard failure, not a graceful degrade (§2). Hardlink
   dedup is real only host-side within one NTFS volume.
4. **Copy-in/clone-in costs a sync protocol, and sandcastle has already shaped
   it**: bundle in; format-patch/diff/untracked out; ship commits, not refs
   (§4.3). That protocol is host-path-free, immune to §1–§3 entirely, and its
   perf downside ("sacrifices bind-mount performance", ADR-0006) is inverted on
   Windows, where the bind mount is the slow path.
5. **If waystation keeps a bind-mount provider for Windows anyway**, the proven
   recipe is sandcastle's: pre-start corrected-pointer overlay mount +
   deterministic parent-git mount path + UID-aligned image, plus repo-owned
   `.gitattributes` normalization to defuse CRLF.

---

## Sources

- git-worktree docs — https://git-scm.com/docs/git-worktree (DETAILS, REPAIR)
- git-clone docs — https://git-scm.com/docs/git-clone (`--local`, `--no-hardlinks`)
- git-config / gitattributes docs — https://git-scm.com/docs/git-config, https://git-scm.com/docs/gitattributes
- Docker Desktop WSL best practices — https://docs.docker.com/desktop/features/wsl/best-practices/
- Docker bind mounts — https://docs.docker.com/engine/storage/bind-mounts/
- Docker Desktop troubleshooting (path conversion, shared-volume permissions) — https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/topics/
- sandcastle source — https://github.com/mattpocock/sandcastle (`src/SandboxProvider.ts`, `src/sandboxes/docker.ts`, `src/mountUtils.ts`, `src/syncIn.ts`, `src/syncOut.ts`, `docs/adr/0006-git-worktree-mounts-on-windows.md`)
- Local experiments, 2026-09-15: Windows 11 Home 10.0.26200, Git for Windows 2.43.0.windows.1, Docker Desktop engine 28.0.4 (WSL2/Linux), `alpine/git` (git 2.54.0)
