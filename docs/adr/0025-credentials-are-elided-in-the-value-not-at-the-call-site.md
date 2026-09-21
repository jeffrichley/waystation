---
status: stable
type: adr
---

# Credentials are elided inside the value, not at each call site

One pure `redact_argv` handles every command line the library exposes: it elides an environment value by key (`KEY=***`, the name kept), elides a published credential shape wherever inside an argument it sits, and takes a URL's password — or its whole userinfo, when no password follows, since nothing then distinguishes a token from a username. Every DEBUG command line goes through `log_argv`, and `CommandFailed` redacts in `__post_init__` rather than at any of its construction sites, so the failure value is redacted by construction. A prompt never reaches a record at all: INFO carries its length and at most 80 characters of its first line, and the body appears only in the per-run file a flow script opts into.

Why: a redactor that has to be *called* is a redactor that gets forgotten, and the blast radius is a credential in a log the user then pastes into an issue. Foreman is the worked example — its positional `GH_TOKEN`-only rule writes `ANTHROPIC_API_KEY` and the OAuth token verbatim into every banner. `CommandFailed` is where every git and sandbox command converges on its way out of the library, and ADR-0010 and the integration-strategy seam both invite third parties to build one; putting the elision in the value means their sites are covered without their cooperation. Matching published issuer prefixes rather than scoring entropy is what lets the sweep run inside an argument without eating `--json-schema` payloads or commit messages.

## Considered options

- Redact at each construction site. Rejected: seven sites the day it was written, and two public extension seams that mint more.
- Redact only in the logging layer. Rejected: `CommandFailed.argv` is returned to the flow script, which prints it.
- Anchor the sweep to a whole argument. Rejected after review found it leaks `--api-key=sk-ant-…`, `Bearer ghp_…` and `--env=KEY=secret` verbatim.
- Score entropy instead of matching issuer shapes. Rejected: eats ordinary arguments and still misses a short token.
- Elide a URL's userinfo unconditionally. Rejected: git's `x-access-token:` sentinel tells you which credential path a push took, and it is not a secret.
- Keep the prompt out of logs entirely, length included. Rejected: a fan-out needs to tell its runs apart at INFO.

## Consequences

`CommandFailed.argv` is always a `tuple`, never the sequence handed in, and a caller comparing against the argv it passed will see `***` where a credential was. Over-redaction is the accepted failure mode: an argument shaped like an assignment (`msg=hello`) loses its value in a log line. A credential in a shape no rule names still leaks, so a new issuer means a new pattern in `_RULES` — one line, by design.

Decided while implementing [ticket 33](https://github.com/jeffrichley/waystation/issues/33), settling the redaction half of [ticket 6](https://github.com/jeffrichley/waystation/issues/6).
