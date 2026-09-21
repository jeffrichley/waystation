---
type: reference
---
# Research: docs generators for a Python library with literate examples

Ticket: [#16](https://github.com/jeffrichley/waystation/issues/16)
Date: 2026-09-15

Question: which Python docs generator can build the site waystation wants —
Google-docstring API reference, `examples/*.py` rendered as ordered tutorial
pages (module docstring as prose, then the source), Markdown pass-through of
`docs/adr/*.md` and `CONTEXT.md`, GitHub Pages deploy from a justfile — and at
what maintenance cost?

Sources: project docs, changelogs, repos and PyPI metadata only, linked
inline. No generator was run; build times are not measured.

---

## Summary

- **mkdocs-material + mkdocstrings-python** covers needs 1, 3, 4 today with
  the strongest Google-docstring renderer of the four; need 2 is a ~40-line
  script you own. The catch is the ground moving under MkDocs itself.
- **Zensical** (Material's successor) reads the same `mkdocs.yml` and runs
  the same handler, but is on "0.0.x versioning (alpha / development
  releases)" with no `hooks`, `exclude_docs` or gen-files.
- **Sphinx + sphinx-gallery** has built-in literate-script pages, but the
  prose must be reStructuredText; Google docstrings need `autodoc` +
  `napoleon` (autodoc2 has no Google parser and is dormant).
- **pdoc** is API-only, imports what it documents, cannot host pages.

Recommendation: Material + mkdocstrings-python now, on a Zensical-compatible
plugin set, example pages from an owned pre-build script rather than a
build plugin — so the swap to Zensical is a one-line justfile change.

## 1. mkdocs-material + mkdocstrings (python handler)

**Health.** Material 9.7.7 (2026-07-17). Release 9.7.0 (2025-11-11) states
"Material for MkDocs is now in maintenance mode", the team will "focus on
Zensical", and commits to "critical bug fixes and security updates ... for
12 months at least"; 9.7.5 pins `mkdocs<2`
([changelog](https://squidfunk.github.io/mkdocs-material/changelog/)).
MkDocs itself: last release 1.6.1 (2024-08-30), `requires_python >=3.8`,
classifiers stop at 3.12 ([PyPI](https://pypi.org/project/mkdocs/)). The
MkDocs 2.0 rewrite drops the plugin system and moves to TOML with "no
migration path for existing projects"
([Material blog, 2026-02-18](https://squidfunk.github.io/mkdocs-material/blog/2026/02/18/mkdocs-2.0/)).
**ProperDocs** (1.6.7, 2026-03-20, BSD-2) is "a fork of MkDocs, aiming to be
a complete drop-in replacement" because "development of the original MkDocs
project was abandoned"; it runs `mkdocs.plugins` entry points
([repo](https://github.com/properdocs/properdocs),
[release notes](https://properdocs.org/about/release-notes/)). oprypin's
mkdocs-gen-files 0.6.1 and mkdocs-literate-nav 0.6.3 (2026-03-16) now
hard-depend on `properdocs>=1.6.5` and pin `mkdocs<=1.6.1`
([pyproject](https://github.com/oprypin/mkdocs-gen-files/blob/master/pyproject.toml)).
mkdocstrings 1.0.6 (2026-07-11); mkdocstrings-python 2.0.8 (2026-08-31),
classifiers 3.9–3.14, built on `griffelib>=2.0`
([changelog](https://mkdocstrings.github.io/python/changelog/),
[PyPI](https://pypi.org/project/mkdocstrings-python/)).

**Need 1 — API reference.** `docstring_style: google` (also numpy, sphinx)
([docstrings](https://mkdocstrings.github.io/python/usage/configuration/docstrings/)).
Signatures: `show_signature_annotations`, `separate_signature` (code block
under the heading, formatted by Black/Ruff), `signature_crossrefs` (linked
types), `modernize_annotations` rewrites `Union[A, B]` → `A | B` and
`Optional[A]` → `A | None`, `show_overloads`
([signatures](https://mkdocstrings.github.io/python/usage/configuration/signatures/)).
`show_labels` (default true) renders labels; griffe's visitor tags
coroutines with `labels={"async"}` in `visit_asyncfunctiondef`
([members](https://mkdocstrings.github.io/python/usage/configuration/members/),
[griffe visitor](https://github.com/mkdocstrings/griffe/blob/main/packages/griffelib/src/griffe/_internal/agents/visitor.py)).
So `RunSucceeded | RunConflicted | RunFailed` renders verbatim with each
member cross-linked. `Protocol` subclasses get no special treatment; they
render as classes. Static analysis (griffe): no import at build time.

**Need 2 — examples as pages.** No built-in literate-script mode. Three
mechanisms:

1. *Owned script* (recommended): walk `examples/*.py`, `ast.get_docstring`
   each, write `docs/examples/<name>.md` = prose + fenced source, in
   `examples/README.md` order. Run it as a `just docs` pre-step, or in-build
   via mkdocs-gen-files (`scripts:` run at build time, pages exist only
   virtually; [docs](https://oprypin.github.io/mkdocs-gen-files/)).
   Ordering: `nav:` in `mkdocs.yml`, or mkdocs-literate-nav (nav from a
   Markdown list, `nav_file`;
   [docs](https://oprypin.github.io/mkdocs-literate-nav/)).
2. *pymdownx.snippets* (bundled with Material): `--8<-- "examples/foo.py"`
   inside a fence pulls the source in; `base_path` (default `['.']`) must
   cover the repo root and `restrict_base_path` (default true) keeps it
   there; line ranges and named sections are supported
   ([docs](https://facelessuser.github.io/pymdown-extensions/extensions/snippets/)).
   Prose still has to be duplicated into the page.
3. *mkdocs-gallery* — the sphinx-gallery port, i.e. the answer to "does
   sphinx-gallery-style support exist outside Sphinx". Markdown text
   blocks, a `README.md` per gallery dir, `plot_` gates execution, Material
   theme required ([docs](https://smarie.github.io/mkdocs-gallery/)).
   Dormant: 0.10.4 on 2024-09-30 is the last release and last commit,
   pinned `mkdocs<2`, classifiers 3.7–3.11
   ([PyPI](https://pypi.org/project/mkdocs-gallery/),
   [repo](https://github.com/smarie/mkdocs-gallery)).

**Need 3 — Markdown pass-through.** With `docs_dir: docs`, `docs/adr/*.md`
are already pages; `exclude_docs` (1.5+) hides `docs/research/` and
`docs/agents/` ([config](https://www.mkdocs.org/user-guide/configuration/)).
`CONTEXT.md` is outside `docs_dir`: a stub page with a snippets include, or
a copy by the pre-build script. Python-Markdown "is not a CommonMark
implementation" — tables and fences are extensions Material enables
([python-markdown](https://python-markdown.github.io/)).

**Need 4 — deploy.** `mkdocs gh-deploy` builds and pushes to `gh-pages` via
ghp-import; no preview step, and untracked local files are included
([docs](https://www.mkdocs.org/user-guide/deploying-your-docs/)). uv:
`[dependency-groups] docs = [...]`, `uv sync --group docs`,
`uv run --group docs mkdocs build`
([uv](https://docs.astral.sh/uv/concepts/projects/dependencies/)).

## 2. Zensical

**Health.** 0.0.62 (2026-09-13); eight releases since 2026-08-16
([releases](https://github.com/zensical/zensical/releases)). MIT, 5.7k
stars, `requires_python >=3.10` ([PyPI](https://pypi.org/project/zensical/)).
Its own words: "currently uses **0.0.x versioning** (alpha / development
releases). We're approaching a **beta release**"
([upgrade](https://zensical.org/docs/upgrade/)); "Zensical is currently
alpha software and we are iterating rapidly", no stable timeline, API docs a
planned module ([roadmap](https://zensical.org/about/roadmap/)). A Rust
"differential runtime" rebuilds only affected artifacts; Markdown is still
Python-Markdown
([python-markdown](https://zensical.org/docs/compatibility/markdown/python-markdown/)).

**Compatibility model.** "Zensical reads the existing `mkdocs.yml`"; "You
can keep `mkdocs.yml` for as long as you want. Both MkDocs and Zensical can
read it, so moving to `zensical.toml` is not required"
([migration](https://zensical.org/docs/compatibility/mkdocs/migration/)).
Only listed plugins run: "Zensical silently ignores the configuration for
plugins that are not listed under Supported plugins. It does not import or
run these plugins." Supported (since): mkdocstrings (0.0.11), autorefs
(0.0.22), macros (0.0.40), markdown-exec (0.0.47), literate-nav, awesome-nav,
redirects, tags, minify (0.0.58), section-index (native). Not supported:
mkdocs-gen-files, mkdocs-gallery, `hooks`
([plugins](https://zensical.org/docs/compatibility/mkdocs/plugins/)).
Unsupported settings include `exclude_docs`, `draft_docs`, `not_in_nav`,
`hooks`; `docs_dir` cannot be the repo root
([basics](https://zensical.org/docs/setup/basics/)).

**Need 1.** Same handler, same options. mkdocstrings has shipped
Zensical-specific fixes in 1.0.1 ("Support cross-references in Zensical"),
1.0.3 and 1.0.6 ([changelog](https://mkdocstrings.github.io/changelog/)) and
builds its own site from a `zensical.toml`
([repo](https://github.com/mkdocstrings/mkdocstrings/blob/main/zensical.toml)).

**Need 2.** No gen-files, no hooks: nothing runs inside the build. Either
the same owned pre-build script (identical under MkDocs and Zensical), or
markdown-exec: a ```` ```python exec="true" ```` block per page that reads
the script and prints prose + fenced source — "Markdown Exec will render
what you print as Markdown"
([docs](https://pawamoy.github.io/markdown-exec/usage/)). Snippets and
literate-nav also work.

**Need 3.** As MkDocs, minus `exclude_docs` — `docs/research/*.md` and
`docs/agents/*.md` would build as unlinked pages unless moved.

**Need 4.** `zensical build --clean` → `site/`; the documented GitHub Pages
path is a workflow using `actions/upload-pages-artifact` +
`actions/deploy-pages`; no `gh-deploy` command
([publish](https://zensical.org/docs/publish-your-site/)).

## 3. Sphinx (autodoc/napoleon or autodoc2, sphinx-gallery, myst-parser)

**Health.** Sphinx 9.1.0 (2025-12-31), `requires_python >=3.12` (9.0
dropped 3.11), classifiers 3.12–3.15
([changes](https://www.sphinx-doc.org/en/master/changes/index.html),
[PyPI](https://pypi.org/project/sphinx/)). myst-parser 5.1.0, `>=3.11`,
`sphinx>=8,<10` ([PyPI](https://pypi.org/project/myst-parser/)).
sphinx-gallery 0.21.0 (2026-04-24), `sphinx>=6`, 3.10–3.14
([PyPI](https://pypi.org/project/sphinx-gallery/)). sphinx-autodoc2: last
release 0.5.0 (2023-11-27), last commit 2024-05-13, `astroid<4` pin
([PyPI](https://pypi.org/project/sphinx-autodoc2/),
[commits](https://github.com/sphinx-extensions2/sphinx-autodoc2/commits/main)).

**Need 1.** `sphinx.ext.autodoc` imports the package at build time;
`sphinx.ext.napoleon` parses Google style
([napoleon](https://www.sphinx-doc.org/en/master/usage/extensions/napoleon.html));
`autodoc_typehints` = `signature` (default) | `description` | `both` | `none`
([autodoc](https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html)).
Unions stringify as `' | '.join(...)` for `Union`/`Optional`/`UnionType`
([typing.py](https://www.sphinx-doc.org/en/master/_modules/sphinx/util/typing.html));
coroutines labelled since 2.1, async generators since 4.3
([2.1](https://www.sphinx-doc.org/en/master/changes/2.1.html),
[4.3](https://www.sphinx-doc.org/en/master/changes/4.3.html)). autodoc2 is
static (astroid) but its parsers are only "'rst', 'myst', or the fully
qualified name of a custom parser class" — no Google parser; the request
(#33, 2023-10) is open without reply
([config](https://sphinx-autodoc2.readthedocs.io/en/latest/config.html),
[#33](https://github.com/sphinx-extensions2/sphinx-autodoc2/issues/33)).

**Need 2.** sphinx-gallery is the reference literate-script tool:
`examples_dirs`/`gallery_dirs`; the opening docstring is the page header
and must begin with an rST title; text blocks are rST after `# %%`
separators; only files matching `filename_pattern` (default `/plot_`)
execute, and `'plot_gallery': 'False'` builds "without executing any of the
scripts"; `within_subsection_order` accepts a custom callable "passed
filenames", so `examples/README.md` order is a ten-line callable; each
examples dir needs a `GALLERY_HEADER.[ext]` in rST
([syntax](https://sphinx-gallery.github.io/stable/syntax.html),
[configuration](https://sphinx-gallery.github.io/stable/configuration.html),
[getting started](https://sphinx-gallery.github.io/stable/getting_started.html)).
The mismatch: prose must be rST, not Markdown — no MyST support through
0.21.0 ([changes](https://sphinx-gallery.github.io/stable/changes.html)).
Non-gallery fallback: a MyST stub per example with `{include}` for the
docstring and `{literalinclude}` for the source, ordered by `toctree`.

**Need 3.** MyST parses `.md` natively; `docs/adr/*.md` are documents when
`docs/` is the source dir; `exclude_patterns` hides the rest. `CONTEXT.md`
is outside it: toctree names are "relative to the source directory", so a
stub with `{include} ../CONTEXT.md` — include paths are "relative to the
document containing the directive", unrestricted
([directives](https://www.sphinx-doc.org/en/master/usage/restructuredtext/directives.html),
[docutils](https://docutils.sourceforge.io/docs/ref/rst/directives.html#include),
[myst](https://myst-parser.readthedocs.io/en/latest/syntax/organising_content.html)).

**Need 4.** `sphinx-build -M html docs site`, `-j auto`, incremental by
default ([sphinx-build](https://www.sphinx-doc.org/en/master/man/sphinx-build.html));
`sphinx.ext.githubpages` writes `.nojekyll`
([githubpages](https://www.sphinx-doc.org/en/master/usage/extensions/githubpages.html));
publishing is ghp-import or `actions/deploy-pages` — no built-in push.

## 4. pdoc

**Health.** 16.0.0 (2025-10-27): "Add support for Python 3.14", "Drop
support for Python 3.9"; classifiers 3.10–3.14
([changelog](https://github.com/mitmproxy/pdoc/blob/main/CHANGELOG.md),
[PyPI](https://pypi.org/project/pdoc/)).

**Need 1.** `--docformat google`: "Process reStructuredText elements, then
Google-style syntax, then Markdown"; "First-class support for type
annotations". Union/Protocol rendering is not specifically documented.
"pdoc makes heavy use of dynamic analysis to extract docstrings. This means
your Python modules will be executed/imported when pdoc runs"
([docs](https://pdoc.dev/docs/pdoc.html)).

**Needs 2–4.** Documenting `examples/*.py` would import — i.e. run — each
flow. Markdown enters only through `.. include::` inside a module
docstring; "pdoc main use case is API documentation. If you have
substantially more complex documentation needs, we recommend using Sphinx!"
([docs](https://pdoc.dev/docs/pdoc.html)). Deploy is `pdoc pkg -o docs/`
plus pdoc's own Pages workflow — trivial, but moot.

## Comparison

| Need | Material + mkdocstrings | Zensical + mkdocstrings | Sphinx (autodoc/napoleon + gallery + MyST) | pdoc |
| --- | --- | --- | --- | --- |
| 1. Google docstrings, `A \| B` unions, async, protocols | Yes; static; `modernize_annotations`, linked unions, `async` label | Same handler; cross-refs fixed in mkdocstrings 1.0.1+ | Yes via runtime import; `' \| '` unions; async labelled; autodoc2 has no Google parser | Google yes; imports modules; unions undocumented |
| 2. `examples/*.py` as ordered prose+source pages | Owned script (or gen-files); snippets; mkdocs-gallery dormant | Owned pre-build script or markdown-exec; no gen-files/hooks | Built-in (sphinx-gallery) but rST prose + `GALLERY_HEADER.rst`; order via custom key | No; would execute the examples |
| 3. `docs/adr/*.md`, `CONTEXT.md` pass-through | Native under `docs/`; `exclude_docs`; stub for `CONTEXT.md` | Native; no `exclude_docs`; stub for `CONTEXT.md` | Native via MyST; `exclude_patterns`; `{include}` stub | `.. include::` into a docstring only |
| 4. Pages deploy from `just`; uv group | `mkdocs gh-deploy`; group works | `zensical build` + Actions; group works | `sphinx-build` + ghp-import/Actions; `-j auto`, incremental | `pdoc -o`; group works |
| 5. Health / 3.13 | Material maintenance mode (to ≥2026-11); MkDocs frozen at 1.6.1, 2.0 drops plugins; handler active, 3.9–3.14 | 0.0.x alpha, weekly; `>=3.10`; MIT; team's future | Sphinx active, `>=3.12`; gallery active; autodoc2 dormant since 2024 | Last release 2025-10; 3.10–3.14 |

## Recommendation

**Material for MkDocs + mkdocstrings-python, configured for a Zensical
swap.** Concretely:

1. `mkdocs.yml` (Zensical reads it; `zensical.toml` "is not required"),
   `docs_dir: docs`, explicit `nav`, `exclude_docs` for `research/` and
   `agents/`.
2. Plugins limited to the Zensical-supported set: `mkdocstrings` (python
   handler, `docstring_style: google`, `separate_signature`,
   `show_signature_annotations`, `signature_crossrefs`,
   `modernize_annotations`), `pymdownx.snippets` with `base_path: [.]`.
   No gen-files, no hooks, no mkdocs-gallery.
3. Need 2 as an owned script, `docs/_gen_examples.py`: read
   `examples/README.md` for order, `ast.get_docstring` each script, write
   `docs/examples/<n>-<name>.md` = prose, then a fenced `--8<--` include of
   the source. `just docs` runs the script then `mkdocs build`;
   `just docs-deploy` runs it then `mkdocs gh-deploy`. This is the bareness
   answer anyway: a Python file the user owns, not a plugin.
4. `[dependency-groups] docs = ["mkdocs-material<10", "mkdocstrings-python",
   "mkdocs<2"]`; run with `uv run --group docs`.

Why not the others: sphinx-gallery's first-class literate scripts want rST
prose and a header file, against Markdown-everywhere ADRs and docstrings;
autodoc2 is out (no Google parser, no release since 2023). pdoc cannot host
pages and would execute the flows. Zensical is the team's future and
supports exactly the plugin surface waystation needs, but self-described
alpha with weekly 0.0.x releases and no `exclude_docs` is not where a v0
library should park its docs today.

**Maintenance cost.** Three pinned packages in one uv group; Material is
frozen except security fixes, so upgrade churn is mkdocstrings-python only.
`mkdocs<2` is permanent. The cliff is MkDocs 1.6.1 meeting a future Python;
the exits are ProperDocs (drop-in, same `mkdocs.yml`) or Zensical (same
`mkdocs.yml` and handler; `just docs` becomes `zensical build --clean`, drop
`exclude_docs`). The pre-build example generator survives both. Cheap early
signal: run `zensical serve` on the same `mkdocs.yml` and note what it
ignores.
