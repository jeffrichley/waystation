---
status: accepted
---

# The landing steps are public on `GitRepo`, and squash is its own strategy built from them

`GitRepo` has five public landing steps. They are the plumbing ADR-0020 describes, cut at the points where a landing rule makes a choice:

- **`read_target(branch, *, base) -> Target`** reads the branch a landing will move. It refuses a branch a worktree is using (`target_checked_out`). `HEAD` is the host's checkout, which `move_target` fast-forwards (ADR-0041). `Target` records the branch, the tip to build on (the base when the branch does not exist yet), and whether it existed.
- **`commit_series(series) -> tuple[str, ...]`** rebuilds each patch as a commit at the series' base in a temporary index (`mailinfo`, `apply --cached`, `write-tree`, `commit-tree`). It keeps each patch's author, date and message.
- **`merge_tree(*, base, ours, theirs) -> str | Conflict`** runs one `merge-tree --write-tree` and returns the merged tree, or the paths that conflicted.
- **`commit_tree(tree, *parents, like, message=None) -> str`** commits a tree. The author and author date come from `like`, and so does the message unless one is given. The committer is the host user at landing.
- **`move_target(target, tip)`** does the compare-and-swap `update-ref`, and raises `Refused("target_moved")` only when a re-read shows the branch moved.

Each step raises with no stage, and `integrate()` names it (ADR-0032). Serialization stays at the entry points (ADR-0033).

`Squash(target, message=None)` is a second shipped strategy beside `Integration`, not a third `mechanism`. It rebuilds the series, runs one `merge-tree` of its net change (`--merge-base=<base> <target tip> <series tip>`) against the target, and commits the merged tree as a single commit on the target. `IntegrationReport` for a squash reads `strategy="Squash"`, `mechanism="squash"`, and `landed` holds that one sha. A conflict is a `Conflict` with no `failed_patch`, as with `merge`, and it becomes `RunConflicted` with the series preserved unsquashed.

`Integration` and `Squash` are both written against these steps and the result values only. `test_the_shipped_strategies_name_nothing_private_to_land_a_series` fails if either class body names a private helper of the module or anything imported from a private module.

Why: #18 story 113 promises strategy authors that "custom landing rules stay small". After ADR-0033 a user's strategy could run any git command, but matching the shipped strategy meant rebuilding about 250 lines of plumbing: `mailinfo` parsing, a temporary index, `merge-tree`'s exit-code rules, the compare-and-swap and its refusal edge, and the rebase and bisect checks that stop a branch being moved under a worktree. A landing rule is the choice of which tree to commit on which parents, and nothing more. Squash is a real second adapter for the seam, as architecture.md requires, and it needs every one of these steps: once built from them, it takes about thirty lines.

## Considered options

- **Squash as a third `mechanism` of `Integration`.** Rejected. It is less code, but the seam keeps only one adapter and the steps stay private, so story 113's promise stays broken. It also widens `.integrate(target, mechanism=...)` for a strategy that takes a `message` the other mechanisms ignore.
- **The steps as free functions in `waystation.integration`.** Rejected in favour of methods. pygit2's `Repository.merge_trees` and `create_commit` are the precedent: a step belongs to the repo it runs in, and a strategy author finds the steps where they already have the repo. A method also goes through `self.git`, so a test double that overrides `git` (as `MovesOnSwap` does) sees every step.
- **Higher-level steps: `pick(commit, onto)`, `squash(series, onto)`.** Rejected. Each is one shipped rule under another name, and a user's rule would still have to rebuild what lies between them. The steps are cut where rules differ: which base to merge from, which parents to commit on, and whose message to use.
- **A `tree_of(commit)` step for the "does this change anything" check.** Not added. It is one `repo.git("rev-parse", f"{tip}^{{tree}}")`, which is what the git runner is for.
- **A `Waystation-Run: <run-id>` trailer on the squash commit.** Not possible at this seam: a strategy is handed a repo and a series, not a run. A flow script that wants the trailer passes `message=`. A salvage commit's own trailer is not carried into the default message.
- **Keeping the unsquashed series on `waystation/<run-id>` after a squash lands.** Not done. Preservation keeps a series that reached no target (ADR-0005), and the strategy does not know the run id to name the branch. The series is kept whenever a squash conflicts or is refused, as with any strategy.

## Consequences

The default squash message follows a forge's squash-merge. A one-commit series keeps its own message. A longer series takes its first subject as the subject, then lists every subject, oldest first. The author and author date are the series' last commit's. That is the agent, who is the host identity (ADR-0006), so the squash's author and committer are both the host user, as with `apply`.

`HEAD` is handled in `read_target` and `move_target` and nowhere else, so both strategies land on it from the same steps, and so does a user's strategy built on them (ADR-0041, #25).

The import surface grows: `Squash` at the top level beside `Integration`, as a shipped implementation, and `Squash` and `Target` in `waystation.integration`. `Target` has to be public because a public step returns it. `.integrate(target, mechanism=...)` is unchanged: `mechanism` is still `"apply"` or `"merge"`, and a squash is `.integrate(Squash(target))`.

Decided in [#81](https://github.com/jeffrichley/waystation/issues/81), out of the architecture review in [#69](https://github.com/jeffrichley/waystation/issues/69).
