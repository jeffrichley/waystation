"""Test support waystation ships, for people writing their own backends.

Importing this package needs pytest, which waystation does not otherwise
depend on: install it as ``waystation[testing]``.
"""

from __future__ import annotations

from waystation.testing.conformance import SandboxConformance

__all__ = ["SandboxConformance"]
