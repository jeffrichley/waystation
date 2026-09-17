"""Pure argv redaction, shared by logged command lines and ``CommandFailed``."""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = ["redact_argv"]

ELIDED = "***"

# An environment assignment wherever it sits in an argument: on its own
# (``ANTHROPIC_API_KEY=…``) or behind a flag (``--env=ANTHROPIC_API_KEY=…``).
# The key must be identifier-shaped and must not continue a word, a flag or a
# dotted name, so ``--api-key=`` and git's ``-c user.email=x`` are not keys.
_ASSIGNMENT = re.compile(r"(?<![-\w.])([A-Za-z_][A-Za-z0-9_]*)=\S+")

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


def redact_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Return ``argv`` with every credential it can name replaced by ``***``.

    Environment values go by key (``KEY=***``, the name kept), a published
    credential shape is elided wherever in an argument it appears, and a URL
    keeps at most its user.
    """
    return tuple(_redact(arg) for arg in argv)


def _redact(arg: str) -> str:
    arg = _ASSIGNMENT.sub(rf"\1={ELIDED}", arg)
    arg = _URL_PASSWORD.sub(rf"\g<head>:{ELIDED}@", arg)
    arg = _URL_USERINFO.sub(rf"\g<head>{ELIDED}@", arg)
    return _TOKEN.sub(ELIDED, arg)
