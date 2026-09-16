---
status: accepted
---

# Runs clone committed refs from a local checkout

A run points at a local checkout path plus an optional base ref, defaulting to the host repo's HEAD. The workspace is a clone of that ref, so the host's uncommitted and untracked state is invisible to the agent — by design, not by error. Why: clone-in makes the workspace a private copy the sandbox owns, keeps URL sources purely additive later, and avoids a speculative `Source` abstraction; a user holding only a URL writes `git clone` themselves.

## Considered options

- Bind-mounting the host worktree so the agent sees the working tree as-is (see ADR-0012 for why not).
- Accepting URL sources in v0.

Decided in [wayfinder ticket 2](https://github.com/jeffrichley/waystation/issues/2).
