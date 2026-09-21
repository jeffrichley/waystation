You are resolving a merge conflict between two pieces of work.

The branch `{preserved}` holds a series of commits that was made on top of
commit `{base}`. It could not be landed on `{target}`, because `{target}`
has moved on since and the two changed the same lines. You are on a branch
at the tip of `{target}` now.

Replay that series onto your branch, commit by commit:

    git cherry-pick {base}..{preserved}

When a pick stops on a conflict, open each conflicted file and resolve it so
that both sides' intent survives: what `{target}` already has, and what the
series was adding. Then `git add` the files and `git cherry-pick --continue`.
Repeat until the whole range is replayed.

Rules:

- Never `git merge`, and never rebase your branch onto `{preserved}`. The
  result must be a linear series of commits on top of where you started.
- Keep each replayed commit's message.
- Change nothing the series didn't touch.
- If a commit turns out to be already present on your branch, skip it with
  `git cherry-pick --skip`.

When you are done, report a one-paragraph summary of how you resolved each
conflict.
