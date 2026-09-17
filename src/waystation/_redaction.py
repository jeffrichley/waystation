"""Pure argv redaction, shared by logged command lines and ``CommandFailed``."""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = ["redact_argv"]

ELIDED = "***"

# An environment assignment as it appears in argv: ``docker run -e KEY=value``.
# Only identifier-shaped keys, so git's ``-c user.email=x`` stays readable.
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$", re.DOTALL)

# Credentials that travel as a bare argument, where no key names them. Each
# alternative is an issuer's own published shape, so the sweep never guesses at
# entropy and never eats an ordinary word.
_TOKEN = re.compile(
    r"""^(?:
        sk-[A-Za-z0-9_-]{8,}                                    # Anthropic, OpenAI
      | gh[pousr]_[A-Za-z0-9]{20,}                              # GitHub token
      | github_pat_[A-Za-z0-9_]{20,}                            # GitHub fine-grained
      | xox[abprs]-[A-Za-z0-9-]{10,}                            # Slack
      | AKIA[0-9A-Z]{16}                                        # AWS access key id
      | eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+   # JWT
    )$""",
    re.VERBOSE,
)


# The password half of a URL's userinfo, as git carries one in a push remote.
_URL_PASSWORD = re.compile(r"(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]+):[^/\s@]*@")


def redact_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Return ``argv`` with every credential it can name replaced by ``***``.

    Environment values go by key (``KEY=***``, the name kept), an argument
    carrying a published credential shape is elided whole, and a URL keeps its
    user but loses its password.
    """
    return tuple(_redact(arg) for arg in argv)


def _redact(arg: str) -> str:
    if _TOKEN.match(arg):
        return ELIDED
    arg = _URL_PASSWORD.sub(rf"\g<head>:{ELIDED}@", arg)
    return _ASSIGNMENT.sub(rf"\1={ELIDED}", arg)
