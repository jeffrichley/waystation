---
status: accepted
---

# Integration lands a series with plumbing, never a working tree

The shipped integration strategy checks nothing out to land a patch series. It rebuilds each patch as a commit at the base ref in a temporary index (`mailinfo`, `apply --cached`, `write-tree`, `commit-tree` — always clean, because the patches were cut against that base), replays the commits onto the target tip with one `git merge-tree --write-tree --merge-base=C^ <tip> C` per commit for `apply`, or one `merge-tree` plus a two-parent `commit-tree` for `merge`, and moves the target with a compare-and-swap `git update-ref <ref> <new> <old>`. A patch whose replay leaves the tree unchanged is skipped, as `git am` skips it. A conflict is `merge-tree`'s exit 1 with the conflicted paths; the failed patch is the loop index; nothing but unreferenced objects is left behind. `target="HEAD"` uses the same engine, then `git merge --ff-only <new>` in the checkout after the clean-tree check, so a conflict never touches the working tree. A target branch checked out in any worktree is refused (`Refused("target_checked_out")`), because `update-ref` would silently desync that checkout. Host git must be 2.40 or newer, and preflight checks it.

Why: measured on Windows, a throwaway worktree costs 5.5–15 s at 5k files and 33–40 s at 50k with a full checkout, runs the user's checkout, applypatch and commit-msg hooks and smudge filters, and a killed `worktree add` leaves a locked admin directory that `prune` skips. Plumbing takes 2–2.6 s at either size — process spawns dominate — fires only reference-transaction hooks, and yields commits byte-identical to `git am`'s for a fixed committer. It is the machinery `merge-tree --write-tree` and `git replay` were built for: forge-side merges and rebases with no checkout. Both paths keep author and message; the committer becomes the host user at landing time.

## Considered options

- A private throwaway worktree (`worktree add --detach` + `git am --3way`), sandcastle's shape. Rejected for cost, hooks and crash residue; a sparse checkout narrows the cost but `git sparse-checkout set` writes `extensions.worktreeConfig` into the host's config.
- Applying straight onto the target tip with `apply --cached --3way`. Rejected: not `am`-equivalent — it fails where the target renamed a patched file.
- `git replay`. Rejected: needs git 2.44 and reports no conflicting paths.

## Consequences

Debian bookworm (git 2.39), Ubuntu 22.04 (2.34) and older Xcode command-line tools cannot host waystation without a newer git. The integrate stage runs none of the user's git hooks.

Decided in [wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7); facts gathered on this host against git 2.34, 2.43 and 2.54.
