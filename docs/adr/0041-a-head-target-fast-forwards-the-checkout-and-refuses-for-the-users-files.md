---
status: accepted
---

# A HEAD target fast-forwards the checkout, and refuses rather than overwrite anything of the user's

`target="HEAD"` goes through the same landing steps as a named branch (ADR-0040). `read_target("HEAD")` reads the branch the checkout has out, or the detached commit, and refuses with `Refused("dirty_tree")` if any tracked file has a staged or unstaged change. It returns a `Target` with `head=True`. The steps then build the landing in plumbing as usual, so a conflict stops before the checkout is touched and becomes `RunConflicted` (ADR-0020). `move_target` for a HEAD target runs `git merge --ff-only --no-autostash <tip>` in the checkout instead of `update-ref`. It checks three things first:

- **The checkout still stands where it was read.** Same branch, or still detached, and same commit. Otherwise it refuses with `Refused("target_moved")`. A fast-forward git fails with the checkout moved is also `target_moved`. One git fails with the checkout where it was is git's own `CommandFailed`, as with `update-ref`.
- **The checkout is still clean.** A tracked change made while the run was working is `dirty_tree`.
- **No file is where the landing puts one.** The landing's added paths come from the diff between the target's tip and the new tip. A file is in the way if it is on one of those paths, or where a directory above one of them must go. Such a file is `dirty_tree`, and the detail names it. Untracked files anywhere else, such as a logs directory, do not block.

Ignored files count as in the way. `merge` is a commit point beside `update-ref`: once started it finishes, and a cancellation is raised after it (ADR-0027).

Why: the promise is that the user's uncommitted work is never clobbered. Git's own fast-forward already refuses to overwrite an untracked file, but it overwrites an ignored one without a word. `.env`, a local config, or a scratch file matched by `.gitignore` is still the user's work, and a run's series that adds the same path is the one case where we would destroy it. The checks run again at the swap because a run is long, and the user is free to keep working in the checkout while it runs. A check made only when the run started would be stale by the swap. `--no-autostash` is there because `merge.autoStash` in the user's config would stash their work and reapply it, which can end in a conflicted checkout.

## Considered options

- **Refuse any untracked file.** Rejected by the ticket. A flow that writes its logs into the repo it targets could never land on its own HEAD.
- **Let `merge --ff-only` decide.** Rejected. It spares untracked files but overwrites ignored ones, and it fails with a message to parse rather than a refusal the library checked itself (ADR-0016).
- **`update-ref` HEAD's branch, then `read-tree -u -m`.** Rejected. The ref moves before the checkout does, so a failure in between leaves a desynced checkout, which is what `target_checked_out` exists to prevent.
- **Check only when the run starts.** Rejected for the staleness above.

## Consequences

A series that adds a path the user has ignored cannot land on HEAD until the user moves that file. The refusal names the file, and the series is preserved. A run stuck on the refusal lands on a named branch instead.

**A flow can land on the HEAD of the repo it lives in.** That is allowed, and its own untracked output, such as `RunLogFiles("logs/")` inside the repo, does not block it. The landing does change the checkout the flow script was started from, including the script itself if the series edits it. The running process already has the script loaded; the next run reads the new version. `test_a_flow_lands_onto_the_head_of_the_repo_that_holds_it` runs this as a real subprocess.

Only `"HEAD"` names the checkout. A lowercase `"head"` is an ordinary branch name to git, and here too. The placeholder refusal before #25 caught both spellings; that refusal is gone.

A fast-forward runs the user's `post-merge` hook, as their own `git merge` would. The plumbing up to the swap runs no hooks (ADR-0020).

Decided in [#25](https://github.com/jeffrichley/waystation/issues/25).
