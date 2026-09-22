"""Pure argv redaction, shared by logged command lines and ``CommandFailed``.

The elision lives inside the value rather than at each call site (ADR-0025),
so a command line a third-party sandbox backend or integration strategy builds
is redacted without its cooperation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = ["redact_argv"]

ELIDED = "***"

# An environment variable's name: identifier-shaped, as a shell requires.
_KEY = r"[A-Za-z_][A-Za-z0-9_]*"

# An environment assignment wherever it sits in an argument: on its own
# (``ANTHROPIC_API_KEY=…``) or behind a flag (``--env=ANTHROPIC_API_KEY=…``).
# The key must not continue a word, a flag or a dotted name, so ``--api-key=``
# and git's ``-c user.email=x`` are not keys.
_ASSIGNMENT = re.compile(rf"(?<![-\w.])({_KEY})=\S+")

# An argument that *is* an assignment, bare or behind one flag, loses its whole
# value: docker's ``-e KEY=VAL`` carries a value with spaces or newlines in it
# as one argument, and the rule above would stop at its first word. This runs
# before that sweep, not instead of it, so an assignment inside a script still
# goes. Nothing tells ``TOKEN=abc def`` from a script opening ``A=1 cmd``, so
# the script loses its tail too — over-redaction is the accepted failure
# (ADR-0025).
_WHOLE_ASSIGNMENT = re.compile(
    rf"\A(?P<head>(?:--?[A-Za-z][\w-]*=)?{_KEY}=).+\Z", re.DOTALL
)

# Credentials that travel without a key naming them — bare, behind a flag, or
# as a URL's userinfo. Each alternative is an issuer's own published shape, so
# the sweep never guesses at entropy and never eats an ordinary word.
_TOKEN = re.compile(
    r"""(?<![A-Za-z0-9_-])(?:
        sk-[A-Za-z0-9_-]{8,}                                    # Anthropic, OpenAI
      | gh[pousr]_[A-Za-z0-9]{20,}                              # GitHub token
      | github_pat_[A-Za-z0-9_]{20,}                            # GitHub fine-grained
      | xox[abprs]-[A-Za-z0-9-]{10,}                            # Slack
      | AKIA[0-9A-Z]{16}                                        # AWS access key id
      | eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+   # JWT
    )""",
    re.VERBOSE,
)

# A URL's userinfo, as git carries one in a push remote. With a password the
# user is kept and the password goes; without one there is no telling a
# username from a token, so the whole userinfo goes.
_URL_PASSWORD = re.compile(r"(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]+):[^/\s@]*@")
_URL_USERINFO = re.compile(r"(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]+@")


# Every rule, in the order each argument passes through them. Teaching the
# redactor a new credential shape is one pattern and one line here.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_WHOLE_ASSIGNMENT, rf"\g<head>{ELIDED}"),
    (_ASSIGNMENT, rf"\1={ELIDED}"),
    (_URL_PASSWORD, rf"\g<head>:{ELIDED}@"),
    (_URL_USERINFO, rf"\g<head>{ELIDED}@"),
    (_TOKEN, ELIDED),
)


def redact_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Return ``argv`` with every credential it can name replaced by ``***``.

    Environment values go by key (``KEY=***``, the name kept), a published
    credential shape is elided wherever in an argument it appears, and a URL
    keeps at most its user.

    Args:
        argv: The command line to redact.

    Returns:
        The same arguments, in order, with each credential replaced.
    """
    return tuple(_redact(arg) for arg in argv)


def _redact(arg: str) -> str:
    for pattern, replacement in _RULES:
        arg = pattern.sub(replacement, arg)
    return arg
