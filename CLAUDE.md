# waystation

A bare, hackable Python library for orchestrating sandboxed AI coding agents — a re-imagining of [sandcastle](https://github.com/mattpocock/sandcastle) in Python. Bareness is the prime directive: flows are plain Python scripts the user owns and edits, not config fed into a framework.

## Read before you change anything

- **`CONTEXT.md`** is the glossary, and it binds. Name things the way it names them; avoid the words it says to avoid.
- **`docs/adr/`** is why. Read the ADRs touching your area — they carry the rejected options too, so you don't re-walk a road. If your change contradicts one, say so out loud instead of quietly overriding it.
- **The ticket is the spec.** Issues live in GitHub (`jeffrichley/waystation`); see `docs/agents/issue-tracker.md`. Its acceptance criteria define the work, and #18 is the parent spec — including the **public import surface**, which is a contract.

## Done means `just check` is green

`ruff check` · `ruff format --check` · `mypy` (strict) · `pytest` (85% floor). CI runs exactly `just check` on Linux and Windows, so local green means CI green. Never lower the floor to pass; never add an ignore you can't justify in the same breath.

## Rules that live nowhere else

- **The public surface is a contract.** Every module curates `__all__`, and everything else is underscore-private. Adding a name to top-level `waystation.__all__` is an API commitment — if #18's import-surface list doesn't name it, say why you're adding it.
- **Nothing is bounded or trapped by default** (ADR-0017). A timeout, retry count or buffer size that no ticket asked for doesn't get a number: it defaults to unbounded, or it gets a knob. Any constant that must exist earns a comment saying why.
- **A failure is a value; only primitives raise** (ADR-0016). An awaited run returns `RunFailed`; a primitive raises `StageError` carrying the same `Failure`. `BaseException` is never caught.
- **When the library does what a user could do, it uses the user's protocol.** Built-in observers are hook bundles (ADR-0001); sandbox backends, agent providers and integration strategies are the same protocols a user implements. There is no privileged internal path — if you're writing one, that's the signal to stop.
- **Cite the decision in the code.** A bare `(ADR-0017)` in a comment is how the next reader finds the argument. Keep doing it.
- **Prefer a test to a paragraph.** A rule a test enforces survives refactors; a rule in prose erodes. When you settle something structural, leave a test holding it.
- **Write the ADR when the decision would otherwise be re-litigated.** Next number, house style: the decision, then *why*, then the options you rejected and what it costs.

## Windows is a first-class host

Development happens on Windows, and CI runs there. It is not the exotic case:

- **Never use text-mode pipes for git** — they rewrite every newline, which changes every line of a patch. `_git.decode` / `encode` exist for this.
- **Git writes objects read-only and Windows refuses to unlink those.** `remove_workspace` handles it; don't reach for a bare `shutil.rmtree`.
- **Kill a process tree, never a process** (ADR-0023). The OS-specific half is injected as a `ProcessStrategy`, not branched on inline.

## Commits

Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`), one concern each — split unrelated work rather than bundling it. This repo's history carries **no** `Co-Authored-By` trailer; match it. Work on a branch; `main` lands through PRs.

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues (`jeffrichley/waystation`), via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
