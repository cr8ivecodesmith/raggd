"""Tests for :mod:`raggd.resources`."""

from __future__ import annotations

import pytest

from raggd.resources import get_resource


def test_get_resource_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        get_resource("does-not-exist.toml")
