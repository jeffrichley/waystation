"""The API reference, generated from each public module's ``__all__`` (#127).

A reference page names its module once, as ``<!-- api: waystation.agents -->``,
and this hook replaces the marker with one mkdocstrings block per name that
module exports. ``__all__`` is the public surface's contract (#18), so the
reference documents exactly it: a name added there appears here, and a name
that is merely importable does not.
"""

from __future__ import annotations

import importlib
import re

__all__ = ["on_page_markdown"]

_MARKER = re.compile(r"^<!-- api: ([\w.]+) -->$", re.MULTILINE)


def on_page_markdown(markdown: str, **_: object) -> str:
    """Expand every reference marker on a page, before markdown is rendered.

    Args:
        markdown: The page's source.
        **_: What mkdocs passes besides (the page, config and files), unused.

    Returns:
        The page, each marker replaced by its module's reference blocks.
    """
    return _MARKER.sub(lambda marker: _blocks(marker.group(1)), markdown)


def _blocks(module: str) -> str:
    """One ``:::`` block per name in ``module.__all__``, in its order."""
    names: list[str] = importlib.import_module(module).__all__
    return "\n\n".join(f"::: {_path(module, name)}" for name in names)


def _path(module: str, name: str) -> str:
    """The path that documents ``name`` as ``module`` exports it.

    ``module.name`` as a rule. A function defined in a module of its own name,
    ``queue`` in ``waystation.queue``, is the exception: that path reads as
    the module, and the function's entry would document the whole module
    instead, so it is named where it is defined.
    """
    exported = getattr(importlib.import_module(module), name)
    defined_in = getattr(exported, "__module__", None)
    if defined_in is not None and defined_in.rpartition(".")[2] == name:
        return f"{defined_in}.{name}"
    return f"{module}.{name}"
