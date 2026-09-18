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

Debian bookworm (git 2.39), Ubuntu 22.04 (2.34) and older Xcode command-line tools cannot host waystation without a newer git. The integrate stage runs none of the user's git hooks. The per-repo lock is in-process only; when something outside the process moves the target between read and write, the compare-and-swap fails and the run fails with `Refused("target_moved")`, its series preserved — the flow script re-integrates, as a Kubernetes client re-reads after a 409. The refusal is given only when a re-read shows the target changed: a swap git fails with the target where it was — another process holding the ref's lock, say — surfaces as git's own `CommandFailed`, because `Refused` is reserved for what waystation checked itself (ADR-0016), and a retry loop keyed on `target_moved` must not spin on a lock or a permissions error. A move caught while the other process still holds its lock reads as unmoved and surfaces the same way; the series is preserved either way. No cross-process file lock: it would write into the host's `.git`, and the compare-and-swap already keeps the target safe.

"Checked out" means what `git branch -f` means by it: a worktree's HEAD, or the branch a rebase or bisect paused there will return to — a rebase detaches HEAD, but finishing it writes that branch over whatever landed. The refusal holds even when the series is empty, as `git branch -f` refuses a no-op: the target is wrong whatever the agent did, and a batch should not pass or fail on which runs happened to commit.

Decided in [wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7); facts gathered on this host against git 2.34, 2.43 and 2.54. The `target_moved` and `target_checked_out` edges were settled in [#24](https://github.com/jeffrichley/waystation/issues/24).
