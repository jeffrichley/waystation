# The docs bundle

`docs/` is an OKF bundle, and vaultwright owns its frontmatter. Before creating a doc here or changing any doc's frontmatter, load the `vaultwright:using-vaultwright` skill, and write frontmatter only with `vaultwright new` / `set` / `supersede` (always `--vault docs`, with paths relative to `docs/`). This repo's own types (`principles`, `skill-config`) are declared in `vaultwright.toml`, and so is what the bundle leaves out.

Skills write their own formats. After a skill writes a doc here, such as `/research` findings, give it frontmatter: `type` from the ordered table in `vaultwright:remediating-a-vault`, which makes research `reference`.

Every doc is reachable from `index.md`. Add a new doc to its directory's `index.md` (plain Markdown, no frontmatter), or to the root `index.md` if its directory has none. An ADR goes into `adr/README.md` instead, which is a typed doc rather than an index.

`vaultwright doctor report docs` is the check. `vaultwright hooks install docs` puts it in a clone's pre-commit hook, where it reports without blocking; the hook is per clone, not versioned.

ADRs have their own rules in `adr/CLAUDE.md`.
