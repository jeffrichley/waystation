"""Smoke tests for the installed package."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_import_waystation() -> None:
    import waystation

    assert waystation.__doc__ is not None
