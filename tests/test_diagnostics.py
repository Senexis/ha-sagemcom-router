"""Diagnostics download."""

from __future__ import annotations

import json

from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import ClientSessionGenerator

from homeassistant.core import HomeAssistant

from .common import SERIAL


async def test_diagnostics_are_redacted(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, entry: MockConfigEntry
) -> None:
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diag["entry"]["password"] == "**REDACTED**"
    assert diag["entry"]["username"] == "**REDACTED**"
    assert diag["data"]["info"]["serial"] == "**REDACTED**"
    assert diag["data"]["wan"]["ipv4"] == "**REDACTED**"
    assert diag["data"]["info"]["model"] == "F0000T"
    text = json.dumps(diag)
    assert SERIAL not in text
    assert "198.51.100.7" not in text  # public IP
