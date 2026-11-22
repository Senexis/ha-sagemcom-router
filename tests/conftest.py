"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from .common import FakeRouter, make_entry, setup_entry


@pytest.fixture
def router() -> Generator[FakeRouter]:
    fake = FakeRouter()
    with patch("custom_components.sagemcom_rest.SagemcomRestClient", fake):
        yield fake


@pytest.fixture
async def entry(hass: HomeAssistant, router: FakeRouter) -> MockConfigEntry:
    """Integration set up read-only (the default)."""
    return await setup_entry(hass, make_entry())


@pytest.fixture
async def controls_entry(hass: HomeAssistant, router: FakeRouter) -> MockConfigEntry:
    """Integration set up with "Enable controls" on."""
    return await setup_entry(hass, make_entry(controls=True))
