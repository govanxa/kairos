"""Shared test fixtures for Kairos test suite."""

import pytest

from kairos import StateStore


@pytest.fixture
def state() -> StateStore:
    """Return a fresh, empty StateStore for each test."""
    return StateStore()
